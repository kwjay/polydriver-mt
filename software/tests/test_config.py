import math
import tomllib
from pathlib import Path

import pytest

from polydriver.config import (
	DEFAULT_COMMS_TIMEOUT_S, BoardSettings, Config, ConfigError, default_config_text, load_config, save_config,
	write_default_config,
)
from polydriver.protocol.constants import Role


def write(tmp_path: Path, text: str) -> Path:
	path = tmp_path / "polydriver.toml"
	path.write_text(text, encoding="utf-8")
	return path


# --- Defaults ---------------------------------------------------------------------

def test_a_missing_file_gives_the_defaults(tmp_path):
	config = load_config(tmp_path / "polydriver.toml")
	assert config.ports == {Role.POLYMER: None, Role.COLOUR: None}
	assert config.comms_timeout_s == DEFAULT_COMMS_TIMEOUT_S
	assert config.boards[Role.POLYMER] == BoardSettings()
	assert config.boards[Role.COLOUR] == BoardSettings()
	assert config.logging.enabled
	assert config.logging.poll_hz == 10.0
	assert config.gui_live_plot
	assert not config.dev_enabled
	assert config.base_dir == tmp_path


def test_board_defaults_send_no_gains_and_no_filtering():
	settings = BoardSettings()
	assert not settings.has_gains
	assert settings.alpha == 1.0
	assert settings.ff_offset_duty == 0.0
	assert settings.ff_gain_duty_per_hz == 0.0


def test_the_default_template_parses_to_the_defaults(tmp_path):
	path = write(tmp_path, default_config_text())
	assert load_config(path) == load_config(tmp_path / "missing.toml").with_base_dir(tmp_path)


def test_the_default_template_explains_every_section():
	text = default_config_text()
	for section in ("[ports]", "[safety]", "[board.polymer]", "[board.colour]", "[logging]", "[paths]", "[gui]", "[dev]"):
		assert section in text


def test_write_default_config_never_overwrites(tmp_path):
	path = tmp_path / "polydriver.toml"
	write_default_config(path)
	assert path.read_text(encoding="utf-8") == default_config_text()
	with pytest.raises(FileExistsError):
		write_default_config(path)


# --- Reading values ------------------------------------------------------------------

FULL = """
[ports]
polymer = "COM6"
colour = "COM5"

[safety]
comms_timeout_s = 1.5

[board.polymer]
kp = 0.8
ki = 2
kd = 0.0
alpha = 0.4
ff_offset_duty = 128
ff_gain_duty_per_hz = 1.6

[board.colour]
alpha = 0.5

[logging]
enabled = false
folder = "runs"
poll_hz = 20

[paths]
jobs = "my jobs"
calibration = "cal/calibration.json"
materials = "materials.json"

[gui]
live_plot = false

[dev]
enabled = true
"""


def test_reads_every_value(tmp_path):
	config = load_config(write(tmp_path, FULL))
	assert config.ports == {Role.POLYMER: "COM6", Role.COLOUR: "COM5"}
	assert config.comms_timeout_s == 1.5
	assert config.boards[Role.POLYMER] == BoardSettings(
		kp=0.8, ki=2.0, kd=0.0, alpha=0.4, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.6,
	)
	assert config.boards[Role.COLOUR] == BoardSettings(alpha=0.5)
	assert not config.logging.enabled
	assert config.logging.poll_hz == 20.0
	assert not config.gui_live_plot
	assert config.dev_enabled


def test_integers_are_accepted_where_numbers_go(tmp_path):
	config = load_config(write(tmp_path, FULL))
	assert isinstance(config.boards[Role.POLYMER].ki, float)
	assert isinstance(config.logging.poll_hz, float)


def test_relative_paths_are_taken_from_the_config_folder(tmp_path):
	config = load_config(write(tmp_path, FULL))
	assert config.resolve(config.logging.folder) == tmp_path / "runs"
	assert config.resolve(config.paths.calibration) == tmp_path / "cal" / "calibration.json"
	assert config.resolve(config.paths.jobs) == tmp_path / "my jobs"


def test_an_absolute_path_stays_as_it_is(tmp_path):
	absolute = (tmp_path / "elsewhere").resolve()
	config = load_config(write(tmp_path, f'[logging]\nfolder = "{absolute.as_posix()}"\n'))
	assert config.resolve(config.logging.folder) == absolute


def test_comms_timeout_in_milliseconds_for_the_board(tmp_path):
	assert load_config(write(tmp_path, "[safety]\ncomms_timeout_s = 1.5\n")).comms_timeout_ms == 1500
	assert load_config(write(tmp_path, "[safety]\ncomms_timeout_s = 0\n")).comms_timeout_ms == 0


# --- Mistakes are named ---------------------------------------------------------------

@pytest.mark.parametrize("text, message", [
	("[board.polymer]\nkpp = 1.0\n", r"\[board.polymer\].*kpp.*unknown"),
	("[logging]\nenable = true\n", r"\[logging\].*enable.*unknown"),
	("[motor]\nspeed = 1\n", r"\[motor\].*unknown section"),
	("[board.dye]\nkp = 1.0\n", r"\[board.dye\].*unknown role.*polymer, colour"),
	("[board.polymer]\nkp = 1.0\n", r"\[board.polymer\].*kp, ki and kd together"),
	("[board.polymer]\nkp = -1.0\nki = 0\nkd = 0\n", r"kp = -1.0.*>= 0"),
	("[board.polymer]\nkp = true\nki = 0\nkd = 0\n", r"kp = True.*number"),
	("[board.polymer]\nkp = nan\nki = 0\nkd = 0\n", r"kp = nan.*finite"),
	("[board.polymer]\nalpha = 0\n", r"alpha = 0.*0 < alpha <= 1"),
	("[board.polymer]\nalpha = 1.5\n", r"alpha = 1.5.*0 < alpha <= 1"),
	("[board.polymer]\nff_offset_duty = 300\n", r"ff_offset_duty = 300.*0 to 255"),
	("[board.polymer]\nff_gain_duty_per_hz = -0.1\n", r"ff_gain_duty_per_hz = -0.1.*>= 0"),
	("[safety]\ncomms_timeout_s = 70\n", r"comms_timeout_s = 70.*65.535"),
	("[safety]\ncomms_timeout_s = 0.2\n", r"comms_timeout_s = 0.2.*at least 0.5"),
	("[safety]\ncomms_timeout_s = 0.5\n[logging]\npoll_hz = 2\n", r"comms_timeout_s = 0.5.*3 polls.*1.5 s"),
	("[logging]\npoll_hz = 0\n", r"poll_hz = 0.*0 < poll_hz <= 50"),
	("[logging]\nenabled = 1\n", r"enabled = 1.*true or false"),
	("[ports]\npolymer = 6\n", r"polymer = 6.*text"),
	("[ports]\npolymer = \"\"\n", r"polymer.*empty"),
	("[paths]\njobs = 3\n", r"jobs = 3.*text"),
	("ports = 3\n", r"ports.*must be a section"),
], ids=[
	"unknown key", "unknown logging key", "unknown section", "unknown role", "gains not together",
	"negative gain", "bool gain", "nan gain", "alpha 0", "alpha above 1", "offset above 255", "negative ff gain",
	"timeout too long", "timeout too short", "timeout under 3 polls", "poll 0", "bool as int", "port not text",
	"empty port", "path not text", "section as value",
])
def test_a_mistake_names_the_file_section_and_key(tmp_path, text, message):
	path = write(tmp_path, text)
	with pytest.raises(ConfigError, match=message) as raised:
		load_config(path)
	assert str(path) in str(raised.value)


def test_broken_toml_says_where(tmp_path):
	path = write(tmp_path, "[board.polymer\nkp = 1\n")
	with pytest.raises(ConfigError, match=r"not valid TOML.*line 1") as raised:
		load_config(path)
	assert isinstance(raised.value.__cause__, tomllib.TOMLDecodeError)


def test_several_mistakes_are_reported_together(tmp_path):
	path = write(tmp_path, "[board.polymer]\nalpha = 2\nkpp = 1\n[logging]\npoll_hz = -1\n")
	with pytest.raises(ConfigError) as raised:
		load_config(path)
	text = str(raised.value)
	assert "alpha = 2" in text and "kpp" in text and "poll_hz = -1" in text


# --- Writing -------------------------------------------------------------------------

def test_save_and_load_give_the_same_config(tmp_path):
	original = load_config(write(tmp_path, FULL))
	copy_path = tmp_path / "copy" / "polydriver.toml"
	copy_path.parent.mkdir()
	save_config(original, copy_path)
	assert load_config(copy_path) == original.with_base_dir(copy_path.parent)


def test_save_leaves_out_gains_that_are_not_set(tmp_path):
	path = tmp_path / "polydriver.toml"
	save_config(load_config(tmp_path / "missing.toml"), path)
	saved = tomllib.loads(path.read_text(encoding="utf-8"))
	assert "kp" not in saved["board"]["polymer"]
	assert "polymer" not in saved["ports"]


def test_a_config_is_immutable():
	with pytest.raises(AttributeError):
		Config.defaults().comms_timeout_s = 5.0


def test_board_settings_check_their_own_values():
	with pytest.raises(ValueError):
		BoardSettings(alpha=0.0)
	with pytest.raises(ValueError):
		BoardSettings(kp=1.0)
	with pytest.raises(ValueError):
		BoardSettings(kp=math.inf, ki=0.0, kd=0.0)

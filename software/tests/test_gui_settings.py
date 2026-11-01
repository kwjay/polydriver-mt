import tomllib

import pytest

from polydriver.config import Config, load_config, save_config, write_default_config
from polydriver.devices import JobRunningError, SettingsApplyError
from polydriver.gui.bridge import Bridge
from polydriver.gui.settings_page import SettingsPage
from polydriver.protocol.constants import Role


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


class Session:
	def __init__(self, refuse=None):
		self.reloaded = []
		self.refuse = refuse

	def reload_config(self, config):
		if self.refuse is not None:
			raise self.refuse
		self.reloaded.append(config)


@pytest.fixture
def path(tmp_path):
	path = tmp_path / "polydriver.toml"
	write_default_config(path)
	return path


def make_page(qtbot, bridge, path, session=None, config=None):
	page = SettingsPage(session or Session(), config or load_config(path), bridge, path)
	qtbot.addWidget(page)
	return page


def field(page, section, key):
	return page.fields[(section, key)]


def save_and_wait(qtbot, page, bridge):
	page.save_button.click()
	qtbot.waitUntil(lambda: not bridge.busy and page.save_button.isEnabled(), timeout=2000)


def on_file(path):
	return tomllib.loads(path.read_text(encoding="utf-8"))


def test_the_form_shows_the_file(qtbot, bridge, path):
	page = make_page(qtbot, bridge, path)
	assert field(page, "safety", "comms_timeout_s").text() == "2"
	assert field(page, "board.polymer", "kp").text() == ""
	assert field(page, "board.polymer", "alpha").text() == "1"
	assert field(page, "logging", "enabled").isChecked()
	assert not field(page, "dev", "enabled").isChecked()
	assert field(page, "paths", "materials").text() == "materials.json"


def test_saved_values_are_written_and_sent_to_the_boards(qtbot, bridge, path):
	session = Session()
	page = make_page(qtbot, bridge, path, session)
	saved = []
	page.config_saved.connect(saved.append)
	for key, text in (("kp", "0.5"), ("ki", "2"), ("kd", "0,01")):
		field(page, "board.polymer", key).setText(text)
	field(page, "dev", "enabled").setChecked(True)
	save_and_wait(qtbot, page, bridge)
	data = on_file(path)
	assert data["board"]["polymer"]["kp"] == 0.5 and data["board"]["polymer"]["kd"] == 0.01
	assert data["dev"]["enabled"] is True
	(config,) = session.reloaded
	assert config.boards[Role.POLYMER].ki == 2.0
	assert saved == [config]
	assert "sent to the connected boards" in page.message_label.text()


def test_bad_values_save_nothing_and_are_all_named(qtbot, bridge, path):
	before = path.read_bytes()
	session = Session()
	page = make_page(qtbot, bridge, path, session)
	field(page, "board.colour", "alpha").setText("3")
	field(page, "logging", "poll_hz").setText("fast")
	field(page, "board.polymer", "kp").setText("1")
	page.save_button.click()
	text = page.message_label.text()
	assert text.startswith("not saved:")
	assert "[board.colour] alpha = 3" in text and "poll_hz = 'fast'" in text and "kp, ki and kd together" in text
	assert path.read_bytes() == before
	assert session.reloaded == []


def test_a_blank_field_takes_its_default(qtbot, bridge, path):
	page = make_page(qtbot, bridge, path)
	field(page, "safety", "comms_timeout_s").setText("")
	save_and_wait(qtbot, page, bridge)
	assert load_config(path).comms_timeout_s == 2.0


def test_the_running_programs_ports_are_kept_and_never_written(qtbot, bridge, path):
	session = Session()
	running = load_config(path)
	from dataclasses import replace
	running = replace(running, ports={Role.POLYMER: "SIM-POLYMER", Role.COLOUR: "SIM-COLOUR"})
	page = make_page(qtbot, bridge, path, session, running)
	field(page, "ports", "polymer").setText("COM6")
	save_and_wait(qtbot, page, bridge)
	assert on_file(path)["ports"] == {"polymer": "COM6"}
	assert session.reloaded[0].ports[Role.POLYMER] == "SIM-POLYMER"


def test_while_a_job_runs_the_file_is_saved_but_the_boards_keep_theirs(qtbot, bridge, path):
	page = make_page(qtbot, bridge, path, Session(refuse=JobRunningError("a job is running on polymer")))
	field(page, "board.polymer", "alpha").setText("0.5")
	save_and_wait(qtbot, page, bridge)
	assert load_config(path).boards[Role.POLYMER].alpha == 0.5
	assert "not sent to the boards: a job is running on polymer" in page.message_label.text()


def test_a_board_that_refuses_the_settings_is_named(qtbot, bridge, path):
	page = make_page(qtbot, bridge, path, Session(refuse=SettingsApplyError({Role.COLOUR: OSError("COM5 lost")})))
	save_and_wait(qtbot, page, bridge)
	assert "a board did not take the settings" in page.message_label.text()


def test_a_broken_file_is_reported_and_never_overwritten(qtbot, bridge, tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text("[safety\n", encoding="utf-8")
	page = make_page(qtbot, bridge, path, config=Config(base_dir=tmp_path))
	assert not page.save_button.isEnabled()
	assert "not valid TOML" in page.message_label.text()


def test_no_file_yet_is_written_by_save(qtbot, bridge, tmp_path):
	path = tmp_path / "polydriver.toml"
	page = make_page(qtbot, bridge, path, config=Config(base_dir=tmp_path))
	assert "Save writes one" in page.message_label.text()
	save_and_wait(qtbot, page, bridge)
	assert load_config(path) == Config(base_dir=tmp_path)


def test_showing_the_screen_reads_the_file_again(qtbot, bridge, path):
	page = make_page(qtbot, bridge, path)
	config = load_config(path)
	from dataclasses import replace
	from polydriver.config import BoardSettings
	save_config(replace(config, boards={**config.boards, Role.COLOUR: BoardSettings(kp=1.0, ki=0.0, kd=0.0)}), path)
	page.show()
	assert field(page, "board.colour", "kp").text() == "1"


def test_a_failed_write_says_so(qtbot, bridge, path, monkeypatch):
	import polydriver.gui.settings_page as module

	def refuse(config, path):
		raise OSError("read-only")

	monkeypatch.setattr(module, "save_config", refuse)
	session = Session()
	page = make_page(qtbot, bridge, path, session)
	save_and_wait(qtbot, page, bridge)
	assert "not saved" in page.message_label.text() and "read-only" in page.message_label.text()
	assert session.reloaded == []

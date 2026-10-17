"""polydriver.toml: everything the host sets on the boards and on itself (C8.1).

A missing file gives the defaults; a broken one raises ConfigError naming the file, section and key
of every mistake at once. Relative paths are taken from the folder the file is in. The checks here
are the process-sense ones (decided 2026-10-10); the board still refuses what would harm the motor.
"""

import math
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomli_w

from .protocol.constants import Role

DEFAULT_CONFIG_PATH = Path("polydriver.toml")
DEFAULT_COMMS_TIMEOUT_S = 2.0
DEFAULT_POLL_HZ = 10.0

# SET_COMMS_TIMEOUT carries u16 milliseconds.
MAX_COMMS_TIMEOUT_S = 65.535
MIN_COMMS_TIMEOUT_S = 0.5
POLLS_PER_TIMEOUT = 3
MAX_POLL_HZ = 50.0
MAX_DUTY = 255.0

ROLES = {"polymer": Role.POLYMER, "colour": Role.COLOUR}
GAIN_KEYS = ("kp", "ki", "kd")
BOARD_KEYS = (*GAIN_KEYS, "alpha", "ff_offset_duty", "ff_gain_duty_per_hz")


class ConfigError(ValueError):
	pass


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool)


def _board_problems(kp, ki, kd, alpha, ff_offset_duty, ff_gain_duty_per_hz) -> list[str]:
	problems = []
	gains = {"kp": kp, "ki": ki, "kd": kd}
	given = [name for name, value in gains.items() if value is not None]
	if given and len(given) != 3:
		problems.append("give kp, ki and kd together (or none, to leave the board UNCONFIGURED)")
	for name, value in gains.items():
		if value is None:
			continue
		if not math.isfinite(value):
			problems.append(f"{name} = {value}: must be a finite number")
		elif value < 0:
			problems.append(f"{name} = {value}: must be >= 0")
	if not (math.isfinite(alpha) and 0 < alpha <= 1):
		problems.append(f"alpha = {alpha}: must be 0 < alpha <= 1 (1 = no filtering)")
	if not (math.isfinite(ff_offset_duty) and 0 <= ff_offset_duty <= MAX_DUTY):
		problems.append(f"ff_offset_duty = {ff_offset_duty}: must be 0 to 255")
	if not math.isfinite(ff_gain_duty_per_hz) or ff_gain_duty_per_hz < 0:
		problems.append(f"ff_gain_duty_per_hz = {ff_gain_duty_per_hz}: must be finite and >= 0")
	return problems


@dataclass(frozen=True)
class BoardSettings:
	"""What one board is told on connect. Gains None = SET_PID is not sent, the board stays
	UNCONFIGURED and refuses closed-loop speeds."""

	kp: float | None = None
	ki: float | None = None
	kd: float | None = None
	alpha: float = 1.0
	ff_offset_duty: float = 0.0
	ff_gain_duty_per_hz: float = 0.0

	def __post_init__(self) -> None:
		problems = _board_problems(self.kp, self.ki, self.kd, self.alpha, self.ff_offset_duty, self.ff_gain_duty_per_hz)
		if problems:
			raise ValueError("; ".join(problems))

	@property
	def has_gains(self) -> bool:
		return self.kp is not None


@dataclass(frozen=True)
class LoggingSettings:
	enabled: bool = True
	folder: Path = Path("logs")
	poll_hz: float = DEFAULT_POLL_HZ


@dataclass(frozen=True)
class PathSettings:
	jobs: Path = Path("jobs")
	calibration: Path = Path("calibration.json")
	materials: Path = Path("materials.json")


def _default_ports() -> dict[Role, str | None]:
	return {role: None for role in ROLES.values()}


def _default_boards() -> dict[Role, BoardSettings]:
	return {role: BoardSettings() for role in ROLES.values()}


@dataclass(frozen=True)
class Config:
	ports: dict[Role, str | None] = field(default_factory=_default_ports)
	comms_timeout_s: float = DEFAULT_COMMS_TIMEOUT_S
	boards: dict[Role, BoardSettings] = field(default_factory=_default_boards)
	logging: LoggingSettings = LoggingSettings()
	paths: PathSettings = PathSettings()
	gui_live_plot: bool = True
	dev_enabled: bool = False
	base_dir: Path = Path(".")

	@classmethod
	def defaults(cls) -> "Config":
		return cls()

	@property
	def comms_timeout_ms(self) -> int:
		return round(self.comms_timeout_s * 1000)

	def resolve(self, path: Path) -> Path:
		return path if path.is_absolute() else self.base_dir / path

	def with_base_dir(self, base_dir: Path) -> "Config":
		return replace(self, base_dir=base_dir)


# --- Reading ------------------------------------------------------------------------

class _Reader:
	def __init__(self, data: dict):
		self.data = data
		self.problems: list[str] = []

	def section(self, name: str, keys: tuple[str, ...]) -> dict:
		value = self.data.get(name, {})
		if not isinstance(value, dict):
			self.problems.append(f"{name} must be a section ([{name}]), got {value!r}")
			return {}
		for key in value:
			if key not in keys:
				self.problems.append(f"[{name}] {key}: unknown key (known: {', '.join(keys)})")
		return value

	def number(self, where: str, values: dict, key: str, default):
		value = values.get(key, default)
		if value is None:
			return None
		if not _is_number(value):
			self.problems.append(f"[{where}] {key} = {value!r}: must be a number")
			return default
		return float(value)

	def flag(self, where: str, values: dict, key: str, default: bool) -> bool:
		value = values.get(key, default)
		if not isinstance(value, bool):
			self.problems.append(f"[{where}] {key} = {value!r}: must be true or false")
			return default
		return value

	def text(self, where: str, values: dict, key: str, default):
		value = values.get(key, default)
		if value is None:
			return None
		if not isinstance(value, str):
			self.problems.append(f"[{where}] {key} = {value!r}: must be text in quotes")
			return default
		if not value.strip():
			self.problems.append(f"[{where}] {key}: must not be empty")
			return default
		return value


def _read(data: dict, base_dir: Path) -> tuple[Config, list[str]]:
	reader = _Reader(data)
	known_sections = ("ports", "safety", "board", "logging", "paths", "gui", "dev")
	for name in data:
		if name not in known_sections:
			reader.problems.append(f"[{name}]: unknown section (known: {', '.join(known_sections)})")

	ports_section = reader.section("ports", tuple(ROLES))
	ports = {role: reader.text("ports", ports_section, name, None) for name, role in ROLES.items()}

	safety = reader.section("safety", ("comms_timeout_s",))
	timeout_s = reader.number("safety", safety, "comms_timeout_s", DEFAULT_COMMS_TIMEOUT_S)

	logging_section = reader.section("logging", ("enabled", "folder", "poll_hz"))
	poll_hz = reader.number("logging", logging_section, "poll_hz", DEFAULT_POLL_HZ)
	if not 0 < poll_hz <= MAX_POLL_HZ:
		reader.problems.append(f"[logging] poll_hz = {_shown(poll_hz)}: must be 0 < poll_hz <= {MAX_POLL_HZ:g}")
		poll_hz = DEFAULT_POLL_HZ
	logging = LoggingSettings(
		enabled=reader.flag("logging", logging_section, "enabled", True),
		folder=Path(reader.text("logging", logging_section, "folder", "logs")),
		poll_hz=poll_hz,
	)

	if timeout_s != 0:
		if timeout_s > MAX_COMMS_TIMEOUT_S:
			reader.problems.append(
				f"[safety] comms_timeout_s = {_shown(timeout_s)}: must be at most {MAX_COMMS_TIMEOUT_S} s (the board's limit)"
			)
		elif timeout_s < MIN_COMMS_TIMEOUT_S:
			reader.problems.append(
				f"[safety] comms_timeout_s = {_shown(timeout_s)}: must be 0 (off) or at least {MIN_COMMS_TIMEOUT_S} s"
			)
		elif timeout_s < POLLS_PER_TIMEOUT / poll_hz:
			reader.problems.append(
				f"[safety] comms_timeout_s = {_shown(timeout_s)}: must cover at least {POLLS_PER_TIMEOUT} polls at "
				f"poll_hz {_shown(poll_hz)} ({POLLS_PER_TIMEOUT / poll_hz:g} s), or one late poll stops the motor"
			)

	paths_section = reader.section("paths", ("jobs", "calibration", "materials"))
	paths = PathSettings(
		jobs=Path(reader.text("paths", paths_section, "jobs", "jobs")),
		calibration=Path(reader.text("paths", paths_section, "calibration", "calibration.json")),
		materials=Path(reader.text("paths", paths_section, "materials", "materials.json")),
	)

	board_section = data.get("board", {})
	if not isinstance(board_section, dict):
		reader.problems.append(f"board must be a section ([board.polymer], [board.colour]), got {board_section!r}")
		board_section = {}
	boards = _default_boards()
	for name, values in board_section.items():
		where = f"board.{name}"
		if name not in ROLES:
			reader.problems.append(f"[{where}]: unknown role (known: {', '.join(ROLES)})")
			continue
		if not isinstance(values, dict):
			reader.problems.append(f"[board] {name} must be a section ([{where}]), got {values!r}")
			continue
		for key in values:
			if key not in BOARD_KEYS:
				reader.problems.append(f"[{where}] {key}: unknown key (known: {', '.join(BOARD_KEYS)})")
		numbers = {
			key: reader.number(where, values, key, None if key in GAIN_KEYS else getattr(BoardSettings(), key))
			for key in BOARD_KEYS
		}
		problems = _board_problems(**numbers)
		if problems:
			reader.problems.extend(f"[{where}] {problem}" for problem in problems)
			continue
		boards[ROLES[name]] = BoardSettings(**numbers)

	gui = reader.section("gui", ("live_plot",))
	dev = reader.section("dev", ("enabled",))
	config = Config(
		ports=ports,
		comms_timeout_s=timeout_s,
		boards=boards,
		logging=logging,
		paths=paths,
		gui_live_plot=reader.flag("gui", gui, "live_plot", True),
		dev_enabled=reader.flag("dev", dev, "enabled", False),
		base_dir=base_dir,
	)
	return config, reader.problems


def _shown(value: float) -> str:
	return f"{value:g}"


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
	path = Path(path)
	if not path.exists():
		return Config(base_dir=path.parent)
	try:
		data = tomllib.loads(path.read_text(encoding="utf-8"))
	except tomllib.TOMLDecodeError as error:
		raise ConfigError(f"{path}: not valid TOML: {error}") from error
	config, problems = _read(data, path.parent)
	if problems:
		raise ConfigError(f"{path}:\n  " + "\n  ".join(problems))
	return config


# --- Writing ------------------------------------------------------------------------

def save_config(config: Config, path: Path) -> None:
	"""Writes the values only: comments in an existing file are not kept."""
	boards = {}
	for name, role in ROLES.items():
		settings = config.boards[role]
		values = {key: getattr(settings, key) for key in BOARD_KEYS if getattr(settings, key) is not None}
		boards[name] = values
	data = {
		"ports": {name: config.ports[role] for name, role in ROLES.items() if config.ports[role] is not None},
		"safety": {"comms_timeout_s": config.comms_timeout_s},
		"board": boards,
		"logging": {
			"enabled": config.logging.enabled,
			"folder": config.logging.folder.as_posix(),
			"poll_hz": config.logging.poll_hz,
		},
		"paths": {
			"jobs": config.paths.jobs.as_posix(),
			"calibration": config.paths.calibration.as_posix(),
			"materials": config.paths.materials.as_posix(),
		},
		"gui": {"live_plot": config.gui_live_plot},
		"dev": {"enabled": config.dev_enabled},
	}
	Path(path).write_text(tomli_w.dumps(data), encoding="utf-8")


def write_default_config(path: Path = DEFAULT_CONFIG_PATH) -> None:
	path = Path(path)
	if path.exists():
		raise FileExistsError(f"{path} already exists; it is not overwritten")
	path.write_text(default_config_text(), encoding="utf-8")


def default_config_text() -> str:
	return _TEMPLATE


_TEMPLATE = """\
# PolydriverMT settings. A missing key takes the value shown here.
# Relative paths are taken from the folder this file is in.

[ports]
# polymer = "COM6"
# colour = "COM5"

[safety]
# The board stops the motor when it hears nothing for this long. 0 = off.
# 0.5 to 65.535 s, and at least 3 polls (3 / poll_hz).
comms_timeout_s = 2.0

# Sent to each board on connect: SET_COMMS_TIMEOUT, SET_PID, SET_FILTER, SET_FEEDFORWARD.
# Without kp, ki and kd the board stays UNCONFIGURED and refuses closed-loop speeds.
[board.polymer]
# kp = 0.5
# ki = 1.0
# kd = 0.0
alpha = 1.0                 # EMA filter, 0 < alpha <= 1 (1 = no filtering)
ff_offset_duty = 0.0        # feedforward: duty = offset + gain * target + PID, 0 to 255
ff_gain_duty_per_hz = 0.0

[board.colour]
# kp = 0.5
# ki = 1.0
# kd = 0.0
alpha = 1.0
ff_offset_duty = 0.0
ff_gain_duty_per_hz = 0.0

[logging]
enabled = true
folder = "logs"
poll_hz = 10.0              # STATUS polls per second, 0 < poll_hz <= 50

[paths]
jobs = "jobs"
calibration = "calibration.json"
materials = "materials.json"

[gui]
live_plot = true

[dev]
enabled = false
"""

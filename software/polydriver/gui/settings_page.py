"""Settings: the config file (polydriver.toml) as a form. Save checks every value the way loading
does (config.config_from_data) and writes the file with save_config, so its comments are lost (as
"Save to config" on Manual). The form is read from the file each time the screen is shown, so what
another screen saved is never overwritten with old values.

After a save the board settings and the comms timeout are sent to the boards that are connected
(Session.reload_config, refused while a job runs). Ports, paths, logging, the live plot and the
developer controls are used from the next start. The running program keeps its own ports, so the
simulated ones of --sim never reach the file."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
	QCheckBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
	QVBoxLayout, QWidget,
)

from ..config import BOARD_KEYS, ROLES, Config, ConfigError, config_data, config_from_data, load_config, save_config
from ..devices import JobRunningError, SettingsApplyError
from .bridge import Bridge
from .messages import error_text

NUMBER, TEXT, FLAG = "number", "text", "flag"
BOARD_LABELS = {
	"kp": "kp", "ki": "ki", "kd": "kd", "alpha": "filter alpha", "ff_offset_duty": "feedforward offset [duty]",
	"ff_gain_duty_per_hz": "feedforward gain [duty/Hz]",
}
SECTIONS: tuple[tuple[str, str, tuple[tuple[str, str, str], ...]], ...] = (
	("ports", "Serial ports (blank: choose on Devices)", tuple((name, name, TEXT) for name in ROLES)),
	("safety", "Safety", (("comms_timeout_s", "comms timeout [s] (0 = off)", NUMBER),)),
	*((f"board.{name}", f"{name.capitalize()} board (blank gains: board stays unconfigured)",
		tuple((key, BOARD_LABELS[key], NUMBER) for key in BOARD_KEYS)) for name in ROLES),
	("logging", "Logging", (("enabled", "record every run", FLAG), ("folder", "folder", TEXT),
							("poll_hz", "polls per second", NUMBER))),
	("paths", "Files", (("jobs", "jobs folder", TEXT), ("calibration", "calibration file", TEXT),
						("materials", "materials file", TEXT))),
	("gui", "Window", (("live_plot", "live plot", FLAG),)),
	("dev", "Developer", (("enabled", "show open loop and duty on Manual", FLAG),)),
)


class SettingsPage(QWidget):
	config_saved = Signal(object)
	"""The Config just written, with the running program's ports and folder."""

	def __init__(self, session, config: Config, bridge: Bridge, config_path: Path):
		super().__init__()
		self.session = session
		self.bridge = bridge
		self.config = config
		self.config_path = Path(config_path)
		self.fields: dict[tuple[str, str], QLineEdit | QCheckBox] = {}
		self.save_button = QPushButton("Save and apply")
		self.reload_button = QPushButton("Reload from file")
		self.message_label = QLabel("")
		self.message_label.setWordWrap(True)

		grid = QGridLayout()
		for number, (section, title, keys) in enumerate(SECTIONS):
			box = QGroupBox(title)
			form = QFormLayout(box)
			for key, label, kind in keys:
				field = QCheckBox() if kind == FLAG else QLineEdit()
				self.fields[(section, key)] = field
				form.addRow(label, field)
			grid.addWidget(box, number // 2, number % 2)
		inner = QWidget()
		inner.setLayout(grid)
		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setWidget(inner)
		buttons = QHBoxLayout()
		buttons.addWidget(self.save_button)
		buttons.addWidget(self.reload_button)
		buttons.addStretch(1)
		layout = QVBoxLayout(self)
		layout.addWidget(QLabel(f"file: {self.config_path} (saving drops its comments)"))
		layout.addWidget(scroll, 1)
		layout.addLayout(buttons)
		layout.addWidget(self.message_label)
		self.save_button.clicked.connect(self.save)
		self.reload_button.clicked.connect(self.reload)
		self.reload()

	def showEvent(self, event) -> None:
		self.reload()
		super().showEvent(event)

	def reload(self) -> None:
		try:
			on_file = load_config(self.config_path)
		except ConfigError as error:
			self.save_button.setEnabled(False)
			self.message_label.setText(f"{error}\nThe file is left as it is: fix it by hand, then restart.")
			return
		self.save_button.setEnabled(True)
		self.message_label.setText("" if self.config_path.exists() else "no config file yet: Save writes one")
		self.fill(config_data(on_file))

	def fill(self, data: dict) -> None:
		for (section, key), field in self.fields.items():
			value = _section(data, section).get(key)
			if isinstance(field, QCheckBox):
				field.setChecked(bool(value))
			else:
				field.setText("" if value is None else f"{value:g}" if isinstance(value, float) else str(value))

	def data(self) -> dict:
		"""The sections as typed: a blank field is left out (its default), a number that is not one is
		kept as text so the problem names it."""
		data: dict = {}
		for (section, key), field in self.fields.items():
			kind = _kind(section, key)
			if kind == FLAG:
				value = field.isChecked()
			else:
				text = field.text().strip()
				if not text:
					continue
				value = _number(text) if kind == NUMBER else text
			target = data
			for part in section.split("."):
				target = target.setdefault(part, {})
			target[key] = value
		return data

	def save(self) -> None:
		try:
			config = config_from_data(self.data(), self.config_path.parent)
		except ConfigError as error:
			self.message_label.setText(f"not saved:\n{error}")
			return
		running = replace(config, ports=self.config.ports, base_dir=self.config.base_dir)
		self.message_label.setText("")
		self.save_button.setEnabled(False)
		self.bridge.run(self._save_and_apply, config, running, name="save settings",
						on_result=lambda outcome: self._saved(running, outcome),
						on_error=self._save_failed)

	def _save_and_apply(self, config: Config, running: Config) -> str:
		"""On the bridge's worker: the file first, then the boards; returns what happened to the boards."""
		save_config(config, self.config_path)
		try:
			self.session.reload_config(running)
		except JobRunningError as error:
			return f"saved, but not sent to the boards: {error}"
		except SettingsApplyError as error:
			return f"saved, but a board did not take the settings: {error}"
		return "saved; sent to the connected boards. Ports, files, logging, the window and the developer " \
			"controls are used from the next start."

	def _saved(self, running: Config, outcome: str) -> None:
		self.save_button.setEnabled(True)
		self.config = running
		self.message_label.setText(f"{outcome}\n{self.config_path}")
		self.config_saved.emit(running)

	def _save_failed(self, error: BaseException) -> None:
		self.save_button.setEnabled(True)
		self.message_label.setText(f"not saved: {error_text('save settings', error)}")


def _section(data: dict, section: str) -> dict:
	for part in section.split("."):
		data = data.get(part, {})
	return data


def _kind(section: str, key: str) -> str:
	for name, _, keys in SECTIONS:
		if name == section:
			return next(kind for k, _, kind in keys if k == key)
	raise KeyError(section)


def _number(text: str):
	try:
		return float(text.replace(",", "."))
	except ValueError:
		return text

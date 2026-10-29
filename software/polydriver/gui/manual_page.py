"""Manual: test one board's speed loop. Send PID gains, filter alpha and feedforward; drive the screw
at a speed (closed loop) or a duty (open loop); STOP; watch target, raw and filtered speed and the
duty live; record the polls to a run log (CSV + sidecar) for later analysis.

The target line is what the board reports in SETTINGS after each command, not what was typed."""

import math
from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
	QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout,
	QWidget,
)

from ..cli.board_commands import flag_names, settings_text
from ..config import BoardSettings, Config
from ..devices import BoardStatus
from ..protocol.constants import Mode, Role
from ..requests import SettingsReport
from ..telemetry import start_run_log
from .bridge import Bridge
from .messages import error_text, role_title
from .telemetry import QtSink

ROLES = (Role.POLYMER, Role.COLOUR)
WINDOW_S = 30.0
MAX_SPEED_HZ = 200.0
SERIES = ("target", "raw", "filtered", "duty")
PENS = {
	"target": pg.mkPen("#9e9e9e", width=2, style=Qt.PenStyle.DashLine),
	"raw": pg.mkPen("#90caf9", width=1),
	"filtered": pg.mkPen("#1565c0", width=2),
	"duty": pg.mkPen("#ef6c00", width=2),
}


class Traces:
	"""The last WINDOW_S seconds of one board's polls."""

	def __init__(self, window_s: float = WINDOW_S):
		self._window_s = window_s
		self._times: deque[float] = deque()
		self._values = {name: deque() for name in SERIES}

	def add(self, t_s: float, target_hz: float, status: BoardStatus) -> None:
		r = status.report
		self._times.append(t_s)
		for name, value in zip(SERIES, (target_hz, r.raw_speed_hz, r.filtered_speed_hz, r.duty)):
			self._values[name].append(value)
		while self._times and self._times[0] < t_s - self._window_s:
			self._times.popleft()
			for values in self._values.values():
				values.popleft()

	def times(self) -> list[float]:
		return list(self._times)

	def values(self, name: str) -> list[float]:
		return list(self._values[name])


def _spin(minimum: float, maximum: float, decimals: int, step: float) -> QDoubleSpinBox:
	spin = QDoubleSpinBox()
	spin.setRange(minimum, maximum)
	spin.setDecimals(decimals)
	spin.setSingleStep(step)
	return spin


class ManualPage(QWidget):
	def __init__(self, session, config: Config, bridge: Bridge, sink: QtSink, *, start_log=start_run_log):
		super().__init__()
		self._start_log = start_log
		self.session = session
		self.config = config
		self.bridge = bridge
		self.role = Role.POLYMER
		self.log = None
		self.traces = {role: Traces() for role in ROLES}
		self._targets = {role: 0.0 for role in ROLES}
		self._last_status: dict[Role, BoardStatus] = {}

		self.role_combo = QComboBox()
		for role in ROLES:
			self.role_combo.addItem(f"{role_title(role)} board", role)

		self.kp = _spin(0.0, 1000.0, 4, 0.05)
		self.ki = _spin(0.0, 1000.0, 4, 0.05)
		self.kd = _spin(0.0, 1000.0, 4, 0.01)
		self.alpha = _spin(0.01, 1.0, 3, 0.05)
		self.ff_offset = _spin(0.0, 255.0, 1, 1.0)
		self.ff_gain = _spin(0.0, 255.0, 3, 0.1)
		self.send_button = QPushButton("Send to board")
		self.read_button = QPushButton("Read from board")
		self.board_label = QLabel("")
		self.board_label.setWordWrap(True)

		self.speed = _spin(0.0, MAX_SPEED_HZ, 2, 0.5)
		self.speed.setSuffix(" Hz")
		self.speed_button = QPushButton("Set speed")
		self.duty = QSpinBox()
		self.duty.setRange(0, 255)
		self.duty_button = QPushButton("Set duty")
		self.closed_loop_button = QPushButton("Closed loop")
		self.open_loop_button = QPushButton("Open loop")
		self.stop_button = QPushButton("STOP this board")
		self.stop_button.setStyleSheet("background: #c62828; color: white; font-weight: bold; padding: 6px;")
		self.mode_label = QLabel("")
		self.message_label = QLabel("")
		self.message_label.setStyleSheet("color: #c62828;")
		self.message_label.setWordWrap(True)

		self.readout_label = QLabel("")
		self.record_button = QPushButton("Record CSV")
		self.record_label = QLabel("")

		self.speed_plot = pg.PlotWidget()
		self.speed_plot.setLabel("left", "speed", units="Hz")
		self.speed_plot.addLegend(offset=(10, 10))
		self.duty_plot = pg.PlotWidget()
		self.duty_plot.setLabel("left", "duty")
		self.duty_plot.setLabel("bottom", "time", units="s")
		self.duty_plot.setYRange(0, 255)
		self.duty_plot.setXLink(self.speed_plot)
		self.curves = {name: self.speed_plot.plot([], [], pen=PENS[name], name=name)
						for name in ("target", "raw", "filtered")}
		self.curves["duty"] = self.duty_plot.plot([], [], pen=PENS["duty"], name="duty")

		self._lay_out()
		self.role_combo.currentIndexChanged.connect(lambda _: self.select_role(self.role_combo.currentData()))
		self.send_button.clicked.connect(self.send_settings)
		self.read_button.clicked.connect(self.read_settings)
		self.speed_button.clicked.connect(lambda: self._command(f"speed {self.speed.value():g} Hz",
																lambda board, hz=self.speed.value(): board.set_speed(hz)))
		self.duty_button.clicked.connect(lambda: self._command(f"duty {self.duty.value()}",
															lambda board, duty=self.duty.value(): board.set_pwm(duty)))
		self.closed_loop_button.clicked.connect(lambda: self._command("closed loop",
																	lambda board: board.set_mode(Mode.CLOSED_LOOP)))
		self.open_loop_button.clicked.connect(lambda: self._command("open loop",
																	lambda board: board.set_mode(Mode.OPEN_LOOP)))
		self.stop_button.clicked.connect(self.stop_board)
		self.record_button.clicked.connect(self.toggle_recording)
		sink.status.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
		self._fill_form(config.boards[self.role])

	def _lay_out(self) -> None:
		settings = QGroupBox("Controller")
		form = QFormLayout(settings)
		form.addRow("kp", self.kp)
		form.addRow("ki", self.ki)
		form.addRow("kd", self.kd)
		form.addRow("filter alpha", self.alpha)
		form.addRow("feedforward offset [duty]", self.ff_offset)
		form.addRow("feedforward gain [duty/Hz]", self.ff_gain)
		buttons = QHBoxLayout()
		buttons.addWidget(self.send_button)
		buttons.addWidget(self.read_button)
		form.addRow(buttons)
		form.addRow(self.board_label)

		drive = QGroupBox("Drive")
		drive_layout = QFormLayout(drive)
		modes = QHBoxLayout()
		modes.addWidget(self.closed_loop_button)
		modes.addWidget(self.open_loop_button)
		drive_layout.addRow("mode", modes)
		speed_row = QHBoxLayout()
		speed_row.addWidget(self.speed)
		speed_row.addWidget(self.speed_button)
		drive_layout.addRow("speed", speed_row)
		duty_row = QHBoxLayout()
		duty_row.addWidget(self.duty)
		duty_row.addWidget(self.duty_button)
		drive_layout.addRow("duty (open loop)", duty_row)
		drive_layout.addRow(self.mode_label)
		drive_layout.addRow(self.stop_button)

		record = QHBoxLayout()
		record.addWidget(self.record_button)
		record.addWidget(self.record_label, 1)

		left = QVBoxLayout()
		left.addWidget(self.role_combo)
		left.addWidget(settings)
		left.addWidget(drive)
		left.addWidget(self.message_label)
		left.addStretch(1)
		left.addLayout(record)
		right = QVBoxLayout()
		right.addWidget(self.readout_label)
		right.addWidget(self.speed_plot, 3)
		right.addWidget(self.duty_plot, 1)
		layout = QHBoxLayout(self)
		layout.addLayout(left)
		layout.addLayout(right, 1)

	# --- Settings ---------------------------------------------------------------------------------

	def form_values(self) -> BoardSettings:
		return BoardSettings(kp=self.kp.value(), ki=self.ki.value(), kd=self.kd.value(), alpha=self.alpha.value(),
							ff_offset_duty=self.ff_offset.value(), ff_gain_duty_per_hz=self.ff_gain.value())

	def select_role(self, role: Role) -> None:
		self.role = role
		self.role_combo.setCurrentIndex(ROLES.index(role))
		self._fill_form(self.config.boards[role])
		self.message_label.setText("")
		self.board_label.setText("")
		self.mode_label.setText("")
		self._show_readout()
		self._redraw()

	def send_settings(self) -> None:
		role, settings = self.role, self.form_values()
		self._clear_message()
		self.bridge.run(self.session.apply_settings, role, settings, name=f"send settings to {role.name.lower()}",
						on_result=lambda report: self._show_settings(role, report),
						on_error=lambda error: self._show_error("send settings", error, role))

	def read_settings(self) -> None:
		role = self.role
		self._clear_message()
		self.bridge.run(lambda: self.session.board(role).settings(), name=f"read settings of {role.name.lower()}",
						on_result=lambda report: self._read_into_form(role, report),
						on_error=lambda error: self._show_error("read settings", error, role))

	def _fill_form(self, settings: BoardSettings) -> None:
		self.kp.setValue(settings.kp or 0.0)
		self.ki.setValue(settings.ki or 0.0)
		self.kd.setValue(settings.kd or 0.0)
		self.alpha.setValue(settings.alpha)
		self.ff_offset.setValue(settings.ff_offset_duty)
		self.ff_gain.setValue(settings.ff_gain_duty_per_hz)

	def _read_into_form(self, role: Role, report: SettingsReport) -> None:
		self._show_settings(role, report)
		if role is self.role:
			self._fill_form(BoardSettings(kp=report.kp, ki=report.ki, kd=report.kd, alpha=report.alpha,
										ff_offset_duty=report.feedforward_offset_duty,
										ff_gain_duty_per_hz=report.feedforward_gain_duty_per_hz))

	# --- Driving ----------------------------------------------------------------------------------

	def stop_board(self) -> None:
		role = self.role
		self.bridge.run_now(self._on_board, role, lambda board: board.stop(), name=f"stop {role.name.lower()}",
							on_result=lambda report: self._show_settings(role, report),
							on_error=lambda error: self._show_error("stop", error, role))

	def on_stopped(self) -> None:
		"""Every board was sent STOP (the window's STOP)."""
		for role in ROLES:
			self._targets[role] = 0.0

	def target_hz(self, role: Role) -> float:
		return self._targets[role]

	def _command(self, what: str, action) -> None:
		role = self.role
		self._clear_message()
		self.bridge.run(self._on_board, role, action, name=f"{what} on {role.name.lower()}",
						on_result=lambda report: self._show_settings(role, report),
						on_error=lambda error: self._show_error(what, error, role))

	def _on_board(self, role: Role, action) -> SettingsReport:
		board = self.session.board(role)
		action(board)
		return board.settings()

	def _show_settings(self, role: Role, report: SettingsReport) -> None:
		closed = report.mode is Mode.CLOSED_LOOP
		self._targets[role] = report.target_speed_hz if closed else math.nan
		if role is self.role:
			self.board_label.setText(f"on the board: {settings_text(report)}")
			self.mode_label.setText(f"closed loop, target {report.target_speed_hz:g} Hz" if closed
									else f"open loop, duty {report.duty_setpoint}")

	def _show_error(self, what: str, error: BaseException, role: Role) -> None:
		text = error_text(f"{what} on {role.name.lower()}", error, role)
		if role is self.role:
			self.message_label.setText(text)

	def _clear_message(self) -> None:
		self.message_label.setText("")

	# --- Live status ------------------------------------------------------------------------------

	def _on_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		self.traces[role].add(host_time_s, self._targets[role], status)
		self._last_status[role] = status
		if role is self.role:
			self._show_readout()
			self._redraw()

	def _show_readout(self) -> None:
		status = self._last_status.get(self.role)
		if status is None:
			self.readout_label.setText("")
			return
		r = status.report
		self.readout_label.setText(f"raw {r.raw_speed_hz:.2f} Hz · filtered {r.filtered_speed_hz:.2f} Hz · "
								f"duty {r.duty} · {flag_names(r.flags)}")

	def _redraw(self) -> None:
		traces = self.traces[self.role]
		times = traces.times()
		for name, curve in self.curves.items():
			curve.setData(times, traces.values(name), connect="finite")

	# --- Recording --------------------------------------------------------------------------------

	def toggle_recording(self) -> None:
		if self.log is None:
			self.record_button.setEnabled(False)
			self.bridge.run(self._start_log, self.session, self.config, note="PID test", name="start recording",
							on_result=self._recording, on_error=self._recording_failed)
		else:
			log, self.log = self.log, None
			self.record_button.setEnabled(False)
			self.bridge.run(log.close, name="stop recording", on_result=lambda _: self._recorded(log),
							on_error=self._recording_failed)

	def shutdown(self) -> None:
		"""After the window closed and the boards got STOP: closes a recording still open."""
		if self.log is not None:
			log, self.log = self.log, None
			log.close()

	def _recording(self, log) -> None:
		self.log = log
		self.record_button.setEnabled(True)
		self.record_button.setText("Stop recording")
		self.record_label.setText(f"recording to {log.csv_path}")

	def _recorded(self, log) -> None:
		self.record_button.setEnabled(True)
		self.record_button.setText("Record CSV")
		self.record_label.setText(f"saved {log.csv_path}")

	def _recording_failed(self, error: BaseException) -> None:
		self.record_button.setEnabled(True)
		self.record_button.setText("Stop recording" if self.log is not None else "Record CSV")
		self.record_label.setText(error_text("recording", error))

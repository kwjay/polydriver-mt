"""Devices: a panel per board (port, Connect, Disconnect, firmware, status lights, speed) and the
list of what the boards report. Every Session call goes through the Bridge; STATUS and events come
from the QtSink."""

import time
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
	QComboBox, QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..cli.board_commands import firmware_text, timeout_text
from ..config import Config
from ..devices import BoardState, BoardStatus, Event, EventKind
from ..protocol.constants import Role, StatusFlag
from .bridge import Bridge
from .messages import error_text, role_title
from .telemetry import QtSink

ROLES = (Role.POLYMER, Role.COLOUR)

LIGHTS = (
	StatusFlag.REGULATING,
	StatusFlag.OPEN_LOOP,
	StatusFlag.ENCODER_STALLED,
	StatusFlag.COMMS_LOST,
	StatusFlag.UNCONFIGURED,
	StatusFlag.ESTOP,
	StatusFlag.WATCHDOG_RESET,
)
LIGHT_COLOURS = {
	StatusFlag.REGULATING: "#2e7d32",
	StatusFlag.OPEN_LOOP: "#1565c0",
	StatusFlag.ENCODER_STALLED: "#ef6c00",
	StatusFlag.COMMS_LOST: "#c62828",
	StatusFlag.UNCONFIGURED: "#ef6c00",
	StatusFlag.ESTOP: "#c62828",
	StatusFlag.WATCHDOG_RESET: "#c62828",
}
OFF_STYLE = "color: palette(mid); border: 1px solid palette(mid); border-radius: 4px; padding: 2px 6px;"
ON_STYLE = "color: white; background: {colour}; border: 1px solid {colour}; border-radius: 4px; padding: 2px 6px;"


def serial_ports() -> list[tuple[str, str]]:
	"""(device, description) of every serial port, as the shell's `ports` lists them."""
	from serial.tools import list_ports
	return [(port.device, port.description) for port in sorted(list_ports.comports(), key=lambda port: port.device)]


class Light(QLabel):
	def __init__(self, flag: StatusFlag):
		super().__init__(flag.name.replace("_", " ").lower())
		self._on_style = ON_STYLE.format(colour=LIGHT_COLOURS[flag])
		self.is_on = False
		self.setStyleSheet(OFF_STYLE)

	def set_on(self, on: bool) -> None:
		if on != self.is_on:
			self.is_on = on
			self.setStyleSheet(self._on_style if on else OFF_STYLE)


class BoardPanel(QGroupBox):
	def __init__(self, role: Role):
		super().__init__(f"{role_title(role)} board")
		self.role = role
		self.connected = False
		self.port_combo = QComboBox()
		self.port_combo.setMinimumWidth(260)
		self.connect_button = QPushButton("Connect")
		self.disconnect_button = QPushButton("Disconnect")
		self.disconnect_button.setEnabled(False)
		self.info_label = QLabel("not connected")
		self.speed_label = QLabel("")
		self.lights = {flag: Light(flag) for flag in LIGHTS}

		top = QHBoxLayout()
		top.addWidget(QLabel("Port"))
		top.addWidget(self.port_combo, 1)
		top.addWidget(self.connect_button)
		top.addWidget(self.disconnect_button)
		lights = QHBoxLayout()
		for light in self.lights.values():
			lights.addWidget(light)
		lights.addStretch(1)
		layout = QVBoxLayout(self)
		layout.addLayout(top)
		layout.addWidget(self.info_label)
		layout.addLayout(lights)
		layout.addWidget(self.speed_label)

	def port(self) -> str | None:
		return self.port_combo.currentData()

	def select_port(self, port: str) -> None:
		index = self.port_combo.findData(port)
		if index >= 0:
			self.port_combo.setCurrentIndex(index)

	def set_ports(self, found: list[tuple[str, str]], configured: str | None) -> None:
		keep = self.port() or configured
		self.port_combo.clear()
		for device, description in found:
			self.port_combo.addItem(f"{device} — {description}" if description else device, device)
		if configured and self.port_combo.findData(configured) < 0:
			self.port_combo.addItem(f"{configured} — in the config, not found", configured)
		if keep:
			self.select_port(keep)

	def show_connecting(self) -> None:
		self.info_label.setText(f"connecting to {self.port()}…")
		self._enable(connect=False, disconnect=False)

	def show_connected(self, state: BoardState) -> None:
		self.connected = True
		v = state.version
		self.info_label.setText(f"{state.port} · {firmware_text(state)} · protocol {v.protocol_version} · "
								f"comms timeout {timeout_text(state.settings.comms_timeout_ms)}")
		self._enable(connect=False, disconnect=True)

	def show_disconnected(self, reason: str = "") -> None:
		self.connected = False
		self.info_label.setText(f"not connected: {reason}" if reason else "not connected")
		self._enable(connect=True, disconnect=False)

	def show_status(self, status: BoardStatus) -> None:
		r = status.report
		for flag, light in self.lights.items():
			light.set_on(flag in r.flags)
		self.speed_label.setText(f"{r.raw_speed_hz:.2f} Hz (filtered {r.filtered_speed_hz:.2f}) · duty {r.duty} · "
								f"job {r.job_state.name}")

	def _enable(self, *, connect: bool, disconnect: bool) -> None:
		self.connect_button.setEnabled(connect)
		self.port_combo.setEnabled(connect)
		self.disconnect_button.setEnabled(disconnect)


class DevicesPage(QWidget):
	MAX_EVENT_LINES = 200

	def __init__(self, session, config: Config, bridge: Bridge, sink: QtSink, *,
				list_ports: Callable[[], list[tuple[str, str]]] = serial_ports):
		super().__init__()
		self.session = session
		self.config = config
		self.bridge = bridge
		self._list_ports = list_ports
		self.panels = {role: BoardPanel(role) for role in ROLES}
		self.refresh_button = QPushButton("Refresh ports")
		self.connect_both_button = QPushButton("Connect both")
		self.events = QPlainTextEdit()
		self.events.setReadOnly(True)
		self.events.setMaximumBlockCount(self.MAX_EVENT_LINES)
		self.events.setFont(QFont("Consolas", 9))

		buttons = QHBoxLayout()
		buttons.addWidget(self.connect_both_button)
		buttons.addWidget(self.refresh_button)
		buttons.addStretch(1)
		layout = QVBoxLayout(self)
		layout.addLayout(buttons)
		for panel in self.panels.values():
			layout.addWidget(panel)
		layout.addWidget(QLabel("What the boards report"))
		layout.addWidget(self.events, 1)

		for role, panel in self.panels.items():
			panel.connect_button.clicked.connect(lambda _=False, role=role: self.connect_board(role))
			panel.disconnect_button.clicked.connect(lambda _=False, role=role: self.disconnect_board(role))
		self.connect_both_button.clicked.connect(self.connect_both)
		self.refresh_button.clicked.connect(self.refresh_ports)
		sink.status.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
		sink.event.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
		self.refresh_ports()

	def refresh_ports(self) -> None:
		found = self._list_ports()
		for role, panel in self.panels.items():
			panel.set_ports(found, self.config.ports.get(role))

	def connect_board(self, role: Role) -> None:
		panel = self.panels[role]
		if panel.connected or not panel.connect_button.isEnabled():
			return
		panel.show_connecting()
		self.bridge.run(self.session.connect, role, panel.port(), name=f"connect {role.name.lower()}",
						on_result=panel.show_connected,
						on_error=lambda error: panel.show_disconnected(error_text(f"connect {role.name.lower()}", error, role)))

	def connect_both(self) -> None:
		for role in ROLES:
			self.connect_board(role)

	def disconnect_board(self, role: Role) -> None:
		panel = self.panels[role]
		self.bridge.run(self.session.disconnect, role, name=f"disconnect {role.name.lower()}",
						on_result=lambda _: panel.show_disconnected())

	def events_text(self) -> str:
		return self.events.toPlainText()

	def _on_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		self.panels[role].show_status(status)

	def _on_event(self, event: Event) -> None:
		self.events.appendPlainText(f"{time.strftime('%H:%M:%S')}  {event.role.name.lower()}: {event.kind}: {event.message}")
		if event.kind is EventKind.LINK_LOST:
			self.panels[event.role].show_disconnected(f"link lost: {event.message}")

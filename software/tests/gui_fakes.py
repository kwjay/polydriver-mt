"""Fakes for the GUI screen tests: a Session and Board that answer at once (or after a set delay),
record what they were asked, and refuse what a test tells them to."""

import threading
import time

from polydriver.devices import BoardState, BoardStatus, Event, EventKind, NotConnectedError
from polydriver.protocol.constants import JobState, Mode, Role, StatusFlag
from polydriver.requests import SettingsReport, StatusReport, VersionReport

VERSION = VersionReport(protocol_version=3, firmware_major=3, firmware_minor=0, firmware_patch=0, max_segments=32)


def make_status(raw_hz=10.0, filtered_hz=9.5, duty=140, flags=StatusFlag.REGULATING, job_state=JobState.EMPTY):
	report = StatusReport(
		raw_speed_hz=raw_hz, filtered_speed_hz=filtered_hz, duty=duty, flags=flags, device_time_us=0,
		edge_count=0, control_resyncs=0, longest_loop_interval_us=0, dropped_frames=0, bad_crc_frames=0,
		job_state=job_state, segment_index=None, job_elapsed_ms=0,
	)
	return BoardStatus(report=report, device_time_us=0, device_restarted=False)


def make_event(kind=EventKind.LINK_LOST, role=Role.POLYMER, message="COM6 lost", host_time_s=1.0):
	return Event(kind=kind, role=role, message=message, host_time_s=host_time_s)


class FakeBoard:
	def __init__(self):
		self.calls = []
		self.refuse = {}
		self.kp, self.ki, self.kd = 0.5, 2.0, 0.0
		self.alpha = 0.5
		self.ff_offset_duty, self.ff_gain_duty_per_hz = 128.0, 1.67
		self.target_speed_hz = 0.0
		self.mode = Mode.CLOSED_LOOP
		self.duty = 0

	def _command(self, name, *args):
		self.calls.append((name, *args))
		if name in self.refuse:
			raise self.refuse[name]

	def set_speed(self, hz):
		self._command("set_speed", hz)
		self.target_speed_hz = hz

	def set_pwm(self, duty):
		self._command("set_pwm", duty)
		self.duty = duty

	def set_mode(self, mode):
		self._command("set_mode", mode)
		self.mode = mode
		self.target_speed_hz = 0.0
		self.duty = 0

	def stop(self):
		self._command("stop")
		self.target_speed_hz = 0.0
		self.duty = 0

	def settings(self):
		return SettingsReport(
			kp=self.kp, ki=self.ki, kd=self.kd, target_speed_hz=self.target_speed_hz, comms_timeout_ms=2000,
			mode=self.mode, duty_setpoint=self.duty, alpha=self.alpha,
			feedforward_offset_duty=self.ff_offset_duty, feedforward_gain_duty_per_hz=self.ff_gain_duty_per_hz,
		)


class FakeSession:
	def __init__(self):
		self.boards = {}
		self.calls = []
		self.refuse = {}
		self.sinks = []
		self.connect_delay_s = 0.0
		self.lock = threading.Lock()

	def connect(self, role, port=None):
		with self.lock:
			self.calls.append(("connect", role, port))
		time.sleep(self.connect_delay_s)
		if ("connect", role) in self.refuse:
			raise self.refuse[("connect", role)]
		board = FakeBoard()
		self.boards[role] = board
		return BoardState(role=role, port=port or f"COM{role.value}", connected=True, version=VERSION,
						settings=board.settings())

	def disconnect(self, role):
		self.calls.append(("disconnect", role))
		if self.boards.pop(role, None) is None:
			raise NotConnectedError(f"the {role.name.lower()} board is not connected")

	def board(self, role):
		if role not in self.boards:
			raise NotConnectedError(f"the {role.name.lower()} board is not connected")
		return self.boards[role]

	def roles(self):
		return list(self.boards)

	def apply_settings(self, role, settings=None):
		self.calls.append(("apply_settings", role, settings))
		board = self.board(role)
		board.kp, board.ki, board.kd = settings.kp, settings.ki, settings.kd
		board.alpha = settings.alpha
		board.ff_offset_duty, board.ff_gain_duty_per_hz = settings.ff_offset_duty, settings.ff_gain_duty_per_hz
		return board.settings()

	def stop_all(self):
		self.calls.append(("stop_all",))
		failures = {}
		for role, board in self.boards.items():
			try:
				board.stop()
			except Exception as error:
				failures[role] = error
		return failures

	def add_sink(self, sink):
		self.sinks.append(sink)

	def remove_sink(self, sink):
		self.sinks.remove(sink)

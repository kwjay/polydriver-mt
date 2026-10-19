"""Fakes shared by the Session and Fleet tests: a board behind a link, answering requests the way the
firmware does, without bytes or threads of its own."""

import threading
import time
from concurrent.futures import Future

from polydriver.config import BoardSettings, Config, LoggingSettings
from polydriver.devices.board import BoardStatus, _f32
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Role, StatusFlag
from polydriver.protocol.serial_worker import LinkError, LinkLostError
from polydriver.requests import (
	GetId, JobClear, JobCommit, JobInfo, JobRun, JobSegment, RequestNackError, RequestSettings, RequestStatus,
	SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetSpeed, SettingsReport, StatusReport, Stop,
	VersionReport,
)
from polydriver.requests.request_queue import Exchange, QueueStateError, RequestTimeoutError

POLYMER_SETTINGS = BoardSettings(kp=0.5, ki=1.0, kd=0.0, alpha=0.4, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.5)
COLOUR_SETTINGS = BoardSettings(alpha=0.5)
ROUND_TRIP_S = 0.003


def make_config(poll_hz: float = 50.0, **changes) -> Config:
	values = dict(
		ports={Role.POLYMER: "COM6", Role.COLOUR: "COM5"},
		comms_timeout_s=2.0,
		boards={Role.POLYMER: POLYMER_SETTINGS, Role.COLOUR: COLOUR_SETTINGS},
		logging=LoggingSettings(poll_hz=poll_hz),
	)
	values.update(changes)
	return Config(**values)


class FakeLink:
	"""A board behind a link. refuse maps a request type to the error that request raises."""

	def __init__(self, port: str, stored_role: Role):
		self.port = port
		self.version = VersionReport(3, 3, 0, 0, 32)
		self.stored_role = stored_role
		self.sent = []
		self.closed = False
		self.lock = threading.Lock()
		self.device_time_us = 1_000_000
		self.configured = False
		self.gains = (0.0, 0.0, 0.0)
		self.alpha = 1.0
		self.feedforward = (0.0, 0.0)
		self.timeout_ms = 0
		self.job_state = JobState.EMPTY
		self.mode = Mode.CLOSED_LOOP
		self.estop = False
		self.segments: list[JobSegment] = []
		self.fail_with: Exception | None = None
		self.refuse: dict[type, Exception] = {}
		self.delays_s: dict[type, float] = {}
		self.status_timeouts = 0
		self.ignore_filter = False
		self.status_delay_s = 0.0
		self.status_in_flight = False
		self.times: dict[type, tuple[float, float, float | None, int]] = {}

	def call(self, request, timeout_s=None):
		with self.lock:
			if self.closed:
				raise LinkLostError(f"{self.port} is closed")
			self.sent.append(request)
			if self.fail_with is not None:
				raise self.fail_with
			if type(request) in self.delays_s:
				time.sleep(self.delays_s[type(request)])
			if type(request) in self.refuse:
				raise self.refuse[type(request)]
			match request:
				case GetId():
					return self.stored_role
				case SetMode(mode=mode):
					self.mode = Mode(mode)
				case SetId(role=role):
					self.stored_role = Role(role)
				case SetCommsTimeout(timeout_ms=timeout_ms):
					self.timeout_ms = timeout_ms
				case SetPid(kp=kp, ki=ki, kd=kd):
					self.gains, self.configured = (_f32(kp), _f32(ki), _f32(kd)), True
				case SetFilter(alpha=alpha) if not self.ignore_filter:
					self.alpha = _f32(alpha)
				case SetFeedforward(offset_duty=offset, gain_duty_per_hz=gain):
					self.feedforward = (_f32(offset), _f32(gain))
				case SetSpeed(target_speed_hz=target) if target > 0 and not self.configured:
					raise RequestNackError(Command.SET_SPEED, NackReason.NOT_CONFIGURED)
				case RequestSettings():
					return SettingsReport(*self.gains, 0.0, self.timeout_ms, Mode.CLOSED_LOOP, 0, self.alpha, *self.feedforward)
				case RequestStatus():
					return self._status()
				case JobClear():
					self._refuse_while_running(Command.JOB_CLEAR)
					self.job_state, self.segments = JobState.EMPTY, []
				case JobSegment(index=index):
					self._refuse_while_running(Command.JOB_SEGMENT)
					if index != len(self.segments):
						raise RequestNackError(Command.JOB_SEGMENT, NackReason.BAD_INDEX)
					self.job_state = JobState.LOADING
					self.segments.append(request)
				case JobCommit(segment_count=count, total_ms=total_ms):
					if count != len(self.segments) or total_ms != sum(s.duration_ms for s in self.segments):
						raise RequestNackError(Command.JOB_COMMIT, NackReason.BAD_JOB)
					self.job_state = JobState.READY
					return JobInfo(JobState.READY, count, total_ms)
				case JobRun():
					if self.job_state is not JobState.READY or self.estop:
						raise RequestNackError(Command.JOB_RUN, NackReason.WRONG_STATE)
					duty_job = self.segments[0].duty
					if self.mode is not (Mode.OPEN_LOOP if duty_job else Mode.CLOSED_LOOP):
						raise RequestNackError(Command.JOB_RUN, NackReason.WRONG_MODE)
					if not duty_job and not self.configured:
						raise RequestNackError(Command.JOB_RUN, NackReason.NOT_CONFIGURED)
					self.job_state = JobState.RUNNING
				case Stop():
					if self.job_state is JobState.RUNNING:
						self.job_state = JobState.ABORTED
			return None

	def _status(self) -> StatusReport:
		self.status_in_flight = True
		try:
			if self.status_delay_s:
				time.sleep(self.status_delay_s)
				self.status_delay_s = 0.0
			if self.status_timeouts > 0:
				self.status_timeouts -= 1
				raise RequestTimeoutError("REQ_STAT got no reply after 4 attempts")
			self.device_time_us += 20_000
			flags = StatusFlag(0) if self.configured else StatusFlag.UNCONFIGURED
			if self.mode is Mode.OPEN_LOOP:
				flags |= StatusFlag.OPEN_LOOP
			if self.estop:
				flags |= StatusFlag.ESTOP
			return StatusReport(12.0, 12.0, 100, flags, self.device_time_us, 0, 0, 10004, 0, 0, self.job_state, None, 0)
		finally:
			self.status_in_flight = False

	def _refuse_while_running(self, command: Command) -> None:
		if self.job_state is JobState.RUNNING:
			raise RequestNackError(command, NackReason.WRONG_STATE)

	def submit_timed(self, request) -> Future:
		"""Answers on its own thread, like the request queue. times[type] = (first_sent_s, sent_s,
		replied_s, attempts) replaces the measured times."""
		future: Future = Future()

		def answer():
			sent_s = time.perf_counter()
			try:
				result, error = self.call(request), None
			except (LinkError, QueueStateError) as lost:
				future.set_exception(lost)
				return
			except Exception as refused:
				result, error = None, refused
			replied_s = None if isinstance(error, RequestTimeoutError) else max(time.perf_counter(), sent_s + ROUND_TRIP_S)
			first_sent_s, last_sent_s, replied_s, attempts = self.times.get(type(request), (sent_s, sent_s, replied_s, 1))
			future.set_result(Exchange(request, 0, attempts, first_sent_s, last_sent_s, replied_s, result, error))

		threading.Thread(target=answer, daemon=True).start()
		return future

	def restart(self) -> None:
		with self.lock:
			self.device_time_us = 5_000
			self.configured = False
			self.gains, self.alpha, self.feedforward, self.timeout_ms = (0.0, 0.0, 0.0), 1.0, (0.0, 0.0), 0
			self.job_state, self.segments = JobState.EMPTY, []
			self.mode, self.estop = Mode.CLOSED_LOOP, False

	def count(self, request_type) -> int:
		with self.lock:
			return sum(1 for r in self.sent if isinstance(r, request_type))

	def close(self) -> None:
		self.closed = True


class Recorder:
	def __init__(self):
		self.statuses: list[tuple[Role, BoardStatus, float]] = []
		self.events = []
		self.lock = threading.Lock()

	def record_status(self, role, status, host_time_s):
		with self.lock:
			self.statuses.append((role, status, host_time_s))

	def record_event(self, event):
		with self.lock:
			self.events.append(event)

	def kinds(self, role=None):
		with self.lock:
			return [e.kind for e in self.events if role is None or e.role is role]

	def status_count(self, role):
		with self.lock:
			return sum(1 for r, _, _ in self.statuses if r is role)


class Boards:
	"""Link factory: one FakeLink per port, each storing the role given here."""

	def __init__(self, roles: dict[str, Role]):
		self.roles = roles
		self.links: dict[str, FakeLink] = {}
		self.opened: list[str] = []

	def __call__(self, port: str) -> FakeLink:
		self.opened.append(port)
		link = FakeLink(port, self.roles[port])
		self.links[port] = link
		return link


def wait_until(predicate, timeout_s: float = 2.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.005)
	return predicate()

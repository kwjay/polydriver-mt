"""Fakes shared by the Session and Fleet tests: a board behind a link, answering requests the way the
firmware does, without bytes or threads of its own."""

import threading
import time
from concurrent.futures import Future

from polydriver.config import BoardSettings, Config, LoggingSettings
from polydriver.devices import LinkCounters
from polydriver.devices.board import BoardStatus, _f32
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Role, StatusFlag
from polydriver.protocol.serial_worker import LinkError, LinkLostError
from polydriver.requests import (
	GetId, JobClear, JobCommit, JobInfo, JobRun, JobSegment, RequestNackError, RequestSettings, RequestStatus,
	SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetPwm, SetSpeed, SettingsReport, StatusReport,
	Stop,
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
		self.boot_s = time.perf_counter() - 1.0
		self.configured = False
		self.gains = (0.0, 0.0, 0.0)
		self.alpha = 1.0
		self.feedforward = (0.0, 0.0)
		self.timeout_ms = 0
		self.job_state = JobState.EMPTY
		self.mode = Mode.CLOSED_LOOP
		self.estop = False
		self.pwm_duty = 0
		self.hz_per_duty = 0.2
		self.segments: list[JobSegment] = []
		self.fail_with: Exception | None = None
		self.refuse: dict[type, Exception] = {}
		self.delays_s: dict[type, float] = {}
		self.status_timeouts = 0
		self.ignore_filter = False
		self.status_delay_s = 0.0
		self.status_in_flight = False
		self.times: dict[type, tuple[float, float, float | None, int]] = {}
		# The job runs on job_time_scale times the real time, so a 60 s job takes 0.6 s at 100.
		self.job_time_scale = 1.0
		self.edge_base = 0
		self.run_started_s: float | None = None
		self.stopped_ms: int | None = None
		# Faults while a job runs: the screw turns at speed_factor x the commanded speed, or not at all
		# (the encoder stalls); extra_flags go into every STATUS.
		self.speed_factor = 1.0
		self.stalled = False
		self.extra_flags = StatusFlag(0)

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
					self.mode, self.pwm_duty = Mode(mode), 0
				case SetPwm(duty=duty):
					if self.mode is not Mode.OPEN_LOOP:
						raise RequestNackError(Command.SET_PWM, NackReason.WRONG_MODE)
					self.pwm_duty = duty
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
					return SettingsReport(*self.gains, 0.0, self.timeout_ms, self.mode, self.pwm_duty, self.alpha, *self.feedforward)
				case RequestStatus():
					return self._status()
				case JobClear():
					self._refuse_while_running(Command.JOB_CLEAR)
					self.job_state, self.segments = JobState.EMPTY, []
					self.run_started_s, self.stopped_ms = None, None
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
					self.job_state, self.run_started_s, self.stopped_ms = JobState.RUNNING, time.perf_counter(), None
				case Stop():
					self.pwm_duty = 0
					self._end_job()
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
			device_time_us = round((time.perf_counter() - self.boot_s) * 1e6) % 2**32
			flags = StatusFlag(0) if self.configured else StatusFlag.UNCONFIGURED
			if self.mode is Mode.OPEN_LOOP:
				flags |= StatusFlag.OPEN_LOOP
			if self.estop:
				flags |= StatusFlag.ESTOP
			if self.mode is Mode.OPEN_LOOP:
				speed_hz, duty = self.pwm_duty * self.hz_per_duty, self.pwm_duty
			else:
				speed_hz, duty = 12.0, 100
			flags |= self.extra_flags
			elapsed_ms, segment, edges = self._job_now()
			if self.run_started_s is not None:
				speed_hz = self._speed_at(elapsed_ms) * self.speed_factor if self.job_state is JobState.RUNNING else 0.0
				if self.stalled and self.job_state is JobState.RUNNING:
					speed_hz, flags = 0.0, flags | StatusFlag.ENCODER_STALLED
			return StatusReport(speed_hz, speed_hz, duty, flags, device_time_us, (self.edge_base + edges) % 2**32, 0, 10004,
								0, 0, self.job_state, segment, elapsed_ms)
		finally:
			self.status_in_flight = False

	# --- The job, run on its own clock --------------------------------------------------

	def _elapsed_ms(self) -> int:
		if self.stopped_ms is not None:
			return self.stopped_ms
		return int((time.perf_counter() - self.run_started_s) * 1000 * self.job_time_scale)

	def _job_now(self) -> tuple[int, int | None, int]:
		"""(elapsed ms, segment index, edges since the start) as the board reports them; a job that
		has run its time is DONE."""
		if self.run_started_s is None:
			return 0, None, 0
		total_ms = sum(s.duration_ms for s in self.segments)
		elapsed_ms = min(self._elapsed_ms(), total_ms)
		if self.job_state is JobState.RUNNING and elapsed_ms >= total_ms:
			self.job_state, self.stopped_ms = JobState.DONE, total_ms
		segment = self._segment_at(elapsed_ms) if self.job_state is JobState.RUNNING else None
		return elapsed_ms, segment, int(self._edges_until(elapsed_ms))

	def _segment_at(self, elapsed_ms: int) -> int:
		start = 0
		for index, segment in enumerate(self.segments):
			if elapsed_ms < start + segment.duration_ms:
				return index
			start += segment.duration_ms
		return len(self.segments) - 1

	def _hz(self, segment) -> float:
		return segment.value * self.hz_per_duty if segment.duty else segment.value

	def _speed_at(self, elapsed_ms: float) -> float:
		start, previous = 0, 0.0
		for segment in self.segments:
			if elapsed_ms < start + segment.duration_ms:
				if not segment.ramp:
					return self._hz(segment)
				return previous + (self._hz(segment) - previous) * (elapsed_ms - start) / segment.duration_ms
			start, previous = start + segment.duration_ms, self._hz(segment)
		return 0.0

	def _edges_until(self, elapsed_ms: float) -> float:
		"""The encoder edges of a screw that follows the job exactly: the integral of its speed."""
		edges, start, previous = 0.0, 0, 0.0
		for segment in self.segments:
			length = min(segment.duration_ms, max(0.0, elapsed_ms - start))
			if length <= 0:
				break
			end_hz = self._hz(segment)
			if segment.ramp:
				reached = previous + (end_hz - previous) * length / segment.duration_ms
				edges += (previous + reached) / 2 * length / 1000
			else:
				edges += end_hz * length / 1000
			start, previous = start + segment.duration_ms, end_hz
		return edges

	def _end_job(self, state: JobState = JobState.ABORTED) -> None:
		if self.job_state is JobState.RUNNING:
			self._job_now()
		if self.job_state is JobState.RUNNING:
			self.stopped_ms = self._elapsed_ms()
			self.job_state = state

	def abort(self, flags: StatusFlag = StatusFlag(0)) -> None:
		"""The board aborts on its own, as on a comms timeout (COMMS_LOST) or ESTOP."""
		with self.lock:
			self.extra_flags |= flags
			self._end_job()

	def freeze_job(self) -> None:
		"""The job's clock stops, so the job never ends."""
		with self.lock:
			if self.job_state is JobState.RUNNING:
				self.stopped_ms = self._elapsed_ms()

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

	@property
	def stats(self) -> LinkCounters:
		with self.lock:
			return LinkCounters(frames_sent=len(self.sent), retries=1, reply_timeouts=1, late_replies=0,
								nacks={NackReason.WRONG_MODE: 2})

	def restart(self) -> None:
		with self.lock:
			self.boot_s = time.perf_counter() - 0.005
			self.configured = False
			self.gains, self.alpha, self.feedforward, self.timeout_ms = (0.0, 0.0, 0.0), 1.0, (0.0, 0.0), 0
			self.job_state, self.segments = JobState.EMPTY, []
			self.mode, self.estop, self.pwm_duty = Mode.CLOSED_LOOP, False, 0
			self.run_started_s, self.stopped_ms = None, None

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

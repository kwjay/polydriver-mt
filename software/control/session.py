"""PolydriverSession: the layer a UI (terminal or GUI) actually talks to.

It owns the SerialWorker + JobManager pair for one serial port, tracks the
live state of every dispenser (target_id) that has been added to it, and
runs a background poller that keeps that state fresh by requesting status
from each tracked dispenser in turn. On top of that it exposes plain,
blocking calls (set_speed, set_pid, request_status, ...) so callers don't
need to know jobs or callbacks exist - submit a job, wait for the
JobManager to resolve it, return the result or raise.

Multiple dispensers are tracked side by side on purpose: the whole point
of this project is comparing several polymer dispensers running off the
same bus, so the session is built around a set of targets from the start
rather than a single one.
"""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from comms.serial_worker import SerialWorker
from jobs.job_manager import JobManager, SerialLike
from jobs.jobs import (
	RequestSettingsJob,
	RequestStatusJob,
	SetPidJob,
	SetSpeedJob,
	SettingsReport,
	StatusReport,
)

logger = logging.getLogger(__name__)

DEFAULT_JOB_TIMEOUT = 0.5
DEFAULT_JOB_RETRIES = 3
DEFAULT_POLL_INTERVAL = 0.5


class SessionError(Exception):
	"""Raised for any session-level failure: not connected, a job that
	never completed in time, or a job that failed outright."""


class TelemetrySink(Protocol):
	"""What the session needs from a telemetry logger - just enough to
	record a status reading and a settings snapshot. TelemetryLogger
	satisfies this structurally; so does any test double with the same
	two methods."""

	def record_status(
		self, target_id: int, report: StatusReport, target_speed: Optional[float] = None
	) -> None: ...
	def record_settings(self, target_id: int, report: SettingsReport) -> None: ...


@dataclass
class DispenserState:
	target_id: int
	name: str
	last_status: Optional[StatusReport] = None
	last_status_at: Optional[float] = None
	last_settings: Optional[SettingsReport] = None
	last_error: Optional[str] = None
	# The last speed this session actually commanded via set_speed(), so a
	# telemetry row can be paired with the setpoint it was measured against
	# (the firmware's own RESP_SETTINGS echo is a separate, on-demand read
	# and isn't kept in sync with this automatically).
	last_commanded_speed: Optional[float] = None


class SessionLike(Protocol):
	"""What a UI (the CLI, or later a GUI) actually needs from a session.
	PolydriverSession satisfies this structurally, and so does a fake used
	in tests - callers depend on this shape, not on PolydriverSession
	itself."""

	def connect(self, port: str, baudrate: int = 115200) -> None: ...
	def disconnect(self) -> None: ...
	def add_target(self, target_id: int, name: Optional[str] = None) -> DispenserState: ...
	def targets(self) -> List[DispenserState]: ...
	def set_speed(self, target_id: int, speed: float) -> None: ...
	def set_pid(self, target_id: int, kp: float, ki: float, kd: float) -> None: ...
	def request_status(self, target_id: int) -> StatusReport: ...
	def request_settings(self, target_id: int) -> SettingsReport: ...
	def attach_telemetry_sink(self, sink: TelemetrySink) -> None: ...
	def detach_telemetry_sink(self) -> None: ...


class PolydriverSession:
	def __init__(
		self,
		poll_interval: float = DEFAULT_POLL_INTERVAL,
		job_timeout: float = DEFAULT_JOB_TIMEOUT,
		job_retries: int = DEFAULT_JOB_RETRIES,
	):
		self.poll_interval = poll_interval
		self.job_timeout = job_timeout
		self.job_retries = job_retries

		self.serial_worker: Optional[SerialWorker] = None
		self.job_manager: Optional[JobManager] = None

		self._targets: Dict[int, DispenserState] = {}
		self._targets_lock = threading.Lock()

		self._telemetry_sink: Optional[TelemetrySink] = None

		self._poll_stop_event: Optional[threading.Event] = None
		self._poll_thread: Optional[threading.Thread] = None

	# --- connection lifecycle -------------------------------------------------

	@property
	def is_connected(self) -> bool:
		return self.job_manager is not None

	def connect(self, port: str, baudrate: int = 115200) -> None:
		"""Open the real serial port and start talking to the bus."""
		if self.is_connected:
			raise SessionError("already connected")
		serial_worker = SerialWorker(port, baudrate)
		serial_worker.start()
		job_manager = JobManager(serial_worker)
		job_manager.start()
		self._attach(serial_worker, job_manager)

	def _attach(self, serial_worker: SerialLike, job_manager: JobManager) -> None:
		"""Hook up an already-started SerialWorker/JobManager pair and start
		the poller. Split out from connect() so tests can attach a fake
		transport without opening a real serial port."""
		self.serial_worker = serial_worker  # type: ignore[assignment]
		self.job_manager = job_manager
		self._poll_stop_event = threading.Event()
		self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
		self._poll_thread.start()

	def disconnect(self) -> None:
		if self._poll_stop_event is not None:
			self._poll_stop_event.set()
		if self._poll_thread is not None:
			# The stop event wakes the poller immediately even mid-sleep, so
			# this join doesn't block for up to a whole poll_interval.
			self._poll_thread.join(timeout=self.job_timeout * (self.job_retries + 1) + 1.0)
			self._poll_thread = None
		self._poll_stop_event = None
		if self.job_manager is not None:
			self.job_manager.stop()
			self.job_manager = None
		if self.serial_worker is not None:
			self.serial_worker.stop()  # type: ignore[union-attr]
			self.serial_worker = None

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.disconnect()

	# --- telemetry logging ------------------------------------------------

	def attach_telemetry_sink(self, sink: TelemetrySink) -> None:
		self._telemetry_sink = sink

	def detach_telemetry_sink(self) -> None:
		self._telemetry_sink = None

	# --- target tracking ----------------------------------------------------

	def add_target(self, target_id: int, name: Optional[str] = None) -> DispenserState:
		with self._targets_lock:
			existing = self._targets.get(target_id)
			if existing is not None:
				return existing
			state = DispenserState(target_id=target_id, name=name or f"driver-{target_id}")
			self._targets[target_id] = state
			return state

	def remove_target(self, target_id: int) -> None:
		with self._targets_lock:
			self._targets.pop(target_id, None)

	def targets(self) -> List[DispenserState]:
		with self._targets_lock:
			return list(self._targets.values())

	def get_state(self, target_id: int) -> Optional[DispenserState]:
		with self._targets_lock:
			return self._targets.get(target_id)

	# --- commands (blocking) -------------------------------------------------

	def set_speed(self, target_id: int, speed: float) -> None:
		job = SetSpeedJob(target_id, speed, timeout=self.job_timeout, retries=self.job_retries)
		self._run_blocking(job)
		state = self.add_target(target_id)
		with self._targets_lock:
			state.last_commanded_speed = speed

	def set_pid(self, target_id: int, kp: float, ki: float, kd: float) -> None:
		job = SetPidJob(target_id, kp, ki, kd, timeout=self.job_timeout, retries=self.job_retries)
		self._run_blocking(job)
		self.add_target(target_id)

	def request_status(self, target_id: int) -> StatusReport:
		job = RequestStatusJob(target_id, timeout=self.job_timeout, retries=self.job_retries)
		report: StatusReport = self._run_blocking(job)
		self._record_status(target_id, report)
		return report

	def request_settings(self, target_id: int) -> SettingsReport:
		job = RequestSettingsJob(target_id, timeout=self.job_timeout, retries=self.job_retries)
		report: SettingsReport = self._run_blocking(job)
		state = self.add_target(target_id)
		with self._targets_lock:
			state.last_settings = report
			state.last_error = None
		if self._telemetry_sink is not None:
			self._telemetry_sink.record_settings(target_id, report)
		return report

	# --- internals -------------------------------------------------------

	def _budget(self) -> float:
		"""Enough wall-clock time for every retry the job might take, plus
		a little slack for scheduling jitter."""
		return self.job_timeout * (self.job_retries + 1) + 0.5

	def _run_blocking(self, job: Any) -> Any:
		if self.job_manager is None:
			raise SessionError("not connected")

		done = threading.Event()
		outcome: Dict[str, Any] = {}

		def _on_success(result: Any) -> None:
			outcome["result"] = result
			done.set()

		def _on_error(error: Exception) -> None:
			outcome["error"] = error
			done.set()

		self.job_manager.submit(job, on_success=_on_success, on_error=_on_error)
		if not done.wait(self._budget()):
			raise SessionError(f"{job!r} did not complete in time")
		if "error" in outcome:
			raise SessionError(str(outcome["error"])) from outcome["error"]
		return outcome.get("result")

	def _record_status(self, target_id: int, report: StatusReport) -> None:
		state = self.add_target(target_id)
		with self._targets_lock:
			state.last_status = report
			state.last_status_at = time.time()
			state.last_error = None
			target_speed = state.last_commanded_speed
		if self._telemetry_sink is not None:
			self._telemetry_sink.record_status(target_id, report, target_speed=target_speed)

	def _poll_loop(self) -> None:
		stop_event = self._poll_stop_event
		assert stop_event is not None
		while not stop_event.is_set():
			for state in self.targets():
				if stop_event.is_set():
					break
				try:
					self.request_status(state.target_id)
				except SessionError as exc:
					with self._targets_lock:
						state.last_error = str(exc)
					logger.warning("status poll failed for target %s: %s", state.target_id, exc)
			# wait() returns as soon as disconnect() sets the event, instead
			# of blocking for the full poll_interval like a plain sleep would.
			stop_event.wait(self.poll_interval)

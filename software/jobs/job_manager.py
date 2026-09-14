import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from comms.link_layer import ResponseFrame
from comms.serial_worker import SerialWorkerError
from .base_job import BaseJob

logger = logging.getLogger(__name__)

OnSuccess = Callable[[Any], None]
OnError = Callable[[Exception], None]


class SerialLike(Protocol):
	rx_queue: "queue.Queue[Any]"
	def send_command(self, target_id: int, cmd: int, payload: bytes = b"") -> None: ...


class JobTimeoutError(Exception):
	"""No response arrived for a job within its timeout, and its retry
	budget (BaseJob.record_attempt) is exhausted."""


class LinkLostError(Exception):
	"""The underlying serial transport reported a fatal I/O error."""


@dataclass
class _PendingJob:
	job: BaseJob
	on_success: Optional[OnSuccess] = None
	on_error: Optional[OnError] = None
	retry_allowed: bool = True


class JobManager:
	def __init__(self, serial_worker: SerialLike, poll_interval: float = 0.01):
		self.serial_worker = serial_worker
		self.poll_interval = poll_interval

		self._pending: "queue.Queue[_PendingJob]" = queue.Queue()
		self._active: Optional[_PendingJob] = None
		self._active_lock = threading.Lock()

		self.is_running = False
		self._thread: Optional[threading.Thread] = None

	def __enter__(self):
		self.start()
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.stop()

	def start(self):
		if self.is_running:
			raise RuntimeError("JobManager is already running")
		self.is_running = True
		self._thread = threading.Thread(target=self._run_loop, daemon=True)
		self._thread.start()

	def stop(self):
		self.is_running = False
		if self._thread is not None:
			self._thread.join(timeout=1.0)
			if self._thread.is_alive():
				logger.warning("Job manager thread did not exit cleanly")
			self._thread = None

	def submit(
		self,
		job: BaseJob,
		on_success: Optional[OnSuccess] = None,
		on_error: Optional[OnError] = None,
	) -> None:
		"""Queue a job for dispatch. Non-blocking: the job is sent, retried
		and matched to its response on the manager's own background thread,
		and the outcome is reported through on_success/on_error from there."""
		self._pending.put(_PendingJob(job, on_success, on_error))

	@property
	def pending_count(self) -> int:
		return self._pending.qsize()

	@property
	def active_job(self) -> Optional[BaseJob]:
		with self._active_lock:
			return self._active.job if self._active is not None else None

	def _run_loop(self):
		while self.is_running:
			try:
				self._drain_responses()
				self._check_active_timeout()
				self._dispatch_next()
			except Exception:
				logger.exception("Unexpected error in job manager loop")
			time.sleep(self.poll_interval)

	def _dispatch_next(self):
		with self._active_lock:
			if self._active is not None:
				return
			try:
				pending = self._pending.get_nowait()
			except queue.Empty:
				return
			self._active = pending
		self._send_active(pending)

	def _send_active(self, pending: _PendingJob):
		job = pending.job
		job.creation_time = time.time()
		try:
			payload = job.build_payload()
			self.serial_worker.send_command(job.target_id, job.command_id, payload)
		except Exception as exc:
			self._finish_active(error=exc)
			return
		pending.retry_allowed = job.record_attempt()

	def _drain_responses(self):
		while True:
			try:
				item = self.serial_worker.rx_queue.get_nowait()
			except queue.Empty:
				return

			if isinstance(item, SerialWorkerError):
				self._fail_all(LinkLostError(str(item)))
				continue

			self._handle_frame(item)

	def _handle_frame(self, frame: ResponseFrame):
		with self._active_lock:
			pending = self._active
		if pending is None or frame.source_id != pending.job.target_id:
			logger.debug("Discarding unmatched response frame: %r", frame)
			return

		try:
			result = pending.job.handle_response(frame)
		except Exception as exc:
			self._finish_active(error=exc)
			return
		self._finish_active(result=result)

	def _check_active_timeout(self):
		with self._active_lock:
			pending = self._active
		if pending is None or not pending.job.is_expired():
			return

		if not pending.retry_allowed:
			self._finish_active(
				error=JobTimeoutError(f"{pending.job!r} timed out with no response")
			)
			return

		logger.debug("%r timed out, retrying", pending.job)
		self._send_active(pending)

	def _finish_active(self, result: Any = None, error: Optional[Exception] = None):
		with self._active_lock:
			pending = self._active
			self._active = None
		if pending is None:
			return
		if error is not None:
			logger.warning("%r failed: %s", pending.job, error)
			if pending.on_error is not None:
				pending.on_error(error)
		else:
			if pending.on_success is not None:
				pending.on_success(result)

	def _fail_all(self, error: Exception):
		"""The serial link itself has died: give up on the active job and
		drain the queue, notifying every submitter rather than leaving them
		waiting forever."""
		self._finish_active(error=error)
		while True:
			try:
				pending = self._pending.get_nowait()
			except queue.Empty:
				break
			if pending.on_error is not None:
				pending.on_error(error)

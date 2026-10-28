"""Runs Session, Board, Runner and file calls off the GUI thread and hands their results back as Qt
signals, so the window never waits on a serial port.

`run` calls go to one worker thread, one at a time, in the order given. `run_now` starts a call at
once on a thread of its own: for STOP, which must never wait behind a slow call such as a connect
(up to 4 s while an Uno boots). Results and errors come back on the thread that owns the Bridge,
i.e. the GUI thread. Use a Bridge from the GUI thread only."""

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

logger = logging.getLogger(__name__)


class BridgeClosedError(RuntimeError):
	pass


@dataclass(eq=False)
class Call:
	name: str
	result: Any = None
	error: BaseException | None = None
	done: bool = False
	on_result: Callable[[Any], None] | None = field(default=None, repr=False)
	on_error: Callable[[BaseException], None] | None = field(default=None, repr=False)
	ordered: bool = field(default=True, repr=False)


class Bridge(QObject):
	finished = Signal(object)
	"""Every Call once it is done, after its on_result / on_error."""
	failed = Signal(object)
	"""A Call that raised and was given no on_error; the window shows these."""
	busy_changed = Signal(bool)
	"""True while `run` calls are queued or running."""

	_delivered = Signal(object)

	def __init__(self, parent: QObject | None = None):
		super().__init__(parent)
		self._queue: queue.Queue = queue.Queue()
		self._pending = 0
		self._closed = False
		self._closing = threading.Event()
		self._now_threads: list[threading.Thread] = []
		self._delivered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
		self._worker = threading.Thread(target=self._work, name="gui-worker", daemon=True)
		self._worker.start()

	@property
	def busy(self) -> bool:
		return self._pending > 0

	def run(self, work: Callable[..., Any], *args, name: str | None = None,
			on_result: Callable[[Any], None] | None = None,
			on_error: Callable[[BaseException], None] | None = None, **kwargs) -> Call:
		call = self._call(work, name, on_result, on_error, ordered=True)
		self._pending += 1
		if self._pending == 1:
			self.busy_changed.emit(True)
		self._queue.put((call, work, args, kwargs))
		return call

	def run_now(self, work: Callable[..., Any], *args, name: str | None = None,
				on_result: Callable[[Any], None] | None = None,
				on_error: Callable[[BaseException], None] | None = None, **kwargs) -> Call:
		call = self._call(work, name, on_result, on_error, ordered=False)
		thread = threading.Thread(target=self._execute, args=(call, work, args, kwargs), name="gui-now", daemon=True)
		self._now_threads = [running for running in self._now_threads if running.is_alive()] + [thread]
		thread.start()
		return call

	def close(self, timeout_s: float) -> bool:
		"""Lets the running calls finish and drops the queued ones; False if they outlast timeout_s."""
		if not self._closed:
			self._closed = True
			self._closing.set()
			self._queue.put(None)
		threads = [self._worker, *self._now_threads]
		deadline = time.monotonic() + timeout_s
		for thread in threads:
			thread.join(max(0.0, deadline - time.monotonic()))
		return not any(thread.is_alive() for thread in threads)

	def _call(self, work, name, on_result, on_error, *, ordered: bool) -> Call:
		if self._closed:
			raise BridgeClosedError("the window is closing; nothing more is sent to the boards")
		return Call(name or getattr(work, "__name__", repr(work)), on_result=on_result, on_error=on_error,
					ordered=ordered)

	def _work(self) -> None:
		while True:
			item = self._queue.get()
			if item is None or self._closing.is_set():
				return
			self._execute(*item)

	def _execute(self, call: Call, work, args, kwargs) -> None:
		try:
			call.result = work(*args, **kwargs)
		except BaseException as error:
			call.error = error
		call.done = True
		self._delivered.emit(call)

	@Slot(object)
	def _deliver(self, call: Call) -> None:
		if call.ordered:
			self._pending -= 1
		try:
			if call.error is None:
				if call.on_result is not None:
					call.on_result(call.result)
			elif call.on_error is not None:
				call.on_error(call.error)
			else:
				self.failed.emit(call)
		finally:
			self.finished.emit(call)
			if call.ordered and self._pending == 0:
				self.busy_changed.emit(False)


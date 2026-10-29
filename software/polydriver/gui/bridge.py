"""Runs Session, Board, Runner and file calls off the GUI thread and hands their results back as Qt
signals, so the window never waits on a serial port.

`run` calls go to one worker thread, one at a time, in the order given. `run_now` starts a call at
once on a thread of its own: for STOP, which must never wait behind a slow call such as a connect
(up to 4 s while an Uno boots). Results and errors come back on the thread that owns the Bridge,
i.e. the GUI thread. Use a Bridge from the GUI thread only.

The threads hold no reference to the Bridge: they hand finished calls to one relay that lives as
long as the application, which finds the Bridge through a weak reference. Otherwise a worker
finishing after the Bridge was dropped would free it on the worker thread, which crashes Qt."""

import queue
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import shiboken6
from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot


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


class _Relay(QObject):
	delivered = Signal(object, object)

	def __init__(self, parent: QObject | None):
		super().__init__(parent)
		self.delivered.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

	@Slot(object, object)
	def _deliver(self, bridge_ref: weakref.ref, call: Call) -> None:
		bridge = bridge_ref()
		if bridge is not None:
			bridge._deliver(call)


_relay: _Relay | None = None


def _the_relay() -> _Relay:
	global _relay
	if _relay is None or not shiboken6.isValid(_relay):
		_relay = _Relay(QCoreApplication.instance())
	return _relay


class Bridge(QObject):
	finished = Signal(object)
	"""Every Call once it is done, after its on_result / on_error."""
	failed = Signal(object)
	"""A Call that raised and was given no on_error; the window shows these."""
	busy_changed = Signal(bool)
	"""True while `run` calls are queued or running."""

	def __init__(self, parent: QObject | None = None):
		super().__init__(parent)
		self._relay = _the_relay()
		self._ref = weakref.ref(self)
		self._queue: queue.Queue = queue.Queue()
		self._pending = 0
		self._closed = False
		self._closing = threading.Event()
		self._now_threads: list[threading.Thread] = []
		self._worker = threading.Thread(target=_work, args=(self._queue, self._closing, self._relay, self._ref),
										name="gui-worker", daemon=True)
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
		thread = threading.Thread(target=_execute, args=(self._relay, self._ref, call, work, args, kwargs),
									name="gui-now", daemon=True)
		self._now_threads = [running for running in self._now_threads if running.is_alive()] + [thread]
		thread.start()
		return call

	def close(self, timeout_s: float) -> bool:
		"""Lets the running calls finish and drops the queued ones; nothing comes back after this (no
		callbacks, no signals). False if the running calls outlast timeout_s."""
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

	def _deliver(self, call: Call) -> None:
		if self._closed:
			return  # the window that asked is closing: its widgets may already be gone
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


def _work(jobs: queue.Queue, closing: threading.Event, relay: _Relay, bridge_ref: weakref.ref) -> None:
	while True:
		item = jobs.get()
		if item is None or closing.is_set():
			return
		_execute(relay, bridge_ref, *item)
		item = None  # an idle worker keeps nothing alive: its call's callbacks reach the pages and the Bridge


def _execute(relay: _Relay, bridge_ref: weakref.ref, call: Call, work, args, kwargs) -> None:
	try:
		call.result = work(*args, **kwargs)
	except BaseException as error:
		call.error = error
	call.done = True
	relay.delivered.emit(bridge_ref, call)

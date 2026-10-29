"""Ending a procedure from another thread so that its last command can never land after a STOP.

The procedure sends each command inside `with cancel.command():`; cancel() takes the same lock,
so once it returns no command of the procedure is in flight and none will follow. Call cancel()
first, then send STOP. cancel() may wait for one request in flight (a retry takes up to ~1 s), so
call it off the GUI thread, as the window's STOP does."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class Cancelled(Exception):
	pass


class Cancel:
	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._event = threading.Event()

	@property
	def is_set(self) -> bool:
		return self._event.is_set()

	def cancel(self) -> None:
		with self._lock:
			self._event.set()

	@contextmanager
	def command(self) -> Iterator[None]:
		with self._lock:
			if self._event.is_set():
				raise Cancelled()
			yield

	def wait(self, timeout_s: float) -> bool:
		"""Sleeps up to timeout_s; True as soon as it is cancelled."""
		return self._event.wait(timeout_s)

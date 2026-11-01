"""The window's running procedures (sweep, step test, calibration run, timed run). One at a time:
a screen asks to begin one and is refused while another runs, so two procedures never drive the
boards at once. The window's STOP cancels them all before it sends STOP (bench.Cancel)."""

import threading

from PySide6.QtCore import QObject, Signal

from ..bench import Cancel


class ProcedureBusyError(RuntimeError):
	pass


class Procedures(QObject):
	changed = Signal(bool)
	"""True when one starts, False when it ends."""

	def __init__(self, parent: QObject | None = None):
		super().__init__(parent)
		self._lock = threading.Lock()
		self._running: tuple[str, Cancel] | None = None

	@property
	def running(self) -> str | None:
		with self._lock:
			return self._running[0] if self._running else None

	def begin(self, name: str) -> Cancel:
		"""On the GUI thread. Raises ProcedureBusyError while another runs."""
		with self._lock:
			if self._running is not None:
				raise ProcedureBusyError(f"{self._running[0]} is running; stop it first")
			cancel = Cancel()
			self._running = (name, cancel)
		self.changed.emit(True)
		return cancel

	def end(self, cancel: Cancel) -> None:
		"""On the GUI thread, when the procedure that got cancel has returned."""
		with self._lock:
			if self._running is None or self._running[1] is not cancel:
				return
			self._running = None
		self.changed.emit(False)

	def cancel_all(self) -> None:
		"""Thread-safe; may wait for one request in flight, so call it off the GUI thread."""
		with self._lock:
			running = self._running
		if running is not None:
			running[1].cancel()

"""The window's Session sink. The Session calls it from each board's poll thread; it only emits a
Qt signal and returns, so a poll is never held up (the safety rules judge every poll). The signals
reach the widgets on the GUI thread."""

from PySide6.QtCore import QObject, Signal

from ..devices import BoardStatus, Event
from ..protocol.constants import Role


class QtSink(QObject):
	status = Signal(object, object, float)
	"""role, BoardStatus, host time [s] on the Session's clock"""
	event = Signal(object)
	"""Event"""

	def record_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		self.status.emit(role, status, host_time_s)

	def record_event(self, event: Event) -> None:
		self.event.emit(event)

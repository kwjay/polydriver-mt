"""The main window: a STOP that is always visible (and on Esc), a sidebar of screens and a page for
each. A screen not built yet names the task that builds it. The window holds no logic and never
calls the boards itself: everything goes through the Bridge."""

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
	QHBoxLayout, QLabel, QListWidget, QMainWindow, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from ..protocol.constants import Role
from .bridge import Bridge, Call
from .messages import error_text

PAGES = (
	("Devices", "C10.2"),
	("Manual", "C10.3"),
	("Materials", "C10.4"),
	("Calibration", "C10.4"),
	("Jobs", "C10.5"),
	("Run", "C10.6"),
	("History", "C10.7"),
	("Settings", "C10.2"),
)
SIDEBAR_WIDTH_PX = 160
STOP_STYLE = ("QPushButton { background: #c62828; color: white; font-weight: bold; font-size: 16px; "
			"padding: 8px 24px; border-radius: 4px; } QPushButton:disabled { background: #e0a0a0; }")


class MainWindow(QMainWindow):
	stopped = Signal()
	"""STOP reached every connected board."""

	def __init__(self, bridge: Bridge, *, title: str, screens: dict[str, QWidget] | None = None,
				stop_all: Callable[[], dict[Role, Exception]] | None = None):
		super().__init__()
		self.bridge = bridge
		self._stop_all = stop_all
		self.setWindowTitle(title)
		self.resize(1200, 800)

		self.stop_button = QPushButton("STOP  (Esc)")
		self.stop_button.setStyleSheet(STOP_STYLE)
		self.stop_button.setEnabled(stop_all is not None)
		self.stop_button.clicked.connect(self.stop_everything)
		self.stop_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
		self.stop_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
		self.stop_shortcut.activated.connect(self.stop_everything)

		screens = screens or {}
		self.sidebar = QListWidget()
		self.sidebar.setObjectName("sidebar")
		self.sidebar.setFixedWidth(SIDEBAR_WIDTH_PX)
		self.pages = QStackedWidget()
		for name, task in PAGES:
			self.sidebar.addItem(name)
			self.pages.addWidget(screens.get(name) or _placeholder(name, task))
		self.sidebar.currentRowChanged.connect(self.pages.setCurrentIndex)
		self.sidebar.setCurrentRow(0)

		top = QHBoxLayout()
		top.addStretch(1)
		top.addWidget(self.stop_button)
		body = QHBoxLayout()
		body.addWidget(self.sidebar)
		body.addWidget(self.pages, 1)
		central = QWidget()
		layout = QVBoxLayout(central)
		layout.setContentsMargins(8, 8, 8, 8)
		layout.addLayout(top)
		layout.addLayout(body, 1)
		self.setCentralWidget(central)

		self.busy_label = QLabel("")
		self.statusBar().addPermanentWidget(self.busy_label)
		bridge.busy_changed.connect(self._show_busy)
		bridge.failed.connect(self._show_failure)

	def page_names(self) -> list[str]:
		return [self.sidebar.item(row).text() for row in range(self.sidebar.count())]

	def current_page(self) -> str:
		return self.page_names()[self.pages.currentIndex()]

	def screen(self, name: str) -> QWidget:
		return self.pages.widget(self._index(name))

	def show_page(self, name: str) -> None:
		self.sidebar.setCurrentRow(self._index(name))

	def stop_everything(self) -> None:
		"""STOP to every connected board, at once: it never waits behind another call."""
		if self._stop_all is None:
			return
		self.statusBar().showMessage("STOP sent…")
		self.bridge.run_now(self._stop_all, name="STOP", on_result=self._show_stopped)

	def _index(self, name: str) -> int:
		names = self.page_names()
		if name not in names:
			raise KeyError(f"no screen called {name}; the screens are {', '.join(names)}")
		return names.index(name)

	def _show_stopped(self, failures: dict[Role, Exception]) -> None:
		if not failures:
			self.statusBar().showMessage("STOP reached every connected board")
		else:
			missed = "; ".join(f"{role.name.lower()}: {error_text('STOP', error, role)}"
								for role, error in failures.items())
			self.statusBar().showMessage(f"STOP did not reach every board ({missed}); cut the motor power if "
										"a screw still turns")
		self.stopped.emit()

	def _show_busy(self, busy: bool) -> None:
		self.busy_label.setText("working…" if busy else "")

	def _show_failure(self, call: Call) -> None:
		self.statusBar().showMessage(f"{call.name}: {error_text(call.name, call.error)}")


def _placeholder(name: str, task: str) -> QLabel:
	label = QLabel(f"{name}: built in {task}")
	label.setAlignment(Qt.AlignmentFlag.AlignCenter)
	return label

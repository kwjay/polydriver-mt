"""The main window: a sidebar of screens and a page for each. A screen not built yet names the task
that builds it. The window holds no logic and never calls the boards itself: everything goes
through the Bridge."""

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget, QWidget

from ..cli.base import describe
from ..startup import LOG_FILE_NAME
from .bridge import Bridge, Call

logger = logging.getLogger(__name__)

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


class MainWindow(QMainWindow):
	def __init__(self, bridge: Bridge, *, title: str):
		super().__init__()
		self.bridge = bridge
		self.setWindowTitle(title)
		self.resize(1100, 700)

		self.sidebar = QListWidget()
		self.sidebar.setObjectName("sidebar")
		self.sidebar.setFixedWidth(SIDEBAR_WIDTH_PX)
		self.pages = QStackedWidget()
		for name, task in PAGES:
			self.sidebar.addItem(name)
			self.pages.addWidget(_placeholder(name, task))
		self.sidebar.currentRowChanged.connect(self.pages.setCurrentIndex)
		self.sidebar.setCurrentRow(0)

		body = QWidget()
		layout = QHBoxLayout(body)
		layout.setContentsMargins(8, 8, 8, 8)
		layout.addWidget(self.sidebar)
		layout.addWidget(self.pages, 1)
		self.setCentralWidget(body)

		self.busy_label = QLabel("")
		self.statusBar().addPermanentWidget(self.busy_label)
		bridge.busy_changed.connect(self._show_busy)
		bridge.failed.connect(self._show_failure)

	def page_names(self) -> list[str]:
		return [self.sidebar.item(row).text() for row in range(self.sidebar.count())]

	def current_page(self) -> str:
		return self.page_names()[self.pages.currentIndex()]

	def show_page(self, name: str) -> None:
		names = self.page_names()
		if name not in names:
			raise KeyError(f"no screen called {name}; the screens are {', '.join(names)}")
		self.sidebar.setCurrentRow(names.index(name))

	def _show_busy(self, busy: bool) -> None:
		self.busy_label.setText("working…" if busy else "")

	def _show_failure(self, call: Call) -> None:
		error = call.error
		text = describe(error)
		if text is None:
			logger.error("%s failed", call.name, exc_info=(type(error), error, error.__traceback__))
			text = f"unexpected error ({type(error).__name__}: {error}); the details are in {LOG_FILE_NAME}"
		self.statusBar().showMessage(f"{call.name}: {text}")


def _placeholder(name: str, task: str) -> QLabel:
	label = QLabel(f"{name}: built in {task}")
	label.setAlignment(Qt.AlignmentFlag.AlignCenter)
	return label

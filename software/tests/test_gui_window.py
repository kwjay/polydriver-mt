import logging
import threading

import pytest
from PySide6.QtCore import Qt

from polydriver.gui.app import PAGES, MainWindow
from polydriver.gui.bridge import Bridge


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def window(qtbot, bridge):
	window = MainWindow(bridge, title="PolydriverMT")
	qtbot.addWidget(window)
	return window


def test_the_sidebar_lists_every_screen_in_order(window):
	assert window.page_names() == [name for name, _ in PAGES]
	assert window.page_names() == [
		"Devices", "Manual", "Materials", "Calibration", "Jobs", "Run", "History", "Settings",
	]


def test_the_window_opens_on_devices(window):
	assert window.current_page() == "Devices"


def test_clicking_a_screen_in_the_sidebar_shows_it(qtbot, window):
	row = window.page_names().index("Jobs")
	item = window.sidebar.item(row)
	qtbot.mouseClick(window.sidebar.viewport(), Qt.MouseButton.LeftButton,
					pos=window.sidebar.visualItemRect(item).center())
	assert window.current_page() == "Jobs"
	assert window.pages.currentIndex() == row


def test_show_page_switches_the_page_and_the_sidebar(window):
	window.show_page("History")
	assert window.current_page() == "History"
	assert window.sidebar.currentRow() == window.page_names().index("History")


def test_show_page_refuses_an_unknown_screen(window):
	with pytest.raises(KeyError, match="no screen called Recipes"):
		window.show_page("Recipes")


def test_a_screen_not_built_yet_names_the_task_that_builds_it(window):
	window.show_page("Run")
	assert "C10.6" in window.pages.currentWidget().text()


def test_the_title_is_the_one_given(qtbot, bridge):
	window = MainWindow(bridge, title="PolydriverMT — simulated boards")
	qtbot.addWidget(window)
	assert window.windowTitle() == "PolydriverMT — simulated boards"


def test_the_status_bar_shows_when_the_boards_are_being_talked_to(qtbot, window, bridge):
	assert window.busy_label.text() == ""
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	assert window.busy_label.text() == "working…"
	release.set()
	qtbot.waitUntil(lambda: window.busy_label.text() == "", timeout=2000)


def test_a_known_error_shows_in_the_status_bar_with_what_failed(qtbot, window, bridge):
	def connect():
		raise OSError("COM7 is held by another program")

	with qtbot.waitSignal(bridge.finished, timeout=2000):
		bridge.run(connect, name="connect polymer")
	assert window.statusBar().currentMessage() == "connect polymer: COM7 is held by another program"


def test_a_bug_shows_as_unexpected_and_goes_to_the_program_log(qtbot, window, bridge, caplog):
	def connect():
		raise ZeroDivisionError("division by zero")

	with caplog.at_level(logging.ERROR, logger="polydriver.gui.app"):
		with qtbot.waitSignal(bridge.finished, timeout=2000):
			bridge.run(connect, name="connect polymer")
	message = window.statusBar().currentMessage()
	assert message.startswith("connect polymer: unexpected error (ZeroDivisionError: division by zero)")
	assert "polydriver.log" in message
	assert caplog.records[-1].exc_info[0] is ZeroDivisionError


def test_the_window_survives_switching_pages_while_a_call_runs(qtbot, window, bridge):
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	for name in window.page_names():
		window.show_page(name)
		qtbot.wait(5)
	assert window.current_page() == "Settings"
	release.set()

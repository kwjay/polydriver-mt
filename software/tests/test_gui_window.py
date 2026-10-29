import logging
import threading

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QLabel

from polydriver.gui.app import PAGES, MainWindow
from polydriver.gui.bridge import Bridge
from polydriver.protocol.constants import Role


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

	with caplog.at_level(logging.ERROR, logger="polydriver.gui.messages"):
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


def test_a_screen_given_replaces_its_placeholder(qtbot, bridge):
	devices = QLabel("the devices screen")
	window = MainWindow(bridge, title="PolydriverMT", screens={"Devices": devices})
	qtbot.addWidget(window)
	assert window.screen("Devices") is devices
	assert "C10.6" in window.screen("Run").text()


def test_screen_refuses_an_unknown_name(window):
	with pytest.raises(KeyError, match="no screen called Recipes"):
		window.screen("Recipes")


def make_stop_window(qtbot, bridge, stop_all):
	window = MainWindow(bridge, title="PolydriverMT", stop_all=stop_all)
	qtbot.addWidget(window)
	return window


def test_stop_sends_stop_to_every_board_and_says_so(qtbot, bridge):
	calls = []
	window = make_stop_window(qtbot, bridge, lambda: calls.append(threading.current_thread()) or {})
	with qtbot.waitSignal(window.stopped, timeout=2000):
		qtbot.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
	assert len(calls) == 1 and calls[0] is not threading.main_thread()
	assert window.statusBar().currentMessage() == "STOP reached every connected board"


def test_escape_is_stop(qtbot, bridge):
	calls = []
	window = make_stop_window(qtbot, bridge, lambda: calls.append(True) or {})
	assert window.stop_shortcut.key() == QKeySequence(Qt.Key.Key_Escape)
	assert window.stop_shortcut.context() == Qt.ShortcutContext.ApplicationShortcut
	with qtbot.waitSignal(window.stopped, timeout=2000):
		window.stop_shortcut.activated.emit()
	assert calls == [True]


def test_stop_does_not_wait_behind_a_slow_call(qtbot, bridge):
	calls = []
	window = make_stop_window(qtbot, bridge, lambda: calls.append(True) or {})
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	with qtbot.waitSignal(window.stopped, timeout=1000):
		window.stop_everything()
	assert calls == [True]
	release.set()


def test_a_board_stop_did_not_reach_is_named(qtbot, bridge):
	window = make_stop_window(qtbot, bridge, lambda: {Role.COLOUR: OSError("COM5 lost")})
	with qtbot.waitSignal(window.stopped, timeout=2000):
		window.stop_everything()
	message = window.statusBar().currentMessage()
	assert message.startswith("STOP did not reach every board (colour: COM5 lost)")
	assert "cut the motor power" in message


def test_without_boards_the_stop_button_is_off(window):
	assert not window.stop_button.isEnabled()
	window.stop_everything()

import math

import pytest
from PySide6.QtCore import Qt

from gui_fakes import FakeSession, make_status
from polydriver.config import BoardSettings, Config
from polydriver.gui.bridge import Bridge
from polydriver.gui.manual_page import WINDOW_S, ManualPage
from polydriver.gui.telemetry import QtSink
from polydriver.telemetry import RunLogError
from polydriver.protocol.constants import Command, Mode, NackReason, Role
from polydriver.requests import RequestNackError


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def session():
	session = FakeSession()
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	session.calls.clear()
	return session


@pytest.fixture
def sink():
	return QtSink()


CONFIG = Config(boards={
	Role.POLYMER: BoardSettings(kp=0.5, ki=2.0, kd=0.1, alpha=0.4, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.5),
	Role.COLOUR: BoardSettings(alpha=0.8),
})


class FakeLog:
	def __init__(self, session, note):
		self.csv_path = "logs/run-test.csv"
		self.note = note
		self.closed = False
		session.add_sink(self)

	def close(self, result=None):
		self.closed = True


def fake_start_log(session, config, *, note=""):
	if not session.roles():
		raise RunLogError("no board connected, so there is nothing to log")
	return FakeLog(session, note)


@pytest.fixture
def page(qtbot, session, bridge, sink, tmp_path):
	page = ManualPage(session, CONFIG, bridge, sink, start_log=fake_start_log)
	qtbot.addWidget(page)
	return page


def click(qtbot, button):
	qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


def wait_idle(qtbot, bridge):
	qtbot.waitUntil(lambda: not bridge.busy, timeout=2000)


def board(session, role=Role.POLYMER):
	return session.boards[role]


# --- Settings -------------------------------------------------------------------------------------

def test_it_starts_on_the_polymer_board_with_the_config_settings(page):
	assert page.role is Role.POLYMER
	assert page.form_values() == BoardSettings(kp=0.5, ki=2.0, kd=0.1, alpha=0.4, ff_offset_duty=128.0,
												ff_gain_duty_per_hz=1.5)


def test_a_board_without_gains_in_the_config_starts_at_zero_gains(page):
	page.select_role(Role.COLOUR)
	assert page.form_values() == BoardSettings(kp=0.0, ki=0.0, kd=0.0, alpha=0.8)


def test_send_puts_the_form_on_the_board_and_shows_what_it_holds(qtbot, page, session, bridge):
	page.kp.setValue(1.25)
	page.ki.setValue(3.0)
	click(qtbot, page.send_button)
	wait_idle(qtbot, bridge)
	sent = session.calls[-1]
	assert sent[:2] == ("apply_settings", Role.POLYMER)
	assert sent[2] == BoardSettings(kp=1.25, ki=3.0, kd=0.1, alpha=0.4, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.5)
	assert page.board_label.text().startswith("on the board: pid 1.25 3 0.1, alpha 0.4, ff 128 + 1.5/Hz")


def test_read_fills_the_form_from_the_board(qtbot, page, session, bridge):
	board(session).kp, board(session).alpha = 0.9, 0.25
	click(qtbot, page.read_button)
	wait_idle(qtbot, bridge)
	assert page.kp.value() == 0.9
	assert page.alpha.value() == 0.25


def test_the_form_cannot_hold_values_the_board_refuses(page):
	page.alpha.setValue(0.0)
	page.kp.setValue(-1.0)
	page.ff_offset.setValue(300.0)
	values = page.form_values()
	assert values.alpha > 0 and values.kp == 0.0 and values.ff_offset_duty == 255.0


def test_a_board_not_connected_is_reported_on_the_page(qtbot, page, session, bridge):
	del session.boards[Role.POLYMER]
	click(qtbot, page.send_button)
	wait_idle(qtbot, bridge)
	assert page.message_label.text() == "the polymer board is not connected (`connect polymer`)"


# --- Driving --------------------------------------------------------------------------------------

def test_set_speed_sends_the_speed_and_takes_the_target_from_the_board(qtbot, page, session, bridge):
	page.speed.setValue(12.5)
	click(qtbot, page.speed_button)
	wait_idle(qtbot, bridge)
	assert board(session).calls == [("set_speed", 12.5)]
	assert page.target_hz(Role.POLYMER) == 12.5


def test_open_and_closed_loop_switch_the_mode(qtbot, page, session, bridge):
	click(qtbot, page.open_loop_button)
	wait_idle(qtbot, bridge)
	click(qtbot, page.closed_loop_button)
	wait_idle(qtbot, bridge)
	assert board(session).calls == [("set_mode", Mode.OPEN_LOOP), ("set_mode", Mode.CLOSED_LOOP)]


def test_set_duty_sends_the_duty_and_there_is_no_target_speed_in_open_loop(qtbot, page, session, bridge):
	click(qtbot, page.open_loop_button)
	page.duty.setValue(180)
	click(qtbot, page.duty_button)
	wait_idle(qtbot, bridge)
	assert board(session).calls[-1] == ("set_pwm", 180)
	assert math.isnan(page.target_hz(Role.POLYMER))
	assert page.mode_label.text() == "open loop, duty 180"


def test_stop_stops_this_board_only(qtbot, page, session, bridge):
	page.speed.setValue(10.0)
	click(qtbot, page.speed_button)
	wait_idle(qtbot, bridge)
	click(qtbot, page.stop_button)
	qtbot.waitUntil(lambda: page.target_hz(Role.POLYMER) == 0.0, timeout=2000)
	assert board(session).calls[-1] == ("stop",)
	assert board(session, Role.COLOUR).calls == []


def test_stop_does_not_wait_behind_a_slow_call(qtbot, page, session, bridge):
	import threading
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	click(qtbot, page.stop_button)
	qtbot.waitUntil(lambda: ("stop",) in board(session).calls, timeout=1000)
	release.set()


def test_commands_go_to_the_chosen_board(qtbot, page, session, bridge):
	page.select_role(Role.COLOUR)
	page.speed.setValue(3.0)
	click(qtbot, page.speed_button)
	wait_idle(qtbot, bridge)
	assert board(session, Role.COLOUR).calls == [("set_speed", 3.0)]
	assert board(session).calls == []


def test_a_refused_command_says_why(qtbot, page, session, bridge):
	board(session).refuse["set_speed"] = RequestNackError(Command.SET_SPEED, NackReason.NOT_CONFIGURED)
	page.speed.setValue(5.0)
	click(qtbot, page.speed_button)
	wait_idle(qtbot, bridge)
	assert page.message_label.text().startswith("refused: SET_SPEED refused: NOT_CONFIGURED")
	assert page.target_hz(Role.POLYMER) == 0.0


def test_after_a_stop_of_every_board_the_targets_are_zero(qtbot, page, session, bridge):
	page.speed.setValue(10.0)
	click(qtbot, page.speed_button)
	wait_idle(qtbot, bridge)
	page.on_stopped()
	assert page.target_hz(Role.POLYMER) == 0.0
	assert page.target_hz(Role.COLOUR) == 0.0


# --- Live plot ------------------------------------------------------------------------------------

def test_each_status_adds_a_point_for_its_board(qtbot, page, sink):
	sink.record_status(Role.POLYMER, make_status(raw_hz=10.0, filtered_hz=9.0, duty=150), 100.0)
	sink.record_status(Role.POLYMER, make_status(raw_hz=11.0, filtered_hz=10.0, duty=155), 100.1)
	sink.record_status(Role.COLOUR, make_status(raw_hz=3.0, filtered_hz=2.5, duty=130), 100.1)
	qtbot.waitUntil(lambda: len(page.traces[Role.COLOUR].times()) == 1, timeout=1000)
	polymer = page.traces[Role.POLYMER]
	assert polymer.times() == [100.0, 100.1]
	assert polymer.values("raw") == [10.0, 11.0]
	assert polymer.values("filtered") == [9.0, 10.0]
	assert polymer.values("duty") == [150, 155]
	assert polymer.values("target") == [0.0, 0.0]


def test_the_target_line_follows_the_commanded_speed(qtbot, page, sink, bridge):
	sink.record_status(Role.POLYMER, make_status(), 1.0)
	page.speed.setValue(8.0)
	click(qtbot, page.speed_button)
	wait_idle(qtbot, bridge)
	sink.record_status(Role.POLYMER, make_status(), 1.1)
	qtbot.waitUntil(lambda: len(page.traces[Role.POLYMER].times()) == 2, timeout=1000)
	assert page.traces[Role.POLYMER].values("target") == [0.0, 8.0]


def test_the_plot_keeps_the_last_window_only(qtbot, page, sink):
	for n in range(5):
		sink.record_status(Role.POLYMER, make_status(), 100.0 + n * WINDOW_S / 2)
	qtbot.waitUntil(lambda: (page.traces[Role.POLYMER].times() or [0])[-1] == 100.0 + 2 * WINDOW_S, timeout=1000)
	assert page.traces[Role.POLYMER].times() == [100.0 + WINDOW_S, 100.0 + 1.5 * WINDOW_S, 100.0 + 2 * WINDOW_S]


def test_the_readout_shows_the_chosen_boards_latest_status(qtbot, page, sink):
	sink.record_status(Role.COLOUR, make_status(raw_hz=3.0, filtered_hz=2.5, duty=130), 1.0)
	sink.record_status(Role.POLYMER, make_status(raw_hz=10.0, filtered_hz=9.75, duty=150), 1.0)
	qtbot.waitUntil(lambda: page.readout_label.text() != "", timeout=1000)
	assert page.readout_label.text() == "raw 10.00 Hz · filtered 9.75 Hz · duty 150 · REGULATING"
	page.select_role(Role.COLOUR)
	assert page.readout_label.text() == "raw 3.00 Hz · filtered 2.50 Hz · duty 130 · REGULATING"


def test_the_curves_show_the_chosen_board(qtbot, page, sink):
	sink.record_status(Role.POLYMER, make_status(raw_hz=10.0), 1.0)
	sink.record_status(Role.COLOUR, make_status(raw_hz=3.0), 1.0)
	qtbot.waitUntil(lambda: len(page.traces[Role.COLOUR].times()) == 1, timeout=1000)
	_, raw = page.curves["raw"].getData()
	assert list(raw) == [10.0]
	page.select_role(Role.COLOUR)
	_, raw = page.curves["raw"].getData()
	assert list(raw) == [3.0]


# --- Recording ------------------------------------------------------------------------------------

def test_record_starts_a_run_log_of_the_connected_boards_and_stop_closes_it(qtbot, page, session, bridge):
	click(qtbot, page.record_button)
	qtbot.waitUntil(lambda: page.log is not None, timeout=2000)
	log = page.log
	assert log.note == "PID test"
	assert log in session.sinks
	assert page.record_label.text().startswith("recording to ")
	assert page.record_button.text() == "Stop recording"
	click(qtbot, page.record_button)
	qtbot.waitUntil(lambda: page.record_label.text().startswith("saved "), timeout=2000)
	assert page.log is None
	assert log.closed
	assert page.record_button.text() == "Record CSV"
	assert page.record_label.text().startswith("saved ")


def test_shutdown_closes_a_log_still_open(qtbot, page, bridge):
	click(qtbot, page.record_button)
	qtbot.waitUntil(lambda: page.log is not None, timeout=2000)
	log = page.log
	page.shutdown()
	assert log.closed
	assert page.log is None


def test_shutdown_without_a_log_does_nothing(page):
	page.shutdown()
	assert page.log is None


# --- Switching boards while a call is under way -----------------------------------------------------

def hold_the_worker(bridge):
	import threading
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	return release


def test_a_reply_for_the_other_board_does_not_overwrite_this_boards_view(qtbot, page, session, bridge):
	release = hold_the_worker(bridge)
	click(qtbot, page.read_button)
	page.select_role(Role.COLOUR)
	release.set()
	wait_idle(qtbot, bridge)
	assert page.board_label.text() == ""
	assert page.mode_label.text() == ""
	assert page.form_values() == BoardSettings(kp=0.0, ki=0.0, kd=0.0, alpha=0.8)


def test_an_error_for_the_other_board_is_not_shown_on_this_one(qtbot, page, session, bridge):
	del session.boards[Role.POLYMER]
	release = hold_the_worker(bridge)
	click(qtbot, page.speed_button)
	page.select_role(Role.COLOUR)
	release.set()
	wait_idle(qtbot, bridge)
	assert page.message_label.text() == ""


def test_a_status_from_the_other_board_leaves_the_readout_alone(qtbot, page, sink):
	sink.record_status(Role.POLYMER, make_status(raw_hz=10.0, filtered_hz=9.75, duty=150), 1.0)
	sink.record_status(Role.COLOUR, make_status(raw_hz=3.0, filtered_hz=2.5, duty=130), 1.0)
	qtbot.waitUntil(lambda: len(page.traces[Role.COLOUR].times()) == 1, timeout=1000)
	assert page.readout_label.text().startswith("raw 10.00 Hz")


def test_a_recording_that_cannot_start_says_why(qtbot, page, session, bridge):
	session.boards.clear()
	click(qtbot, page.record_button)
	qtbot.waitUntil(lambda: page.record_button.isEnabled(), timeout=2000)
	assert "no board connected" in page.record_label.text()
	assert page.record_button.text() == "Record CSV"
	assert page.log is None

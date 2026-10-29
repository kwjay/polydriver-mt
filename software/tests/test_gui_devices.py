import threading

import pytest
from PySide6.QtCore import Qt

from gui_fakes import FakeSession, make_event, make_status
from polydriver.config import Config
from polydriver.devices import EventKind
from polydriver.gui.bridge import Bridge
from polydriver.gui.devices_page import LIGHTS, DevicesPage
from polydriver.gui.telemetry import QtSink
from polydriver.protocol.constants import JobState, Role, StatusFlag


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def session():
	return FakeSession()


@pytest.fixture
def sink():
	return QtSink()


def make_page(qtbot, session, bridge, sink, *, ports=("COM3", "COM6"), config=None):
	config = config or Config(ports={Role.POLYMER: "COM6", Role.COLOUR: "COM5"})
	page = DevicesPage(session, config, bridge, sink, list_ports=lambda: [(port, f"USB serial {port}") for port in ports])
	qtbot.addWidget(page)
	return page


@pytest.fixture
def page(qtbot, session, bridge, sink):
	return make_page(qtbot, session, bridge, sink)


def click(qtbot, button):
	qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


def connect(qtbot, page, role):
	click(qtbot, page.panels[role].connect_button)
	qtbot.waitUntil(lambda: page.panels[role].connected, timeout=2000)


# --- The sink -------------------------------------------------------------------------------------

def test_the_sink_hands_a_status_from_a_poll_thread_to_the_gui_thread(qtbot, sink):
	got = []
	sink.status.connect(lambda role, status, t: got.append((role, status, t, threading.current_thread())))
	status = make_status()
	thread = threading.Thread(target=sink.record_status, args=(Role.COLOUR, status, 12.5))
	thread.start()
	thread.join(1.0)
	assert not thread.is_alive()
	qtbot.waitUntil(lambda: bool(got), timeout=1000)
	assert got == [(Role.COLOUR, status, 12.5, threading.main_thread())]


def test_the_sink_hands_an_event_from_a_poll_thread_to_the_gui_thread(qtbot, sink):
	got = []
	sink.event.connect(lambda event: got.append((event, threading.current_thread())))
	event = make_event()
	thread = threading.Thread(target=sink.record_event, args=(event,))
	thread.start()
	thread.join(1.0)
	qtbot.waitUntil(lambda: bool(got), timeout=1000)
	assert got == [(event, threading.main_thread())]


# --- Ports ----------------------------------------------------------------------------------------

def test_each_board_has_a_panel(page):
	assert list(page.panels) == [Role.POLYMER, Role.COLOUR]
	assert page.panels[Role.POLYMER].title() == "Polymer board"
	assert page.panels[Role.COLOUR].title() == "Colour board"


def test_the_port_lists_hold_the_serial_ports_and_start_on_the_configured_one(page):
	combo = page.panels[Role.POLYMER].port_combo
	assert [combo.itemData(i) for i in range(combo.count())] == ["COM3", "COM6"]
	colour = page.panels[Role.COLOUR].port_combo
	assert [colour.itemData(i) for i in range(colour.count())] == ["COM3", "COM6", "COM5"]
	assert page.panels[Role.POLYMER].port() == "COM6"
	assert page.panels[Role.COLOUR].port() == "COM5"
	assert combo.itemText(0) == "COM3 — USB serial COM3"


def test_a_configured_port_not_found_is_listed_as_such(page):
	combo = page.panels[Role.COLOUR].port_combo
	assert combo.itemText(combo.currentIndex()) == "COM5 — in the config, not found"


def test_without_a_configured_port_the_first_found_one_is_picked(qtbot, session, bridge, sink):
	page = make_page(qtbot, session, bridge, sink, config=Config())
	assert page.panels[Role.POLYMER].port() == "COM3"


def test_refreshing_the_ports_keeps_the_choice(qtbot, session, bridge, sink):
	ports = ["COM3", "COM6"]
	page = DevicesPage(session, Config(), bridge, sink, list_ports=lambda: [(port, "") for port in ports])
	qtbot.addWidget(page)
	page.panels[Role.POLYMER].select_port("COM6")
	ports.append("COM9")
	click(qtbot, page.refresh_button)
	combo = page.panels[Role.POLYMER].port_combo
	assert [combo.itemData(i) for i in range(combo.count())] == ["COM3", "COM6", "COM9"]
	assert page.panels[Role.POLYMER].port() == "COM6"


# --- Connecting -----------------------------------------------------------------------------------

def test_connect_opens_the_board_on_the_chosen_port(qtbot, page, session):
	page.panels[Role.POLYMER].select_port("COM3")
	connect(qtbot, page, Role.POLYMER)
	assert session.calls == [("connect", Role.POLYMER, "COM3")]


def test_a_connected_board_shows_its_port_firmware_and_timeout(qtbot, page):
	connect(qtbot, page, Role.POLYMER)
	panel = page.panels[Role.POLYMER]
	assert panel.info_label.text() == "COM6 · firmware 3.0.0 · protocol 3 · comms timeout 2 s"
	assert not panel.connect_button.isEnabled()
	assert not panel.port_combo.isEnabled()
	assert panel.disconnect_button.isEnabled()


def test_connect_both_connects_each_board(qtbot, page, session):
	click(qtbot, page.connect_both_button)
	qtbot.waitUntil(lambda: all(panel.connected for panel in page.panels.values()), timeout=2000)
	assert sorted(call[1] for call in session.calls) == [Role.POLYMER, Role.COLOUR]


def test_connect_both_skips_a_board_already_connected(qtbot, page, session):
	connect(qtbot, page, Role.POLYMER)
	click(qtbot, page.connect_both_button)
	qtbot.waitUntil(lambda: page.panels[Role.COLOUR].connected, timeout=2000)
	assert [call[1] for call in session.calls] == [Role.POLYMER, Role.COLOUR]


def test_a_connect_in_progress_shows_and_cannot_be_started_twice(qtbot, page, session):
	session.connect_delay_s = 0.3
	panel = page.panels[Role.POLYMER]
	click(qtbot, panel.connect_button)
	assert panel.info_label.text() == "connecting to COM6…"
	assert not panel.connect_button.isEnabled()
	qtbot.waitUntil(lambda: panel.connected, timeout=2000)


def test_a_failed_connect_says_why_and_can_be_tried_again(qtbot, page, session):
	session.refuse[("connect", Role.POLYMER)] = OSError("could not open port COM6")
	panel = page.panels[Role.POLYMER]
	click(qtbot, panel.connect_button)
	qtbot.waitUntil(lambda: panel.connect_button.isEnabled(), timeout=2000)
	assert panel.info_label.text() == "not connected: could not open port COM6"
	assert not panel.connected
	assert panel.port_combo.isEnabled()


def test_disconnect_closes_the_board(qtbot, page, session):
	connect(qtbot, page, Role.POLYMER)
	panel = page.panels[Role.POLYMER]
	click(qtbot, panel.disconnect_button)
	qtbot.waitUntil(lambda: not panel.connected, timeout=2000)
	assert session.calls[-1] == ("disconnect", Role.POLYMER)
	assert panel.info_label.text() == "not connected"
	assert panel.connect_button.isEnabled()
	assert not panel.disconnect_button.isEnabled()


# --- Live status ----------------------------------------------------------------------------------

def test_the_lights_follow_the_status_flags(qtbot, page, sink):
	connect(qtbot, page, Role.POLYMER)
	sink.record_status(Role.POLYMER, make_status(flags=StatusFlag.REGULATING | StatusFlag.ENCODER_STALLED), 1.0)
	panel = page.panels[Role.POLYMER]
	qtbot.waitUntil(lambda: panel.lights[StatusFlag.REGULATING].is_on, timeout=1000)
	assert {flag for flag in LIGHTS if panel.lights[flag].is_on} == {StatusFlag.REGULATING, StatusFlag.ENCODER_STALLED}
	sink.record_status(Role.POLYMER, make_status(flags=StatusFlag(0)), 1.1)
	qtbot.waitUntil(lambda: not panel.lights[StatusFlag.REGULATING].is_on, timeout=1000)
	assert not any(panel.lights[flag].is_on for flag in LIGHTS)


def test_every_status_flag_has_a_light(page):
	assert set(LIGHTS) == set(StatusFlag)
	assert set(page.panels[Role.COLOUR].lights) == set(StatusFlag)


def test_a_status_shows_the_speed_duty_and_job_state(qtbot, page, sink):
	connect(qtbot, page, Role.COLOUR)
	sink.record_status(Role.COLOUR, make_status(raw_hz=12.345, filtered_hz=12.0, duty=150,
												job_state=JobState.READY), 1.0)
	panel = page.panels[Role.COLOUR]
	qtbot.waitUntil(lambda: panel.speed_label.text() != "", timeout=1000)
	assert panel.speed_label.text() == "12.35 Hz (filtered 12.00) · duty 150 · job READY"


def test_a_status_for_one_board_leaves_the_other_alone(qtbot, page, sink):
	sink.record_status(Role.COLOUR, make_status(), 1.0)
	qtbot.waitUntil(lambda: page.panels[Role.COLOUR].speed_label.text() != "", timeout=1000)
	assert page.panels[Role.POLYMER].speed_label.text() == ""


# --- Events ---------------------------------------------------------------------------------------

def test_events_are_listed_with_the_board_and_what_happened(qtbot, page, sink):
	sink.record_event(make_event(EventKind.NO_ROLE_STORED, Role.COLOUR, "COM5 has no stored role"))
	qtbot.waitUntil(lambda: page.events_text() != "", timeout=1000)
	assert page.events_text().endswith("colour: no role stored: COM5 has no stored role")


def test_commands_sent_to_a_board_are_listed_too(qtbot, page, sink):
	sink.record_event(make_event(EventKind.COMMAND, Role.POLYMER, "speed 10 Hz"))
	qtbot.waitUntil(lambda: page.events_text() != "", timeout=1000)
	assert "polymer: command: speed 10 Hz" in page.events_text()


def test_a_lost_link_shows_the_board_as_not_connected(qtbot, page, sink):
	connect(qtbot, page, Role.POLYMER)
	sink.record_event(make_event(EventKind.LINK_LOST, Role.POLYMER, "COM6 lost"))
	panel = page.panels[Role.POLYMER]
	qtbot.waitUntil(lambda: not panel.connected, timeout=1000)
	assert panel.info_label.text() == "not connected: link lost: COM6 lost"
	assert panel.connect_button.isEnabled()


def test_other_events_leave_a_board_connected(qtbot, page, sink):
	connect(qtbot, page, Role.POLYMER)
	sink.record_event(make_event(EventKind.RESTARTED, Role.POLYMER, "COM6 restarted"))
	qtbot.waitUntil(lambda: "restarted" in page.events_text(), timeout=1000)
	assert page.panels[Role.POLYMER].connected


def test_the_event_list_keeps_the_latest_lines(qtbot, page, sink):
	for n in range(page.MAX_EVENT_LINES + 20):
		sink.record_event(make_event(EventKind.COMMAND, Role.POLYMER, f"line {n}"))
	qtbot.waitUntil(lambda: f"line {page.MAX_EVENT_LINES + 19}" in page.events_text(), timeout=2000)
	assert page.events_text().count("\n") == page.MAX_EVENT_LINES - 1
	assert "line 19\n" not in page.events_text()

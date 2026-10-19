import time

import pytest

from polydriver.config import BoardSettings
from polydriver.devices import BoardNotRespondingError, SettingsMismatchError
from polydriver.devices.board import _f32
from polydriver.devices.session import (
	AlreadyConnectedError, EventKind, NoPortError, NotConnectedError, PortInUseError, RoleMismatchError, Session,
)
from polydriver.protocol.constants import Role
from polydriver.protocol.serial_worker import LinkLostError
from polydriver.requests import (
	GetId, RequestSettings, RequestStatus, SetCommsTimeout, SetFeedforward, SetFilter, SetPid, SetSpeed, Stop,
)
from polydriver.requests.request_queue import RequestTimeoutError

from fakes import COLOUR_SETTINGS, Boards, Recorder, make_config, wait_until


@pytest.fixture
def boards():
	return Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR, "COM9": Role.NONE})


@pytest.fixture
def recorder():
	return Recorder()


@pytest.fixture
def session(boards, recorder):
	session = Session(make_config(), link_factory=boards)
	session.add_sink(recorder)
	yield session
	session.close()


# --- Connecting ----------------------------------------------------------------------

def test_connect_applies_the_config_in_order(session, boards):
	state = session.connect(Role.POLYMER)
	link = boards.links["COM6"]
	assert link.sent[:6] == [
		GetId(), SetCommsTimeout(2000), SetPid(0.5, 1.0, 0.0), SetFilter(0.4), SetFeedforward(128.0, 1.5),
		RequestSettings(),
	]
	assert state.port == "COM6"
	assert state.connected
	assert state.version.protocol_version == 3
	assert state.settings.alpha == _f32(0.4)
	assert state.polls == 0


def test_without_gains_in_the_config_the_board_stays_unconfigured(session, boards):
	session.connect(Role.COLOUR)
	assert boards.links["COM5"].count(SetPid) == 0


def test_the_port_comes_from_the_config_unless_given(session, boards):
	boards.roles["COM7"] = Role.POLYMER
	session.connect(Role.POLYMER, port="COM7")
	assert boards.opened == ["COM7"]


def test_no_port_anywhere_is_an_error(boards):
	session = Session(make_config(ports={Role.POLYMER: None, Role.COLOUR: None}), link_factory=boards)
	with pytest.raises(NoPortError, match="polymer"):
		session.connect(Role.POLYMER)
	assert boards.opened == []


def test_one_port_cannot_serve_both_roles(session, boards):
	session.connect(Role.POLYMER)
	with pytest.raises(PortInUseError, match="COM6"):
		session.connect(Role.COLOUR, port="COM6")
	assert boards.opened == ["COM6"]


def test_connect_announces_itself_to_the_sinks(session, recorder):
	session.connect(Role.POLYMER)
	assert recorder.kinds(Role.POLYMER)[:2] == [EventKind.SETTINGS_APPLIED, EventKind.CONNECTED]
	assert "protocol 3" in recorder.events[1].message


def test_only_the_two_dispenser_roles_connect(session):
	with pytest.raises(ValueError):
		session.connect(Role.NONE)


def test_a_role_connects_once(session):
	session.connect(Role.POLYMER)
	with pytest.raises(AlreadyConnectedError):
		session.connect(Role.POLYMER)


def test_swapped_ports_are_refused_and_the_port_closed(boards, recorder):
	session = Session(make_config(ports={Role.POLYMER: "COM5", Role.COLOUR: "COM6"}), link_factory=boards)
	with pytest.raises(RoleMismatchError, match="swapped"):
		session.connect(Role.POLYMER)
	assert boards.links["COM5"].closed
	assert boards.links["COM5"].count(SetPid) == 0
	assert session.roles() == []


def test_a_board_without_a_stored_role_connects_with_a_warning(session, recorder):
	session.connect(Role.POLYMER, port="COM9")
	assert EventKind.NO_ROLE_STORED in recorder.kinds(Role.POLYMER)


def test_settings_that_do_not_stick_fail_the_connect(boards):
	def factory(port):
		link = boards(port)
		link.ignore_filter = True
		return link

	session = Session(make_config(), link_factory=factory)
	with pytest.raises(SettingsMismatchError, match="alpha"):
		session.connect(Role.POLYMER)
	assert boards.links["COM6"].closed
	assert session.roles() == []


def test_a_board_that_does_not_answer_leaves_nothing_behind(boards):
	def factory(port):
		raise BoardNotRespondingError(f"{port}: no answer to GET_VERSION within 4 s")

	session = Session(make_config(), link_factory=factory)
	with pytest.raises(BoardNotRespondingError):
		session.connect(Role.POLYMER)
	assert session.roles() == []
	session.close()


# --- Polling ----------------------------------------------------------------------------

def test_both_boards_are_polled_and_reported_by_role(session, recorder):
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= 5 and recorder.status_count(Role.COLOUR) >= 5)
	state = session.state(Role.COLOUR)
	assert state.last_status.report.raw_speed_hz == 12.0
	assert state.polls >= 5
	times = [t for r, _, t in recorder.statuses if r is Role.POLYMER]
	assert times == sorted(times)


def test_the_poll_rate_follows_the_config(boards, recorder):
	session = Session(make_config(poll_hz=20.0), link_factory=boards)
	session.add_sink(recorder)
	session.connect(Role.POLYMER)
	time.sleep(0.5)
	count = recorder.status_count(Role.POLYMER)
	session.close()
	assert 7 <= count <= 13


def test_a_slow_poll_is_not_made_up_with_a_burst(boards, recorder):
	session = Session(make_config(poll_hz=20.0), link_factory=boards)
	session.add_sink(recorder)
	session.connect(Role.POLYMER)
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= 1)
	count = recorder.status_count(Role.POLYMER)
	boards.links["COM6"].status_delay_s = 0.4
	time.sleep(0.55)
	after = recorder.status_count(Role.POLYMER)
	session.close()
	assert after - count <= 6


def test_a_lost_link_stops_only_that_board(session, boards, recorder):
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost: device unplugged")
	assert wait_until(lambda: EventKind.LINK_LOST in recorder.kinds(Role.COLOUR))
	assert not session.state(Role.COLOUR).connected
	assert "unplugged" in session.state(Role.COLOUR).last_error
	before = recorder.status_count(Role.POLYMER)
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= before + 5)
	assert session.roles() == [Role.POLYMER]


def test_missed_polls_are_counted_and_polling_goes_on(session, boards, recorder):
	session.connect(Role.POLYMER)
	boards.links["COM6"].status_timeouts = 3
	assert wait_until(lambda: EventKind.POLL_RECOVERED in recorder.kinds(Role.POLYMER))
	state = session.state(Role.POLYMER)
	assert state.poll_failures == 3
	assert state.connected
	assert recorder.kinds(Role.POLYMER).count(EventKind.POLL_FAILED) == 1


def test_a_restart_is_announced_and_the_config_sent_again(session, boards, recorder):
	session.connect(Role.POLYMER)
	link = boards.links["COM6"]
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= 2)
	link.restart()
	assert wait_until(lambda: recorder.kinds(Role.POLYMER).count(EventKind.SETTINGS_APPLIED) == 2)
	assert EventKind.RESTARTED in recorder.kinds(Role.POLYMER)
	assert link.count(SetPid) == 2
	assert link.configured
	assert link.alpha == _f32(0.4)


def test_settings_that_fail_after_a_restart_are_tried_again(session, boards, recorder):
	session.connect(Role.POLYMER)
	link = boards.links["COM6"]
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= 2)
	link.ignore_filter = True
	link.restart()
	assert wait_until(lambda: link.count(SetPid) >= 4)
	assert recorder.kinds(Role.POLYMER).count(EventKind.SETTINGS_FAILED) == 1
	assert "alpha" in session.state(Role.POLYMER).last_error
	link.ignore_filter = False
	assert wait_until(lambda: recorder.kinds(Role.POLYMER).count(EventKind.SETTINGS_APPLIED) == 2)
	assert link.alpha == _f32(0.4)
	count = link.count(SetPid)
	time.sleep(0.1)
	assert link.count(SetPid) == count


def test_a_failing_sink_does_not_stop_polling(session, recorder):
	class Broken:
		def record_status(self, role, status, host_time_s):
			raise RuntimeError("sink bug")

		def record_event(self, event):
			raise RuntimeError("sink bug")

	session.add_sink(Broken())
	session.connect(Role.POLYMER)
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= 5)


def test_a_removed_sink_hears_nothing_more(session, recorder):
	session.connect(Role.POLYMER)
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= 1)
	session.remove_sink(recorder)
	count = recorder.status_count(Role.POLYMER)
	time.sleep(0.1)
	assert recorder.status_count(Role.POLYMER) == count


def test_poll_now_polls_through_the_sinks(session, recorder):
	session.connect(Role.POLYMER)
	before = recorder.status_count(Role.POLYMER)
	status = session.poll_now(Role.POLYMER)
	assert status.report.raw_speed_hz == 12.0
	assert recorder.status_count(Role.POLYMER) > before


def test_poll_now_raises_what_made_the_poll_fail(session, boards):
	session.connect(Role.POLYMER)
	boards.links["COM6"].status_timeouts = 1
	with pytest.raises(RequestTimeoutError):
		session.poll_now(Role.POLYMER)
	session.poll_now(Role.POLYMER)


def test_poll_now_reports_a_lost_link(session, boards):
	session.connect(Role.POLYMER)
	boards.links["COM6"].fail_with = LinkLostError("COM6 lost")
	with pytest.raises(LinkLostError):
		session.poll_now(Role.POLYMER)
	assert not session.state(Role.POLYMER).connected


def test_paused_polling_waits_for_the_poll_in_flight_and_sends_none(session, boards):
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	polymer, colour = boards.links["COM6"], boards.links["COM5"]
	polymer.status_delay_s = 0.2
	assert wait_until(lambda: polymer.status_in_flight)
	with session.paused_polling():
		assert not polymer.status_in_flight
		counts = polymer.count(RequestStatus), colour.count(RequestStatus)
		time.sleep(0.1)
		assert (polymer.count(RequestStatus), colour.count(RequestStatus)) == counts
	assert wait_until(lambda: polymer.count(RequestStatus) > counts[0] and colour.count(RequestStatus) > counts[1])


# --- Using and leaving ----------------------------------------------------------------

def test_board_gives_the_typed_calls(session, boards):
	session.connect(Role.POLYMER)
	session.board(Role.POLYMER).set_speed(20.0)
	assert SetSpeed(20.0) in boards.links["COM6"].sent


def test_board_of_an_unconnected_role_is_an_error(session):
	with pytest.raises(NotConnectedError, match="colour"):
		session.board(Role.COLOUR)
	with pytest.raises(NotConnectedError):
		session.state(Role.COLOUR)


def test_reload_config_applies_the_new_settings(session, boards):
	session.connect(Role.POLYMER)
	session.reload_config(make_config(boards={Role.POLYMER: BoardSettings(kp=2.0, ki=0.0, kd=0.0, alpha=0.9),
											Role.COLOUR: COLOUR_SETTINGS}))
	link = boards.links["COM6"]
	assert link.gains == (2.0, 0.0, 0.0)
	assert link.alpha == _f32(0.9)


def test_a_reload_during_connect_reaches_the_new_board(boards):
	new = make_config(boards={Role.POLYMER: BoardSettings(kp=2.0, ki=0.0, kd=0.0), Role.COLOUR: COLOUR_SETTINGS})

	def factory(port):
		link = boards(port)
		original = link.call

		def call(request, timeout_s=None):
			if isinstance(request, SetFilter) and session.config is not new:
				session.reload_config(new)
			return original(request, timeout_s)

		link.call = call
		return link

	session = Session(make_config(), link_factory=factory)
	session.connect(Role.POLYMER)
	assert wait_until(lambda: boards.links["COM6"].gains == (2.0, 0.0, 0.0))
	session.close()


def test_stop_all_reaches_every_board_even_if_one_fails(session, boards):
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	boards.links["COM6"].fail_with = LinkLostError("COM6 lost")
	failures = session.stop_all()
	assert boards.links["COM5"].count(Stop) == 1
	assert set(failures) == {Role.POLYMER}


def test_disconnect_closes_one_board_and_keeps_the_other(session, boards, recorder):
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	session.disconnect(Role.COLOUR)
	assert boards.links["COM5"].closed
	assert boards.links["COM5"].count(Stop) == 1
	assert session.roles() == [Role.POLYMER]
	before = recorder.status_count(Role.POLYMER)
	assert wait_until(lambda: recorder.status_count(Role.POLYMER) >= before + 3)


def test_close_stops_the_motors_and_closes_every_link(boards, recorder):
	session = Session(make_config(), link_factory=boards)
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	session.close()
	for port in ("COM6", "COM5"):
		assert boards.links[port].count(Stop) == 1
		assert boards.links[port].closed
	assert session.roles() == []
	session.close()


def test_close_still_closes_a_board_whose_stop_fails(boards):
	session = Session(make_config(), link_factory=boards)
	session.connect(Role.POLYMER)
	boards.links["COM6"].fail_with = RequestTimeoutError("STOP got no reply")
	session.close()
	assert boards.links["COM6"].closed


def test_the_session_is_a_context_manager(boards):
	with Session(make_config(), link_factory=boards) as session:
		session.connect(Role.POLYMER)
	assert boards.links["COM6"].closed


def test_a_reconnect_after_a_lost_link_works(session, boards, recorder):
	session.connect(Role.COLOUR)
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost")
	assert wait_until(lambda: not session.state(Role.COLOUR).connected)
	session.connect(Role.COLOUR)
	assert session.state(Role.COLOUR).connected
	assert boards.opened == ["COM5", "COM5"]

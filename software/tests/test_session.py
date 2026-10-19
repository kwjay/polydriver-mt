import threading
import time

import pytest

from polydriver.config import BoardSettings, Config, LoggingSettings
from polydriver.devices import BoardNotRespondingError, SettingsMismatchError
from polydriver.devices.board import BoardStatus, _f32
from polydriver.devices.session import (
	AlreadyConnectedError, EventKind, NoPortError, NotConnectedError, PortInUseError, RoleMismatchError, Session,
)
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Role, StatusFlag
from polydriver.protocol.serial_worker import LinkLostError
from polydriver.requests import (
	GetId, RequestNackError, RequestSettings, RequestStatus, SetCommsTimeout, SetFeedforward, SetFilter, SetId,
	SetPid, SetSpeed, SettingsReport, StatusReport, Stop, VersionReport,
)
from polydriver.requests.request_queue import RequestTimeoutError

POLYMER_SETTINGS = BoardSettings(kp=0.5, ki=1.0, kd=0.0, alpha=0.4, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.5)
COLOUR_SETTINGS = BoardSettings(alpha=0.5)


def make_config(poll_hz: float = 50.0, **changes) -> Config:
	values = dict(
		ports={Role.POLYMER: "COM6", Role.COLOUR: "COM5"},
		comms_timeout_s=2.0,
		boards={Role.POLYMER: POLYMER_SETTINGS, Role.COLOUR: COLOUR_SETTINGS},
		logging=LoggingSettings(poll_hz=poll_hz),
	)
	values.update(changes)
	return Config(**values)


class FakeLink:
	"""A board behind a link, answering the requests the Session sends like the firmware does."""

	def __init__(self, port: str, stored_role: Role):
		self.port = port
		self.version = VersionReport(3, 3, 0, 0, 32)
		self.stored_role = stored_role
		self.sent = []
		self.closed = False
		self.lock = threading.Lock()
		self.device_time_us = 1_000_000
		self.configured = False
		self.gains = (0.0, 0.0, 0.0)
		self.alpha = 1.0
		self.feedforward = (0.0, 0.0)
		self.timeout_ms = 0
		self.fail_with: Exception | None = None
		self.status_timeouts = 0
		self.ignore_filter = False
		self.status_delay_s = 0.0

	def call(self, request, timeout_s=None):
		with self.lock:
			if self.closed:
				raise LinkLostError(f"{self.port} is closed")
			self.sent.append(request)
			if self.fail_with is not None:
				raise self.fail_with
			match request:
				case GetId():
					return self.stored_role
				case SetId(role=role):
					self.stored_role = Role(role)
				case SetCommsTimeout(timeout_ms=timeout_ms):
					self.timeout_ms = timeout_ms
				case SetPid(kp=kp, ki=ki, kd=kd):
					self.gains, self.configured = (_f32(kp), _f32(ki), _f32(kd)), True
				case SetFilter(alpha=alpha) if not self.ignore_filter:
					self.alpha = _f32(alpha)
				case SetFeedforward(offset_duty=offset, gain_duty_per_hz=gain):
					self.feedforward = (_f32(offset), _f32(gain))
				case SetSpeed(target_speed_hz=target) if target > 0 and not self.configured:
					raise RequestNackError(Command.SET_SPEED, NackReason.NOT_CONFIGURED)
				case RequestSettings():
					return SettingsReport(*self.gains, 0.0, self.timeout_ms, Mode.CLOSED_LOOP, 0, self.alpha, *self.feedforward)
				case RequestStatus():
					if self.status_delay_s:
						time.sleep(self.status_delay_s)
						self.status_delay_s = 0.0
					if self.status_timeouts > 0:
						self.status_timeouts -= 1
						raise RequestTimeoutError("REQ_STAT got no reply after 4 attempts")
					self.device_time_us += 20_000
					flags = StatusFlag(0) if self.configured else StatusFlag.UNCONFIGURED
					return StatusReport(12.0, 12.0, 100, flags, self.device_time_us, 0, 0, 10004, 0, 0, JobState.EMPTY,
										None, 0)
			return None

	def restart(self) -> None:
		with self.lock:
			self.device_time_us = 5_000
			self.configured = False
			self.gains, self.alpha, self.feedforward, self.timeout_ms = (0.0, 0.0, 0.0), 1.0, (0.0, 0.0), 0

	def count(self, request_type) -> int:
		with self.lock:
			return sum(1 for r in self.sent if isinstance(r, request_type))

	def close(self) -> None:
		self.closed = True


class Recorder:
	def __init__(self):
		self.statuses: list[tuple[Role, BoardStatus, float]] = []
		self.events = []
		self.lock = threading.Lock()

	def record_status(self, role, status, host_time_s):
		with self.lock:
			self.statuses.append((role, status, host_time_s))

	def record_event(self, event):
		with self.lock:
			self.events.append(event)

	def kinds(self, role=None):
		with self.lock:
			return [e.kind for e in self.events if role is None or e.role is role]

	def status_count(self, role):
		with self.lock:
			return sum(1 for r, _, _ in self.statuses if r is role)


class Boards:
	"""Link factory: one FakeLink per port, each storing the role given here."""

	def __init__(self, roles: dict[str, Role]):
		self.roles = roles
		self.links: dict[str, FakeLink] = {}
		self.opened: list[str] = []

	def __call__(self, port: str) -> FakeLink:
		self.opened.append(port)
		link = FakeLink(port, self.roles[port])
		self.links[port] = link
		return link


def wait_until(predicate, timeout_s: float = 2.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.005)
	return predicate()


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

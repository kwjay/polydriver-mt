import logging
import time

import pytest

from polydriver.devices import NotConnectedError, Segment, Session
from polydriver.devices.fleet import (
	BoardTiming, Fleet, FleetCommand, FleetRunError, FleetStateError, FleetUploadError,
)
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Role
from polydriver.protocol.serial_worker import LinkLostError
from polydriver.requests import JobClear, JobCommit, JobInfo, JobRun, RequestNackError, RequestStatus, Stop

from fakes import Boards, make_config, wait_until

POLYMER_JOB = [Segment(20.0, 1000), Segment(30.0, 2000, ramp=True)]
COLOUR_JOB = [Segment(100, 3000, duty=True)]
JOBS = {Role.POLYMER: POLYMER_JOB, Role.COLOUR: COLOUR_JOB}


@pytest.fixture
def boards():
	return Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR})


@pytest.fixture
def session(boards):
	session = Session(make_config(), link_factory=boards)
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	session.board(Role.COLOUR).set_mode(Mode.OPEN_LOOP)
	yield session
	session.close()


@pytest.fixture
def fleet(session):
	return Fleet(session)


@pytest.fixture
def polymer(boards):
	return boards.links["COM6"]


@pytest.fixture
def colour(boards):
	return boards.links["COM5"]


def timing(role, first_sent_s, replied_s, sent_s=None, attempts=1, error=None):
	return BoardTiming(role, first_sent_s, first_sent_s if sent_s is None else sent_s, replied_s, attempts, error)


# --- Upload ----------------------------------------------------------------------------

def test_upload_loads_both_boards_until_ready(fleet, polymer, colour):
	infos = fleet.upload(JOBS)
	assert infos[Role.POLYMER].segment_count == 2
	assert infos[Role.POLYMER].total_ms == 3000
	assert infos[Role.COLOUR].job_state is JobState.READY
	assert polymer.job_state is JobState.READY and colour.job_state is JobState.READY
	assert [s.value for s in polymer.segments] == [20.0, 30.0]


def test_upload_to_a_board_that_is_not_connected_sends_nothing(session, fleet, polymer):
	session.disconnect(Role.COLOUR)
	with pytest.raises(NotConnectedError, match="colour"):
		fleet.upload(JOBS)
	assert polymer.count(JobClear) == 0


def test_a_failed_upload_names_the_board_and_clears_the_other(fleet, polymer, colour):
	colour.refuse[JobCommit] = RequestNackError(Command.JOB_COMMIT, NackReason.BAD_JOB)
	with pytest.raises(FleetUploadError, match="colour.*BAD_JOB") as raised:
		fleet.upload(JOBS)
	assert set(raised.value.failures) == {Role.COLOUR}
	assert polymer.job_state is JobState.EMPTY


def test_a_job_the_board_rejects_before_sending_is_reported_too(fleet, polymer):
	with pytest.raises(FleetUploadError, match="polymer.*empty"):
		fleet.upload({Role.POLYMER: [], Role.COLOUR: COLOUR_JOB})


def test_a_board_not_ready_after_its_commit_fails_the_upload(fleet, polymer, colour):
	original = colour.call

	def call(request, timeout_s=None):
		result = original(request, timeout_s)
		if isinstance(request, JobCommit):
			return JobInfo(JobState.LOADING, result.segment_count, result.total_ms)
		return result

	colour.call = call
	with pytest.raises(FleetUploadError, match="colour.*LOADING"):
		fleet.upload(JOBS)
	assert polymer.job_state is JobState.EMPTY


def test_upload_needs_a_job(fleet):
	with pytest.raises(ValueError):
		fleet.upload({})


def test_upload_runs_on_both_boards_at_once(fleet, polymer, colour):
	polymer.delays_s[JobCommit] = colour.delays_s[JobCommit] = 0.2
	started_s = time.perf_counter()
	fleet.upload(JOBS)
	assert time.perf_counter() - started_s < 0.35


# --- Run --------------------------------------------------------------------------------

def test_run_starts_both_boards_and_keeps_the_timings(fleet, polymer, colour):
	fleet.upload(JOBS)
	command = fleet.run()
	assert command.command is Command.JOB_RUN
	assert command.ok
	assert set(command.timings) == {Role.POLYMER, Role.COLOUR}
	assert polymer.job_state is JobState.RUNNING and colour.job_state is JobState.RUNNING
	assert all(t.round_trip_s is not None and t.round_trip_s >= 0 for t in command.timings.values())
	assert command.gap_s() is not None
	assert fleet.history == [command]


def test_run_before_an_upload_is_refused(fleet, polymer):
	with pytest.raises(FleetStateError, match="upload"):
		fleet.run()
	assert polymer.count(JobRun) == 0


def test_a_job_runs_once_per_upload(fleet):
	fleet.upload(JOBS)
	fleet.run()
	with pytest.raises(FleetStateError, match="before run"):
		fleet.run()


def test_run_checks_both_boards_are_ready_first(fleet, polymer, colour):
	fleet.upload(JOBS)
	colour.restart()
	with pytest.raises(FleetStateError, match="colour.*EMPTY"):
		fleet.run()
	assert polymer.count(JobRun) == 0
	assert colour.count(JobRun) == 0


@pytest.mark.parametrize("change, message", [
	(lambda session, polymer, colour: session.board(Role.COLOUR).set_mode(Mode.CLOSED_LOOP), "colour.*open loop"),
	(lambda session, polymer, colour: session.board(Role.POLYMER).set_mode(Mode.OPEN_LOOP), "polymer.*closed loop"),
	(lambda session, polymer, colour: setattr(polymer, "configured", False), "polymer.*no PID gains"),
	(lambda session, polymer, colour: setattr(colour, "estop", True), "colour.*ESTOP"),
], ids=["duty job in closed loop", "speed job in open loop", "no gains", "estop"])
def test_run_refuses_what_the_board_would_refuse(session, fleet, polymer, colour, change, message):
	fleet.upload(JOBS)
	change(session, polymer, colour)
	with pytest.raises(FleetStateError, match=message):
		fleet.run()
	assert polymer.count(JobRun) == 0 and colour.count(JobRun) == 0


def test_run_refused_by_one_board_stops_both(fleet, polymer, colour):
	fleet.upload(JOBS)
	colour.refuse[JobRun] = RequestNackError(Command.JOB_RUN, NackReason.NOT_CONFIGURED)
	with pytest.raises(FleetRunError, match="colour.*NOT_CONFIGURED") as raised:
		fleet.run()
	assert raised.value.run.failures.keys() == {Role.COLOUR}
	assert raised.value.stop.ok
	assert polymer.job_state is JobState.ABORTED
	assert colour.count(Stop) == 1
	assert [c.command for c in fleet.history] == [Command.JOB_RUN, Command.STOP]


def test_a_link_lost_during_run_stops_the_other(fleet, polymer, colour):
	fleet.upload(JOBS)
	colour.refuse[JobRun] = LinkLostError("COM5 lost")
	with pytest.raises(FleetRunError, match="COM5 lost"):
		fleet.run()
	assert polymer.job_state is JobState.ABORTED


def test_no_status_is_sent_while_the_runs_are_answered(fleet, polymer, colour):
	fleet.upload(JOBS)
	polymer.delays_s[JobRun] = 0.15
	counts = []
	original = colour.call

	def call(request, timeout_s=None):
		result = original(request, timeout_s)
		if isinstance(request, JobRun):
			counts.append(colour.count(RequestStatus))
		return result

	colour.call = call
	fleet.run()
	assert colour.count(RequestStatus) == counts[0]
	assert wait_until(lambda: colour.count(RequestStatus) > counts[0])


def test_the_runs_go_out_together(fleet, polymer, colour):
	fleet.upload(JOBS)
	polymer.delays_s[JobRun] = 0.1
	command = fleet.run()
	assert abs(command.gap_s()) < 0.05


def test_run_and_its_gap_are_logged(fleet, caplog):
	fleet.upload(JOBS)
	with caplog.at_level(logging.INFO, logger="polydriver.devices.fleet"):
		fleet.run()
	text = caplog.text
	assert "JOB_RUN polymer" in text and "JOB_RUN colour" in text
	assert "start gap" in text


# --- Stop -------------------------------------------------------------------------------

def test_stop_reaches_both_boards(fleet, polymer, colour):
	command = fleet.stop()
	assert command.ok
	assert polymer.count(Stop) == 1 and colour.count(Stop) == 1
	assert command.command is Command.STOP


def test_stop_ends_a_running_job(fleet, polymer, colour):
	fleet.upload(JOBS)
	fleet.run()
	fleet.stop()
	assert polymer.job_state is JobState.ABORTED and colour.job_state is JobState.ABORTED


def test_stop_reaches_one_board_when_the_other_is_lost(session, fleet, polymer, colour):
	colour.fail_with = LinkLostError("COM5 lost")
	command = fleet.stop()
	assert polymer.count(Stop) == 1
	assert set(command.failures) == {Role.COLOUR}
	assert "COM5 lost" in command.failures[Role.COLOUR]


def test_stop_names_a_board_of_the_job_that_dropped_out(session, fleet, polymer, boards):
	fleet.upload(JOBS)
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost")
	assert wait_until(lambda: not session.state(Role.COLOUR).connected)
	command = fleet.stop()
	assert polymer.count(Stop) == 1
	assert "not connected" in command.failures[Role.COLOUR]


# --- Timings ------------------------------------------------------------------------------

def test_the_gap_is_second_send_minus_first_send():
	command = FleetCommand(Command.JOB_RUN, {
		Role.POLYMER: timing(Role.POLYMER, 10.000, 10.004),
		Role.COLOUR: timing(Role.COLOUR, 10.001, 10.005),
	})
	assert command.gap_s() == pytest.approx(0.001)
	assert command.gap_s(Role.COLOUR, Role.POLYMER) == pytest.approx(-0.001)


def test_the_gap_bounds_come_from_when_each_board_can_have_run():
	command = FleetCommand(Command.JOB_RUN, {
		Role.POLYMER: timing(Role.POLYMER, 10.000, 10.004),
		Role.COLOUR: timing(Role.COLOUR, 10.001, 10.005),
	})
	low, high = command.gap_bounds_s()
	assert low == pytest.approx(-0.003)
	assert high == pytest.approx(0.005)


def test_a_retried_run_counts_from_its_first_send():
	command = FleetCommand(Command.JOB_RUN, {
		Role.POLYMER: timing(Role.POLYMER, 10.000, 10.004),
		Role.COLOUR: timing(Role.COLOUR, 10.001, 10.205, sent_s=10.201, attempts=2),
	})
	assert command.gap_s() == pytest.approx(0.001)
	assert command.gap_bounds_s()[1] == pytest.approx(0.205)


def test_no_gap_without_two_answered_boards():
	command = FleetCommand(Command.JOB_RUN, {
		Role.POLYMER: timing(Role.POLYMER, 10.000, 10.004),
		Role.COLOUR: timing(Role.COLOUR, 10.001, None, error="JOB_RUN got no reply after 4 attempts"),
	})
	assert not command.ok
	assert command.gap_s() is None
	assert command.gap_bounds_s() is None


def test_no_gap_when_one_board_refused():
	command = FleetCommand(Command.JOB_RUN, {
		Role.POLYMER: timing(Role.POLYMER, 10.000, 10.004),
		Role.COLOUR: timing(Role.COLOUR, 10.001, 10.005, error="JOB_RUN refused: WRONG_STATE"),
	})
	assert command.gap_s() is None

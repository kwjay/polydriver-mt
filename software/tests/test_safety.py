import pytest

from polydriver.devices.board import Segment
from polydriver.devices.session import Event, EventKind
from polydriver.jobs import ChannelProgram, GradientProgram
from polydriver.protocol.constants import JobState, Role, StatusFlag
from polydriver.requests.reports import StatusReport
from polydriver.run import SafetyLimits, SafetyWatch

# 10 s at 20 Hz, then a ramp to 40 Hz over 10 s: 20 s in all, on both boards.
SEGMENTS = (Segment(20.0, 10_000), Segment(40.0, 10_000, ramp=True))
SLOW = (Segment(1.5, 10_000), Segment(1.5, 10_000))
STALLED = StatusFlag.ENCODER_STALLED | StatusFlag.REGULATING


def program(colour=SEGMENTS):
	def channel(role, segments):
		return ChannelProgram(role, segments, 0.0, 0.0, 0.0, "M", "A", "hash")
	return GradientProgram(channel("polymer", SEGMENTS), channel("colour", colour), 20_000, 0.005)


def watch(colour=SEGMENTS, **limits):
	return SafetyWatch(program(colour), SafetyLimits(**limits), started_s=100.0)


def report(state=JobState.RUNNING, elapsed_ms=1000, speed_hz=20.0, flags=StatusFlag.REGULATING):
	return StatusReport(speed_hz, speed_hz, 150, flags, 0, 0, 0, 0, 0, 0, state, 0, elapsed_ms)


def event(kind, role=Role.COLOUR, message="details"):
	return Event(kind, role, message, 101.0)


# --- A board drops out -------------------------------------------------------------------------

def test_a_lost_link_aborts_the_run():
	verdict = watch().on_event(event(EventKind.LINK_LOST, message="COM5 lost"))
	assert verdict.abort == "the colour board's link was lost: COM5 lost"


def test_a_disconnected_board_aborts_the_run():
	assert watch().on_event(event(EventKind.DISCONNECTED, Role.POLYMER)).abort == "the polymer board was disconnected"


def test_a_restarted_board_aborts_the_run():
	assert watch().on_event(event(EventKind.RESTARTED)).abort == "the colour board restarted and lost its job"


@pytest.mark.parametrize("kind", [EventKind.POLL_FAILED, EventKind.POLL_RECOVERED, EventKind.COMMAND,
								EventKind.SETTINGS_APPLIED])
def test_other_events_do_not(kind):
	assert watch().on_event(event(kind)).abort is None


def test_a_board_that_falls_silent_aborts_the_run_after_the_silence_limit():
	w = watch(silence_s=2.0)
	w.on_status(Role.POLYMER, report(), 101.0)
	w.on_status(Role.COLOUR, report(), 101.0)
	assert w.on_tick(103.0).abort is None
	w.on_status(Role.POLYMER, report(), 103.0)
	assert w.on_tick(103.1).abort == "no STATUS from the colour board for 2.1 s"


def test_the_silence_counts_from_the_start():
	assert watch(silence_s=2.0).on_tick(102.5).abort == "no STATUS from the polymer board for 2.5 s"


def test_a_finished_board_may_fall_silent():
	w = watch(silence_s=2.0, overtime_s=100.0)
	w.on_status(Role.POLYMER, report(JobState.DONE, 20_000), 120.0)
	w.on_status(Role.COLOUR, report(JobState.RUNNING, 19_000), 120.0)
	assert w.on_tick(121.5).abort is None
	w.on_status(Role.COLOUR, report(JobState.RUNNING, 19_000), 121.5)
	assert w.on_tick(122.5).abort is None


# --- A board ends its job -------------------------------------------------------------------------

@pytest.mark.parametrize("flags, cause", [
	(StatusFlag(0), ""),
	(StatusFlag.COMMS_LOST, ": it heard nothing from the host for its comms timeout"),
	(StatusFlag.ESTOP, ": ESTOP"),
])
def test_a_board_that_aborts_its_job_aborts_the_run(flags, cause):
	verdict = watch().on_status(Role.COLOUR, report(JobState.ABORTED, 4000, 0.0, flags), 104.0)
	assert verdict.abort == f"the colour board aborted its job at 4 s{cause}"


@pytest.mark.parametrize("state", [JobState.EMPTY, JobState.LOADING, JobState.READY])
def test_a_board_that_no_longer_has_its_job_aborts_the_run(state):
	verdict = watch().on_status(Role.POLYMER, report(state, 0, 0.0), 104.0)
	assert verdict.abort == f"the polymer board no longer runs its job (it reports {state.name}; did it restart?)"


def test_a_board_that_is_done_is_fine():
	assert watch().on_status(Role.COLOUR, report(JobState.DONE, 20_000, 0.0), 120.0).abort is None


# --- Tracking ---------------------------------------------------------------------------------------

def test_a_speed_off_for_a_moment_is_fine_but_off_for_the_limit_aborts():
	w = watch(tracking_s=3.0)
	for elapsed_ms in range(2000, 4900, 100):
		assert w.on_status(Role.COLOUR, report(elapsed_ms=elapsed_ms, speed_hz=12.0), 100.0).abort is None
	verdict = w.on_status(Role.COLOUR, report(elapsed_ms=5000, speed_hz=12.0), 100.0)
	assert verdict.abort == ("the colour speed was off by more than 25 % (and 1 Hz) for 3 s: 12 Hz measured, "
							"20 Hz commanded at 5 s")


def test_the_tracking_follows_the_ramp():
	w = watch(tracking_s=1.0)
	for elapsed_ms in range(10_000, 15_000, 500):
		commanded = 20.0 + 20.0 * (elapsed_ms - 10_000) / 10_000
		assert w.on_status(Role.POLYMER, report(elapsed_ms=elapsed_ms, speed_hz=commanded * 1.2), 100.0).abort is None
	verdict = w.on_status(Role.POLYMER, report(elapsed_ms=15_000, speed_hz=20.0), 100.0)
	assert verdict.abort is None
	assert w.on_status(Role.POLYMER, report(elapsed_ms=16_000, speed_hz=20.0), 100.0).abort.startswith(
		"the polymer speed was off")


def test_coming_back_on_speed_starts_the_count_again():
	w = watch(tracking_s=3.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=1000, speed_hz=12.0), 100.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=3500, speed_hz=20.0), 100.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=4000, speed_hz=12.0), 100.0)
	assert w.on_status(Role.COLOUR, report(elapsed_ms=6900, speed_hz=12.0), 100.0).abort is None
	assert w.on_status(Role.COLOUR, report(elapsed_ms=7000, speed_hz=12.0), 100.0).abort is not None


def test_an_error_inside_the_fraction_or_the_floor_is_fine():
	w = watch(tracking_s=0.5)
	assert w.on_status(Role.COLOUR, report(elapsed_ms=1000, speed_hz=24.9), 100.0).abort is None
	assert w.on_status(Role.COLOUR, report(elapsed_ms=2000, speed_hz=24.9), 100.0).abort is None
	slow = watch(tracking_s=0.5, tracking_fraction=0.01, tracking_floor_hz=6.0)
	slow.on_status(Role.COLOUR, report(elapsed_ms=1000, speed_hz=14.5), 100.0)
	assert slow.on_status(Role.COLOUR, report(elapsed_ms=2000, speed_hz=14.5), 100.0).abort is None


def test_a_speed_too_slow_to_measure_is_not_judged():
	w = watch(colour=SLOW, tracking_s=0.5)
	stalled = StatusFlag.ENCODER_STALLED | StatusFlag.REGULATING
	for elapsed_ms in (1000, 2000, 5000):
		verdict = w.on_status(Role.COLOUR, report(elapsed_ms=elapsed_ms, speed_hz=0.0, flags=stalled), 100.0)
		assert verdict == verdict.__class__()


# 2 s at 20 Hz, 2 s at 1.5 Hz (too slow to measure), then 20 Hz again.
DIP = (Segment(20.0, 2000), Segment(1.5, 2000), Segment(20.0, 10_000), Segment(20.0, 6000))


def test_a_stretch_too_slow_to_measure_starts_the_tracking_count_again():
	w = watch(colour=DIP, tracking_s=3.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=1000, speed_hz=12.0), 100.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=3000, speed_hz=0.0), 100.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=4500, speed_hz=12.0), 100.0)
	assert w.on_status(Role.COLOUR, report(elapsed_ms=7000, speed_hz=12.0), 100.0).abort is None
	assert w.on_status(Role.COLOUR, report(elapsed_ms=7500, speed_hz=12.0), 100.0).abort is not None


def test_after_a_stretch_too_slow_to_measure_the_screw_spins_up_again_before_a_stall_counts():
	w = watch(colour=DIP)
	w.on_status(Role.COLOUR, report(elapsed_ms=500, speed_hz=20.0), 100.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=1500, speed_hz=20.0), 100.0)
	assert w.on_status(Role.COLOUR, report(elapsed_ms=3000, speed_hz=0.0, flags=STALLED), 100.0).warnings == ()
	assert w.on_status(Role.COLOUR, report(elapsed_ms=4100, speed_hz=0.0, flags=STALLED), 100.0).warnings == ()
	w.on_status(Role.COLOUR, report(elapsed_ms=4500, speed_hz=20.0), 100.0)
	assert w.on_status(Role.COLOUR, report(elapsed_ms=5000, speed_hz=0.0, flags=STALLED), 100.0).warnings == (
		"the colour encoder stalled at 5 s with 20 Hz commanded",)


# --- Encoder stall: a warning ------------------------------------------------------------------------

def test_a_screw_spinning_up_is_no_stall():
	w = watch()
	assert w.on_status(Role.COLOUR, report(elapsed_ms=10, speed_hz=0.0, flags=STALLED), 100.0).warnings == ()
	assert w.on_status(Role.COLOUR, report(elapsed_ms=110, speed_hz=0.0, flags=STALLED), 100.0).warnings == ()


def test_an_encoder_stall_is_a_warning_once_and_not_an_abort():
	w = watch(tracking_s=3.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=500, speed_hz=20.0), 100.0)
	first = w.on_status(Role.COLOUR, report(elapsed_ms=1000, speed_hz=0.0, flags=STALLED), 100.0)
	assert first.warnings == ("the colour encoder stalled at 1 s with 20 Hz commanded",) and first.abort is None
	again = w.on_status(Role.COLOUR, report(elapsed_ms=1500, speed_hz=0.0, flags=STALLED), 100.0)
	assert again.warnings == () and again.abort is None
	w.on_status(Role.COLOUR, report(elapsed_ms=2000, speed_hz=20.0), 100.0)
	later = w.on_status(Role.COLOUR, report(elapsed_ms=2500, speed_hz=0.0, flags=STALLED), 100.0)
	assert later.warnings == ("the colour encoder stalled at 2.5 s with 20 Hz commanded",)


def test_a_stall_that_lasts_is_a_tracking_error_and_says_so():
	w = watch(tracking_s=3.0)
	w.on_status(Role.COLOUR, report(elapsed_ms=1000, speed_hz=0.0, flags=STALLED), 100.0)
	verdict = w.on_status(Role.COLOUR, report(elapsed_ms=4000, speed_hz=0.0, flags=STALLED), 100.0)
	assert verdict.abort.endswith("0 Hz measured, 20 Hz commanded at 4 s (encoder stalled)")


# --- Overtime ---------------------------------------------------------------------------------------------

def test_a_job_that_runs_over_its_time_aborts_the_run():
	w = watch(overtime_s=2.0, silence_s=100.0)
	assert w.on_tick(121.9).abort is None
	assert w.on_tick(122.1).abort == "the job has run more than 2 s over its 20 s"


def test_boards_that_are_done_are_never_over_time():
	w = watch(overtime_s=2.0, silence_s=100.0)
	w.on_status(Role.POLYMER, report(JobState.DONE, 20_000, 0.0), 120.0)
	w.on_status(Role.COLOUR, report(JobState.DONE, 20_000, 0.0), 120.0)
	assert w.on_tick(130.0).abort is None


# --- Limits --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["tracking_fraction", "tracking_floor_hz", "tracking_s", "overtime_s", "silence_s"])
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), True])
def test_every_limit_must_be_a_number_above_0(name, value):
	with pytest.raises(ValueError, match=f"{name} .* must be a finite number > 0"):
		SafetyLimits(**{name: value})

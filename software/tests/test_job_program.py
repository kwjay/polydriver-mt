import random
from dataclasses import replace
from pathlib import Path

import pytest

from polydriver.calibration import CalibrationStore, rate_for_speed, speed_for_rate
from polydriver.devices.board import Segment
from polydriver.jobs import (
	CalibrationRef,
	ExponentialCurve,
	JobCompileError,
	LinearCurve,
	SCurve,
	StepsCurve,
	TableCurve,
	compile_gradient_job,
	load_job,
	speed_at,
)
from polydriver.jobs.program import ChannelCalibration, _farthest_end
from polydriver.materials import Kind, Material, MaterialRegistry

EXAMPLE = Path(__file__).parent / "data" / "gradient_job_example.json"

# Straight calibrations: rate exactly proportional to speed over the job's range.
PP_STRAIGHT = ((10.0, 0.5), (20.0, 1.0), (30.0, 1.5), (40.0, 2.0))
MB_STRAIGHT = ((2.0, 0.012), (5.0, 0.03), (10.0, 0.06), (15.0, 0.09))
# Bent ones, as real screws give.
PP_BENT = ((10.0, 0.4), (20.0, 1.1), (30.0, 1.45), (40.0, 2.0))
MB_BENT = ((2.0, 0.01), (5.0, 0.03), (8.0, 0.05), (15.0, 0.09))


def materials():
	registry = MaterialRegistry()
	registry.put(Material("PP", Kind.POLYMER, dispenser="B"))
	registry.put(Material("MB-red", Kind.MASTERBATCH, dispenser="A"))
	return registry


def calibrations(pp=PP_BENT, mb=MB_BENT):
	store = CalibrationStore()
	for speed_hz, rate_g_s in pp:
		store.add_measurement("B", "PP", speed_hz, rate_g_s * 10, 10.0)
	for speed_hz, rate_g_s in mb:
		for _ in range(2):
			store.add_measurement("A", "MB-red", speed_hz, rate_g_s * 100, 100.0)
	return store


def example(store, **changes):
	job = load_job(EXAMPLE)
	job = replace(job, polymer=CalibrationRef.of(store.curve("B", "PP")),
				colour=CalibrationRef.of(store.curve("A", "MB-red")))
	return replace(job, **changes) if changes else job


def short(store, **changes):
	"""The example scaled to 60 s with 3 s holds, for the tests that do not need the full run."""
	values = dict(amount_given={"duration_s": 60, "total_flow_g_s": 1.5}, hold_start_s=3.0, hold_end_s=3.0)
	values.update(changes)
	return example(store, **values)


def board_speeds(segments, total_ms):
	"""What the board commands at each 10 ms tick: a step holds its value, a ramp goes linearly from
	the previous segment's value (0 Hz before the first) to its own."""
	speeds, start, previous, index = [], 0, 0.0, 0
	for t in range(0, total_ms, 10):
		while t >= start + segments[index].duration_ms:
			start += segments[index].duration_ms
			previous = segments[index].value
			index += 1
		segment = segments[index]
		fraction = (t - start) / segment.duration_ms
		speeds.append(previous + (segment.value - previous) * fraction if segment.ramp else segment.value)
	return speeds


def worst_error(program, job, store):
	"""The largest |delivered - target| / target over every tick of both channels."""
	worst = 0.0
	for role, curve in (("polymer", store.curve("B", "PP")), ("colour", store.curve("A", "MB-red"))):
		channel = getattr(program, role)
		for k, speed in enumerate(board_speeds(channel.segments, program.total_ms)):
			target = getattr(job.profile, f"{role}_g_s")(k * 0.01)
			delivered = 0.0 if speed == 0 else rate_for_speed(curve, speed)
			worst = max(worst, abs(delivered - target) / target)
	return worst


# --- A straight line ----------------------------------------------------------------------------

def test_a_straight_line_gives_one_ramp_between_the_holds():
	store = calibrations(PP_STRAIGHT, MB_STRAIGHT)
	program = compile_gradient_job(example(store), materials(), store)
	colour = program.colour.segments
	assert [(s.ramp, s.duration_ms) for s in colour] == [(False, 30_000), (True, 540_000), (False, 30_000)]
	assert colour[0].value == pytest.approx(speed_for_rate(store.curve("A", "MB-red"), 0.015))
	assert colour[1].value == colour[2].value == pytest.approx(speed_for_rate(store.curve("A", "MB-red"), 0.075))
	assert len(program.polymer.segments) == 3
	assert program.total_ms == 600_000
	assert program.colour.max_error < 1e-9 and program.polymer.max_error < 1e-9


def test_without_a_start_hold_the_job_begins_with_a_10_ms_step():
	store = calibrations(PP_STRAIGHT, MB_STRAIGHT)
	program = compile_gradient_job(short(store, hold_start_s=0.0, hold_end_s=0.0), materials(), store)
	first, second = program.colour.segments[:2]
	assert (first.ramp, first.duration_ms, second.ramp) == (False, 10, True)
	assert sum(s.duration_ms for s in program.colour.segments) == 60_000


def test_speed_at_is_what_the_board_commands():
	segments = (Segment(10.0, 1000), Segment(30.0, 2000, ramp=True), Segment(30.0, 500), Segment(0.0, 1000, ramp=True))
	ticks = board_speeds(list(segments), 4500)
	assert [speed_at(segments, t) for t in range(0, 4500, 10)] == pytest.approx(ticks)
	assert (speed_at(segments, 2000), speed_at(segments, 4250), speed_at(segments, 4500)) == (20.0, 7.5, 0.0)
	assert speed_at((Segment(8.0, 1000, ramp=True),), 250) == 2.0


# --- Every curve type fits the board ----------------------------------------------------------

CURVES = [LinearCurve(), ExponentialCurve(3.0), ExponentialCurve(-3.0), SCurve(10.0), StepsCurve(5),
		TableCurve([(0, 0), (0.3, 0.7), (0.6, 0.8), (1, 1)])]


@pytest.mark.parametrize("curve", CURVES, ids=repr)
def test_every_curve_type_compiles_within_the_segment_limit_and_the_error(curve):
	store = calibrations()
	job = example(store, curve=curve)
	program = compile_gradient_job(job, materials(), store)
	for channel in (program.polymer, program.colour):
		assert len(channel.segments) <= 32
		assert sum(s.duration_ms for s in channel.segments) == program.total_ms == 600_000
		assert all(s.duration_ms >= 10 for s in channel.segments)
		assert channel.max_error <= 0.005
	assert program.colour.expected_grams == pytest.approx(program.colour.target_grams, rel=0.005)
	assert program.polymer.expected_grams == pytest.approx(program.polymer.target_grams, rel=0.005)
	assert program.colour.target_grams + program.polymer.target_grams == pytest.approx(900.0)


@pytest.mark.parametrize("curve", CURVES, ids=repr)
def test_a_board_simulated_tick_by_tick_stays_within_the_error(curve):
	store = calibrations()
	job = short(store, curve=curve)
	assert worst_error(compile_gradient_job(job, materials(), store), job, store) <= 0.005 + 1e-9


def test_a_steps_curve_is_one_step_per_level_with_the_holds_merged_in():
	store = calibrations()
	program = compile_gradient_job(example(store, curve=StepsCurve(5)), materials(), store)
	colour = program.colour.segments
	assert not any(s.ramp for s in colour)
	assert [s.duration_ms for s in colour] == [30_000 + 108_000, 108_000, 108_000, 108_000, 108_000 + 30_000]


def test_a_looser_limit_needs_fewer_segments():
	store = calibrations()
	job = short(store, curve=ExponentialCurve(4.0))
	counts = [len(compile_gradient_job(job, materials(), store, error_limit=limit).colour.segments)
			for limit in (0.02, 0.005, 0.001)]
	assert counts[0] <= counts[1] <= counts[2] and counts[0] < counts[2]


def test_the_program_names_what_it_was_made_from():
	store = calibrations()
	program = compile_gradient_job(short(store), materials(), store)
	assert (program.colour.material, program.colour.dispenser) == ("MB-red", "A")
	assert program.colour.curve_hash == store.curve("A", "MB-red").hash
	assert program.error_limit == 0.005


def test_holds_off_the_10_ms_grid_still_give_whole_ticks():
	store = calibrations()
	program = compile_gradient_job(short(store, hold_start_s=3.005, hold_end_s=2.995, curve=ExponentialCurve(3.0)),
								materials(), store)
	for channel in (program.polymer, program.colour):
		assert all(s.duration_ms >= 10 for s in channel.segments)
		assert sum(s.duration_ms for s in channel.segments) == 60_000
		assert channel.max_error <= 0.005


def test_each_ramp_reaches_as_far_as_its_ticks_allow_and_no_farther():
	store = calibrations()
	job = short(store, curve=ExponentialCurve(3.0))
	segments = compile_gradient_job(job, materials(), store).colour.segments
	calibration = ChannelCalibration(store.curve("A", "MB-red"))

	def target(t_ms):
		rate = job.profile.colour_g_s(t_ms / 1000)
		return calibration.speed(rate), calibration.tolerance_hz(rate, calibration.speed(rate), 0.005)

	def within(a, b):
		(sa, _), (sb, _) = target(a), target(b)
		return all(abs(sa + (sb - sa) * (t - a) / (b - a) - target(t)[0]) <= target(t)[1] for t in range(a + 10, b, 10))

	start, ramps = 0, []
	for segment in segments:
		if segment.ramp:
			ramps.append((start, start + segment.duration_ms))
		start += segment.duration_ms
	assert len(ramps) > 3
	for (a, b), following in zip(ramps, ramps[1:] + [None]):
		assert within(a, b)
		if following is not None and following[1] - b >= 20:
			assert not within(a, b + 10), f"the ramp from {a} ms could have reached {b + 10} ms"


def test_a_ramp_never_leaves_less_than_a_tick_before_the_end_of_its_part():
	# Ticks at 10, 20, 30; the part ends at 35. Reaching 30 fits, reaching 35 does not, and 30 would leave
	# 5 ms, so the ramp stops at 20.
	speeds = {0: 0.0, 10: 1.0, 20: 2.0, 30: 3.0, 35: 9.0}
	assert _farthest_end(0, 35, [10, 20, 30], [1.0, 2.0, 3.0], [0.01] * 3, speeds.__getitem__) == 20


def test_a_ramp_from_off_the_grid_lasts_at_least_a_tick():
	# From 5 ms, nothing fits past the first tick; the shortest ramp that lasts a tick ends at 20, not 10.
	speeds = {5: 0.0, 10: 0.5, 20: 5.0, 30: 0.0}
	assert _farthest_end(5, 40, [10, 20, 30], [0.5, 5.0, 0.0], [0.01] * 3, speeds.__getitem__) == 20


# --- Refused --------------------------------------------------------------------------------------

def test_a_flow_that_changes_faster_than_a_tick_can_follow_is_refused_with_where():
	# Without a start hold the first tick is a step at the 10 ms speed: 1.008 % instead of 1 % colour.
	store = calibrations()
	job = short(store, amount_given={"duration_s": 5.0, "total_flow_g_s": 1.5}, hold_start_s=0.0, hold_end_s=0.0)
	with pytest.raises(JobCompileError, match=r"colour: the linear gradient changes faster than the board's 10 ms ticks "
						r"can follow within 0.5 % \(off by 0.8 % at 0 s\)"):
		compile_gradient_job(job, materials(), store)


def test_more_segments_than_the_board_holds_is_refused_naming_the_curve():
	store = calibrations()
	job = short(store, curve=ExponentialCurve(10.0))
	with pytest.raises(JobCompileError, match=r"colour: the exponential \(k=10\) gradient needs \d+ segments to stay "
						r"within 0.05 %; the board holds 8"):
		compile_gradient_job(job, materials(), store, error_limit=0.0005, max_segments=8)


def test_a_job_that_does_not_fit_its_calibrations_is_refused_with_the_check():
	store = calibrations(mb=((3.0, 0.02), (5.0, 0.03), (8.0, 0.05), (15.0, 0.09)))
	with pytest.raises(JobCompileError, match="the job does not fit: colour, 0 to 75 s .*below the calibrated"):
		compile_gradient_job(example(store), materials(), store)


def test_a_ramp_up_from_a_stopped_screw_is_refused():
	store = calibrations(mb=((1.0, 0.0), (2.0, 0.01), (5.0, 0.03), (15.0, 0.09)))
	with pytest.raises(JobCompileError, match="colour: the gradient starts the screw from 0 g/s"):
		compile_gradient_job(short(store, phi_start=0.0), materials(), store)


def test_a_stopped_screw_in_a_hold_or_step_is_fine():
	store = calibrations(mb=((1.0, 0.0), (2.0, 0.01), (5.0, 0.03), (15.0, 0.09)))
	program = compile_gradient_job(short(store, phi_start=0.0, curve=StepsCurve(3)), materials(), store)
	assert program.colour.segments[0] == Segment(0.0, 3_000 + 18_000)


@pytest.mark.parametrize("changes, message", [
	(dict(hold_start_s=0.005), "the start hold of 0.005 s is shorter than one 10 ms control tick"),
	(dict(curve=StepsCurve(100), hold_start_s=0.0, hold_end_s=59.5), "a step of steps .levels=100. lasts 5 ms"),
])
def test_parts_shorter_than_a_tick_are_refused(changes, message):
	store = calibrations()
	with pytest.raises(JobCompileError, match=message):
		compile_gradient_job(short(store, **changes), materials(), store)


@pytest.mark.parametrize("limit", [0.0, -0.01, 1.0, float("nan"), True])
def test_the_error_limit_must_be_a_fraction_between_0_and_1(limit):
	store = calibrations()
	with pytest.raises(JobCompileError, match="error limit"):
		compile_gradient_job(short(store), materials(), store, error_limit=limit)


# --- The fast calibration lookups agree with the plain ones --------------------------------------

@pytest.mark.parametrize("points", [MB_BENT, ((1.0, 0.0), (2.0, 0.0), (3.0, 0.5), (4.0, 0.5), (6.0, 2.0))])
def test_the_programs_lookups_give_the_same_answers_as_the_calibration_module(points):
	store = CalibrationStore()
	for speed_hz, rate_g_s in points:
		store.add_measurement("A", "X", speed_hz, rate_g_s * 10, 10.0)
	curve = store.curve("A", "X")
	fast = ChannelCalibration(curve)
	rng = random.Random(3)
	low, high = curve.points[0], curve.points[-1]
	for rate in [p.rate_g_s for p in curve.points] + [rng.uniform(low.rate_g_s, high.rate_g_s) for _ in range(200)]:
		assert fast.speed(rate) == pytest.approx(speed_for_rate(curve, rate), abs=1e-12)
	for speed in [p.speed_hz for p in curve.points] + [rng.uniform(low.speed_hz, high.speed_hz) for _ in range(200)]:
		assert fast.rate(speed) == pytest.approx(rate_for_speed(curve, speed), abs=1e-12)

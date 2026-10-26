import pytest

from polydriver.calibration import CalibrationStore, rate_for_speed
from polydriver.devices.board import Segment
from polydriver.jobs import DutyRow, JobCompileError, RateRow, compile_duty_job, compile_rate_job
from polydriver.materials import Kind, Material

PLA = Material("PLA", Kind.POLYMER, dispenser="B")

# Straight 0.06 g/s per Hz from 10 to 40 Hz: easy to check by hand.
LINEAR = ((10.0, 0.4), (20.0, 1.0), (30.0, 1.6), (40.0, 2.2))
# A dead band to 20 Hz, then a bend at 30 Hz.
BENT = ((10.0, 0.0), (20.0, 0.0), (30.0, 1.0), (40.0, 3.0))


def curve(points=LINEAR, material="PLA", dispenser="B"):
	store = CalibrationStore()
	for speed_hz, rate_g_s in points:
		store.add_measurement(dispenser, material, speed_hz, rate_g_s * 10.0, 10.0)
	return store.curve(dispenser, material)


def speeds(job):
	return [round(s.value, 6) for s in job.segments]


# --- Steps ---------------------------------------------------------------------------------------

def test_the_pla_job_gets_the_calibrated_speeds():
	job = compile_rate_job(PLA, curve(), [RateRow(1.2, 10.0), RateRow(2.0, 15.0)])
	assert speeds(job) == [pytest.approx(23.333333), pytest.approx(36.666667)]
	assert [s.duration_ms for s in job.segments] == [10_000, 15_000]
	assert not any(s.ramp or s.duty for s in job.segments)
	assert job.total_ms == 25_000
	assert job.expected_grams == pytest.approx(1.2 * 10 + 2.0 * 15)
	assert job.expected_grams_per_row == pytest.approx((12.0, 30.0))


def test_the_job_names_what_it_was_made_from():
	c = curve()
	job = compile_rate_job(PLA, c, [RateRow(1.2, 10.0)])
	assert (job.material, job.dispenser, job.curve_hash) == ("PLA", "B", c.hash)


def test_zero_g_s_stops_the_motor_without_needing_the_calibration():
	job = compile_rate_job(PLA, curve(), [RateRow(1.0, 5.0), RateRow(0.0, 2.0), RateRow(1.0, 5.0)])
	assert speeds(job) == [20.0, 0.0, 20.0]
	assert job.expected_grams_per_row == pytest.approx((5.0, 0.0, 5.0))


def test_a_rate_outside_the_calibration_names_its_row():
	with pytest.raises(JobCompileError, match=r"row 2: 2.5 g/s is outside .*B / PLA.*0.4 to 2.2 g/s"):
		compile_rate_job(PLA, curve(), [RateRow(1.0, 5.0), RateRow(2.5, 5.0)])
	with pytest.raises(JobCompileError, match="row 1: 0.1 g/s is outside"):
		compile_rate_job(PLA, curve(), [RateRow(0.1, 5.0)])


def test_a_calibration_that_falls_is_reported_as_such_not_as_out_of_range():
	falling = ((10.0, 0.0), (20.0, 1.5), (30.0, 1.4), (40.0, 2.5))
	with pytest.raises(JobCompileError, match="row 1: .*the rate falls from 1.5 g/s at 20 Hz") as raised:
		compile_rate_job(PLA, curve(falling), [RateRow(1.0, 5.0)])
	assert "outside" not in str(raised.value)


def test_a_rate_outside_the_material_flow_window_names_its_row():
	pla = Material("PLA", Kind.POLYMER, dispenser="B", flow_min_g_s=0.5, flow_max_g_s=1.5)
	with pytest.raises(JobCompileError, match="row 2: 2 g/s is outside PLA's flow window 0.5 to 1.5 g/s"):
		compile_rate_job(pla, curve(), [RateRow(1.0, 5.0), RateRow(2.0, 5.0)])
	assert compile_rate_job(pla, curve(), [RateRow(0.0, 5.0), RateRow(1.5, 5.0)]).segments


def test_the_curve_must_be_for_the_material():
	with pytest.raises(JobCompileError, match="the calibration B / TPU is not for PLA"):
		compile_rate_job(PLA, curve(material="TPU"), [RateRow(1.0, 5.0)])


# --- Ramps ---------------------------------------------------------------------------------------

def test_a_ramp_on_a_straight_calibration_is_one_segment():
	job = compile_rate_job(PLA, curve(), [RateRow(1.0, 5.0), RateRow(2.2, 10.0, ramp=True)])
	assert job.segments[1] == Segment(40.0, 10_000, ramp=True)
	assert job.expected_grams_per_row == pytest.approx((5.0, (1.0 + 2.2) / 2 * 10))


def test_a_ramp_is_split_where_the_calibration_bends_so_g_s_stays_linear():
	c = curve(BENT)
	job = compile_rate_job(PLA, c, [RateRow(0.5, 4.0), RateRow(2.5, 8.0, ramp=True)])
	# 0.5 -> 2.5 g/s crosses the 30 Hz point (1.0 g/s) a quarter of the way in.
	assert speeds(job) == [25.0, 30.0, 37.5]
	assert [s.duration_ms for s in job.segments] == [4_000, 2_000, 6_000]
	assert [s.ramp for s in job.segments] == [False, True, True]
	assert job.row_of_segment == (1, 2, 2)
	assert job.expected_grams_per_row == pytest.approx((2.0, (0.5 + 2.5) / 2 * 8))


def test_every_point_of_a_split_ramp_is_on_the_requested_line():
	c = curve(BENT)
	job = compile_rate_job(PLA, c, [RateRow(0.5, 4.0), RateRow(2.5, 8.0, ramp=True)])
	previous_hz, start_ms = job.segments[0].value, 4_000
	for segment in job.segments[1:]:
		for fraction in (0.0, 0.3, 1.0):
			hz = previous_hz + (segment.value - previous_hz) * fraction
			t_s = (start_ms + segment.duration_ms * fraction) / 1000 - 4.0
			assert rate_for_speed(c, hz) == pytest.approx(0.5 + (2.5 - 0.5) * t_s / 8.0)
		previous_hz, start_ms = segment.value, start_ms + segment.duration_ms


def test_a_ramp_down_is_split_too():
	job = compile_rate_job(PLA, curve(BENT), [RateRow(2.5, 4.0), RateRow(0.5, 8.0, ramp=True)])
	assert speeds(job) == [37.5, 30.0, 25.0]
	assert [s.duration_ms for s in job.segments] == [4_000, 6_000, 2_000]


# Bends at 30 and 40 Hz; 20 Hz lies on the straight line from 10 to 30 Hz, so it is no bend.
TWO_BENDS = ((10.0, 0.0), (20.0, 0.5), (30.0, 1.0), (40.0, 3.0), (50.0, 4.0))


@pytest.mark.parametrize("from_g_s, to_g_s, expected_hz, expected_ms", [
	(0.25, 3.5, [15.0, 30.0, 40.0, 45.0], [4_000, 1_108, 2_954, 738]),
	(3.5, 0.25, [45.0, 40.0, 30.0, 15.0], [4_000, 738, 2_954, 1_108]),
])
def test_a_ramp_across_two_bends_keeps_them_in_time_order(from_g_s, to_g_s, expected_hz, expected_ms):
	c = curve(TWO_BENDS)
	job = compile_rate_job(PLA, c, [RateRow(from_g_s, 4.0), RateRow(to_g_s, 4.8, ramp=True)])
	assert speeds(job) == expected_hz
	assert [s.duration_ms for s in job.segments] == expected_ms
	assert job.expected_grams == pytest.approx(board_grams(c, job), rel=1e-6)


def test_a_ramp_to_the_same_rate_is_a_flat_ramp():
	job = compile_rate_job(PLA, curve(), [RateRow(1.0, 5.0), RateRow(1.0, 5.0, ramp=True)])
	assert speeds(job) == [20.0, 20.0]


def board_grams(c, job):
	"""What the board delivers: the speed linear in time within each segment, integrated finely."""
	grams, previous_hz = 0.0, 0.0
	for segment in job.segments:
		steps = 4_000
		for k in range(steps):
			fraction = (k + 0.5) / steps if segment.ramp else 1.0
			hz = previous_hz + (segment.value - previous_hz) * fraction
			grams += (0.0 if hz == 0 else rate_for_speed(c, hz)) * segment.duration_ms / 1000 / steps
		previous_hz = segment.value
	return grams


# A bend 6 ms into a 1 s ramp from 1.0 to 2.2 g/s, and one 6 ms before its end.
NEAR_START = ((10.0, 0.4), (20.0, 1.0), (20.1, 1.0072), (40.0, 2.2))
NEAR_END = ((10.0, 0.4), (20.0, 1.0), (39.9, 2.1928), (40.0, 2.2))


@pytest.mark.parametrize("points", [NEAR_START, NEAR_END])
def test_a_piece_shorter_than_a_tick_is_merged_without_moving_the_ramp_ends(points):
	c = curve(points)
	job = compile_rate_job(PLA, c, [RateRow(1.0, 5.0), RateRow(2.2, 1.0, ramp=True)])
	assert job.segments[1:] == (Segment(40.0, 1_000, ramp=True),)
	assert job.expected_grams == pytest.approx(board_grams(c, job), rel=1e-6)
	assert job.expected_grams != pytest.approx(5.0 + 1.6, rel=1e-6), "the merged piece still crosses the bend"


@pytest.mark.parametrize("rows", [
	[RateRow(1.0, 5.0, ramp=True)],
	[RateRow(0.0, 5.0), RateRow(1.0, 5.0, ramp=True)],
])
def test_a_ramp_cannot_start_with_the_motor_stopped(rows):
	with pytest.raises(JobCompileError, match=f"row {len(rows)}: a ramp from 0 g/s starts with the motor stopped"):
		compile_rate_job(PLA, curve(), rows)


def test_a_ramp_cannot_end_at_zero():
	with pytest.raises(JobCompileError, match="row 2: a ramp to 0 g/s.*step at 0 g/s"):
		compile_rate_job(PLA, curve(), [RateRow(1.0, 5.0), RateRow(0.0, 5.0, ramp=True)])


# --- Times ---------------------------------------------------------------------------------------

def test_rounding_to_milliseconds_never_adds_up_to_drift():
	job = compile_rate_job(PLA, curve(), [RateRow(1.0, 0.0333)] * 30)
	assert job.total_ms == 999
	assert sum(s.duration_ms for s in job.segments) == 999


@pytest.mark.parametrize("duration_s", [0.0, -1.0, float("nan"), float("inf"), True])
def test_a_duration_must_be_a_number_above_0(duration_s):
	with pytest.raises(JobCompileError, match="row 1: the duration .* s must be a finite number > 0"):
		compile_rate_job(PLA, curve(), [RateRow(1.0, duration_s)])


def test_a_row_must_last_at_least_one_tick():
	with pytest.raises(JobCompileError, match="row 1: 0.009 s is shorter than one 10 ms control tick"):
		compile_rate_job(PLA, curve(), [RateRow(1.0, 0.009)])


@pytest.mark.parametrize("rate", [-1.0, float("nan"), True])
def test_a_rate_must_be_a_number_of_0_or_more(rate):
	with pytest.raises(JobCompileError, match="row 1: the rate"):
		compile_rate_job(PLA, curve(), [RateRow(rate, 1.0)])


def test_an_empty_job_is_refused():
	with pytest.raises(JobCompileError, match="no rows"):
		compile_rate_job(PLA, curve(), [])


def test_too_many_segments_is_refused_with_the_count():
	rows = [RateRow(1.0, 1.0), RateRow(1.5, 1.0)] * 3
	assert len(compile_rate_job(PLA, curve(), rows, max_segments=6).segments) == 6
	with pytest.raises(JobCompileError, match="7 segments, the board holds 6"):
		compile_rate_job(PLA, curve(), rows + [RateRow(1.0, 1.0)], max_segments=6)


def test_the_limit_counts_the_segments_a_split_ramp_adds():
	rows = [RateRow(0.5, 4.0), RateRow(2.5, 8.0, ramp=True)]
	with pytest.raises(JobCompileError, match="3 segments, the board holds 2"):
		compile_rate_job(PLA, curve(BENT), rows, max_segments=2)


# --- Duty jobs (bench) ---------------------------------------------------------------------------

def test_a_duty_job_needs_no_calibration():
	job = compile_duty_job([DutyRow(64, 5.0), DutyRow(128, 2.5, ramp=True)])
	assert job.segments == (Segment(64, 5_000, duty=True), Segment(128, 2_500, ramp=True, duty=True))
	assert job.total_ms == 7_500
	assert (job.expected_grams, job.material, job.curve_hash) == (None, None, None)


@pytest.mark.parametrize("duty", [-1, 256, float("nan"), True])
def test_a_duty_must_be_0_to_255(duty):
	with pytest.raises(JobCompileError, match="row 1: duty"):
		compile_duty_job([DutyRow(duty, 1.0)])


def test_a_duty_job_has_the_same_time_and_count_rules():
	with pytest.raises(JobCompileError, match="row 2: .* s"):
		compile_duty_job([DutyRow(10, 1.0), DutyRow(10, 0.001)])
	with pytest.raises(JobCompileError, match="3 segments, the board holds 2"):
		compile_duty_job([DutyRow(10, 1.0)] * 3, max_segments=2)

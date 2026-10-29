import csv
import math

import pytest

from polydriver.analysis.step_response import (
	StepError, StepMetrics, parse_steps, step_metrics, write_step_metrics,
)


def first_order(from_hz, to_hz, tau_s, *, step_s=1.0, end_s=6.0, dt_s=0.01):
	times, speeds = [], []
	t = 0.0
	while t <= end_s + 1e-9:
		times.append(t)
		speeds.append(from_hz if t < step_s else to_hz + (from_hz - to_hz) * math.exp(-(t - step_s) / tau_s))
		t += dt_s
	return times, speeds


def test_a_first_order_step_has_its_textbook_rise_and_settling():
	times, speeds = first_order(0.0, 10.0, 0.5)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0, band_fraction=0.02)
	assert m.rise_time_s == pytest.approx(0.5 * math.log(9), abs=0.02)
	assert m.settling_time_s == pytest.approx(0.5 * math.log(50), abs=0.02)
	assert m.overshoot_percent == pytest.approx(0.0)
	assert m.steady_error_hz == pytest.approx(0.0, abs=0.01)


def test_a_downward_step_is_measured_the_same_way():
	times, speeds = first_order(20.0, 10.0, 0.5)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=20.0, to_hz=10.0, band_fraction=0.02)
	assert m.rise_time_s == pytest.approx(0.5 * math.log(9), abs=0.02)
	assert m.overshoot_percent == pytest.approx(0.0)


def ringing(from_hz, to_hz, *, step_s=1.0, end_s=8.0, dt_s=0.01, zeta=0.3, wn=6.0):
	times, speeds = [], []
	wd = wn * math.sqrt(1 - zeta ** 2)
	phi = math.acos(zeta)
	t = 0.0
	while t <= end_s + 1e-9:
		times.append(t)
		if t < step_s:
			speeds.append(from_hz)
		else:
			tt = t - step_s
			y = 1 - math.exp(-zeta * wn * tt) * math.sin(wd * tt + phi) / math.sqrt(1 - zeta ** 2)
			speeds.append(from_hz + (to_hz - from_hz) * y)
		t += dt_s
	return times, speeds


def test_overshoot_matches_the_damping():
	times, speeds = ringing(0.0, 10.0, zeta=0.3)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0)
	expected = 100 * math.exp(-0.3 * math.pi / math.sqrt(1 - 0.09))
	assert m.overshoot_percent == pytest.approx(expected, abs=0.5)


def test_overshoot_of_a_downward_step_is_below_the_target():
	times, speeds = ringing(20.0, 10.0, zeta=0.3)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=20.0, to_hz=10.0)
	assert m.overshoot_percent == pytest.approx(37.2, abs=0.5)


def test_settling_counts_from_the_last_time_it_left_the_band():
	times, speeds = ringing(0.0, 10.0, zeta=0.3)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0, band_fraction=0.05)
	inside_after = [t for t, v in zip(times, speeds) if t >= 1.0 + m.settling_time_s]
	assert all(abs(v - 10.0) <= 0.5 for t, v in zip(times, speeds) if t in inside_after)
	assert m.settling_time_s > m.rise_time_s


def test_a_step_that_never_settles_has_no_settling_time():
	times, speeds = first_order(0.0, 10.0, 0.5)
	speeds = [v + (0.8 if i % 2 else -0.8) for i, v in enumerate(speeds)]
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0, band_fraction=0.05)
	assert m.settling_time_s is None


def test_a_step_that_never_gets_there_has_no_rise_time():
	times, speeds = first_order(0.0, 5.0, 0.5)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0)
	assert m.rise_time_s is None
	assert m.settling_time_s is None
	assert m.steady_error_hz == pytest.approx(-5.0, abs=0.01)
	assert m.steady_error_percent == pytest.approx(-50.0, abs=0.1)


def test_steady_error_is_the_mean_over_the_last_window():
	times, speeds = first_order(0.0, 10.0, 0.2)
	speeds = [v - 0.3 if t > 4 else v for t, v in zip(times, speeds)]
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0, steady_window_s=1.5)
	assert m.steady_error_hz == pytest.approx(-0.3, abs=1e-6)


def test_a_step_to_zero_has_no_error_percent():
	times, speeds = first_order(10.0, 0.0, 0.3)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=10.0, to_hz=0.0)
	assert m.steady_error_percent is None


def test_samples_before_the_step_are_ignored():
	times, speeds = first_order(0.0, 10.0, 0.5)
	speeds[10] = 50.0
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0)
	assert m.overshoot_percent == pytest.approx(0.0)


def test_rise_time_is_interpolated_between_polls():
	times = [0.0, 1.0, 1.1, 1.2, 1.3, 2.0, 3.0, 4.0]
	speeds = [0.0, 0.0, 0.5, 5.0, 9.5, 10.0, 10.0, 10.0]
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0)
	t10 = 1.1 + 0.1 * (1.0 - 0.5) / (5.0 - 0.5)
	t90 = 1.2 + 0.1 * (9.0 - 5.0) / (9.5 - 5.0)
	assert m.rise_time_s == pytest.approx(t90 - t10)


def test_a_step_of_nothing_is_refused():
	with pytest.raises(StepError, match="same speed"):
		step_metrics([0, 1, 2, 3], [5, 5, 5, 5], step_s=0.0, from_hz=5.0, to_hz=5.0)


def test_too_few_polls_after_the_step_is_refused():
	with pytest.raises(StepError, match="3 polls"):
		step_metrics([0.0, 1.0, 1.1], [0.0, 0.0, 5.0], step_s=1.0, from_hz=0.0, to_hz=10.0)


def test_unequal_lists_are_refused():
	with pytest.raises(StepError, match="times and speeds"):
		step_metrics([0.0, 1.0], [0.0], step_s=0.0, from_hz=0.0, to_hz=1.0)


def test_a_band_outside_zero_to_one_is_refused():
	times, speeds = first_order(0.0, 10.0, 0.5)
	with pytest.raises(StepError, match="band"):
		step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0, band_fraction=0.0)


# --- Steps as typed --------------------------------------------------------------------------------

def test_steps_are_read_as_typed():
	assert parse_steps("0, 10, 20, 10") == [0.0, 10.0, 20.0, 10.0]
	assert parse_steps("5 15;7.5") == [5.0, 15.0, 7.5]


def test_steps_need_two_speeds():
	with pytest.raises(StepError, match="at least two"):
		parse_steps("10")


def test_steps_refuse_words_and_negative_speeds():
	with pytest.raises(StepError, match="'ten' is not a speed"):
		parse_steps("0, ten")
	with pytest.raises(StepError, match="-5 Hz"):
		parse_steps("0, -5")


def test_two_equal_steps_in_a_row_are_refused():
	with pytest.raises(StepError, match="10 Hz twice in a row"):
		parse_steps("0, 10, 10")


# --- Saving ----------------------------------------------------------------------------------------

def test_metrics_are_saved_one_row_per_step(tmp_path):
	path = tmp_path / "run-steps.csv"
	metrics = [
		StepMetrics(0.0, 10.0, 0.4, 5.0, 1.2, -0.05, -0.5),
		StepMetrics(10.0, 0.0, None, 0.0, None, 0.01, None),
	]
	write_step_metrics(path, metrics, note="kp 0.5 ki 2")
	with path.open(encoding="utf-8") as file:
		rows = list(csv.reader(file))
	assert rows[0] == ["# kp 0.5 ki 2"]
	assert rows[1] == ["from_hz", "to_hz", "rise_time_s", "overshoot_percent", "settling_time_s", "steady_error_hz",
						"steady_error_percent"]
	assert rows[2] == ["0.0", "10.0", "0.4", "5.0", "1.2", "-0.05", "-0.5"]
	assert rows[3] == ["10.0", "0.0", "", "0.0", "", "0.01", ""]


def test_the_band_is_a_share_of_the_step_not_of_the_target():
	times, speeds = first_order(5.0, 10.0, 0.5)
	m = step_metrics(times, speeds, step_s=1.0, from_hz=5.0, to_hz=10.0, band_fraction=0.02)
	assert m.settling_time_s == pytest.approx(0.5 * math.log(50), abs=0.02)


def test_a_step_inside_the_band_from_the_first_poll_settles_at_that_poll():
	times = [0.0, 1.05, 1.2, 1.4, 1.6]
	speeds = [0.0, 10.0, 10.0, 10.1, 10.0]
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0)
	assert m.settling_time_s == pytest.approx(0.05)


def test_settling_is_interpolated_between_polls():
	times = [1.0, 1.1, 1.2, 1.3, 1.4]
	speeds = [0.0, 8.0, 9.0, 10.0, 10.0]
	m = step_metrics(times, speeds, step_s=1.0, from_hz=0.0, to_hz=10.0, band_fraction=0.05)
	assert m.settling_time_s == pytest.approx(0.2 + 0.1 * (1.0 - 0.5) / (1.0 - 0.0))

import pytest

from polydriver.calibration import (
	CalibrationError, CalibrationStore, NonMonotonicError, NotEnoughPointsError, OutOfRangeError, lowest_producing_speed,
	rate_for_speed, speed_for_rate,
)


def curve(*rows, dispenser="B", material="PLA"):
	"""rows of (speed_hz, rate_g_s), each as one 10 s weighing."""
	store = CalibrationStore()
	for speed_hz, rate_g_s in rows:
		store.add_measurement(dispenser, material, speed_hz, rate_g_s * 10.0, 10.0)
	return store.curve(dispenser, material)


LINE = curve((10.0, 1.0), (20.0, 2.0), (40.0, 3.0))


# --- rate_for_speed -------------------------------------------------------------------------

def test_rate_at_a_measured_speed_is_its_mean():
	assert rate_for_speed(LINE, 20.0) == pytest.approx(2.0)


def test_rate_between_two_speeds_is_interpolated_on_the_right_segment():
	assert rate_for_speed(LINE, 15.0) == pytest.approx(1.5)
	assert rate_for_speed(LINE, 30.0) == pytest.approx(2.5)


def test_both_ends_are_inside_the_range():
	assert rate_for_speed(LINE, 10.0) == pytest.approx(1.0)
	assert rate_for_speed(LINE, 40.0) == pytest.approx(3.0)


@pytest.mark.parametrize("speed_hz", [9.99, 40.01, 0.0])
def test_a_speed_outside_the_measured_range_is_refused(speed_hz):
	with pytest.raises(OutOfRangeError, match=r"B / PLA.*10 to 40 Hz"):
		rate_for_speed(LINE, speed_hz)


def test_repeats_are_averaged_before_interpolating():
	store = CalibrationStore()
	store.add_measurement("B", "PLA", 10.0, 10.0, 10.0)
	store.add_measurement("B", "PLA", 10.0, 12.0, 10.0)
	store.add_measurement("B", "PLA", 20.0, 21.0, 10.0)
	assert rate_for_speed(store.curve("B", "PLA"), 10.0) == pytest.approx(1.1)


def test_rate_for_speed_works_on_non_monotonic_data():
	bumpy = curve((10.0, 0.0), (20.0, 1.5), (30.0, 1.4), (40.0, 2.5))
	assert rate_for_speed(bumpy, 25.0) == pytest.approx(1.45)


# --- speed_for_rate ------------------------------------------------------------------------

def test_speed_for_rate_is_the_inverse():
	assert speed_for_rate(LINE, 1.5) == pytest.approx(15.0)
	assert speed_for_rate(LINE, 2.5) == pytest.approx(30.0)
	assert speed_for_rate(LINE, rate_for_speed(LINE, 33.0)) == pytest.approx(33.0)


@pytest.mark.parametrize("rate_g_s", [0.99, 3.01])
def test_a_rate_outside_the_measured_range_is_refused(rate_g_s):
	with pytest.raises(OutOfRangeError, match=r"B / PLA.*1 to 3 g/s"):
		speed_for_rate(LINE, rate_g_s)


def test_a_rate_that_falls_with_speed_is_refused_naming_the_two_speeds():
	bumpy = curve((10.0, 0.0), (20.0, 1.5), (30.0, 1.4), (40.0, 2.5))
	with pytest.raises(NonMonotonicError, match=r"B / PLA.*1.5 g/s at 20 Hz.*1.4 g/s at 30 Hz"):
		speed_for_rate(bumpy, 2.0)


def test_a_falling_rate_is_refused_even_for_a_rate_away_from_the_dip():
	bumpy = curve((10.0, 0.0), (20.0, 1.5), (30.0, 1.4), (40.0, 2.5))
	with pytest.raises(NonMonotonicError):
		speed_for_rate(bumpy, 0.5)


def test_several_equal_rates_give_the_lowest_speed():
	flat = curve((10.0, 0.0), (20.0, 0.0), (30.0, 0.0), (40.0, 2.0))
	assert speed_for_rate(flat, 0.0) == pytest.approx(10.0)


def test_a_plateau_in_the_middle_gives_its_lowest_speed():
	plateau = curve((10.0, 1.0), (20.0, 1.5), (30.0, 1.5), (40.0, 2.5))
	assert speed_for_rate(plateau, 1.5) == pytest.approx(20.0)
	assert speed_for_rate(plateau, 2.0) == pytest.approx(35.0)


def test_above_a_dead_band_the_rate_is_interpolated_from_its_last_zero():
	dead = curve((10.0, 0.0), (20.0, 0.0), (30.0, 1.0))
	assert speed_for_rate(dead, 0.5) == pytest.approx(25.0)


# --- Too little data, bad input ------------------------------------------------------------------

def test_one_speed_is_not_enough():
	store = CalibrationStore()
	store.add_measurement("B", "PLA", 20.0, 15.0, 10.0)
	store.add_measurement("B", "PLA", 20.0, 16.0, 10.0)
	single = store.curve("B", "PLA")
	with pytest.raises(NotEnoughPointsError, match="B / PLA.*2 different speeds.*has 1"):
		rate_for_speed(single, 20.0)
	with pytest.raises(NotEnoughPointsError):
		speed_for_rate(single, 1.5)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_a_value_that_is_not_a_finite_number_is_refused(value):
	with pytest.raises(CalibrationError, match="number"):
		rate_for_speed(LINE, value)
	with pytest.raises(CalibrationError, match="number"):
		speed_for_rate(LINE, value)


def test_the_errors_are_calibration_errors():
	assert issubclass(OutOfRangeError, CalibrationError)
	assert issubclass(NonMonotonicError, CalibrationError)
	assert issubclass(NotEnoughPointsError, CalibrationError)


# --- lowest_producing_speed ------------------------------------------------------------------------

def test_the_lowest_producing_speed_is_the_first_speed_that_gives_material():
	dead = curve((10.0, 0.0), (20.0, 0.0), (30.0, 1.0), (40.0, 2.0))
	assert lowest_producing_speed(dead) == 30.0


def test_no_producing_speed_is_none():
	assert lowest_producing_speed(curve((10.0, 0.0), (20.0, 0.0))) is None


def test_the_lowest_producing_speed_needs_only_one_point():
	assert lowest_producing_speed(curve((10.0, 1.0))) == 10.0

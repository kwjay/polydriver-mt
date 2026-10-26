import pytest

from polydriver.calibration import (
	CalibrationError,
	CalibrationStore,
	NotEnoughPointsError,
	fit_report,
	format_fit_report,
)


def curve(*rows, dispenser="B", material="PLA"):
	"""rows of (speed_hz, rate_g_s), each one 10 s weighing."""
	store = CalibrationStore()
	for speed_hz, rate_g_s in rows:
		store.add_measurement(dispenser, material, speed_hz, rate_g_s * 10.0, 10.0)
	return store.curve(dispenser, material)


# --- The line ---------------------------------------------------------------------------

def test_a_known_straight_line_is_found_exactly():
	report = fit_report(curve((10.0, 0.5), (20.0, 1.0), (30.0, 1.5), (40.0, 2.0)))
	assert report.slope_g_s_per_hz == pytest.approx(0.05)
	assert report.intercept_g_s == pytest.approx(0.0, abs=1e-12)
	assert report.r_squared == pytest.approx(1.0)
	assert all(r.residual_g_s == pytest.approx(0.0, abs=1e-12) for r in report.residuals)


def test_an_intercept_and_where_the_line_reaches_zero():
	report = fit_report(curve((20.0, 1.0), (30.0, 2.0), (40.0, 3.0)))
	assert report.slope_g_s_per_hz == pytest.approx(0.1)
	assert report.intercept_g_s == pytest.approx(-1.0)
	assert report.zero_rate_speed_hz == pytest.approx(10.0)


def test_scatter_lowers_r_squared_and_shows_in_the_residuals():
	report = fit_report(curve((10.0, 1.0), (20.0, 2.4), (30.0, 2.6), (40.0, 4.0)))
	assert 0.9 < report.r_squared < 1.0
	residual = {r.speed_hz: r.residual_g_s for r in report.residuals}
	assert residual[20.0] > 0 > residual[30.0]
	assert sum(residual.values()) == pytest.approx(0.0, abs=1e-12)


def test_the_fit_uses_every_repeat_not_only_the_means():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 10.0), (20.0, 17.0), (20.0, 20.0), (20.0, 23.0), (30.0, 30.0)):
		store.add_measurement("B", "PLA", speed_hz, grams, 10.0)
	report = fit_report(store.curve("B", "PLA"))
	assert report.measurements_used == 5
	assert report.slope_g_s_per_hz == pytest.approx(0.1)
	assert report.r_squared < 0.99, "the means lie on a line; only the scatter between repeats lowers R²"
	[at_20] = [r for r in report.residuals if r.speed_hz == 20.0]
	assert at_20.rate_g_s == pytest.approx(2.0)
	assert at_20.residual_g_s == pytest.approx(0.0, abs=1e-12)
	assert at_20.repeats == 3


# --- Dead band ------------------------------------------------------------------------------

DEAD_BAND = ((10.0, 0.0), (20.0, 0.0), (30.0, 1.0), (40.0, 2.0), (50.0, 3.0))


def test_the_dead_band_is_left_out_of_the_line_by_default():
	report = fit_report(curve(*DEAD_BAND))
	assert (report.from_speed_hz, report.to_speed_hz) == (30.0, 50.0)
	assert report.slope_g_s_per_hz == pytest.approx(0.1)
	assert report.r_squared == pytest.approx(1.0)
	assert [r.speed_hz for r in report.residuals] == [30.0, 40.0, 50.0]


def test_the_dead_band_can_be_fitted_too():
	report = fit_report(curve(*DEAD_BAND), include_dead_band=True)
	assert report.from_speed_hz == 10.0
	assert report.r_squared < 1.0
	assert len(report.residuals) == 5


# --- Noisy points -------------------------------------------------------------------------

def test_a_point_whose_repeats_disagree_by_more_than_10_percent_is_flagged():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 10.0), (10.0, 10.5), (20.0, 20.0), (20.0, 23.0), (30.0, 30.0)):
		store.add_measurement("B", "PLA", speed_hz, grams, 10.0)
	report = fit_report(store.curve("B", "PLA"))
	assert [p.speed_hz for p in report.noisy_points] == [20.0]
	assert report.noisy_points[0].spread_g_s == pytest.approx(0.3)


def test_the_limit_can_be_changed():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 10.0), (10.0, 10.5), (20.0, 20.0), (30.0, 30.0)):
		store.add_measurement("B", "PLA", speed_hz, grams, 10.0)
	assert fit_report(store.curve("B", "PLA")).noisy_points == ()
	assert [p.speed_hz for p in fit_report(store.curve("B", "PLA"), spread_limit=0.04).noisy_points] == [10.0]


@pytest.mark.parametrize("limit", [-0.1, float("nan"), float("inf")])
def test_a_nonsense_limit_is_refused(limit):
	with pytest.raises(CalibrationError, match="spread limit"):
		fit_report(curve((10.0, 1.0), (20.0, 2.0)), spread_limit=limit)


def test_a_single_measurement_is_never_noisy():
	assert fit_report(curve((10.0, 1.0), (20.0, 2.0))).noisy_points == ()


def test_noise_inside_the_dead_band_is_flagged_too():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 0.0), (10.0, 0.5), (20.0, 10.0), (30.0, 20.0)):
		store.add_measurement("B", "PLA", speed_hz, grams, 10.0)
	report = fit_report(store.curve("B", "PLA"))
	assert [p.speed_hz for p in report.noisy_points] == [10.0]


def test_a_dead_band_of_steady_zeros_is_not_noisy():
	assert fit_report(curve(*DEAD_BAND, *DEAD_BAND)).noisy_points == ()


# --- Not enough to fit ------------------------------------------------------------------------

def test_a_line_needs_two_speeds_in_the_fitted_range():
	with pytest.raises(NotEnoughPointsError, match="B / PLA.*2 different speeds from 30 Hz up, there is 1"):
		fit_report(curve((10.0, 0.0), (20.0, 0.0), (30.0, 1.0)))


def test_nothing_produced_cannot_be_fitted_without_the_dead_band():
	with pytest.raises(NotEnoughPointsError, match="no speed gave any material"):
		fit_report(curve((10.0, 0.0), (20.0, 0.0)))


def test_a_flat_line_has_no_r_squared_and_no_zero_crossing():
	report = fit_report(curve((10.0, 1.0), (20.0, 1.0), (30.0, 1.0)))
	assert report.slope_g_s_per_hz == pytest.approx(0.0, abs=1e-12)
	assert report.r_squared is None
	assert report.zero_rate_speed_hz is None


def test_the_report_names_its_curve():
	report = fit_report(curve((10.0, 1.0), (20.0, 2.0), material="TPU", dispenser="A"))
	assert (report.dispenser, report.material) == ("A", "TPU")
	assert report.curve_hash


# --- Text -------------------------------------------------------------------------------------

def test_the_text_lists_the_line_and_the_noisy_points():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 0.0), (20.0, 10.0), (30.0, 20.0), (30.0, 24.0), (40.0, 30.0)):
		store.add_measurement("B", "PLA", speed_hz, grams, 10.0)
	text = "\n".join(format_fit_report(fit_report(store.curve("B", "PLA"))))
	assert "B / PLA" in text
	assert "fitted 20 to 40 Hz, 4 measurements" in text
	assert "R² = 0.9" in text
	assert "line reaches 0 g/s at" in text
	assert "more than 10% of their mean at:" in text
	assert "30 Hz: 2.2 g/s, spread 0.4 g/s (18%) over 2 repeats" in text


def test_the_text_says_when_nothing_is_noisy_and_when_r_squared_is_undefined():
	text = "\n".join(format_fit_report(fit_report(curve((10.0, 1.0), (20.0, 1.0)))))
	assert "R² = n/a" in text
	assert "reaches 0 g/s" not in text
	assert "no point's repeats disagree by more than 10%" in text

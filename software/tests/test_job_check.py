import re
from dataclasses import replace
from pathlib import Path

import pytest

from polydriver.calibration import CalibrationStore, speed_for_rate
from polydriver.jobs import MIN_MEASURABLE_SPEED_HZ, CalibrationRef, check_job, load_job
from polydriver.materials import Kind, Material, MaterialRegistry

EXAMPLE = Path(__file__).parent / "data" / "gradient_job_example.json"
CAPTURE_MATH = Path(__file__).resolve().parents[2] / "firmware" / "polydriver" / "lib" / "core" / "CaptureMath.h"

# Polymer 1.485 -> 1.425 g/s and colour 0.015 -> 0.075 g/s over the example job.
PP_POINTS = ((10.0, 0.5), (20.0, 1.0), (30.0, 1.5), (40.0, 2.0))
MB_POINTS = ((2.0, 0.01), (5.0, 0.03), (10.0, 0.06), (15.0, 0.09))


def registry(pp=None, mb=None):
	materials = MaterialRegistry()
	materials.put(pp or Material("PP", Kind.POLYMER, dispenser="B"))
	materials.put(mb or Material("MB-red", Kind.MASTERBATCH, dispenser="A"))
	return materials


def store(pp_points=PP_POINTS, mb_points=MB_POINTS, mb_repeats=((0.0, 0.0),)):
	"""mb_repeats: per point, extra repeats as offsets in g/s."""
	calibrations = CalibrationStore()
	for speed_hz, rate_g_s in pp_points:
		calibrations.add_measurement("B", "PP", speed_hz, rate_g_s * 10, 10.0)
	for index, (speed_hz, rate_g_s) in enumerate(mb_points):
		calibrations.add_measurement("A", "MB-red", speed_hz, rate_g_s * 100, 100.0)
		for offset in mb_repeats[min(index, len(mb_repeats) - 1)]:
			calibrations.add_measurement("A", "MB-red", speed_hz, (rate_g_s + offset) * 100, 100.0)
	return calibrations


def current(job, calibrations):
	"""The example job, saved against the calibrations it is checked with."""
	return replace(job, polymer=CalibrationRef.of(calibrations.curve("B", "PP")),
				colour=CalibrationRef.of(calibrations.curve("A", "MB-red")))


def checked(calibrations=None, materials=None, step_s=1.0, **profile):
	calibrations = calibrations or store()
	job = current(load_job(EXAMPLE), calibrations)
	if profile:
		job = replace(job, **profile)
	return check_job(job, materials or registry(), calibrations, step_s=step_s)


def flagged(result, role, code):
	return [p.t_s for p in result.points if any(i.code == code for i in getattr(p, role).issues)]


# --- A job that fits ------------------------------------------------------------------------------

def test_the_example_job_fits_the_example_calibrations():
	result = checked()
	assert result.ok
	assert (result.problems, result.warnings) == ((), ())
	assert result.report() == ["no problems: every point is inside both calibrations"]


def test_every_point_carries_both_rates_and_speeds_for_the_preview():
	result = checked()
	assert [p.t_s for p in result.points][:3] == [0.0, 1.0, 2.0] and result.points[-1].t_s == 600.0
	first = result.points[0]
	assert (first.phi, first.colour.rate_g_s, first.polymer.rate_g_s) == (0.01, pytest.approx(0.015), pytest.approx(1.485))
	assert first.colour.speed_hz == pytest.approx(speed_for_rate(store().curve("A", "MB-red"), 0.015))
	assert first.polymer.speed_hz == pytest.approx(29.7)


def test_the_hold_boundaries_are_always_points():
	assert [p.t_s for p in checked(step_s=100.0).points] == [0, 30, 100, 200, 300, 400, 500, 570, 600]


def test_the_example_as_written_warns_that_both_calibrations_changed():
	result = check_job(load_job(EXAMPLE), registry(), store())
	assert result.ok
	assert result.warnings == (
		"polymer: the calibration B / PP has changed since the job was saved",
		"colour: the calibration A / MB-red has changed since the job was saved",
	)


# --- Point by point -------------------------------------------------------------------------------

def test_colour_below_its_calibration_is_reported_point_by_point():
	result = checked(store(mb_points=((3.0, 0.02), (5.0, 0.03), (10.0, 0.06), (15.0, 0.09))))
	assert not result.ok
	times = flagged(result, "colour", "below_calibration")
	assert times == [float(t) for t in range(len(times))] and 74 <= times[-1] <= 75
	assert all(p.colour.speed_hz is None for p in result.points if p.t_s in times)	assert flagged(result, "colour", "below_reliable_flow") == [], "the error says it already"
	assert re.fullmatch(
		r"error: colour, 0 to 7[45] s \(7[56] points\): below the calibrated 0\.02 to 0\.09 g/s of A / MB-red; "
		r"lowest 0\.015 g/s at 0 s", result.report()[0])


def test_polymer_above_its_calibration():
	result = checked(store(pp_points=((10.0, 0.5), (29.0, 1.45))))
	times = flagged(result, "polymer", "above_calibration")
	assert times[0] == 0.0 and not result.ok
	assert "above the calibrated 0.5 to 1.45 g/s of B / PP; highest 1.485 g/s at 0 s" in result.report()[0]


def test_a_flow_outside_the_material_window():
	pp = Material("PP", Kind.POLYMER, dispenser="B", flow_min_g_s=1.0, flow_max_g_s=1.46)
	result = checked(materials=registry(pp=pp))
	assert flagged(result, "polymer", "above_flow_window")[0] == 0.0
	assert "above PP's flow window 1 to 1.46 g/s; highest 1.485 g/s at 0 s" in result.report()[0]
	mb = Material("MB-red", Kind.MASTERBATCH, dispenser="A", flow_min_g_s=0.02, flow_max_g_s=0.1)
	assert flagged(checked(materials=registry(mb=mb)), "colour", "below_flow_window")[0] == 0.0


def test_a_colour_share_of_0_stops_the_colour_screw_and_is_not_checked():
	result = checked(phi_start=0.0)
	stopped = [p for p in result.points if p.t_s <= 30]
	assert all(p.colour.rate_g_s == 0 and p.colour.speed_hz == 0.0 and not p.colour.issues for p in stopped)
	assert flagged(result, "colour", "below_calibration")[0] == 31.0


def test_a_single_bad_point_is_reported_as_one_point():
	result = checked(store(pp_points=((10.0, 0.5), (29.6, 1.48))), step_s=100.0, hold_start_s=0.0)
	assert result.report()[0] == "error: polymer, at 0 s: 1.485 g/s is above the calibrated 0.5 to 1.48 g/s of B / PP"


# --- Warnings: the colour channel at the bottom of its range ------------------------------------------------

def test_colour_below_its_lowest_reliable_flow_is_a_warning():
	# The 2 Hz repeats disagree (0.01 and 0.02 g/s); 5 Hz is the lowest point whose repeats agree.
	result = checked(store(mb_repeats=((0.01,), (0.0,))))
	assert result.ok
	times = flagged(result, "colour", "below_reliable_flow")
	assert times[0] == 0.0 and 164 <= times[-1] <= 165
	assert any("below the lowest reliable flow 0.03 g/s of A / MB-red" in line for line in result.report())


def test_a_dead_band_is_never_the_lowest_reliable_flow():
	# 1 Hz gives nothing, steadily; 2 Hz disagrees; 5 Hz is the lowest reliable point.
	points = ((1.0, 0.0), (2.0, 0.01), (5.0, 0.03), (10.0, 0.06), (15.0, 0.09))
	result = checked(store(mb_points=points, mb_repeats=((0.0,), (0.01,), (0.0,))))
	assert flagged(result, "colour", "below_reliable_flow")[0] == 0.0


def test_without_repeats_the_reliable_flow_is_unknown():
	result = checked(store(mb_repeats=((),)))
	assert result.ok
	assert result.warnings == (
		"colour: no point of the calibration A / MB-red has repeats that agree within 10 %, so its lowest reliable "
		"flow is unknown; repeat the low-speed measurements",
	)


def test_the_polymer_channel_needs_no_repeats():
	assert checked(store(mb_repeats=((0.0,),))).warnings == ()


def test_a_speed_below_what_the_encoder_measures_is_a_warning():
	result = checked(store(mb_points=((1.0, 0.01), (2.0, 0.02), (10.0, 0.1))))
	times = flagged(result, "colour", "below_measurable_speed")
	assert result.ok and times[0] == 0.0 and 65 <= times[-1] <= 67
	assert any("below 1.907 Hz, the lowest speed the encoder measures" in line for line in result.report())


def test_the_encoder_floor_is_computed_from_the_firmware_constants():
	header = CAPTURE_MATH.read_text(encoding="utf-8")
	value = {name: float(re.search(rf"\b{name} = ([0-9.]+)", header).group(1))
			for name in ("CLOCK_HZ", "PRESCALER", "EDGE_TIMEOUT_OVERFLOWS", "TIMER_PERIOD_TICKS")}
	floor = value["CLOCK_HZ"] / (value["PRESCALER"] * value["TIMER_PERIOD_TICKS"] * value["EDGE_TIMEOUT_OVERFLOWS"])
	assert MIN_MEASURABLE_SPEED_HZ == pytest.approx(floor, rel=1e-12)
	assert MIN_MEASURABLE_SPEED_HZ == pytest.approx(1.907, abs=1e-3)


# --- Job-wide problems --------------------------------------------------------------------------

def test_a_material_that_is_not_registered():
	materials = MaterialRegistry()
	materials.put(Material("PP", Kind.POLYMER, dispenser="B"))
	result = checked(materials=materials)
	assert not result.ok
	assert result.problems == ("colour: no material 'MB-red'; there are: PP",)


def test_a_material_of_the_wrong_kind_for_its_board():
	result = checked(materials=registry(mb=Material("MB-red", Kind.POLYMER)))
	assert result.problems == ("colour: MB-red is a polymer; the colour board takes a masterbatch",)


def test_a_missing_calibration_blocks_its_channel():
	calibrations = store()
	job = current(load_job(EXAMPLE), calibrations)
	calibrations.remove_measurement("B", "PP", 0)
	for _ in range(3):
		calibrations.remove_measurement("B", "PP", 0)
	result = check_job(job, registry(), calibrations)
	assert result.problems == ("polymer: no calibration for dispenser B, material PP; there are: A / MB-red",)
	assert all(p.polymer.speed_hz is None and not p.polymer.issues for p in result.points)
	assert result.report()[0] == "error: " + result.problems[0]


def test_a_falling_calibration_blocks_its_channel():
	result = checked(store(mb_points=((2.0, 0.01), (5.0, 0.04), (10.0, 0.03), (15.0, 0.09))))
	assert len(result.problems) == 1 and result.problems[0].startswith("colour: A / MB-red: the rate falls")
	assert all(p.colour.speed_hz is None for p in result.points)


def test_the_report_lists_errors_before_warnings():
	materials = MaterialRegistry()
	materials.put(Material("PP", Kind.POLYMER, dispenser="B"))
	result = check_job(load_job(EXAMPLE), materials, store(mb_repeats=((0.01,), (0.0,))))
	kinds = [line.split(":")[0] for line in result.report()]
	assert kinds == sorted(kinds) and kinds[0] == "error" and kinds[-1] == "warning"

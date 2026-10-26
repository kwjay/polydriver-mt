import math
import random

import pytest

from polydriver.jobs import (
	CURVE_NAMES,
	CurveError,
	ExponentialCurve,
	LinearCurve,
	SCurve,
	StepsCurve,
	TableCurve,
	make_curve,
)

GRID = [i / 2000 for i in range(2001)]


def every_curve():
	rng = random.Random(7)
	curves = [LinearCurve(), ExponentialCurve(), SCurve(), StepsCurve(), TableCurve([(0, 0), (0.3, 0.6), (1, 1)])]
	curves += [ExponentialCurve(rng.choice([-1, 1]) * rng.uniform(0.01, 50)) for _ in range(20)]
	curves += [ExponentialCurve(k) for k in (-50.0, -1e-6, 1e-6, 50.0)]
	curves += [SCurve(rng.uniform(0.01, 50)) for _ in range(10)] + [SCurve(1e-6), SCurve(50.0)]
	curves += [StepsCurve(n) for n in (2, 3, 7, 100)]
	for _ in range(10):
		xs = sorted(rng.uniform(0, 1) for _ in range(rng.randint(0, 6)))
		ps = sorted(rng.uniform(0, 1) for _ in xs)
		curves.append(TableCurve([(0, 0), *zip(xs, ps), (1, 1)]) if len(set(xs)) == len(xs) else LinearCurve())
	return curves


CURVES = every_curve()


# --- Properties every curve has ------------------------------------------------------------------

@pytest.mark.parametrize("curve", CURVES, ids=repr)
def test_the_ends_are_exact(curve):
	assert curve.progress(0.0) == 0.0
	assert curve.progress(1.0) == 1.0


@pytest.mark.parametrize("curve", CURVES, ids=repr)
def test_progress_never_falls_and_stays_between_0_and_1(curve):
	values = [curve.progress(x) for x in GRID]
	assert all(0.0 <= v <= 1.0 for v in values)
	assert all(b >= a for a, b in zip(values, values[1:]))


@pytest.mark.parametrize("curve", CURVES, ids=repr)
def test_the_mean_is_the_area_under_the_curve(curve):
	steps = 40_000
	area = sum(curve.progress((i + 0.5) / steps) for i in range(steps)) / steps
	assert curve.mean() == pytest.approx(area, abs=5e-5)


@pytest.mark.parametrize("curve", CURVES, ids=repr)
def test_a_curve_can_be_rebuilt_from_its_spec(curve):
	name, params, table = curve.spec()
	rebuilt = make_curve(name, params, table)
	assert [rebuilt.progress(x) for x in GRID[::50]] == [curve.progress(x) for x in GRID[::50]]


@pytest.mark.parametrize("curve", CURVES[:5], ids=repr)
@pytest.mark.parametrize("x", [-0.001, 1.001, float("nan")])
def test_progress_outside_0_to_1_is_refused(curve, x):
	with pytest.raises(CurveError, match="0 to 1"):
		curve.progress(x)


def test_curves_are_equal_when_their_specs_are():
	assert SCurve(8.0) == SCurve(8) == make_curve("s-curve", {"k": 8.0})
	assert SCurve(8.0) != SCurve(9.0) and LinearCurve() != SCurve(8.0) and LinearCurve() != "linear"
	assert TableCurve([(0, 0), (1, 1)]) == TableCurve([[0.0, 0.0], [1.0, 1.0]]) != LinearCurve()
	assert len({SCurve(8.0), SCurve(8.0), LinearCurve()}) == 2


# --- Each shape ------------------------------------------------------------------------------------

def test_linear():
	assert LinearCurve().progress(0.25) == 0.25
	assert LinearCurve().mean() == 0.5


def test_exponential_starts_slow_for_k_above_0_and_fast_below():
	assert ExponentialCurve(3.0).progress(0.5) == pytest.approx(math.expm1(1.5) / math.expm1(3.0))
	assert ExponentialCurve(3.0).progress(0.5) < 0.5 < ExponentialCurve(-3.0).progress(0.5)


def test_a_small_k_is_nearly_linear():
	assert ExponentialCurve(1e-6).progress(0.3) == pytest.approx(0.3, abs=1e-6)
	assert SCurve(1e-6).progress(0.3) == pytest.approx(0.3, abs=1e-6)


def test_the_s_curve_is_symmetric_and_steeper_with_k():
	for x in (0.1, 0.3, 0.45):
		assert SCurve(8.0).progress(1 - x) == pytest.approx(1 - SCurve(8.0).progress(x))
	assert SCurve(20.0).progress(0.4) < SCurve(5.0).progress(0.4) < 0.4


def test_steps_hold_equal_levels_for_equal_times():
	steps = StepsCurve(5)
	assert [steps.progress(x) for x in (0.0, 0.19, 0.2, 0.5, 0.79, 0.8, 1.0)] == [0, 0, 0.25, 0.5, 0.75, 1, 1]


def test_a_table_is_straight_lines_between_its_points():
	table = TableCurve([(0, 0), (0.5, 0.8), (1, 1)])
	assert table.progress(0.25) == pytest.approx(0.4)
	assert table.progress(0.75) == pytest.approx(0.9)
	assert table.mean() == pytest.approx(0.5 * 0.4 + 0.5 * 0.9)


def test_a_table_may_hold_flat():
	table = TableCurve([(0, 0), (0.2, 0.5), (0.8, 0.5), (1, 1)])
	assert table.progress(0.5) == 0.5


# --- Refused shapes -------------------------------------------------------------------------------

@pytest.mark.parametrize("make, message", [
	(lambda: ExponentialCurve(0.0), "k = 0 is the linear curve"),
	(lambda: ExponentialCurve(51.0), "k .* -50 to 50"),
	(lambda: ExponentialCurve(float("nan")), "k"),
	(lambda: ExponentialCurve(True), "k"),
	(lambda: SCurve(0.0), "k .* above 0"),
	(lambda: SCurve(-2.0), "k .* above 0"),
	(lambda: SCurve(51.0), "k .* 50"),
	(lambda: StepsCurve(1), "levels .* at least 2"),
	(lambda: StepsCurve(2.5), "levels .* whole number"),
	(lambda: StepsCurve(True), "levels"),
	(lambda: TableCurve([(0, 0)]), "at least 2 points"),
	(lambda: TableCurve([(0.1, 0), (1, 1)]), "start at .0, 0."),
	(lambda: TableCurve([(0, 0), (1, 0.9)]), "end at .1, 1."),
	(lambda: TableCurve([(0, 0), (0.5, 0.5), (0.5, 0.6), (1, 1)]), "point 3: x .* after"),
	(lambda: TableCurve([(0, 0), (0.5, 0.6), (0.7, 0.5), (1, 1)]), "point 3: progress .* falls"),
	(lambda: TableCurve([(0, 0), (0.5, 1.2), (1, 1)]), "point 2: progress"),
	(lambda: TableCurve([(0, 0), ("a", 0.5), (1, 1)]), "point 2"),
	(lambda: TableCurve([(0, 0), (0.5,), (1, 1)]), "point 2: .*pair"),
	(lambda: TableCurve(None), "list of points"),
])
def test_a_bad_shape_is_refused(make, message):
	with pytest.raises(CurveError, match=message):
		make()


# --- From a job file's spec --------------------------------------------------------------------

def test_the_names_are_the_job_file_names():
	assert CURVE_NAMES == ("linear", "exponential", "s-curve", "steps", "table")


def test_make_curve_with_params_and_defaults():
	assert make_curve("linear").spec() == ("linear", {}, None)
	assert make_curve("exponential", {"k": -2.0}).spec() == ("exponential", {"k": -2.0}, None)
	assert make_curve("steps", {}).spec() == ("steps", {"levels": 5}, None)
	assert make_curve("table", None, [[0, 0], [1, 1]]).spec() == ("table", {}, [[0.0, 0.0], [1.0, 1.0]])


@pytest.mark.parametrize("args, message", [
	(("cubic",), "unknown curve 'cubic'; the curves are linear, exponential, s-curve, steps, table"),
	(("exponential", {"steepness": 2}), "exponential: unknown parameter 'steepness'; it takes k"),
	(("linear", {"k": 2}), "linear: unknown parameter 'k'; it takes none"),
	(("linear", None, [[0, 0], [1, 1]]), "linear: a table is only used by the table curve"),
	(("table",), "table: needs a table"),
	(("steps", [5]), "steps: the parameters must be an object"),
])
def test_make_curve_refuses_a_bad_spec(args, message):
	with pytest.raises(CurveError, match=message):
		make_curve(*args)

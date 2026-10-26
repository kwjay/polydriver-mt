import pytest

from polydriver.jobs import (
	Amount,
	ExponentialCurve,
	GradientProfile,
	LinearCurve,
	ProfileError,
	SCurve,
	StepsCurve,
	TableCurve,
	resolve_amount,
)

AMOUNT = Amount(duration_s=600.0, total_mass_g=900.0, total_flow_g_s=1.5)


def profile(**changes):
	values = dict(phi_start=0.01, phi_end=0.05, curve=LinearCurve(), amount=AMOUNT, hold_start_s=30.0, hold_end_s=30.0)
	values.update(changes)
	return GradientProfile(**values)


# --- Amount: any two give the third -------------------------------------------------------------

@pytest.mark.parametrize("given", [
	dict(duration_s=600.0, total_flow_g_s=1.5),
	dict(duration_s=600.0, total_mass_g=900.0),
	dict(total_mass_g=900.0, total_flow_g_s=1.5),
	dict(duration_s=600.0, total_mass_g=900.0, total_flow_g_s=1.5),
])
def test_any_two_of_duration_mass_and_flow_give_the_third(given):
	amount = resolve_amount(**given)
	assert amount.duration_s == pytest.approx(600.0)
	assert amount.total_mass_g == pytest.approx(900.0)
	assert amount.total_flow_g_s == pytest.approx(1.5)


def test_three_values_must_agree():
	with pytest.raises(ProfileError, match="900 g is not 600 s x 2 g/s = 1200 g"):
		resolve_amount(duration_s=600.0, total_mass_g=900.0, total_flow_g_s=2.0)


@pytest.mark.parametrize("given", [dict(), dict(duration_s=600.0), dict(total_mass_g=900.0)])
def test_fewer_than_two_values_is_refused(given):
	with pytest.raises(ProfileError, match="give two of duration_s, total_mass_g and total_flow_g_s"):
		resolve_amount(**given)


@pytest.mark.parametrize("name", ["duration_s", "total_mass_g", "total_flow_g_s"])
@pytest.mark.parametrize("value", [0.0, -1.0, float("nan"), float("inf"), True])
def test_each_value_must_be_a_number_above_0(name, value):
	given = dict(duration_s=600.0, total_mass_g=900.0, total_flow_g_s=1.5)
	given[name] = value
	with pytest.raises(ProfileError, match=f"{name} .* must be a finite number > 0"):
		resolve_amount(**given)


# --- The colour share over time -----------------------------------------------------------------

def test_the_holds_keep_the_end_shares():
	p = profile()
	assert [p.phi(t) for t in (0.0, 15.0, 30.0)] == [0.01, 0.01, 0.01]
	assert [p.phi(t) for t in (570.0, 585.0, 600.0)] == [0.05, 0.05, 0.05]


def test_the_curve_runs_between_the_holds():
	p = profile()
	assert p.gradient_s == 540.0
	assert p.phi(300.0) == pytest.approx(0.03)
	assert profile(curve=StepsCurve(3)).phi(300.0) == pytest.approx(0.03)


def test_without_holds_the_curve_takes_the_whole_run():
	p = profile(hold_start_s=0.0, hold_end_s=0.0)
	assert (p.phi(0.0), p.phi(300.0), p.phi(600.0)) == (0.01, pytest.approx(0.03), 0.05)


def test_a_falling_gradient():
	p = profile(phi_start=0.05, phi_end=0.01)
	assert p.phi(300.0) == pytest.approx(0.03)
	assert p.phi(600.0) == 0.01


def test_the_last_step_reaches_phi_end_exactly():
	# 0.7 + (0.1 - 0.7) is 0.09999999999999998 in floating point.
	p = profile(phi_start=0.7, phi_end=0.1, curve=StepsCurve(4), hold_start_s=0.0, hold_end_s=0.0)
	assert p.phi(599.0) == 0.1
	assert p.phi(1.0) == 0.7


def test_colour_is_its_share_of_the_total_and_the_polymer_fills_the_rest():
	p = profile()
	for t in (0.0, 100.0, 300.0, 600.0):
		assert p.colour_g_s(t) == pytest.approx(p.phi(t) * 1.5)
		assert p.colour_g_s(t) + p.polymer_g_s(t) == pytest.approx(1.5)


@pytest.mark.parametrize("t", [-0.1, 600.1, float("nan")])
def test_a_time_outside_the_run_is_refused(t):
	with pytest.raises(ProfileError, match="0 to 600 s"):
		profile().phi(t)


# --- Mass adds up ------------------------------------------------------------------------------

SHAPES = [LinearCurve(), ExponentialCurve(4.0), ExponentialCurve(-4.0), SCurve(12.0), StepsCurve(4),
		TableCurve([(0, 0), (0.25, 0.7), (1, 1)])]


@pytest.mark.parametrize("curve", SHAPES, ids=repr)
@pytest.mark.parametrize("holds", [(0.0, 0.0), (30.0, 30.0), (100.0, 0.0)])
def test_the_two_masses_add_up_to_the_total(curve, holds):
	p = profile(curve=curve, hold_start_s=holds[0], hold_end_s=holds[1])
	assert p.colour_mass_g + p.polymer_mass_g == pytest.approx(900.0, rel=1e-12)


@pytest.mark.parametrize("curve", SHAPES, ids=repr)
@pytest.mark.parametrize("holds", [(0.0, 0.0), (30.0, 30.0), (100.0, 0.0)])
def test_the_colour_mass_is_the_area_under_the_colour_flow(curve, holds):
	p = profile(curve=curve, hold_start_s=holds[0], hold_end_s=holds[1])
	steps = 60_000
	area = sum(p.colour_g_s((i + 0.5) * 600.0 / steps) for i in range(steps)) * 600.0 / steps
	assert p.colour_mass_g == pytest.approx(area, rel=1e-4)


def test_a_linear_gradient_by_hand():
	# 30 s at 1 %, 540 s averaging 3 %, 30 s at 5 %, all at 1.5 g/s.
	assert profile().colour_mass_g == pytest.approx(1.5 * (30 * 0.01 + 540 * 0.03 + 30 * 0.05))


# --- Preview samples ---------------------------------------------------------------------------

def test_samples_cover_the_run_and_end_exactly_at_its_end():
	samples = profile().samples(step_s=250.0)
	assert [s.t_s for s in samples] == [0.0, 250.0, 500.0, 600.0]
	assert samples[-1].phi == 0.05
	assert samples[1].colour_g_s + samples[1].polymer_g_s == pytest.approx(1.5)


def test_samples_need_a_step_above_0():
	with pytest.raises(ProfileError, match="step"):
		profile().samples(step_s=0.0)


# --- Refused profiles ---------------------------------------------------------------------------

@pytest.mark.parametrize("changes, message", [
	(dict(phi_start=-0.01), "phi_start .* 0 to 1"),
	(dict(phi_end=1.01), "phi_end .* 0 to 1"),
	(dict(phi_end=float("nan")), "phi_end"),
	(dict(phi_start=True), "phi_start"),
	(dict(hold_start_s=-1.0), "hold_start_s .* >= 0"),
	(dict(hold_end_s=float("inf")), "hold_end_s"),
	(dict(hold_start_s=300.0, hold_end_s=300.0), "the holds .300 s . 300 s. take the whole 600 s run and leave no time for the gradient"),
	(dict(curve="linear"), "curve"),
	(dict(amount=(600.0, 900.0, 1.5)), "amount"),
])
def test_a_bad_profile_is_refused(changes, message):
	with pytest.raises(ProfileError, match=message):
		profile(**changes)

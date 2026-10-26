"""Least-squares fit of one calibration curve (C7.3), for reporting only: dosing uses the measured
points (lookup.py), never this line. The line is fitted to every measurement, repeats included,
and by default only from the lowest producing speed up, because 0 g/s points in the dead band
would bend it. Points whose repeats disagree by more than spread_limit of their mean are listed.
"""

import math
from dataclasses import dataclass

from .data import CalibrationError, Curve, Point
from .lookup import NotEnoughPointsError, lowest_producing_speed

SPREAD_LIMIT = 0.10


@dataclass(frozen=True)
class Residual:
	"""A measured point against the line: its mean rate minus the line's rate at that speed."""

	speed_hz: float
	rate_g_s: float
	repeats: int
	fitted_g_s: float
	residual_g_s: float


@dataclass(frozen=True)
class FitReport:
	dispenser: str
	material: str
	curve_hash: str
	from_speed_hz: float
	to_speed_hz: float
	measurements_used: int
	slope_g_s_per_hz: float
	intercept_g_s: float
	r_squared: float | None
	residuals: tuple[Residual, ...]
	noisy_points: tuple[Point, ...]
	spread_limit: float

	@property
	def zero_rate_speed_hz(self) -> float | None:
		"""Where the line reaches 0 g/s: an estimate of the dead band's edge."""
		if self.slope_g_s_per_hz == 0:
			return None
		return -self.intercept_g_s / self.slope_g_s_per_hz


def fit_report(curve: Curve, *, include_dead_band: bool = False, spread_limit: float = SPREAD_LIMIT) -> FitReport:
	name = f"{curve.dispenser} / {curve.material}"
	if not (math.isfinite(spread_limit) and spread_limit >= 0):
		raise CalibrationError(f"the spread limit must be a fraction of 0 or more, not {spread_limit!r}")
	if include_dead_band:
		from_speed_hz = min((m.speed_hz for m in curve.measurements), default=0.0)
	else:
		lowest = lowest_producing_speed(curve)
		if lowest is None:
			raise NotEnoughPointsError(f"{name}: no speed gave any material, so there is no line to fit")
		from_speed_hz = lowest
	used = [m for m in curve.measurements if m.speed_hz >= from_speed_hz]
	speeds = sorted({m.speed_hz for m in used})
	if len(speeds) < 2:
		raise NotEnoughPointsError(
			f"{name}: a line needs at least 2 different speeds from {from_speed_hz:g} Hz up, there is {len(speeds)}"
		)

	xs = [m.speed_hz for m in used]
	ys = [m.rate_g_s for m in used]
	mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
	sxx = sum((x - mean_x) ** 2 for x in xs)
	sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
	slope = sxy / sxx
	intercept = mean_y - slope * mean_x
	ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
	ss_tot = sum((y - mean_y) ** 2 for y in ys)
	r_squared = None if ss_tot == 0 else 1.0 - ss_res / ss_tot

	residuals = tuple(
		Residual(p.speed_hz, p.rate_g_s, p.repeats, intercept + slope * p.speed_hz,
				p.rate_g_s - (intercept + slope * p.speed_hz))
		for p in curve.points if p.speed_hz >= from_speed_hz
	)
	noisy = tuple(p for p in curve.points if p.spread_g_s > spread_limit * p.rate_g_s)
	return FitReport(curve.dispenser, curve.material, curve.hash, from_speed_hz, speeds[-1], len(used), slope,
					intercept, r_squared, residuals, noisy, spread_limit)


def format_fit_report(report: FitReport) -> list[str]:
	lines = [
		f"{report.dispenser} / {report.material}  (curve {report.curve_hash[:12]})",
		f"  fitted {report.from_speed_hz:g} to {report.to_speed_hz:g} Hz, {report.measurements_used} measurements",
		f"  rate = {report.slope_g_s_per_hz:.6g} g/s per Hz x speed {report.intercept_g_s:+.6g} g/s",
		"  R² = " + ("n/a (every rate is the same)" if report.r_squared is None else f"{report.r_squared:.4f}"),
	]
	if report.zero_rate_speed_hz is not None:
		lines.append(f"  line reaches 0 g/s at {report.zero_rate_speed_hz:.4g} Hz")
	lines.append("  speed Hz   rate g/s   line g/s   residual   repeats")
	for r in report.residuals:
		lines.append(
			f"  {r.speed_hz:8g}   {r.rate_g_s:8.4g}   {r.fitted_g_s:8.4g}   {r.residual_g_s:+8.3g}   {r.repeats:7d}"
		)
	if report.noisy_points:
		lines.append(f"  repeats disagree by more than {report.spread_limit:.0%} of their mean at:")
		for p in report.noisy_points:
			lines.append(
				f"    {p.speed_hz:g} Hz: {p.rate_g_s:.4g} g/s, spread {p.spread_g_s:.3g} g/s "
				f"({p.spread_g_s / p.rate_g_s:.0%}) over {p.repeats} repeats"
			)
	else:
		lines.append(f"  no point's repeats disagree by more than {report.spread_limit:.0%}")
	return lines

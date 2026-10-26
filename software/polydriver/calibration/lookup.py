"""Lookups on one calibration curve (C7.2): rate for a speed and speed for a rate, by straight
lines between the measured points (repeats averaged). Nothing outside the measured range is
answered (no extrapolation), and speed_for_rate refuses a curve whose rate falls anywhere as the
speed rises, naming the two speeds, because the inverse would then be ambiguous. Equal rates
(a dead band, a plateau) give the lowest speed that reaches them.
"""

import math

from .data import CalibrationError, Curve, Point


class NotEnoughPointsError(CalibrationError):
	pass


class OutOfRangeError(CalibrationError):
	pass


class NonMonotonicError(CalibrationError):
	pass


def rate_for_speed(curve: Curve, speed_hz: float) -> float:
	_check_number(speed_hz, "speed")
	points = _points(curve)
	low, high = points[0].speed_hz, points[-1].speed_hz
	if not low <= speed_hz <= high:
		raise OutOfRangeError(
			f"{_name(curve)}: {speed_hz:g} Hz is outside the measured {low:g} to {high:g} Hz; measure that speed "
			"rather than guess it"
		)
	for a, b in zip(points, points[1:]):
		if a.speed_hz <= speed_hz <= b.speed_hz:
			fraction = (speed_hz - a.speed_hz) / (b.speed_hz - a.speed_hz)
			return a.rate_g_s + fraction * (b.rate_g_s - a.rate_g_s)
	raise AssertionError("a speed inside the range lies on a segment")


def speed_for_rate(curve: Curve, rate_g_s: float) -> float:
	_check_number(rate_g_s, "rate")
	points = _points(curve)
	for a, b in zip(points, points[1:]):
		if b.rate_g_s < a.rate_g_s:
			raise NonMonotonicError(
				f"{_name(curve)}: the rate falls from {a.rate_g_s:g} g/s at {a.speed_hz:g} Hz to {b.rate_g_s:g} g/s at "
				f"{b.speed_hz:g} Hz, so a rate could belong to more than one speed; repeat those measurements"
			)
	low, high = points[0].rate_g_s, points[-1].rate_g_s
	if not low <= rate_g_s <= high:
		raise OutOfRangeError(f"{_name(curve)}: {rate_g_s:g} g/s is outside the measured {low:g} to {high:g} g/s")
	for a, b in zip(points, points[1:]):
		if rate_g_s == a.rate_g_s:
			return a.speed_hz
		if a.rate_g_s < rate_g_s <= b.rate_g_s:
			fraction = (rate_g_s - a.rate_g_s) / (b.rate_g_s - a.rate_g_s)
			return a.speed_hz + fraction * (b.speed_hz - a.speed_hz)
	raise AssertionError("a rate inside the range lies on a segment")


def lowest_producing_speed(curve: Curve) -> float | None:
	"""The lowest measured speed that gave any material (the old deadband_speed)."""
	return next((p.speed_hz for p in curve.points if p.rate_g_s > 0), None)


def _points(curve: Curve) -> list[Point]:
	points = curve.points
	if len(points) < 2:
		raise NotEnoughPointsError(
			f"{_name(curve)}: a lookup needs at least 2 different speeds, the curve has {len(points)}"
		)
	return points


def _check_number(value: float, what: str) -> None:
	if not math.isfinite(value):
		raise CalibrationError(f"the {what} must be a finite number, not {value!r}")


def _name(curve: Curve) -> str:
	return f"{curve.dispenser} / {curve.material}"

"""Job compile (C7.5): rows of g/s (or duty, for the bench) into segments the board runs.

0 g/s means the motor stopped (0 Hz) and needs no calibration. Every other rate is turned into a
speed by the calibration (lookup.py), refused outside the measured range or the material's flow
window. The board ramps linearly in Hz, from the previous segment's value (0 for the first), but
the calibration bends, so a g/s ramp is split at every calibration point it crosses: between two
neighbouring points the rate is linear in speed, so each piece is linear in g/s too. Boundaries
are rounded to whole ms from the job's start, so rounding never adds up.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass

from ..calibration import CalibrationError, Curve, rate_for_speed, speed_for_rate
from ..devices.board import MAX_DUTY, MIN_SEGMENT_MS, Segment
from ..materials import Material

# JobProgram::MAX_SEGMENTS in the firmware; a connected board reports its own in VERSION.
DEFAULT_MAX_SEGMENTS = 32


class JobCompileError(Exception):
	pass


@dataclass(frozen=True)
class RateRow:
	"""Hold rate_g_s for duration_s, or with ramp, go linearly to it from the previous row's rate."""

	rate_g_s: float
	duration_s: float
	ramp: bool = False


@dataclass(frozen=True)
class DutyRow:
	duty: float
	duration_s: float
	ramp: bool = False


@dataclass(frozen=True)
class CompiledJob:
	segments: tuple[Segment, ...]
	row_of_segment: tuple[int, ...]
	total_ms: int
	expected_grams: float | None
	expected_grams_per_row: tuple[float, ...] | None
	material: str | None = None
	dispenser: str | None = None
	curve_hash: str | None = None


def compile_rate_job(material: Material, curve: Curve, rows: Sequence[RateRow], *,
					max_segments: int = DEFAULT_MAX_SEGMENTS) -> CompiledJob:
	if curve.material != material.name:
		raise JobCompileError(f"the calibration {curve.dispenser} / {curve.material} is not for {material.name}")
	bounds_ms = _row_bounds_ms(rows)
	segments: list[Segment] = []
	row_of_segment: list[int] = []
	previous_g_s = 0.0
	for number, (row, (start_ms, end_ms)) in enumerate(zip(rows, bounds_ms), start=1):
		rate = row.rate_g_s
		if not (_is_number(rate) and rate >= 0):
			raise JobCompileError(f"row {number}: the rate {rate!r} g/s must be a number >= 0")
		if rate > 0 and not material.allows_flow(rate):
			raise JobCompileError(
				f"row {number}: {rate:g} g/s is outside {material.name}'s flow window "
				f"{material.flow_min_g_s:g} to {material.flow_max_g_s:g} g/s"
			)
		if row.ramp:
			if previous_g_s == 0:
				raise JobCompileError(
					f"row {number}: a ramp from 0 g/s starts with the motor stopped, below the calibrated speeds; "
					"start with a step at the lowest rate you want and ramp from there"
				)
			if rate == 0:
				raise JobCompileError(
					f"row {number}: a ramp to 0 g/s would slow the motor below the calibrated speeds; end the ramp "
					"above 0 and add a step at 0 g/s"
				)
			pieces = _ramp_pieces(curve, number, previous_g_s, rate, start_ms, end_ms)
			segments += [Segment(hz, ms, ramp=True) for hz, ms in pieces]
			row_of_segment += [number] * len(pieces)
		else:
			segments.append(Segment(_speed(curve, number, rate), end_ms - start_ms))
			row_of_segment.append(number)
		previous_g_s = rate
	_check_count(segments, max_segments)
	grams = _grams_per_row(curve, segments, row_of_segment, len(rows))
	return CompiledJob(tuple(segments), tuple(row_of_segment), bounds_ms[-1][1], sum(grams), grams,
					material.name, curve.dispenser, curve.hash)


def compile_duty_job(rows: Sequence[DutyRow], *, max_segments: int = DEFAULT_MAX_SEGMENTS) -> CompiledJob:
	bounds_ms = _row_bounds_ms(rows)
	segments = []
	for number, (row, (start_ms, end_ms)) in enumerate(zip(rows, bounds_ms), start=1):
		if not (_is_number(row.duty) and 0 <= row.duty <= MAX_DUTY):
			raise JobCompileError(f"row {number}: duty {row.duty!r} must be 0 to {MAX_DUTY}")
		segments.append(Segment(row.duty, end_ms - start_ms, ramp=row.ramp, duty=True))
	_check_count(segments, max_segments)
	return CompiledJob(tuple(segments), tuple(range(1, len(rows) + 1)), bounds_ms[-1][1], None, None)


# --- Times and counts ----------------------------------------------------------------------

def _row_bounds_ms(rows: Sequence[RateRow | DutyRow]) -> list[tuple[int, int]]:
	if not rows:
		raise JobCompileError("the job has no rows")
	bounds, elapsed_s = [], 0.0
	for number, row in enumerate(rows, start=1):
		if not (_is_number(row.duration_s) and row.duration_s > 0):
			raise JobCompileError(f"row {number}: the duration {row.duration_s!r} s must be a finite number > 0")
		start_ms = round(elapsed_s * 1000)
		elapsed_s += row.duration_s
		end_ms = round(elapsed_s * 1000)
		if end_ms - start_ms < MIN_SEGMENT_MS:
			raise JobCompileError(
				f"row {number}: {row.duration_s:g} s is shorter than one {MIN_SEGMENT_MS} ms control tick"
			)
		bounds.append((start_ms, end_ms))
	return bounds


def _check_count(segments: list[Segment], max_segments: int) -> None:
	if len(segments) > max_segments:
		raise JobCompileError(
			f"the job compiles to {len(segments)} segments, the board holds {max_segments}; use fewer rows, or "
			"ramps that cross fewer calibration points"
		)


# --- Speeds --------------------------------------------------------------------------------

def _speed(curve: Curve, number: int, rate_g_s: float) -> float:
	if rate_g_s == 0:
		return 0.0
	try:
		return speed_for_rate(curve, rate_g_s)
	except CalibrationError as error:
		points = curve.points
		if points and not points[0].rate_g_s <= rate_g_s <= points[-1].rate_g_s:
			raise JobCompileError(
				f"row {number}: {rate_g_s:g} g/s is outside the calibration {curve.dispenser} / {curve.material}, "
				f"measured {points[0].rate_g_s:g} to {points[-1].rate_g_s:g} g/s"
			) from error
		raise JobCompileError(f"row {number}: {error}") from error


def _ramp_pieces(curve: Curve, number: int, from_g_s: float, to_g_s: float, start_ms: int,
				end_ms: int) -> list[tuple[float, int]]:
	"""(speed at the piece's end, piece length in ms); pieces shorter than a tick are merged."""
	low, high = min(from_g_s, to_g_s), max(from_g_s, to_g_s)
	marks = [(float(start_ms), _speed(curve, number, from_g_s))]
	crossed = [p for p in curve.points if low < p.rate_g_s < high]
	for point in sorted(crossed, key=lambda p: abs(p.rate_g_s - from_g_s)):
		fraction = (point.rate_g_s - from_g_s) / (to_g_s - from_g_s)
		marks.append((start_ms + fraction * (end_ms - start_ms), point.speed_hz))
	marks.append((float(end_ms), _speed(curve, number, to_g_s)))
	marks = [marks[0]] + [m for a, m, b in zip(marks, marks[1:], marks[2:]) if not _in_line(a, m, b)] + [marks[-1]]
	times = [round(t) for t, _ in marks]
	speeds = [hz for _, hz in marks]
	while True:
		short = next((i for i in range(len(times) - 1) if times[i + 1] - times[i] < MIN_SEGMENT_MS), None)
		if short is None:
			break
		drop = short + 1 if short + 1 < len(times) - 1 else short
		del times[drop], speeds[drop]
	return [(speeds[i], times[i] - times[i - 1]) for i in range(1, len(times))]


def _in_line(a: tuple[float, float], m: tuple[float, float], b: tuple[float, float]) -> bool:
	"""m adds no bend: the speed is linear in time from a through m to b."""
	(ta, ha), (tm, hm), (tb, hb) = a, m, b
	return math.isclose((hm - ha) * (tb - ta), (hb - ha) * (tm - ta), rel_tol=1e-9, abs_tol=1e-9)


# --- Expected grams -------------------------------------------------------------------------

def segment_grams(curve: Curve, segments: Sequence[Segment]) -> tuple[float, ...]:
	"""The grams each speed segment gives as the board runs it (a ramp from the previous segment's
	value, 0 Hz before the first), by the calibration."""
	grams = []
	previous_hz = 0.0
	for segment in segments:
		rate = _mean_rate(curve, previous_hz, segment.value) if segment.ramp else _rate(curve, segment.value)
		grams.append(rate * segment.duration_ms / 1000)
		previous_hz = segment.value
	return tuple(grams)


def _grams_per_row(curve: Curve, segments: list[Segment], row_of_segment: list[int], rows: int) -> tuple[float, ...]:
	"""From the segments as the board will run them, so rounding and merged pieces are counted."""
	grams = [0.0] * rows
	for g, number in zip(segment_grams(curve, segments), row_of_segment):
		grams[number - 1] += g
	return tuple(grams)


def _rate(curve: Curve, speed_hz: float) -> float:
	return 0.0 if speed_hz == 0 else rate_for_speed(curve, speed_hz)


def _mean_rate(curve: Curve, from_hz: float, to_hz: float) -> float:
	"""The mean rate while the speed moves linearly from from_hz to to_hz: the calibration is linear
	between its points, so the trapezoid rule over them is exact."""
	low, high = min(from_hz, to_hz), max(from_hz, to_hz)
	if high == low:
		return _rate(curve, low)
	speeds = [low] + [p.speed_hz for p in curve.points if low < p.speed_hz < high] + [high]
	rates = [_rate(curve, hz) for hz in speeds]
	area = sum((b - a) * (ra + rb) / 2 for a, b, ra, rb in zip(speeds, speeds[1:], rates, rates[1:]))
	return area / (high - low)


def _is_number(value: object) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

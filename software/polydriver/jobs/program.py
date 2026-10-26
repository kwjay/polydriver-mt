"""Gradient profile into board segments (C7.8): one segment list per channel, with the fewest ramps
that keep each channel's flow within error_limit of its target at every 10 ms control tick.

The holds and each level of a steps curve are steps; neighbouring equal steps are merged. Every
continuous part becomes ramps, each as long as its ticks allow (greedy, in one pass per ramp). A
tick allows the speed a tolerance in Hz: the error limit x the target rate over the steepest
calibration piece at and next to the target speed. The error is then measured as the board makes
it: at each tick it ramps linearly in Hz from the previous segment's value and the delivered rate
is the calibration at that speed; a program above the limit anywhere is refused, never sent. The
board starts a job's first ramp from 0 Hz, so a job without a start hold begins with a one-tick step.
"""

import math
from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass

from ..calibration import CalibrationStore, Curve
from ..devices.board import MIN_SEGMENT_MS, Segment
from ..materials import MaterialRegistry
from .check import check_job
from .compile import DEFAULT_MAX_SEGMENTS, JobCompileError, segment_grams
from .curves import ProgressCurve, StepsCurve, TableCurve
from .profile import GradientProfile
from .recipe import ROLES, GradientJob

ERROR_LIMIT = 0.005
TICK_MS = MIN_SEGMENT_MS


@dataclass(frozen=True)
class ChannelProgram:
	role: str
	segments: tuple[Segment, ...]
	expected_grams: float
	target_grams: float
	max_error: float
	material: str
	dispenser: str
	curve_hash: str


@dataclass(frozen=True)
class GradientProgram:
	polymer: ChannelProgram
	colour: ChannelProgram
	total_ms: int
	error_limit: float


class ChannelCalibration:
	"""The lookups of calibration/lookup.py on a curve already checked to be usable, by bisection."""

	def __init__(self, curve: Curve):
		points = curve.points
		self.speeds = [p.speed_hz for p in points]
		self.rates = [p.rate_g_s for p in points]
		self.slopes = [(r1 - r0) / (s1 - s0) for s0, s1, r0, r1 in zip(self.speeds, self.speeds[1:], self.rates, self.rates[1:])]

	def speed(self, rate_g_s: float) -> float:
		i = bisect_left(self.rates, rate_g_s)
		if self.rates[i] == rate_g_s:
			return self.speeds[i]
		(s0, s1), (r0, r1) = self.speeds[i - 1:i + 1], self.rates[i - 1:i + 1]
		return s0 + (rate_g_s - r0) / (r1 - r0) * (s1 - s0)

	def rate(self, speed_hz: float) -> float:
		i = max(1, bisect_left(self.speeds, speed_hz))
		(s0, s1), (r0, r1) = self.speeds[i - 1:i + 1], self.rates[i - 1:i + 1]
		return r0 + (speed_hz - s0) / (s1 - s0) * (r1 - r0)

	def tolerance_hz(self, rate_g_s: float, speed_hz: float, limit: float) -> float:
		"""How far the speed may be off for the rate to be off by at most limit x rate: the steepest
		of the calibration pieces at and next to this speed decides."""
		i = max(1, bisect_left(self.speeds, speed_hz)) - 1
		steepest = max(self.slopes[k] for k in (i - 1, i, i + 1) if 0 <= k < len(self.slopes))
		return math.inf if steepest == 0 else limit * rate_g_s / steepest


def compile_gradient_job(job: GradientJob, materials: MaterialRegistry, store: CalibrationStore, *,
						error_limit: float = ERROR_LIMIT, max_segments: int = DEFAULT_MAX_SEGMENTS) -> GradientProgram:
	if isinstance(error_limit, bool) or not (isinstance(error_limit, (int, float)) and 0 < error_limit < 1):
		raise JobCompileError(f"the error limit {error_limit!r} must be a fraction above 0 and below 1")
	checked = check_job(job, materials, store)
	if not checked.ok:
		errors = [line.removeprefix("error: ") for line in checked.report() if line.startswith("error: ")]
		raise JobCompileError("the job does not fit: " + "; ".join(errors))
	profile = job.profile
	pieces = _pieces(profile)
	channels, problems = {}, []
	for role in ROLES:
		ref = getattr(job, role)
		curve = store.curve(ref.dispenser, ref.material)
		segments, (max_error, worst_t_ms) = _channel_segments(role, profile, pieces, ChannelCalibration(curve), error_limit)
		if max_error > error_limit * (1 + 1e-9):
			problems.append(
				f"{role}: the {describe_curve(profile.curve)} gradient changes faster than the board's {TICK_MS} ms ticks "
				f"can follow within {error_limit * 100:g} % (off by {max_error * 100:.3g} % at {worst_t_ms / 1000:g} s)"
			)
		elif len(segments) > max_segments:
			problems.append(
				f"{role}: the {describe_curve(profile.curve)} gradient needs {len(segments)} segments to stay within "
				f"{error_limit * 100:g} %; the board holds {max_segments}. Allow a larger error, use a gentler curve or "
				"fewer steps"
			)
		target = profile.colour_mass_g if role == "colour" else profile.polymer_mass_g
		channels[role] = ChannelProgram(role, tuple(segments), sum(segment_grams(curve, segments)), target, max_error,
										ref.material, ref.dispenser, curve.hash)
	if problems:
		raise JobCompileError("; ".join(problems))
	return GradientProgram(channels["polymer"], channels["colour"], pieces[-1][1], error_limit)


def describe_curve(curve: ProgressCurve) -> str:
	if isinstance(curve, TableCurve):
		return f"table ({len(curve.points)} points)"
	params = ", ".join(f"{k}={v:g}" for k, v in curve.params().items())
	return f"{curve.NAME} ({params})" if params else curve.NAME


# --- Pieces: holds, steps and continuous parts ------------------------------------------------

def _pieces(profile: GradientProfile) -> list[tuple[int, int, float | None]]:
	"""(start ms, end ms, progress if constant, None if the curve runs)."""
	total_ms = round(profile.duration_s * 1000)
	start_ms = round(profile.hold_start_s * 1000)
	end_ms = round((profile.duration_s - profile.hold_end_s) * 1000)
	pieces: list[tuple[int, int, float | None]] = []
	if start_ms > 0:
		_need_a_tick(start_ms, f"the start hold of {profile.hold_start_s:g} s")
		pieces.append((0, start_ms, 0.0))
	curve = profile.curve
	if isinstance(curve, StepsCurve):
		n = curve.levels
		bounds = [start_ms + round(k / n * (end_ms - start_ms)) for k in range(n + 1)]
		for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
			_need_a_tick(b - a, f"a step of {describe_curve(curve)}", ms=True)
			pieces.append((a, b, k / (n - 1)))
	else:
		_need_a_tick(end_ms - start_ms, f"the gradient of {(end_ms - start_ms) / 1000:g} s", )
		pieces.append((start_ms, end_ms, None))
	if end_ms < total_ms:
		_need_a_tick(total_ms - end_ms, f"the end hold of {profile.hold_end_s:g} s")
		pieces.append((end_ms, total_ms, 1.0))
	return pieces


def _need_a_tick(length_ms: int, what: str, ms: bool = False) -> None:
	if length_ms < TICK_MS:
		lasts = f"lasts {length_ms} ms, " if ms else "is "
		raise JobCompileError(f"{what} {lasts}shorter than one {TICK_MS} ms control tick")


# --- One channel ----------------------------------------------------------------------------

def _channel_segments(role: str, profile: GradientProfile, pieces: list[tuple[int, int, float | None]],
					calibration: ChannelCalibration, limit: float) -> tuple[list[Segment], tuple[float, int]]:
	flow = profile.colour_g_s if role == "colour" else profile.polymer_g_s
	total_ms = pieces[-1][1]

	def rate_at(t_ms: float) -> float:
		return flow(min(t_ms / 1000, profile.duration_s))

	def speed_of(rate: float) -> float:
		return 0.0 if rate == 0 else calibration.speed(rate)

	grid = [rate_at(t) for t in range(0, total_ms, TICK_MS)]
	segments: list[Segment] = []
	for start_ms, end_ms, progress in pieces:
		if progress is not None:
			phi = profile.phi_start * (1 - progress) + profile.phi_end * progress
			rate = phi if role == "colour" else 1 - phi
			segments.append(Segment(speed_of(rate * profile.amount.total_flow_g_s), end_ms - start_ms))
			continue
		for t, word in ((start_ms, "starts"), (end_ms, "ends")):
			if rate_at(t) == 0:
				raise JobCompileError(
					f"{role}: the gradient {word} the screw {'from' if word == 'starts' else 'at'} 0 g/s, and a screw cannot "
					"follow a ramp to or from standing still; keep 0 g/s in a hold or a step and ramp between flows above 0"
				)
		if not segments:
			segments.append(Segment(speed_of(rate_at(TICK_MS)), TICK_MS))
			start_ms += TICK_MS
		segments += _ramps(start_ms, end_ms, grid, rate_at, calibration, limit)
	segments = _merged(segments)
	return segments, _worst_error(segments, grid, calibration)


def _ramps(start_ms: int, end_ms: int, grid: list[float], rate_at: Callable[[float], float],
		calibration: ChannelCalibration, limit: float) -> list[Segment]:
	"""grid: the target rate at every tick of the job; rate_at gives it between ticks."""
	first = start_ms // TICK_MS + 1
	ticks = list(range(first * TICK_MS, end_ms, TICK_MS))
	rates = grid[first:first + len(ticks)]
	speeds = [calibration.speed(r) for r in rates]
	tolerances = [calibration.tolerance_hz(r, s, limit) for r, s in zip(rates, speeds)]
	known = dict(zip(ticks, speeds))

	def speed(t: int) -> float:
		if t not in known:
			known[t] = calibration.speed(rate_at(t))
		return known[t]

	segments, here = [], start_ms
	while here < end_ms:
		end = _farthest_end(here, end_ms, ticks, speeds, tolerances, speed)
		segments.append(Segment(speed(end), end - here, ramp=True))
		here = end
	return segments


def _farthest_end(here: int, end_ms: int, ticks: list[int], speeds: list[float], tolerances: list[float],
				speed: Callable[[int], float]) -> int:
	"""The farthest end a ramp from here can reach with every tick on the way within its tolerance in
	Hz, in one pass: each tick allows the ramp's slope only an interval, so the slopes still allowed
	shrink tick by tick, and an end fits when the slope to it is still allowed."""
	from_hz = speed(here)
	low, high = -math.inf, math.inf
	first = bisect_left(ticks, here + TICK_MS)
	shortest = ticks[first] if first < len(ticks) and ticks[first] <= end_ms - TICK_MS else end_ms
	best = None
	for k in range(bisect_left(ticks, here + 1), len(ticks)):
		t = ticks[k]
		elapsed = t - here
		if here + TICK_MS <= t <= end_ms - TICK_MS and low <= (speeds[k] - from_hz) / elapsed <= high:
			best = t
		low = max(low, (speeds[k] - tolerances[k] - from_hz) / elapsed)
		high = min(high, (speeds[k] + tolerances[k] - from_hz) / elapsed)
		if low > high:
			return best if best is not None else shortest
	if low <= (speed(end_ms) - from_hz) / (end_ms - here) <= high:
		return end_ms
	return best if best is not None else shortest


def _merged(segments: list[Segment]) -> list[Segment]:
	merged: list[Segment] = []
	for segment in segments:
		previous = merged[-1] if merged else None
		if previous is not None and not previous.ramp and not segment.ramp and previous.value == segment.value:
			merged[-1] = Segment(previous.value, previous.duration_ms + segment.duration_ms)
		else:
			merged.append(segment)
	return merged


def _worst_error(segments: list[Segment], grid: list[float], calibration: ChannelCalibration) -> tuple[float, int]:
	"""The largest |delivered - target| / target over the ticks, as the board runs the segments, and its
	tick in ms. A stopped screw is a step at 0 Hz, which delivers its 0 g/s exactly."""
	worst, worst_t, start, previous, index = 0.0, 0, 0, 0.0, 0
	for k, target in enumerate(grid):
		t = k * TICK_MS
		while t >= start + segments[index].duration_ms:
			start += segments[index].duration_ms
			previous = segments[index].value
			index += 1
		segment = segments[index]
		speed = previous + (segment.value - previous) * (t - start) / segment.duration_ms if segment.ramp else segment.value
		if target > 0 and abs(calibration.rate(speed) - target) / target > worst:
			worst, worst_t = abs(calibration.rate(speed) - target) / target, t
	return worst, worst_t

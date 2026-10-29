"""How a board's speed loop answers a step of its target: rise time (10-90 % of the step), overshoot
past the target in % of the step, settling time into a band around the target, and the steady
error over the end of the hold. Measured on the filtered speed, which the PID regulates."""

import csv
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import astuple, dataclass, fields
from pathlib import Path

MIN_POLLS = 3


class StepError(ValueError):
	pass


@dataclass(frozen=True)
class StepMetrics:
	from_hz: float
	to_hz: float
	rise_time_s: float | None
	"""None when the speed never got to 90 % of the step."""
	overshoot_percent: float
	settling_time_s: float | None
	"""None when the speed was outside the band at the end of the hold."""
	steady_error_hz: float
	steady_error_percent: float | None
	"""None for a step to 0 Hz."""


def step_metrics(
	times_s: Sequence[float],
	speeds_hz: Sequence[float],
	*,
	step_s: float,
	from_hz: float,
	to_hz: float,
	band_fraction: float = 0.05,
	steady_window_s: float = 1.0,
) -> StepMetrics:
	"""times_s and speeds_hz: the polls of one hold, on the clock step_s is on; polls before step_s
	are ignored. band_fraction is of the step's size."""
	if len(times_s) != len(speeds_hz):
		raise StepError(f"times and speeds differ in length ({len(times_s)} and {len(speeds_hz)})")
	if not 0 < band_fraction < 1:
		raise StepError(f"band {band_fraction} must be between 0 and 1 (0.05 = 5 % of the step)")
	size_hz = to_hz - from_hz
	if size_hz == 0:
		raise StepError(f"from and to are the same speed ({to_hz:g} Hz): there is no step to measure")
	after = [(t, v) for t, v in zip(times_s, speeds_hz) if t >= step_s]
	if len(after) < MIN_POLLS:
		raise StepError(f"{len(after)} poll(s) after the step; at least {MIN_POLLS} polls are needed: hold longer")
	times = [t - step_s for t, _ in after]
	speeds = [v for _, v in after]
	progress = [(v - from_hz) / size_hz for v in speeds]

	t10, t90 = _first_crossing(times, progress, 0.1), _first_crossing(times, progress, 0.9)
	rise_time_s = t90 - t10 if t10 is not None and t90 is not None else None
	overshoot_percent = max(0.0, (max(progress) - 1.0) * 100)

	band_hz = band_fraction * abs(size_hz)
	errors = [abs(v - to_hz) for v in speeds]
	settling_time_s = _settling_time(times, errors, band_hz)

	end_s = times[-1]
	steady = [v for t, v in zip(times, speeds) if t >= end_s - steady_window_s]
	steady_error_hz = statistics.fmean(steady) - to_hz
	steady_error_percent = steady_error_hz / to_hz * 100 if to_hz != 0 else None
	return StepMetrics(from_hz, to_hz, rise_time_s, overshoot_percent, settling_time_s, steady_error_hz,
						steady_error_percent)


def _first_crossing(times: list[float], progress: list[float], level: float) -> float | None:
	for i, p in enumerate(progress):
		if p >= level:
			if i == 0:
				return times[0]
			p0, t0 = progress[i - 1], times[i - 1]
			return t0 + (times[i] - t0) * (level - p0) / (p - p0)
	return None


def _settling_time(times: list[float], errors: list[float], band_hz: float) -> float | None:
	if errors[-1] > band_hz:
		return None
	outside = [i for i, e in enumerate(errors) if e > band_hz]
	if not outside:
		return times[0]
	i = outside[-1]
	e0, e1 = errors[i], errors[i + 1]
	return times[i] + (times[i + 1] - times[i]) * (e0 - band_hz) / (e0 - e1)


def parse_steps(text: str) -> list[float]:
	"""Speeds as typed: '0, 10, 20, 10' (commas, semicolons or spaces)."""
	steps = []
	for word in (w for w in re.split(r"[,;\s]+", text.strip()) if w):
		try:
			value = float(word)
		except ValueError:
			raise StepError(f"'{word}' is not a speed: type speeds in Hz, e.g. 0, 10, 20, 10") from None
		if not math.isfinite(value) or value < 0:
			raise StepError(f"{word} Hz: speeds must be 0 Hz or more")
		if steps and value == steps[-1]:
			raise StepError(f"{value:g} Hz twice in a row is not a step")
		steps.append(value)
	if len(steps) < 2:
		raise StepError("give at least two speeds: where the test starts and the first step, e.g. 0, 10")
	return steps


COLUMNS = [f.name for f in fields(StepMetrics)]


def write_step_metrics(path: Path, metrics: Sequence[StepMetrics], *, note: str = "") -> None:
	"""One row per step; empty cells where a metric could not be measured. A first line '# note'."""
	with Path(path).open("w", newline="", encoding="utf-8") as file:
		writer = csv.writer(file)
		if note:
			writer.writerow([f"# {note}"])
		writer.writerow(COLUMNS)
		for m in metrics:
			writer.writerow(astuple(m))  # csv writes None as an empty cell

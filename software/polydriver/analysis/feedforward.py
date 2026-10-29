"""The feedforward a board needs (SET_FEEDFORWARD: duty = offset + gain * target) from an
open-loop duty sweep: a least-squares line of duty against the measured speed, through the steps
where the screw turned."""

import csv
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_DUTY = 255
MIN_TURNING_ROWS = 3


class FeedforwardFitError(ValueError):
	pass


@dataclass(frozen=True)
class SweepRow:
	"""One sweep step: the mean raw speed over the second half of its hold (bench.sweep)."""

	duty: int
	mean_raw_hz: float
	samples: int
	stalled_samples: int

	@property
	def turning(self) -> bool:
		return self.stalled_samples == 0 and self.mean_raw_hz > 0


@dataclass(frozen=True)
class FeedforwardFit:
	start_duty: int
	ff_offset_duty: float
	ff_gain_duty_per_hz: float
	r_squared: float
	points: int

	def report(self) -> str:
		return (f"start duty {self.start_duty} · offset {self.ff_offset_duty:.1f} · gain {self.ff_gain_duty_per_hz:.3f} "
				f"duty/Hz · R² {self.r_squared:.3f} ({self.points} steps)")


def fit_feedforward(rows: Sequence[SweepRow]) -> FeedforwardFit:
	"""A step counts only if no poll in it read the encoder as stalled: a part-stalled mean is too low."""
	turning = sorted((row for row in rows if row.turning), key=lambda row: row.duty)
	if len(turning) < MIN_TURNING_ROWS:
		raise FeedforwardFitError(
			f"the screw turned in {len(turning)} sweep step(s); a fit needs at least {MIN_TURNING_ROWS}: sweep up to a "
			"higher duty or in smaller steps")
	speeds = [row.mean_raw_hz for row in turning]
	duties = [float(row.duty) for row in turning]
	if max(speeds) - min(speeds) <= 1e-9:
		raise FeedforwardFitError("the speed did not change over the turning steps, so there is no line to fit")
	slope, intercept = statistics.linear_regression(speeds, duties)
	if slope <= 0:
		raise FeedforwardFitError("the speed falls as the duty rises: check the encoder wiring and sweep again")
	if not 0 <= intercept <= MAX_DUTY:
		raise FeedforwardFitError(
			f"the fitted offset {intercept:.1f} lies outside 0-255, which the board refuses: the steps are not on a "
			"line; sweep again over the turning range only")
	correlation = statistics.correlation(speeds, duties) if len(set(duties)) > 1 else 1.0
	return FeedforwardFit(
		start_duty=turning[0].duty,
		ff_offset_duty=intercept,
		ff_gain_duty_per_hz=slope,
		r_squared=correlation ** 2,
		points=len(turning),
	)


def load_sweep(path: Path) -> list[SweepRow]:
	"""Rows of a sweep CSV (bench.sweep.SWEEP_COLUMNS, as written by the GUI and `dev sweep`)."""
	with Path(path).open(newline="", encoding="utf-8") as file:
		reader = csv.DictReader(file)
		needed = ("duty", "mean_raw_hz", "samples", "stalled_samples")
		missing = [name for name in needed if name not in (reader.fieldnames or [])]
		if missing:
			raise FeedforwardFitError(f"{path} is not a sweep file: no column {', '.join(missing)}")
		rows = []
		for line, record in enumerate(reader, start=2):
			try:
				row = SweepRow(int(record["duty"]), float(record["mean_raw_hz"]), int(record["samples"]),
								int(record["stalled_samples"]))
			except (TypeError, ValueError) as error:
				raise FeedforwardFitError(f"{path} line {line}: {error}") from error
			if not math.isfinite(row.mean_raw_hz):
				raise FeedforwardFitError(f"{path} line {line}: speed {row.mean_raw_hz} is not a number")
			rows.append(row)
	return rows

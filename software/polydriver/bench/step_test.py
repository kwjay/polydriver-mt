"""Closed-loop step test of one board: hold the first speed to settle, then step to each next one
and hold it, measuring each step (analysis.step_metrics) on the filtered speed. Optionally made a
run log of (CSV + sidecar) with the step metrics written next to it as <run>-steps.csv."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..analysis.step_response import StepError, StepMetrics, step_metrics, write_step_metrics
from ..protocol.constants import Mode, Role
from .cancel import Cancel, Cancelled
from .sweep import PollFeed


@dataclass(frozen=True)
class StepResult:
	from_hz: float
	to_hz: float
	metrics: StepMetrics | None
	problem: str | None = None
	"""Why there are no metrics (too few polls)."""


@dataclass(frozen=True)
class StepTestResult:
	steps: tuple[StepResult, ...]
	ended_early: bool
	log_path: Path | None = None
	metrics_path: Path | None = None
	cleanup_problem: str | None = None
	"""Set when the board could not be sent target 0 at the end."""


def run_step_test(
	session,
	role: Role,
	speeds_hz: Sequence[float],
	hold_s: float,
	*,
	band_fraction: float = 0.05,
	cancel: Cancel | None = None,
	on_step: Callable[[int, int, StepResult], None] | None = None,
	start_log: Callable[[], object] | None = None,
	note: str = "",
	clock: Callable[[], float] = time.perf_counter,
) -> StepTestResult:
	"""clock must be the Session's clock (perf_counter by default), since the polls carry its time.
	start_log() returns a run log (csv_path, close()); it is closed at the end. The board is left in
	closed loop at target 0. on_step(index, count, result) is called from this thread."""
	if len(speeds_hz) < 2:
		raise StepError("give at least two speeds: where the test starts and the first step")
	if hold_s <= 0:
		raise StepError(f"hold {hold_s} s must be > 0")
	cancel = cancel or Cancel()
	board = session.board(role)
	feed = PollFeed(role)
	results: list[StepResult] = []
	ended_early = False
	log = None
	session.add_sink(feed)
	try:
		log = start_log() if start_log else None
		with cancel.command():
			if board.settings().mode is not Mode.CLOSED_LOOP:
				board.set_mode(Mode.CLOSED_LOOP)
			board.set_speed(speeds_hz[0])
		feed.collect(hold_s, cancel, clock)
		count = len(speeds_hz) - 1
		for index in range(count):
			from_hz, to_hz = speeds_hz[index], speeds_hz[index + 1]
			with cancel.command():
				step_s = clock()
				board.set_speed(to_hz)
			samples = feed.collect(hold_s, cancel, clock)
			if samples is None:
				raise Cancelled()
			result = _measure(from_hz, to_hz, step_s, samples, band_fraction)
			results.append(result)
			if on_step:
				on_step(index, count, result)
	except Cancelled:
		ended_early = True
	finally:
		session.remove_sink(feed)
		cleanup_problem = _stop(board)
		if log is not None:
			log.close()
	log_path = metrics_path = None
	if log is not None:
		log_path = Path(log.csv_path)
		metrics_path = log_path.with_name(f"{log_path.stem}-steps.csv")
		write_step_metrics(metrics_path, [r.metrics for r in results if r.metrics], note=note)
	return StepTestResult(tuple(results), ended_early, log_path, metrics_path, cleanup_problem)


def _measure(from_hz: float, to_hz: float, step_s: float, samples, band_fraction: float) -> StepResult:
	times = [t for _, t in samples]
	speeds = [status.report.filtered_speed_hz for status, _ in samples]
	try:
		metrics = step_metrics(times, speeds, step_s=step_s, from_hz=from_hz, to_hz=to_hz, band_fraction=band_fraction)
	except StepError as error:
		return StepResult(from_hz, to_hz, None, str(error))
	return StepResult(from_hz, to_hz, metrics)


def _stop(board) -> str | None:
	try:
		board.set_speed(0.0)
	except Exception as error:
		return f"could not send target 0 at the end ({error})"
	return None

"""Open-loop duty sweep of one board: hold each duty, take the mean speed over the second half of
the hold (after it settled), optionally write each step to a CSV as it is measured. The rows feed
analysis.fit_feedforward. The shell's `dev sweep` uses the same row and columns."""

import csv
import queue
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..analysis.feedforward import SweepRow
from ..devices import BoardStatus
from ..protocol.constants import Mode, Role, StatusFlag
from .cancel import Cancel, Cancelled

SWEEP_COLUMNS = ["duty", "mean_raw_hz", "stdev_raw_hz", "min_raw_hz", "max_raw_hz", "mean_filtered_hz", "samples",
				"stalled_samples"]
MIN_POLLS_PER_HOLD = 20
_WAIT_S = 0.05


def sweep_row(duty: int, samples: list[BoardStatus]) -> list:
	"""Mean over the second half of the hold, after the speed has settled."""
	settled = samples[len(samples) // 2:]
	raw = [s.report.raw_speed_hz for s in settled]
	filtered = [s.report.filtered_speed_hz for s in settled]
	stalled = sum(1 for s in settled if StatusFlag.ENCODER_STALLED in s.report.flags)
	stdev = statistics.stdev(raw) if len(raw) > 1 else 0.0
	return [duty, statistics.fmean(raw), stdev, min(raw), max(raw), statistics.fmean(filtered), len(settled), stalled]


class PollFeed:
	"""Session sink handing one board's polls to the procedure waiting for them."""

	def __init__(self, role: Role):
		self._role = role
		self._queue: queue.Queue = queue.Queue()

	def record_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		if role is self._role:
			self._queue.put((status, host_time_s))

	def record_event(self, event) -> None:
		pass

	def collect(self, hold_s: float, cancel: Cancel, clock: Callable[[], float]) -> list[tuple[BoardStatus, float]] | None:
		"""The polls that arrive during hold_s; None when cancelled."""
		samples = []
		deadline = clock() + hold_s
		while (left := deadline - clock()) > 0:
			if cancel.is_set:
				return None
			try:
				samples.append(self._queue.get(timeout=min(_WAIT_S, left)))
			except queue.Empty:
				pass
		return None if cancel.is_set else samples


@dataclass(frozen=True)
class SweepResult:
	rows: tuple[SweepRow, ...]
	ended_early: bool
	path: Path | None
	cleanup_problem: str | None = None
	"""Set when duty 0 or the board's mode could not be put back at the end."""


def run_sweep(
	session,
	role: Role,
	duties: Sequence[int],
	hold_s: float,
	*,
	path: Path | None = None,
	cancel: Cancel | None = None,
	on_step: Callable[[int, int, SweepRow], None] | None = None,
	clock: Callable[[], float] = time.monotonic,
) -> SweepResult:
	"""Switches the board to open loop for the sweep and back to the mode it was in after, with duty
	0 (switching mode stops the motor). on_step(index, count, row) is called from this thread."""
	if not duties:
		raise ValueError("no duties to sweep")
	if hold_s <= 0:
		raise ValueError(f"hold {hold_s} s must be > 0")
	cancel = cancel or Cancel()
	board = session.board(role)
	previous_mode = board.settings().mode
	feed = PollFeed(role)
	rows: list[SweepRow] = []
	ended_early = False
	session.add_sink(feed)
	file = Path(path).open("w", newline="", encoding="utf-8") if path is not None else None
	try:
		writer = csv.writer(file) if file else None
		if writer:
			writer.writerow(SWEEP_COLUMNS)
		with cancel.command():
			if previous_mode is not Mode.OPEN_LOOP:
				board.set_mode(Mode.OPEN_LOOP)
		for index, duty in enumerate(duties):
			with cancel.command():
				board.set_pwm(duty)
			samples = feed.collect(hold_s, cancel, clock)
			if samples is None:
				ended_early = True
				break
			if not samples:
				continue
			values = sweep_row(duty, [status for status, _ in samples])
			if writer:
				writer.writerow(values)
				file.flush()
			row = SweepRow(duty=values[0], mean_raw_hz=values[1], samples=values[6], stalled_samples=values[7])
			rows.append(row)
			if on_step:
				on_step(index, len(duties), row)
	except Cancelled:
		ended_early = True
	finally:
		session.remove_sink(feed)
		if file:
			file.close()
		cleanup_problem = _put_back(board, previous_mode)
	return SweepResult(tuple(rows), ended_early, Path(path) if path is not None else None, cleanup_problem)


def _put_back(board, previous_mode: Mode) -> str | None:
	"""Never raises: a failure here must not hide the sweep's own error."""
	try:
		board.set_pwm(0)
		if previous_mode is not Mode.OPEN_LOOP:
			board.set_mode(previous_mode)
	except Exception as error:
		return f"could not put duty 0 and {previous_mode.name.lower().replace('_', ' ')} back ({error})"
	return None

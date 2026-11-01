"""One board runs a job of its own segments to the end: the board times it on its 10 ms tick and
stops the motor itself when it is DONE. Used for a calibration run (one speed for T seconds) and for
the Manual screen's timed runs (a speed or a g/s for N seconds)."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..devices import Segment
from ..protocol.constants import JobState, Mode, Role
from .cancel import Cancel, Cancelled
from .sweep import PollFeed

ENDED = (JobState.DONE, JobState.ABORTED)
SLACK_S = 3.0
"""How long past the job's own time to wait for the board to report the end."""


class TimedRunError(RuntimeError):
	pass


@dataclass(frozen=True)
class TimedRunResult:
	state: JobState
	"""DONE when the board ran the whole job; ABORTED when it stopped it (comms timeout, STOP)."""
	total_ms: int
	elapsed_ms: int
	edges: int
	"""Encoder edges counted from the start to the end of the job."""
	ended_early: bool
	cleanup_problem: str | None = None

	@property
	def done(self) -> bool:
		return self.state is JobState.DONE and not self.ended_early


def speed_segments(speed_hz: float, duration_s: float) -> list[Segment]:
	"""One step: the board goes to speed_hz at once and holds it for duration_s."""
	if not speed_hz > 0:
		raise TimedRunError(f"speed {speed_hz:g} Hz: a timed run needs a speed above 0")
	duration_ms = round(duration_s * 1000)
	if duration_ms < 10:
		raise TimedRunError(f"{duration_s:g} s is shorter than one 10 ms control tick")
	return [Segment(value=float(speed_hz), duration_ms=duration_ms)]


def run_segments(
	session,
	role: Role,
	segments: Sequence[Segment],
	*,
	cancel: Cancel | None = None,
	on_progress: Callable[[int, int], None] | None = None,
	clock: Callable[[], float] = time.monotonic,
) -> TimedRunResult:
	"""Speed segments need closed loop (switched to if needed) and gains on the board.
	on_progress(elapsed_ms, total_ms) is called from this thread at the poll rate."""
	cancel = cancel or Cancel()
	board = session.board(role)
	total_ms = sum(segment.duration_ms for segment in segments)
	duty = bool(segments) and segments[0].duty
	feed = PollFeed(role)
	session.add_sink(feed)
	start_edges = None
	last = None
	ended_early = False
	cleanup_problem = None
	try:
		with cancel.command():
			wanted = Mode.OPEN_LOOP if duty else Mode.CLOSED_LOOP
			if board.settings().mode is not wanted:
				board.set_mode(wanted)
			board.upload_job(segments)
			start_edges = session.poll_now(role).report.edge_count
			board.run_job()
		deadline = clock() + total_ms / 1000 + SLACK_S
		while True:
			samples = feed.collect(0.1, cancel, clock)
			if samples is None:
				raise Cancelled()
			for status, _ in samples:
				last = status.report
				if on_progress and last.job_state is JobState.RUNNING:
					on_progress(last.job_elapsed_ms, total_ms)
			if last is not None and last.job_state in ENDED:
				break
			if clock() > deadline:
				raise TimedRunError(f"the board did not report the end of the job {SLACK_S:g} s after its "
									f"{total_ms / 1000:g} s; it was sent STOP")
	except Cancelled:
		ended_early = True
	except BaseException:
		cleanup_problem = _stop(board)
		raise
	finally:
		session.remove_sink(feed)
	if ended_early:
		cleanup_problem = _stop(board)
		try:
			last = session.poll_now(role).report
		except Exception:
			pass
	state = last.job_state if last is not None else JobState.ABORTED
	elapsed_ms = last.job_elapsed_ms if last is not None else 0
	edges = (last.edge_count - start_edges) if last is not None and start_edges is not None else 0
	return TimedRunResult(state, total_ms, elapsed_ms, edges, ended_early, cleanup_problem)


def _stop(board) -> str | None:
	try:
		board.stop()
	except Exception as error:
		return f"STOP did not reach the board ({error}); it stops itself at its comms timeout"
	return None

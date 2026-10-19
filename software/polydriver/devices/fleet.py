"""Both dispensers as one: upload a job to each, start them together, stop them together (C6.4).

Every RUN and STOP keeps the send and reply time per board, so the start gap between the two
boards is measured on every run (thesis 6.3).
"""

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from ..protocol.constants import Command, JobState, Role, StatusFlag
from ..requests import JobInfo, JobRun, StatusReport, Stop
from ..requests.request_queue import Exchange
from .board import Segment
from .link import DeviceError
from .session import NotConnectedError, Session

logger = logging.getLogger(__name__)

ROLES = (Role.POLYMER, Role.COLOUR)
# Longer than the queue can take for one request: 4 attempts of 0.2 s.
ANSWER_TIMEOUT_S = 5.0


class FleetError(DeviceError):
	pass


class FleetStateError(FleetError):
	pass


class FleetUploadError(FleetError):
	def __init__(self, failures: dict[Role, Exception]):
		self.failures = failures
		super().__init__("upload failed: " + "; ".join(f"{_name(r)}: {e}" for r, e in failures.items()))


@dataclass(frozen=True)
class BoardTiming:
	"""Host clock (perf_counter). The board ran the command between first_sent_s and replied_s."""

	role: Role
	first_sent_s: float | None
	sent_s: float | None
	replied_s: float | None
	attempts: int
	error: str | None = None

	@property
	def ok(self) -> bool:
		return self.error is None

	@property
	def round_trip_s(self) -> float | None:
		if self.sent_s is None or self.replied_s is None:
			return None
		return self.replied_s - self.sent_s


@dataclass(frozen=True)
class FleetCommand:
	command: Command
	timings: dict[Role, BoardTiming]

	@property
	def ok(self) -> bool:
		return all(timing.ok for timing in self.timings.values())

	@property
	def failures(self) -> dict[Role, str]:
		return {role: timing.error for role, timing in self.timings.items() if timing.error is not None}

	def gap_s(self, first: Role = Role.POLYMER, second: Role = Role.COLOUR) -> float | None:
		"""How much later second got the command, from the first sends. Both frames are equally long,
		so each board sees its frame the same time after the send."""
		pair = self._answered(first, second)
		if pair is None:
			return None
		return pair[1].first_sent_s - pair[0].first_sent_s

	def gap_bounds_s(self, first: Role = Role.POLYMER, second: Role = Role.COLOUR) -> tuple[float, float] | None:
		"""The smallest and largest gap the times allow."""
		pair = self._answered(first, second)
		if pair is None:
			return None
		a, b = pair
		return b.first_sent_s - a.replied_s, b.replied_s - a.first_sent_s

	def _answered(self, first: Role, second: Role) -> tuple[BoardTiming, BoardTiming] | None:
		a, b = self.timings.get(first), self.timings.get(second)
		if a is None or b is None or not (a.ok and b.ok) or a.replied_s is None or b.replied_s is None:
			return None
		return a, b


class FleetRunError(FleetError):
	def __init__(self, run: FleetCommand, stop: FleetCommand):
		self.run = run
		self.stop = stop
		failed = "; ".join(f"{_name(role)}: {error}" for role, error in run.failures.items())
		super().__init__(f"the job did not start on every board ({failed}); STOP sent to all")


class Fleet:
	def __init__(self, session: Session, roles: Sequence[Role] = ROLES):
		self._session = session
		self._roles = tuple(roles)
		self._uploaded: tuple[Role, ...] = ()
		self._duty_jobs: set[Role] = set()
		self.history: list[FleetCommand] = []

	def upload(self, jobs: Mapping[Role, Sequence[Segment]]) -> dict[Role, JobInfo]:
		"""Loads each board at the same time; returns once every board is READY. If any board fails,
		the others are cleared, so no board is left READY with only its half of the job."""
		if not jobs:
			raise ValueError("upload() needs a job for at least one board")
		boards = {role: self._session.board(role) for role in jobs}
		self._uploaded = ()
		with ThreadPoolExecutor(len(boards), thread_name_prefix="upload") as pool:
			futures = {role: pool.submit(board.upload_job, jobs[role]) for role, board in boards.items()}
		infos: dict[Role, JobInfo] = {}
		failures: dict[Role, Exception] = {}
		for role, future in futures.items():
			try:
				info = future.result()
			except Exception as error:
				failures[role] = error
				continue
			if info.job_state is not JobState.READY:
				failures[role] = FleetStateError(f"the board reports {info.job_state.name} after the upload, not READY")
			infos[role] = info
		if failures:
			for role in infos.keys() - failures.keys():
				try:
					boards[role].clear_job()
				except Exception as error:
					logger.warning("%s board: clearing the job after a failed upload failed: %s", _name(role), error)
			raise FleetUploadError(failures)
		self._uploaded = tuple(jobs)
		self._duty_jobs = {role for role, segments in jobs.items() if segments[0].duty}
		return infos

	def run(self) -> FleetCommand:
		"""Checks every board can run its job, then sends both RUNs at once with polling held, so
		neither waits behind a STATUS. If any board does not start, all are stopped."""
		if not self._uploaded:
			raise FleetStateError("upload a job before run()")
		boards = {role: self._session.board(role) for role in self._uploaded}
		for role in boards:
			self._check_can_run(role, self._session.poll_now(role).report)
		with self._session.paused_polling():
			futures = {role: board.link.submit_timed(JobRun()) for role, board in boards.items()}
			command = self._collect(Command.JOB_RUN, futures)
		self._uploaded = ()
		if not command.ok:
			raise FleetRunError(command, self.stop())
		return command

	def _check_can_run(self, role: Role, report: StatusReport) -> None:
		"""The board's own JOB_RUN checks, made first so that neither board starts when one would refuse."""
		name = _name(role)
		if report.job_state is not JobState.READY:
			raise FleetStateError(f"the {name} board's job is {report.job_state.name}, not READY; upload it again")
		if StatusFlag.ESTOP in report.flags:
			raise FleetStateError(f"the {name} board is in ESTOP; clear it first")
		open_loop = StatusFlag.OPEN_LOOP in report.flags
		if role in self._duty_jobs:
			if not open_loop:
				raise FleetStateError(f"the {name} board has a duty job, which runs in open loop; it is in closed loop")
		elif open_loop:
			raise FleetStateError(f"the {name} board has a speed job, which runs in closed loop; it is in open loop")
		elif StatusFlag.UNCONFIGURED in report.flags:
			raise FleetStateError(f"the {name} board has no PID gains for its speed job; set [board.{name}] kp, ki, kd")

	def stop(self) -> FleetCommand:
		"""STOP to every board of the fleet at once. Never raises: a board it could not reach is in
		the result's failures."""
		futures = {}
		missing = {}
		for role in self._roles:
			try:
				futures[role] = self._session.board(role).link.submit_timed(Stop())
			except NotConnectedError as error:
				missing[role] = BoardTiming(role, None, None, None, 0, str(error))
			except Exception as error:
				missing[role] = BoardTiming(role, None, None, None, 0, f"STOP could not be sent: {error}")
		return self._collect(Command.STOP, futures, missing)

	def _collect(self, command: Command, futures: dict, missing: dict[Role, BoardTiming] | None = None) -> FleetCommand:
		timings = dict(missing or {})
		for role, future in futures.items():
			try:
				exchange: Exchange = future.result(ANSWER_TIMEOUT_S)
			except FutureTimeoutError:
				timings[role] = BoardTiming(role, None, None, None, 0, f"no answer within {ANSWER_TIMEOUT_S:g} s")
				continue
			except Exception as error:
				timings[role] = BoardTiming(role, None, None, None, 0, str(error))
				continue
			error = None if exchange.error is None else str(exchange.error)
			timings[role] = BoardTiming(role, exchange.first_sent_s, exchange.sent_s, exchange.replied_s,
										exchange.attempts, error)
		result = FleetCommand(command, {role: timings[role] for role in sorted(timings)})
		self.history.append(result)
		_log(result)
		return result


def _log(command: FleetCommand) -> None:
	for role, timing in command.timings.items():
		if timing.ok and timing.round_trip_s is not None:
			logger.info("%s %s: sent at %.6f s, ACK after %.2f ms, %d attempt(s)", command.command.name, _name(role),
						timing.first_sent_s, timing.round_trip_s * 1000, timing.attempts)
		else:
			logger.warning("%s %s: failed: %s", command.command.name, _name(role), timing.error)
	gap_s = command.gap_s()
	if command.command is Command.JOB_RUN and gap_s is not None:
		low_s, high_s = command.gap_bounds_s()
		logger.info("JOB_RUN start gap colour - polymer: %.3f ms (between %.3f and %.3f ms)",
					gap_s * 1000, low_s * 1000, high_s * 1000)


def _name(role: Role) -> str:
	return role.name.lower()

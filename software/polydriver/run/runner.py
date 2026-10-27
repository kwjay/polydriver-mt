"""Runs a gradient job on both boards (C8.4): compile, upload, start together, watch STATUS.

A run moves IDLE -> UPLOADED -> READY -> RUNNING -> DONE, or to ABORTED from any unfinished state.
upload() compiles the job (nothing reaches a board if it does not fit), sends each board its
material's gains and feedforward where the material has them, and uploads both halves; READY is
when both boards report READY. start() sends both RUNs at once (Fleet). From then on the runner
reads every STATUS the session polls: DONE when both boards are DONE, ABORTED when either is, or
on stop(). At the end the config's settings go back on the boards that had a material's, and the
run log gets the grams and the result and is closed.

Delivered grams are each board's encoder edges during its job times the grams per edge at the
speed the board measures (the calibration's g/s over Hz), so no encoder pulses per revolution are
needed. The edges count the material; the speed only picks the grams per edge, which changes slowly
with it. Speed taken from edges over the poll interval instead would be coarse (0 to 3 edges per
100 ms) and, through the bend of the calibration, biased: +1.4 % on the simulated boards.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ..calibration import CalibrationStore, Curve
from ..devices.board import BoardStatus
from ..devices.fleet import Fleet, FleetCommand, FleetRunError
from ..devices.session import Event, Session
from ..jobs import ERROR_LIMIT, GradientJob, GradientProgram, compile_gradient_job, job_dict
from ..jobs.program import ChannelCalibration
from ..materials import MaterialRegistry
from ..protocol.constants import JobState, Role
from ..requests import StatusReport
from ..telemetry import RunLog, log_board

logger = logging.getLogger(__name__)

ROLES = (Role.POLYMER, Role.COLOUR)
EDGE_WRAP = 2**32


class RunState(StrEnum):
	IDLE = "IDLE"
	UPLOADED = "UPLOADED"
	READY = "READY"
	RUNNING = "RUNNING"
	DONE = "DONE"
	ABORTED = "ABORTED"


FORWARD = {
	RunState.IDLE: (RunState.UPLOADED, RunState.ABORTED),
	RunState.UPLOADED: (RunState.READY, RunState.ABORTED),
	RunState.READY: (RunState.RUNNING, RunState.ABORTED),
	RunState.RUNNING: (RunState.DONE, RunState.ABORTED),
}


class RunStateError(Exception):
	pass


class RunStates:
	"""The run's state and every state it has been in, with the time it got there."""

	def __init__(self, clock: Callable[[], float] = time.perf_counter):
		self._clock = clock
		self._lock = threading.Lock()
		self.history: list[tuple[RunState, float]] = [(RunState.IDLE, clock())]

	@property
	def state(self) -> RunState:
		return self.history[-1][0]

	@property
	def finished(self) -> bool:
		return self.state in (RunState.DONE, RunState.ABORTED)

	def move(self, to: RunState, hint: str = "") -> None:
		with self._lock:
			if to not in FORWARD.get(self.state, ()):
				raise RunStateError(f"a run that is {self.state} cannot become {to}{hint}")
			self.history.append((to, self._clock()))


class DeliveredGrams:
	"""Grams one board delivered during its job, from its STATUS edge count, job clock and measured
	speed. Each poll interval adds its edges times the grams per edge at the interval's mean speed;
	the poll that reports the job ended uses the speed before it, as the board no longer drives. With
	no speed measured (a stalled encoder) the interval's edges over its time stand in. Counting starts
	from the READY count (the screw stands while the job waits), or from the first poll seen; a
	finished job's clock stands still, so edges after DONE or ABORTED (coasting) add nothing.
	Outside the calibration the nearest end's grams per edge are used."""

	def __init__(self, curve: Curve):
		self._calibration = ChannelCalibration(curve)
		self._last: tuple[int, int, float] | None = None
		self.grams = 0.0
		self.counted_from_ms: int | None = None
		self.counted_to_ms: int | None = None

	def add(self, report: StatusReport) -> None:
		state, edges, elapsed_ms = report.job_state, report.edge_count, report.job_elapsed_ms
		speed_hz = report.raw_speed_hz
		if state is JobState.READY:
			self._last, self.grams = (edges, 0, speed_hz), 0.0
			self.counted_from_ms = self.counted_to_ms = 0
			return
		if state not in (JobState.RUNNING, JobState.DONE, JobState.ABORTED):
			return
		if self._last is None:
			self._last = (edges, elapsed_ms, speed_hz)
			self.counted_from_ms = self.counted_to_ms = elapsed_ms
			return
		last_edges, last_ms, last_hz = self._last
		length_ms = elapsed_ms - last_ms
		if length_ms <= 0:
			return
		count = (edges - last_edges) % EDGE_WRAP
		mean_hz = (last_hz + speed_hz) / 2 if state is JobState.RUNNING else last_hz
		if mean_hz <= 0:
			mean_hz = count / (length_ms / 1000)
		if count:
			self.grams += count * self._rate(mean_hz) / mean_hz
		self._last, self.counted_to_ms = (edges, elapsed_ms, speed_hz), elapsed_ms

	def _rate(self, speed_hz: float) -> float:
		speeds, rates = self._calibration.speeds, self._calibration.rates
		if speed_hz <= speeds[0]:
			return rates[0] * speed_hz / speeds[0] if speeds[0] > 0 else rates[0]
		if speed_hz >= speeds[-1]:
			return rates[-1] * speed_hz / speeds[-1]
		return self._calibration.rate(speed_hz)


@dataclass(frozen=True)
class ChannelProgress:
	role: str
	job_state: JobState | None
	segment_index: int | None
	elapsed_s: float
	delivered_g: float
	expected_g: float | None


@dataclass(frozen=True)
class RunProgress:
	state: RunState
	elapsed_s: float
	total_s: float
	fraction: float
	polymer: ChannelProgress
	colour: ChannelProgress


class Runner:
	def __init__(self, session: Session, job: GradientJob, materials: MaterialRegistry, store: CalibrationStore, *,
				run_log: RunLog | None = None, error_limit: float = ERROR_LIMIT,
				clock: Callable[[], float] = time.perf_counter):
		self._session = session
		self._job = job
		self._materials = materials
		self._store = store
		self._log = run_log
		self._error_limit = error_limit
		self._fleet = Fleet(session)
		self._states = RunStates(clock)
		self._lock = threading.Lock()
		self._latest: dict[Role, StatusReport] = {}
		self._grams: dict[Role, DeliveredGrams] = {}
		self._own_settings: set[Role] = set()
		self._ending = False
		self._finished = threading.Event()
		self.program: GradientProgram | None = None

	@property
	def state(self) -> RunState:
		return self._states.state

	@property
	def history(self) -> list[tuple[RunState, float]]:
		return list(self._states.history)

	# --- Steps ---------------------------------------------------------------------------

	def upload(self) -> GradientProgram:
		if self.state is not RunState.IDLE:
			raise RunStateError(f"a run that is {self.state} cannot become {RunState.UPLOADED}; start a new run")
		limit = min(self._session.board(role).link.version.max_segments for role in ROLES)
		program = compile_gradient_job(self._job, self._materials, self._store, error_limit=self._error_limit,
									max_segments=limit)
		self._use_material_settings()
		with self._lock:
			self._grams = {role: DeliveredGrams(self._store.curve(getattr(program, _name(role)).dispenser,
																	getattr(program, _name(role)).material))
						for role in ROLES}
		self._session.add_sink(self)
		try:
			self._fleet.upload({role: getattr(program, _name(role)).segments for role in ROLES})
		except Exception:
			self._session.remove_sink(self)
			with self._lock:
				self._latest.clear()
				self._grams.clear()
			self._restore_settings()
			raise
		self.program = program
		self._states.move(RunState.UPLOADED)
		self._log_upload(program)
		for role in ROLES:
			try:
				self._session.poll_now(role)
			except Exception as error:
				logger.warning("%s board: polling after the upload failed: %s; READY waits for the next poll",
							_name(role), error)
		return program

	def start(self) -> FleetCommand:
		if self.state is not RunState.READY:
			hint = {
				RunState.IDLE: "; upload the job first",
				RunState.UPLOADED: "; wait until both boards report READY",
			}.get(self.state, "; start a new run")
			raise RunStateError(f"a run that is {self.state} cannot become {RunState.RUNNING}{hint}")
		try:
			command = self._fleet.run()
		except FleetRunError as error:
			self._log_command(error.run)
			self._log_command(error.stop)
			self._end(RunState.ABORTED)
			raise
		self._states.move(RunState.RUNNING)
		self._log_command(command)
		return command

	def stop(self) -> FleetCommand:
		"""STOP to both boards, whatever the state; an unfinished run ends ABORTED."""
		command = self._fleet.stop()
		self._log_command(command)
		self._end(RunState.ABORTED)
		return command

	def wait(self, timeout_s: float | None = None) -> RunState:
		"""Until the run has ended and been wound up (settings back, log closed)."""
		self._finished.wait(timeout_s)
		return self.state

	def progress(self) -> RunProgress:
		with self._lock:
			latest = dict(self._latest)
			grams = {role: g.grams for role, g in self._grams.items()}
		total_s = (self.program.total_ms if self.program else round(self._job.profile.duration_s * 1000)) / 1000
		channels = {}
		for role in ROLES:
			report = latest.get(role)
			channel = getattr(self.program, _name(role)) if self.program else None
			channels[role] = ChannelProgress(
				_name(role), report.job_state if report else None, report.segment_index if report else None,
				report.job_elapsed_ms / 1000 if report else 0.0, grams.get(role, 0.0),
				channel.expected_grams if channel else None,
			)
		elapsed_s = max(c.elapsed_s for c in channels.values())
		return RunProgress(self.state, elapsed_s, total_s, elapsed_s / total_s, channels[Role.POLYMER],
						channels[Role.COLOUR])

	# --- What the session polls ------------------------------------------------------------

	def record_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		end = None
		with self._lock:
			if role not in ROLES:
				return
			report = status.report
			self._latest[role] = report
			if role in self._grams:
				self._grams[role].add(report)
			if len(self._latest) < len(ROLES):
				return
			states = {r.job_state for r in self._latest.values()}
			if self.state is RunState.UPLOADED and states == {JobState.READY}:
				self._states.move(RunState.READY)
			elif self.state is RunState.RUNNING:
				if JobState.ABORTED in states:
					end = RunState.ABORTED
				elif states == {JobState.DONE}:
					end = RunState.DONE
		if end is not None:
			self._end(end)

	def record_event(self, event: Event) -> None:
		pass

	# --- Ending --------------------------------------------------------------------------------

	def _end(self, state: RunState) -> None:
		"""Moves to state once and winds the run up on its own thread, so a poll thread is never held."""
		with self._lock:
			if self._ending:
				return
			self._ending = True
			if not self._states.finished:
				self._states.move(state)
		threading.Thread(target=self._wind_up, name="run-end", daemon=True).start()

	def _wind_up(self) -> None:
		try:
			self._session.remove_sink(self)
			self._restore_settings()
			if self._log is not None:
				with self._lock:
					grams = {role: g.grams for role, g in self._grams.items()}
				for role, delivered in grams.items():
					self._log.set_grams(role, estimated_g=delivered)
				self._log_run()
				self._log.close(result=self.state.value)
		except Exception:
			logger.exception("winding up the run failed")
		finally:
			self._finished.set()

	# --- Settings ----------------------------------------------------------------------------

	def _use_material_settings(self) -> None:
		config = self._session.config
		for role in ROLES:
			base = config.boards[role]
			settings = self._materials.get(getattr(self._job, _name(role)).material).board_settings(base)
			if settings != base:
				self._session.apply_settings(role, settings)
				self._own_settings.add(role)

	def _restore_settings(self) -> None:
		"""The config's settings back on the boards that had their material's. Polled first, so the
		session sees the job has ended."""
		for role in sorted(self._own_settings):
			try:
				self._session.poll_now(role)
				self._session.apply_settings(role)
			except Exception as error:
				logger.warning("%s board: putting the config's settings back failed: %s", _name(role), error)
		self._own_settings.clear()

	# --- Run log -------------------------------------------------------------------------------

	def _log_upload(self, program: GradientProgram) -> None:
		if self._log is None:
			return
		self._log.set_job(job_dict(self._job))
		for role in ROLES:
			channel = getattr(program, _name(role))
			if role in self._own_settings:
				log_board(self._log, self._session, role)
			self._log.set_segments(role, channel.segments, channel.curve_hash)
			self._log.set_grams(role, expected_g=channel.expected_grams)
		self._log_run()

	def _log_command(self, command: FleetCommand) -> None:
		if self._log is not None:
			self._log.add_fleet_command(command)

	def _log_run(self) -> None:
		self._log.set_run({
			"error_limit": self._error_limit,
			"states": [{"state": state.value, "t_s": self._log.relative_s(t)} for state, t in self.history],
		})


def _name(role: Role) -> str:
	return role.name.lower()

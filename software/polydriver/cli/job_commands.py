"""Job commands (C9.4): a job on one board, and gradient job files for both.

A one-board job is rows of g/s, turned into speeds by the material's calibration (the one the
calibrate commands keep, so a new measurement counts at once), or rows of duty for the bench. A
gradient job file is checked against both calibrations when loaded; `run` sends it to both boards.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..calibration import CalibrationStore
from ..jobs import (
	DEFAULT_MAX_SEGMENTS, CompiledJob, DutyRow, GradientJob, RateRow, check_job, compile_duty_job,
	compile_gradient_job, compile_rate_job, load_job,
)
from ..jobs.check import KIND_OF_ROLE
from ..materials import Material, MaterialRegistry, load_materials
from ..protocol.constants import Role
from .base import describe
from .parsing import UsageError, parse_duty, parse_number, parse_role, role_name, split_args

NEW_USAGE = "job new [MATERIAL [DISPENSER]]"
ADD_USAGE = "job add [duty] VALUE SECONDS [ramp]"
REMOVE_USAGE = "job remove N"
UPLOAD_USAGE = "job upload ROLE"
RUN_USAGE = "job run [ROLE]"
STOP_USAGE = "job stop [ROLE]"
FILE_USAGE = "job load FILE | job validate FILE"
NO_JOB = "no job yet (`job new MATERIAL` for g/s, `job new` for duty)"


@dataclass
class BenchJob:
	"""material None: a duty job."""

	material: Material | None
	dispenser: str | None
	rows: list[RateRow | DutyRow] = field(default_factory=list)

	def compile(self, store: CalibrationStore, max_segments: int) -> CompiledJob:
		if self.material is None:
			return compile_duty_job(self.rows, max_segments=max_segments)
		curve = store.curve(self.dispenser, self.material.name)
		return compile_rate_job(self.material, curve, self.rows, max_segments=max_segments)


class JobCommands:
	bench_job: BenchJob | None = None
	job_role: Role | None = None
	gradient_job: GradientJob | None = None
	gradient_job_path: Path | None = None

	def do_job(self, arg: str) -> None:
		"""job [new | add | remove | show | upload | run | stop | load | validate]  - a job on one board, or a gradient job file

		job new MATERIAL [DISPENSER]  - a g/s job by the material's calibration (its own dispenser unless given)
		job new  - a duty job, for the bench; the board must be in open loop (`dev mode ROLE open`)
		job add [duty] VALUE SECONDS [ramp]  - hold VALUE g/s (or duty) for SECONDS, or ramp to it from the row before
		job remove N  - drop row N
		job show  - the rows, the speeds they compile to and the grams expected
		job upload ROLE  - send the job to that board, which reports READY
		job run [ROLE]  - start it (on the board it was uploaded to)
		job stop [ROLE]  - STOP that board
		job load FILE  - a gradient job file, checked against both calibrations; `run upload` sends it to both boards
		job validate FILE  - check a gradient job file without loading it
		A FILE that is only a name is looked for in [paths] jobs, with .json added.
		"""
		words = split_args(arg)
		action, rest = (words[0], words[1:]) if words else ("show", [])
		actions = {
			"new": self._job_new, "add": self._job_add, "remove": self._job_remove, "show": self._job_show,
			"upload": self._job_upload, "run": self._job_run, "stop": self._job_stop,
			"load": lambda words: self._job_file(words, keep=True),
			"validate": lambda words: self._job_file(words, keep=False),
		}
		if action not in actions:
			raise UsageError(f"{action!r} is not a job action")
		actions[action](rest)

	# --- One board ------------------------------------------------------------------------------

	def _job_new(self, words: list[str]) -> None:
		if len(words) > 2:
			raise UsageError("a material and maybe a dispenser, or nothing for a duty job", usage=NEW_USAGE)
		if not words:
			self.bench_job = BenchJob(None, None)
			self.say("  new duty job, for the bench: the board must be in open loop (`dev mode ROLE open`); "
					"`job add duty DUTY SECONDS [ramp]`")
			return
		material = self._materials().get(words[0])
		dispenser = words[1] if len(words) == 2 else material.dispenser
		curve = self._calibration_store().curve(dispenser, material.name)
		self.bench_job = BenchJob(material, dispenser)
		self.say(f"  new g/s job for {material.name} on dispenser {dispenser} (calibration {dispenser} / "
				f"{material.name}, curve {curve.short_hash}); `job add G/S SECONDS [ramp]`")

	def _job_add(self, words: list[str]) -> None:
		job = self.bench_job
		if job is None:
			self.say(f"  {NO_JOB}")
			return
		duty = words[:1] == ["duty"]
		if duty:
			words = words[1:]
		if len(words) not in (2, 3):
			raise UsageError("a value and seconds, maybe ramp", usage=ADD_USAGE)
		if len(words) == 3 and words[2] != "ramp":
			raise UsageError(f"{words[2]!r} is not ramp", usage=ADD_USAGE)
		if duty and job.material is not None:
			self.say(f"  this is a g/s job for {job.material.name}; `job new` starts a duty job")
			return
		if not duty and job.material is None:
			self.say("  this is a duty job; `job new MATERIAL` starts a g/s job")
			return
		try:
			value = parse_duty(words[0], "DUTY") if duty else parse_number(words[0], "G/S")
			seconds = parse_number(words[1], "SECONDS")
		except UsageError as error:
			raise UsageError(str(error), usage=ADD_USAGE) from None
		if seconds <= 0:
			raise UsageError(f"SECONDS must be more than 0, not {words[1]!r}", usage=ADD_USAGE)
		ramp = len(words) == 3
		job.rows.append(DutyRow(value, seconds, ramp) if duty else RateRow(value, seconds, ramp))
		self._job_show([])

	def _job_remove(self, words: list[str]) -> None:
		job = self.bench_job
		if job is None:
			self.say(f"  {NO_JOB}")
			return
		if len(words) != 1:
			raise UsageError("the row's number", usage=REMOVE_USAGE)
		try:
			number = int(words[0])
		except ValueError:
			raise UsageError(f"N must be a whole number, not {words[0]!r}", usage=REMOVE_USAGE) from None
		if not 1 <= number <= len(job.rows):
			self.say(f"  there is no row {number}; the job has {len(job.rows)}")
			return
		del job.rows[number - 1]
		self.say(f"  removed row {number}")
		self._job_show([])

	def _job_show(self, words: list[str]) -> None:
		if words:
			raise UsageError("no words", usage="job show")
		job = self.bench_job
		if job is None:
			self.say(f"  {NO_JOB}")
			return
		self.say(f"  g/s job for {job.material.name} on {job.dispenser} / {job.material.name}:"
				if job.material is not None else "  duty job:")
		if not job.rows:
			self.say(f"  no rows yet (`{ADD_USAGE}`)")
			return
		try:
			compiled, problem = job.compile(self._calibration_store(), DEFAULT_MAX_SEGMENTS), None
		except Exception as error:
			if describe(error) is None:
				raise
			compiled, problem = None, str(error)
		for number, row in enumerate(job.rows, start=1):
			shape = "ramp to" if row.ramp else "step to"
			value = f"duty {row.duty:g}" if isinstance(row, DutyRow) else f"{row.rate_g_s:g} g/s"
			line = f"  {number:3d}  {shape} {value} for {row.duration_s:g} s"
			if compiled is not None and compiled.expected_grams_per_row is not None:
				speeds = [s.value for s, r in zip(compiled.segments, compiled.row_of_segment) if r == number]
				line += (f"   -> {', '.join(f'{hz:.4g}' for hz in speeds)} Hz, "
						f"{compiled.expected_grams_per_row[number - 1]:.4g} g")
			self.say(line)
		if compiled is None:
			self.say(f"  ! {problem}; the job can't be uploaded")
			return
		count = len(compiled.segments)
		text = f"  {compiled.total_ms / 1000:g} s, {count} segment{'' if count == 1 else 's'}"
		if compiled.expected_grams is not None:
			text += f", {compiled.expected_grams:.4g} g expected"
		self.say(text)

	def _job_upload(self, words: list[str]) -> None:
		if len(words) != 1:
			raise UsageError("which board", usage=UPLOAD_USAGE)
		role = parse_role(words[0])
		job = self.bench_job
		if job is None:
			self.say(f"  {NO_JOB}")
			return
		board = self.session.board(role)
		compiled = job.compile(self._calibration_store(), board.link.version.max_segments)
		name = role_name(role)
		material = job.material
		if material is not None and KIND_OF_ROLE[name] is not material.kind:
			self.say(f"  ! {material.name} is a {material.kind.value}, and this is the {name} board")
		info = board.upload_job(compiled.segments)
		self.job_role = role
		self.say(f"  {name}: {info.job_state.name}, {info.segment_count} segments, {info.total_ms} ms "
				"(`job run` starts it)")

	def _job_run(self, words: list[str]) -> None:
		role = self._job_board(words, RUN_USAGE)
		self.session.board(role).run_job()
		name = role_name(role)
		self.say(f"  {name}: running (`status {name}` follows it, `job stop` aborts it)")

	def _job_stop(self, words: list[str]) -> None:
		role = self._job_board(words, STOP_USAGE)
		self.session.board(role).stop()
		self.say(f"  STOP sent to {role_name(role)}")

	def _job_board(self, words: list[str], usage: str) -> Role:
		if len(words) > 1:
			raise UsageError("at most one board", usage=usage)
		if words:
			return parse_role(words[0])
		if self.job_role is None:
			raise UsageError("which board: no job was uploaded yet", usage=usage)
		return self.job_role

	# --- Gradient job files ----------------------------------------------------------------------

	def _job_file(self, words: list[str], keep: bool) -> None:
		if len(words) != 1:
			raise UsageError("one job file", usage=FILE_USAGE)
		path = self._job_path(words[0])
		job = load_job(path)
		materials, store = self._materials(), self._calibration_store()
		check = check_job(job, materials, store)
		for line in check.report():
			self.say(f"  {line}")
		program = None
		if check.ok:
			try:
				program = compile_gradient_job(job, materials, store)
			except Exception as error:
				if describe(error) is None:
					raise
				self.say(f"  error: {error}")
		if program is not None:
			for name in ("polymer", "colour"):
				channel = getattr(program, name)
				self.say(f"  {name}: {channel.material} on dispenser {channel.dispenser}, {len(channel.segments)} "
						f"segments, {channel.expected_grams:.4g} g expected")
			self.say(f"  {program.total_ms / 1000:g} s")
		if not keep:
			self.say(f"  {path.name}: " + ("it can run (`job load` loads it)" if program else "it cannot run"))
			return
		if program is None:
			still = f"; still {self.gradient_job.name}" if self.gradient_job is not None else ""
			self.say(f"  not loaded{still}")
			return
		self.gradient_job, self.gradient_job_path = job, path
		self.say(f"  loaded {path}: {job.name}; `run upload` sends it to both boards")

	def _job_path(self, text: str) -> Path:
		path = Path(text)
		if path.exists() or len(path.parts) > 1:
			return path
		return self.config.resolve(self.config.paths.jobs) / (text if path.suffix else f"{text}.json")

	def _materials(self) -> MaterialRegistry:
		return load_materials(self.config.resolve(self.config.paths.materials))

"""A gradient job on two simulated boards, end to end (C8.6). It goes from the job file through the
runner to the CSV and sidecar, as on the real boards, and then checks the log. A fault can be
forced on either board at a time into the run (seconds after the RUN).

From software/:

	python -m polydriver.sim.end_to_end examples/simulated/jobs/two-minute-gradient.json
	python -m polydriver.sim.end_to_end examples/simulated/jobs/two-minute-gradient.json --fault colour:jam@40

The config (by default examples/simulated/polydriver.toml) gives the gains, the logging and the
calibration and materials files; its ports are not used. The exit code is 0 when the log checks out,
1 when it does not, 2 when the run could not be set up.
"""

import argparse
import math
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from ..calibration import CalibrationError, load_calibration
from ..config import Config, ConfigError, load_config
from ..devices import Session
from ..jobs import GradientProgram, JobError, check_job, load_job
from ..materials import MaterialError, load_materials
from ..protocol.constants import Role
from ..run import Runner, RunProgress, RunState
from ..telemetry import LogCheck, check_run_log, start_run_log
from .fake_board import SimBench, SimBoard

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "examples" / "simulated" / "polydriver.toml"
ROLES = (Role.POLYMER, Role.COLOUR)
ROLE_NAMES = {role.name.lower(): role for role in ROLES}
ENDED = (RunState.DONE, RunState.ABORTED)
# How long past its total time a run may take before it is stopped: the safety rule's overtime
# plus the wind-up.
SPARE_S = 10.0
PROGRESS_EVERY_S = 10.0


def _noise(board: SimBoard) -> None:
	board.corrupt_replies = board.corrupt_requests = board.drop_replies = 1


def _fast_clock(board: SimBoard) -> None:
	board.clock_error = 0.005


def _jam(board: SimBoard) -> None:
	board.jammed = True


FAULT_KINDS: dict[str, tuple[str, Callable[[SimBoard], None]]] = {
	"jam": ("the screw jams: it stops at once and no edges come", _jam),
	"link-lost": ("the USB cable is pulled; the board runs on until its comms timeout", SimBoard.lose_link),
	"restart": ("the board resets and loses its job", SimBoard.restart),
	"fast-clock": ("the board's clock runs 0.5 % fast from then on", _fast_clock),
	"noise": ("one corrupted reply, one corrupted request and one dropped reply", _noise),
}


class EndToEndError(Exception):
	pass


@dataclass(frozen=True)
class Fault:
	role: Role
	kind: str
	at_s: float

	def __post_init__(self) -> None:
		if self.role not in ROLES:
			raise ValueError(f"board {self.role!r}: polymer or colour")
		if self.kind not in FAULT_KINDS:
			raise ValueError(f"kind {self.kind!r}: {', '.join(FAULT_KINDS)}")
		if not (isinstance(self.at_s, (int, float)) and math.isfinite(self.at_s) and self.at_s >= 0):
			raise ValueError(f"time {self.at_s!r}: seconds after the start, >= 0")

	@classmethod
	def parse(cls, text: str) -> "Fault":
		"""BOARD:KIND@SECONDS, e.g. colour:jam@40."""
		board, _, rest = text.strip().partition(":")
		kind, at, seconds = rest.partition("@")
		if not (board and kind and at and seconds):
			raise ValueError(f"fault {text!r}: write it as BOARD:KIND@SECONDS, e.g. colour:jam@40")
		if board not in ROLE_NAMES:
			raise ValueError(f"fault {text!r}: board {board!r}: polymer or colour")
		try:
			at_s = float(seconds)
		except ValueError:
			raise ValueError(f"fault {text!r}: time {seconds!r}: seconds after the start, >= 0") from None
		try:
			return cls(ROLE_NAMES[board], kind, at_s)
		except ValueError as error:
			raise ValueError(f"fault {text!r}: {error}") from None

	def apply(self, board: SimBoard) -> None:
		FAULT_KINDS[self.kind][1](board)

	def __str__(self) -> str:
		return f"{self.role.name.lower()} {self.kind} at {self.at_s:g} s"


@dataclass(frozen=True)
class EndToEndRun:
	state: RunState
	reason: str | None
	warnings: tuple[str, ...]
	program: GradientProgram
	progress: RunProgress
	csv_path: Path
	sidecar_path: Path
	check: LogCheck
	bench: SimBench


def run_end_to_end(
	config: Config,
	job_path: Path,
	*,
	faults: Sequence[Fault] = (),
	note: str | None = None,
	on_progress: Callable[[RunProgress], None] | None = None,
	progress_every_s: float = PROGRESS_EVERY_S,
) -> EndToEndRun:
	"""The job is checked against its calibrations first; one that does not fit never reaches a
	board. The log goes to the config's log folder whatever [logging] enabled says."""
	job_path = Path(job_path)
	try:
		materials = load_materials(config.resolve(config.paths.materials))
		store = load_calibration(config.resolve(config.paths.calibration))
		job = load_job(job_path)
	except (MaterialError, CalibrationError, JobError) as error:
		raise EndToEndError(str(error)) from error
	job_check = check_job(job, materials, store)
	if not job_check.ok:
		raise EndToEndError(f"{job_path}: the job does not fit its calibrations:\n" + "\n".join(job_check.report()))

	bench = SimBench.pair()
	config = replace(config, ports=bench.ports)
	if note is None:
		note = f"simulated run of {job_path.name}" + "".join(f"; fault: {fault}" for fault in faults)
	with Session(config, link_factory=bench.link_factory) as session:
		for role in ROLES:
			session.connect(role)
		log = start_run_log(session, config, note=note)
		runner = Runner(session, job, materials, store, run_log=log)
		try:
			program = runner.upload()
		except Exception as error:
			log.close()
			raise EndToEndError(f"the job could not be uploaded: {error}") from error
		try:
			runner.start()
		except Exception:
			pass  # the run ends ABORTED with the reason, and the log is closed by its wind-up
		timers = [threading.Timer(fault.at_s, fault.apply, (bench[bench.ports[fault.role]],)) for fault in faults]
		for timer in timers:
			timer.daemon = True
			timer.start()
		try:
			deadline_s = program.total_ms / 1000 + runner.limits.overtime_s + SPARE_S
			waited_s = 0.0
			while runner.wait(progress_every_s) not in ENDED:
				waited_s += progress_every_s
				if on_progress is not None:
					on_progress(runner.progress())
				if waited_s > deadline_s:
					runner.stop()
			runner.wait(SPARE_S)
		finally:
			for timer in timers:
				timer.cancel()
		progress = runner.progress()
	return EndToEndRun(runner.state, runner.reason, progress.warnings, program, progress, log.csv_path,
					log.sidecar_path, check_run_log(log.csv_path), bench)


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
	out = stdout or sys.stdout
	parser = argparse.ArgumentParser(prog="python -m polydriver.sim.end_to_end",
									description="A gradient job on two simulated boards, logged and checked.")
	parser.add_argument("job", help="the gradient job file")
	parser.add_argument("--config", default=str(EXAMPLE_CONFIG),
						help="config file (default: examples/simulated/polydriver.toml)")
	parser.add_argument("--fault", action="append", default=[], metavar="BOARD:KIND@SECONDS",
						help=f"force a fault, e.g. colour:jam@40; kinds: {', '.join(FAULT_KINDS)} (may be repeated)")
	parser.add_argument("--note", default=None, help="the note in the sidecar")
	args = parser.parse_args(argv)

	def say(text: str) -> None:
		print(text, file=out, flush=True)

	try:
		faults = [Fault.parse(text) for text in args.fault]
		config_path = Path(args.config)
		if not config_path.exists():
			raise EndToEndError(f"no config file at {args.config}")
		config = load_config(config_path)
	except (ValueError, ConfigError, EndToEndError) as error:
		say(str(error))
		return 2

	job_path = Path(args.job)
	try:
		duration = f"{load_job(job_path).profile.duration_s:g} s"
	except JobError:
		duration = "?"
	say(f"{job_path.name}: {duration} on two simulated boards, " + (
		"; ".join(f"fault: {fault}" for fault in faults) if faults else "no fault"))

	def show(progress: RunProgress) -> None:
		say(f"  {progress.elapsed_s:6.1f} s ({progress.fraction:4.0%}): polymer {progress.polymer.delivered_g:.2f} g, "
			f"colour {progress.colour.delivered_g:.3f} g")

	try:
		run = run_end_to_end(config, job_path, faults=faults, note=args.note, on_progress=show)
	except EndToEndError as error:
		say(str(error))
		return 2
	say(f"run: {run.state}" + (f": {run.reason}" if run.reason else ""))
	for warning in run.warnings:
		say(f"warning: {warning}")
	for line in run.check.report():
		say(line)
	say(f"CSV: {run.csv_path}")
	say(f"sidecar: {run.sidecar_path}")
	return 0 if run.check.ok else 1


if __name__ == "__main__":
	sys.exit(main())

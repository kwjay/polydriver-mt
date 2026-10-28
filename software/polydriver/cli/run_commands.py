"""Run commands (C9.4): the loaded gradient job on both boards, through the runner (C8.4) with its
safety rules (C8.5) and, unless switched off, a run log (C8.2). The shell says when a run ends;
Ctrl+C and quit stop a run that is going, so it ends ABORTED with its log closed."""

import threading

from ..devices.fleet import FleetUploadError
from ..protocol.constants import Role
from ..run import Runner, RunState
from ..telemetry import RunLog, start_run_log
from .base import describe
from .parsing import UsageError, role_name, split_args

DISPENSERS = (Role.POLYMER, Role.COLOUR)
ENDED = (RunState.DONE, RunState.ABORTED)
UPLOAD_USAGE = "run upload [log | nolog]"
LOG_WORDS = {"log": True, "nolog": False}
# How long quit waits for a stopped run to wind up (STOP, the last polls, the log closed).
WIND_UP_S = 10.0


class RunCommands:
	runner: Runner | None = None
	runner_log: RunLog | None = None

	def do_run(self, arg: str) -> None:
		"""run [upload [log | nolog] | start | stop | status]  - the loaded gradient job on both boards

		run upload [log | nolog]  - compile the job, send it to both boards and wait for READY; logged
		    unless nolog (by default as the job file says, else as [logging] enabled says)
		run start  - start both boards together
		run stop  - STOP both boards; the run ends ABORTED
		run status  - the run's state, time, grams so far, warnings and log
		Ctrl+C and quit stop a run too. The shell says when a run ends, and why.
		"""
		words = split_args(arg)
		action, rest = (words[0], words[1:]) if words else ("status", [])
		actions = {"upload": self._run_upload, "start": self._run_start, "stop": self._run_stop, "status": self._run_status}
		if action not in actions:
			raise UsageError(f"{action!r} is not a run action")
		if action != "upload" and rest:
			raise UsageError("no words", usage=f"run {action}")
		actions[action](rest)

	def _run_upload(self, words: list[str]) -> None:
		if len(words) > 1 or (words and words[0] not in LOG_WORDS):
			raise UsageError(f"log or nolog, not {' '.join(words)!r}", usage=UPLOAD_USAGE)
		job = self.gradient_job
		if job is None:
			self.say("  no gradient job loaded (`job load FILE` first)")
			return
		if self.runner is not None and self.runner.state not in ENDED:
			self.say(f"  a run is {self.runner.state} (`run stop` first)")
			return
		if self.run_log is not None:
			self.say("  a run log is open (`log off` first; the run writes its own)")
			return
		for role in DISPENSERS:
			self.session.board(role)
		default = job.log if job.log is not None else self.config.logging.enabled
		logged = LOG_WORDS[words[0]] if words else default
		log = start_run_log(self.session, self.config, note=f"gradient job {job.name}") if logged else None
		runner = Runner(self.session, job, self._materials(), self._calibration_store(), run_log=log)
		try:
			program = runner.upload()
		except Exception as error:
			if log is not None:
				log.close()
			if isinstance(error, FleetUploadError):
				self.say("  not uploaded:")
				for role, failure in error.failures.items():
					self.say(f"  {role_name(role)}: {describe(failure, role) or failure}")
				return
			if describe(error) is None:
				raise
			self.say(f"  not uploaded: {describe(error)}")
			return
		self.runner, self.runner_log = runner, log
		self.say(f"  uploaded to both boards: polymer {len(program.polymer.segments)} segments, colour "
				f"{len(program.colour.segments)} segments, {program.total_ms / 1000:g} s")
		self.say(f"  log {log.csv_path}" if log is not None else "  no log")
		if runner.state is RunState.READY:
			self.say("  both boards READY: `run start`")
		else:
			self.say(f"  run {runner.state}: waiting for both boards to report READY (`run status`)")

	def _run_start(self, words: list[str]) -> None:
		runner = self.runner
		if runner is None:
			self.say("  no run uploaded (`run upload` first)")
			return
		command = runner.start()
		gap_s = command.gap_s()
		gap = f" {gap_s * 1000:.1f} ms apart" if gap_s is not None else ""
		self.say(f"  started both boards{gap}; `run status` follows it, `run stop` or Ctrl+C stops it")
		threading.Thread(target=self._say_when_ended, args=(runner, self.runner_log), name="run-ended",
						daemon=True).start()

	def _say_when_ended(self, runner: Runner, log: RunLog | None) -> None:
		state = runner.wait()
		self.say(f"! run {state}" + (f": {runner.reason}" if runner.reason else "")
				+ (f"; log {log.csv_path}" if log is not None else ""))

	def _run_stop(self, words: list[str]) -> None:
		runner = self.runner
		if runner is None:
			self.say("  no run yet")
			return
		command = runner.stop()
		failed = {role: t.error for role, t in command.timings.items() if t.error}
		if not failed:
			self.say("  STOP sent to both boards")
		for role, error in failed.items():
			self.say(f"  STOP FAILED on {role_name(role)}: {error}")

	def _run_status(self, words: list[str]) -> None:
		runner = self.runner
		if runner is None:
			self.say("  no run yet (`job load FILE`, then `run upload`)")
			return
		p = runner.progress()
		text = f"  run {p.state}"
		if p.state is RunState.RUNNING or p.state in ENDED:
			text += f": {p.elapsed_s:.1f} of {p.total_s:g} s ({p.fraction * 100:.0f} %)"
		self.say(text)
		for channel in (p.polymer, p.colour):
			state = channel.job_state.name if channel.job_state is not None else "not polled yet"
			segment = f" segment {channel.segment_index}" if channel.segment_index is not None else ""
			expected = f" of {channel.expected_g:.4g} g expected" if channel.expected_g is not None else ""
			self.say(f"  {channel.role}: {state}{segment}, {channel.delivered_g:.4g} g{expected}")
		if p.reason:
			self.say(f"  why: {p.reason}")
		for warning in p.warnings:
			self.say(f"  ! {warning}")
		self.say(f"  log {self.runner_log.csv_path}" if self.runner_log is not None else "  no log")

	# --- Stopping a run with the shell ------------------------------------------------------------

	def _stop_a_running_run(self) -> RunState | None:
		runner = self.runner
		if runner is None or runner.state in ENDED or runner.state is RunState.IDLE:
			return None
		try:
			runner.stop()
		except Exception as error:
			self.say(f"  stopping the run failed: {describe(error) or error}")
		return runner.state

	def interrupt(self) -> None:
		self._stop_a_running_run()
		super().interrupt()

	def before_quit(self) -> None:
		if self._stop_a_running_run() is not None:
			self.runner.wait(WIND_UP_S)
			self.say(f"  run stopped: {self.runner.state}")

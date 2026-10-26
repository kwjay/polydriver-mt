"""Log commands (C9.5): a run log of both boards (CSV + sidecar) by hand, and what only the person
at the bench knows: a note and the weighed mass. Note and mass also work after `log off`, on the
last run. Every command sent to a board while logging lands in the sidecar's events."""

from ..protocol.constants import Role
from ..telemetry import RunLog, start_run_log
from .parsing import UsageError, parse_number, parse_role, role_name, split_args


class LogCommands:
	run_log: RunLog | None = None
	last_run_log: RunLog | None = None

	def do_log(self, arg: str) -> None:
		"""log [on [NOTE] | off | note TEXT | mass ROLE GRAMS]  - log both boards to CSV + sidecar; `log` alone says where

		log on [NOTE]  - start a run log in [logging] folder (the boards that are connected)
		log off  - end it
		log note TEXT  - set the note of this run, or of the last one
		log mass ROLE GRAMS  - the weighed mass for one board, usually after `log off`
		"""
		words = split_args(arg)
		if not words:
			self._log_status()
			return
		action, rest = words[0], words[1:]
		if action == "on":
			self._log_on(" ".join(rest))
		elif action == "off" and not rest:
			self._log_off()
		elif action == "note" and rest:
			self._run_for_update().set_note(" ".join(rest))
			self.say(f"  note saved in {self.last_run_log.sidecar_path}")
		elif action == "mass":
			self._log_mass(rest)
		else:
			raise UsageError(f"{action!r} is not a log action")

	def _log_status(self) -> None:
		log = self.run_log
		if log is not None:
			self.say(f"  logging to {log.csv_path}: {log.rows} rows so far")
		elif self.last_run_log is not None:
			self.say(f"  not logging; last run {self.last_run_log.csv_path}")
		else:
			self.say("  not logging (`log on` starts)")

	def _log_on(self, note: str) -> None:
		if self.run_log is not None:
			self.say(f"  already logging to {self.run_log.csv_path} (`log off` first)")
			return
		roles = self.session.roles()
		log = start_run_log(self.session, self.config, roles=roles, note=note)
		self.run_log = self.last_run_log = log
		names = ", ".join(role_name(role) for role in roles)
		self.say(f"  logging {names} at {self.config.logging.poll_hz:g} Hz to {log.csv_path}")

	def _log_off(self) -> None:
		log = self.run_log
		if log is None:
			self.say("  not logging")
			return
		self.run_log = None
		log.close()
		self.say(f"  {log.rows} rows in {log.csv_path}; run details in {log.sidecar_path}")
		self.say("  `log mass ROLE GRAMS` adds the weighed mass, `log note TEXT` a note")

	def _log_mass(self, words: list[str]) -> None:
		if len(words) != 2:
			raise UsageError("a board and grams", usage="log mass ROLE GRAMS")
		role: Role = parse_role(words[0])
		grams = parse_number(words[1], "GRAMS")
		log = self._run_for_update()
		try:
			log.set_weighed_mass(role, grams)
		except ValueError as error:
			self.say(f"  {error}")
			return
		self.say(f"  weighed mass {role_name(role)}: {grams:g} g, in {log.sidecar_path}")

	def _run_for_update(self) -> RunLog:
		if self.last_run_log is None:
			raise UsageError("no run log yet (`log on` starts one)")
		return self.last_run_log

	def on_quit(self) -> None:
		if self.run_log is not None:
			self.run_log.close()
			self.say(f"  run log closed: {self.run_log.csv_path}")
			self.run_log = None

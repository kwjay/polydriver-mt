"""The parts every command group shares: output, error reporting, Ctrl+C, quit.

A command never ends the shell by raising: a mistake in the words prints the usage line, a refused
or failed request prints why, and anything unexpected prints "internal error" with the traceback
in the log. A shell that died would leave the motors running until the comms timeout.
"""

import cmd
import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TextIO

from ..config import Config, ConfigError
from ..devices import DeviceError, Event, EventKind, NotConnectedError, SessionLike
from ..protocol.constants import NackReason, Role
from ..protocol.serial_worker import LinkError, LinkOpenError
from ..requests import RequestError, RequestNackError
from ..requests.request_queue import QueueStateError, RequestTimeoutError
from .keys import KeyWatcher
from .parsing import UsageError, role_name

logger = logging.getLogger(__name__)

NACK_HINTS = {
	NackReason.NOT_CONFIGURED: "closed loop needs gains: set [board.{role}] kp, ki, kd in polydriver.toml and "
							"`config reload`, or `pid {role} KP KI KD`",
	NackReason.WRONG_MODE: "the board is in the other mode (closed loop takes speeds, open loop takes duties)",
	NackReason.WRONG_STATE: "a job is running (`stop {role}`) or ESTOP is latched (`clear {role}`)",
	NackReason.BAD_VALUE: "the board refused the value (out of range, negative or not a number)",
}

# Shown as they happen. The rest (connected, settings applied, ...) the commands report themselves.
PRINTED_EVENTS = {
	EventKind.NO_ROLE_STORED: "`id {role}`",
	EventKind.RESTARTED: "",
	EventKind.SETTINGS_FAILED: "",
	EventKind.POLL_FAILED: "",
	EventKind.POLL_RECOVERED: "",
	EventKind.LINK_LOST: "",
}


def describe(error: BaseException, role: Role | None = None) -> str | None:
	"""What to tell the user about an error a command ran into; None for a bug."""
	name = role_name(role) if role is not None else "ROLE"
	if isinstance(error, RequestNackError):
		hint = NACK_HINTS.get(error.reason, "")
		return f"refused: {error}" + (f"\n    hint: {hint.format(role=name)}" if hint else "")
	if isinstance(error, RequestTimeoutError):
		return f"no reply: {error}"
	if isinstance(error, NotConnectedError):
		return f"{error} (`connect {name}`)"
	if isinstance(error, LinkOpenError):
		return f"{error} (`ports` lists the ports; is another program holding it open?)"
	if isinstance(error, (LinkError, QueueStateError)):
		return f"link lost: {error}"
	if isinstance(error, (DeviceError, RequestError, ConfigError, OSError)):
		return str(error)
	return None


class EventPrinter:
	"""Session sink that prints what happens between commands (a lost link, a restart)."""

	def __init__(self, say: Callable[[str], None]):
		self._say = say

	def record_status(self, role, status, host_time_s) -> None:
		pass

	def record_event(self, event: Event) -> None:
		if event.kind not in PRINTED_EVENTS:
			return
		name = role_name(event.role)
		hint = PRINTED_EVENTS[event.kind].format(role=name)
		self._say(f"! {name}: {event.kind}: {event.message}" + (f" ({hint})" if hint else ""))


class ShellBase(cmd.Cmd):
	prompt = "polydriver> "

	def __init__(self, session: SessionLike, config: Config, config_path: Path, *, stdin: TextIO, stdout: TextIO):
		super().__init__(stdin=stdin, stdout=stdout)
		# input() gives line editing and history, but only reads the real console.
		self.use_rawinput = stdin is sys.stdin
		self.session = session
		self.config = config
		self.config_path = config_path
		self._output_lock = threading.Lock()
		self.keys = KeyWatcher(stdin)
		self.clock = time.perf_counter
		session.add_sink(EventPrinter(self.say))

	def say(self, text: str) -> None:
		with self._output_lock:
			self.stdout.write(text + "\n")
			self.stdout.flush()

	def get_names(self) -> list[str]:
		return [name for name in super().get_names() if name not in ("do_EOF", "do_exit")]

	def emptyline(self) -> bool:
		return False

	def default(self, line: str) -> None:
		self.say(f"unknown command: {line.split()[0]}  (`help` lists them)")

	def onecmd(self, line: str) -> bool:
		name = self.parseline(line)[0] or ""
		try:
			return bool(super().onecmd(line))
		except UsageError as error:
			self.say(f"usage: {error.usage or self._usage(name)}  ({error})")
		except Exception as error:
			message = describe(error)
			self.say(message if message is not None else self._internal_error(error))
		return False

	def each(self, roles: Iterable[Role], action: Callable[[Role], None]) -> dict[Role, str]:
		"""Runs action for each board; a failure on one is printed and never skips the next. Returns
		the failures."""
		failures = {}
		for role in roles:
			try:
				action(role)
			except UsageError:
				raise
			except Exception as error:
				message = describe(error, role)
				failures[role] = message if message is not None else self._internal_error(error)
				self.say(f"  {role_name(role)}: {failures[role]}")
		return failures

	def stop_every_board(self, prefix: str = "  ") -> None:
		roles = self.session.roles()
		if not roles:
			self.say(f"{prefix}no board connected")
			return
		failures = self.each(roles, lambda role: self.session.board(role).stop())
		reached = [role_name(role) for role in roles if role not in failures]
		if reached:
			self.say(f"{prefix}STOP sent to {', '.join(reached)}")
		for role in failures:
			self.say(f"{prefix}STOP FAILED on {role_name(role)} - {self._without_stop()}")

	def interrupt(self) -> None:
		"""Ctrl+C."""
		self.stop_every_board("Ctrl+C: ")

	def _without_stop(self) -> str:
		if self.config.comms_timeout_ms:
			return f"the board stops by itself {self.config.comms_timeout_s:g} s after it last heard from this PC"
		return "the comms timeout is off, so cut the motor power"

	def _usage(self, name: str) -> str:
		method = getattr(self, f"do_{name}", None)
		doc = (method.__doc__ or name) if method else name
		return doc.strip().splitlines()[0].split("  - ")[0]

	def _internal_error(self, error: BaseException) -> str:
		logger.error("internal error", exc_info=error)
		return f"internal error: {type(error).__name__}: {error} (the traceback is in the log)"

	# --- Leaving --------------------------------------------------------------------

	def do_quit(self, arg: str) -> bool:
		"""quit  - send STOP to every board, close the ports and leave"""
		self.session.close()
		self.say("bye")
		return True

	do_exit = do_quit
	do_EOF = do_quit

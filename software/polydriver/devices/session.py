"""Both dispenser boards, by role (C6.3).

connect() opens a board, checks its stored role and sends the config; from then on a thread per
board polls STATUS at [logging] poll_hz. That traffic also keeps the board's comms timeout from
firing. A restarted board gets the config again (decided 2026-10-10). One board failing never
stops the other.
"""

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, TypeVar

from ..config import BoardSettings, Config
from ..protocol.constants import JobState, Role
from ..protocol.serial_worker import LinkError
from ..requests import Request, SettingsReport, VersionReport
from ..requests.request_queue import QueueStateError
from .board import Board, BoardStatus
from .link import DeviceError, Link

logger = logging.getLogger(__name__)

# The longest a poll thread can be inside one call: re-sending the config is 5 requests, each up
# to 4 attempts of 0.2 s.
JOIN_TIMEOUT_S = 5.0

Result = TypeVar("Result")

# The link is gone for good: the port vanished or the queue was closed under the caller.
LOST_ERRORS = (LinkError, QueueStateError)


class SessionError(DeviceError):
	pass


class NoPortError(SessionError):
	pass


class PortInUseError(SessionError):
	pass


class AlreadyConnectedError(SessionError):
	pass


class NotConnectedError(SessionError):
	pass


class RoleMismatchError(SessionError):
	pass


class SettingsApplyError(SessionError):
	def __init__(self, failures: dict[Role, Exception]):
		self.failures = failures
		super().__init__("; ".join(f"{_name(role)}: {error}" for role, error in failures.items()))


class SessionClosedError(SessionError):
	pass


class JobRunningError(SessionError):
	pass


class EventKind(StrEnum):
	CONNECTED = "connected"
	NO_ROLE_STORED = "no role stored"
	SETTINGS_APPLIED = "settings applied"
	SETTINGS_FAILED = "settings failed"
	RESTARTED = "restarted"
	POLL_FAILED = "poll failed"
	POLL_RECOVERED = "poll recovered"
	LINK_LOST = "link lost"
	DISCONNECTED = "disconnected"
	COMMAND = "command"


EVENT_LOG_LEVELS = {
	EventKind.NO_ROLE_STORED: logging.WARNING,
	EventKind.SETTINGS_FAILED: logging.WARNING,
	EventKind.RESTARTED: logging.WARNING,
	EventKind.POLL_FAILED: logging.WARNING,
	EventKind.LINK_LOST: logging.ERROR,
}


@dataclass(frozen=True)
class Event:
	kind: EventKind
	role: Role
	message: str
	host_time_s: float


@dataclass(frozen=True)
class BoardState:
	role: Role
	port: str
	connected: bool
	version: VersionReport
	settings: SettingsReport
	last_status: BoardStatus | None = None
	last_status_host_s: float | None = None
	polls: int = 0
	poll_failures: int = 0
	device_restarts: int = 0
	last_error: str | None = None


class LinkLike(Protocol):
	@property
	def port(self) -> str: ...

	@property
	def version(self) -> VersionReport: ...

	def call(self, request: Request[Result], timeout_s: float | None = None) -> Result: ...

	def close(self) -> None: ...


class TelemetrySink(Protocol):
	"""Called one call at a time, from the poll threads and from the thread that connects."""

	def record_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None: ...

	def record_event(self, event: Event) -> None: ...


class SessionLike(Protocol):
	def connect(self, role: Role, port: str | None = None) -> BoardState: ...

	def disconnect(self, role: Role) -> None: ...

	def roles(self) -> list[Role]: ...

	def board(self, role: Role) -> Board: ...

	def state(self, role: Role) -> BoardState: ...

	def add_sink(self, sink: TelemetrySink) -> None: ...

	def remove_sink(self, sink: TelemetrySink) -> None: ...

	def poll_now(self, role: Role) -> BoardStatus: ...

	def paused_polling(self) -> AbstractContextManager[None]: ...

	def reload_config(self, config: Config) -> None: ...

	def apply_settings(self, role: Role, settings: BoardSettings | None = None) -> SettingsReport: ...

	def stop_all(self) -> dict[Role, Exception]: ...

	def close(self) -> None: ...


class _Entry:
	def __init__(self, role: Role, link: LinkLike, board: Board, settings: SettingsReport):
		self.role = role
		self.link = link
		self.board = board
		self.state = BoardState(role, link.port, True, link.version, settings)
		self.stop = threading.Event()
		self.apply_lock = threading.Lock()
		# Held for a whole poll, so paused_polling() knows no STATUS is in flight.
		self.poll_lock = threading.RLock()
		self.thread: threading.Thread | None = None
		self.failing_polls = 0
		self.needs_settings = False
		self.settings_failing = False


class Session:
	def __init__(
		self,
		config: Config,
		*,
		link_factory: Callable[[str], LinkLike] = Link.open,
		clock: Callable[[], float] = time.perf_counter,
	):
		self._config = config
		self._link_factory = link_factory
		self._clock = clock
		self._lock = threading.Lock()
		self._entries: dict[Role, _Entry] = {}
		self._connecting: dict[Role, str] = {}
		self._closed = False
		self._sinks: list[TelemetrySink] = []
		self._sink_lock = threading.RLock()
		self._broken_sinks: set[int] = set()

	def __enter__(self) -> "Session":
		return self

	def __exit__(self, exc_type, exc_value, traceback) -> None:
		self.close()

	@property
	def config(self) -> Config:
		return self._config

	# --- Connecting -----------------------------------------------------------------

	def connect(self, role: Role, port: str | None = None) -> BoardState:
		if role not in (Role.POLYMER, Role.COLOUR):
			raise ValueError(f"connect() needs the polymer or the colour role, not {role!r}")
		with self._lock:
			if self._closed:
				raise SessionClosedError("the session is closed")
			entry = self._entries.get(role)
			if role in self._connecting or (entry is not None and entry.state.connected):
				raise AlreadyConnectedError(f"the {_name(role)} board is already connected")
			port = port or self._config.ports.get(role)
			if port is None:
				raise NoPortError(
					f"no port for the {_name(role)} board: set [ports] {_name(role)} in polydriver.toml or give one"
				)
			for other, used in self._ports_in_use().items():
				if _same_port(used, port):
					raise PortInUseError(f"{port} is already used by the {_name(other)} board")
			self._connecting[role] = port

		try:
			link = self._link_factory(port)
		except BaseException:
			self._release(role)
			raise
		try:
			board = Board(link, on_change=lambda change: self._emit(EventKind.COMMAND, role, change))
			stored_role = board.role()
			if stored_role not in (role, Role.NONE):
				raise RoleMismatchError(
					f"{port} holds the {_name(stored_role)} board, not the {_name(role)} board; the ports look "
					"swapped, check [ports] in polydriver.toml"
				)
			with self._lock:
				config = self._config
			settings = board.apply_settings(config.boards[role], config.comms_timeout_ms)
		except BaseException:
			link.close()
			self._release(role)
			raise

		entry = _Entry(role, link, board, settings)
		with self._lock:
			del self._connecting[role]
			closed = self._closed
			if not closed:
				self._entries[role] = entry
				# reload_config() ran while this board was connecting.
				entry.needs_settings = self._config is not config
		if closed:
			_stop_and_close(entry)
			raise SessionClosedError("the session was closed while connecting")

		if stored_role is Role.NONE:
			self._emit(EventKind.NO_ROLE_STORED, role,
						f"{port} has no stored role, so a swapped port cannot be caught; store {_name(role)} on it")
		self._emit(EventKind.SETTINGS_APPLIED, role, f"{port}: {settings_text(settings)}")
		version = link.version
		self._emit(EventKind.CONNECTED, role, (
			f"{port}, firmware {version.firmware_major}.{version.firmware_minor}.{version.firmware_patch}, "
			f"protocol {version.protocol_version}"
		))
		state = entry.state
		entry.thread = threading.Thread(target=self._poll, args=(entry,), name=f"poll-{_name(role)}", daemon=True)
		entry.thread.start()
		return state

	def disconnect(self, role: Role) -> None:
		with self._lock:
			entry = self._entries.pop(role, None)
		if entry is None:
			raise NotConnectedError(f"the {_name(role)} board is not connected")
		was_connected = entry.state.connected
		_stop_and_close(entry)
		if was_connected:
			self._emit(EventKind.DISCONNECTED, role, f"{entry.state.port} closed")

	def close(self) -> None:
		with self._lock:
			self._closed = True
			entries = list(self._entries.values())
			self._entries.clear()
		for entry in entries:
			entry.stop.set()
		for entry in entries:
			_stop_and_close(entry)

	# --- Using -----------------------------------------------------------------------

	def roles(self) -> list[Role]:
		with self._lock:
			return [role for role, entry in sorted(self._entries.items()) if entry.state.connected]

	def board(self, role: Role) -> Board:
		return self._connected_entry(role).board

	def state(self, role: Role) -> BoardState:
		with self._lock:
			entry = self._entries.get(role)
			if entry is None:
				raise NotConnectedError(f"the {_name(role)} board is not connected")
			return entry.state

	def poll_now(self, role: Role) -> BoardStatus:
		"""One poll now, through the same path as the poll thread (sinks, restart handling); raises
		what made it fail."""
		entry = self._connected_entry(role)
		with entry.poll_lock:
			error = self._poll_once(entry)
			status = entry.state.last_status
		if error is not None:
			raise error
		return status

	@contextmanager
	def paused_polling(self) -> Iterator[None]:
		"""Waits for the polls in flight to finish and sends none until the block ends, so a command
		whose timing matters does not wait behind a STATUS on either board."""
		with self._lock:
			entries = [entry for _, entry in sorted(self._entries.items()) if entry.state.connected]
		with ExitStack() as stack:
			for entry in entries:
				stack.enter_context(entry.poll_lock)
			yield

	def reload_config(self, config: Config) -> None:
		"""Refused while a board runs a job (by its last poll): new gains or a new timeout must not
		change a run halfway."""
		with self._lock:
			entries = [entry for entry in self._entries.values() if entry.state.connected]
			running = [_name(entry.role) for entry in entries if _job_running(entry.state)]
			if running:
				raise JobRunningError(
					f"a job is running on {', '.join(running)}; reload the config after it ends, or `stop` it first"
				)
			self._config = config
		failures: dict[Role, Exception] = {}
		for entry in entries:
			try:
				self._apply(entry)
			except Exception as error:
				failures[entry.role] = error
		if failures:
			raise SettingsApplyError(failures)

	def apply_settings(self, role: Role, settings: BoardSettings | None = None) -> SettingsReport:
		"""Sends these settings to one board (the config's when None), as for a run whose material has
		its own gains; the config stays as it is, so a restart or a reload puts the config's back.
		Refused while that board runs a job."""
		entry = self._connected_entry(role)
		with self._lock:
			if _job_running(entry.state):
				raise JobRunningError(f"a job is running on {_name(role)}; its settings stay until the job ends")
		self._apply(entry, settings)
		return entry.state.settings

	def stop_all(self) -> dict[Role, Exception]:
		with self._lock:
			entries = [entry for entry in self._entries.values() if entry.state.connected]
		failures: dict[Role, Exception] = {}
		for entry in entries:
			try:
				entry.board.stop()
			except Exception as error:
				failures[entry.role] = error
		return failures

	# --- Sinks -----------------------------------------------------------------------

	def add_sink(self, sink: TelemetrySink) -> None:
		with self._sink_lock:
			self._sinks.append(sink)

	def remove_sink(self, sink: TelemetrySink) -> None:
		with self._sink_lock:
			self._sinks.remove(sink)

	def _notify(self, method: str, *args) -> None:
		with self._sink_lock:
			for sink in list(self._sinks):
				try:
					getattr(sink, method)(*args)
				except Exception:
					if id(sink) not in self._broken_sinks:
						self._broken_sinks.add(id(sink))
						logger.exception("telemetry sink %r failed; its later failures are not logged", sink)

	def _emit(self, kind: EventKind, role: Role, message: str) -> None:
		logger.log(EVENT_LOG_LEVELS.get(kind, logging.INFO), "%s board: %s: %s", _name(role), kind, message)
		self._notify("record_event", Event(kind, role, message, self._clock()))

	# --- Polling ---------------------------------------------------------------------

	def _poll(self, entry: _Entry) -> None:
		next_s = self._clock()
		while not entry.stop.is_set():
			with entry.poll_lock:
				if entry.stop.is_set():
					return
				self._poll_once(entry)
			if not entry.state.connected:
				return
			with self._lock:
				period_s = 1.0 / self._config.logging.poll_hz
			next_s += period_s
			now_s = self._clock()
			if next_s < now_s:
				next_s = now_s
			entry.stop.wait(next_s - now_s)

	def _poll_once(self, entry: _Entry) -> Exception | None:
		"""Called holding entry.poll_lock. Returns what made the poll fail; a lost link also marks the
		board not connected."""
		role = entry.role
		try:
			status = entry.board.status()
		except LOST_ERRORS as error:
			self._lose(entry, error)
			return error
		except Exception as error:
			self._update(entry, poll_failures=entry.state.poll_failures + 1, last_error=str(error))
			entry.failing_polls += 1
			if entry.failing_polls == 1:
				self._emit(EventKind.POLL_FAILED, role, f"{error}; polling goes on")
			return error

		host_time_s = self._clock()
		self._update(entry, last_status=status, last_status_host_s=host_time_s, polls=entry.state.polls + 1,
						device_restarts=entry.board.device_restarts)
		if entry.failing_polls:
			self._emit(EventKind.POLL_RECOVERED, role, f"answering again after {entry.failing_polls} missed polls")
			entry.failing_polls = 0
		self._notify("record_status", role, status, host_time_s)

		if status.device_restarted:
			entry.needs_settings = True
			self._emit(EventKind.RESTARTED, role, (
				f"the board restarted ({entry.board.device_restarts} so far): the motor stopped and any job "
				"is lost; sending the settings again"
			))
		if entry.needs_settings and not entry.stop.is_set():
			try:
				self._apply(entry)
			except LOST_ERRORS as error:
				self._lose(entry, error)
				return error
			except Exception as error:
				self._update(entry, last_error=str(error))
				if not entry.settings_failing:
					entry.settings_failing = True
					self._emit(EventKind.SETTINGS_FAILED, role, f"{error}; trying again at the next poll")
		return None

	def _apply(self, entry: _Entry, board_settings: BoardSettings | None = None) -> None:
		with entry.apply_lock:
			with self._lock:
				config = self._config
			settings = entry.board.apply_settings(board_settings or config.boards[entry.role], config.comms_timeout_ms)
			entry.needs_settings = entry.settings_failing = False
			self._update(entry, settings=settings)
		self._emit(EventKind.SETTINGS_APPLIED, entry.role, f"{entry.state.port}: {settings_text(entry.state.settings)}")

	def _lose(self, entry: _Entry, error: Exception) -> None:
		entry.link.close()
		if entry.stop.is_set():
			return
		self._update(entry, connected=False, last_error=str(error))
		self._emit(EventKind.LINK_LOST, entry.role, f"{error}; connect again once the board is back")

	# --- Helpers ---------------------------------------------------------------------

	def _update(self, entry: _Entry, **changes) -> None:
		with self._lock:
			entry.state = replace(entry.state, **changes)

	def _connected_entry(self, role: Role) -> _Entry:
		with self._lock:
			entry = self._entries.get(role)
			if entry is None or not entry.state.connected:
				raise NotConnectedError(f"the {_name(role)} board is not connected")
			return entry

	def _release(self, role: Role) -> None:
		with self._lock:
			del self._connecting[role]

	def _ports_in_use(self) -> dict[Role, str]:
		ports = {role: entry.state.port for role, entry in self._entries.items() if entry.state.connected}
		return ports | self._connecting


def _stop_and_close(entry: _Entry) -> None:
	entry.stop.set()
	own_thread = entry.thread is threading.current_thread()
	if entry.thread is not None and not own_thread:
		entry.thread.join(JOIN_TIMEOUT_S)
	if entry.state.connected:
		try:
			entry.board.stop()
		except Exception as error:
			logger.warning("%s board: STOP on closing failed: %s", _name(entry.role), error)
	entry.link.close()
	if entry.thread is not None and not own_thread:
		entry.thread.join(JOIN_TIMEOUT_S)


def settings_text(s: SettingsReport) -> str:
	"""What the board holds after apply_settings, as read back from it."""
	timeout = "off" if s.comms_timeout_ms == 0 else f"{s.comms_timeout_ms / 1000:g} s"
	return (f"pid {s.kp:g} {s.ki:g} {s.kd:g}, alpha {s.alpha:g}, ff {s.feedforward_offset_duty:g} + "
			f"{s.feedforward_gain_duty_per_hz:g}/Hz, timeout {timeout}")


def _job_running(state: BoardState) -> bool:
	return state.last_status is not None and state.last_status.report.job_state is JobState.RUNNING


def _same_port(a: str, b: str) -> bool:
	return os.path.normcase(a) == os.path.normcase(b)


def _name(role: Role) -> str:
	return role.name.lower()

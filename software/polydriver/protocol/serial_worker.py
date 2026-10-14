"""One serial port: writes request bytes, reads and parses replies on a background thread.

Writes happen in the caller's thread, so a request goes out at once instead of waiting for the
reader's read timeout. The reader closes the port itself when the link fails (C2.13).
"""

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

import serial

from .constants import BAUD_RATE
from .link_layer import ReplyFrame, ReplyParser

logger = logging.getLogger(__name__)

READ_TIMEOUT_S = 0.05
WRITE_TIMEOUT_S = 1.0
JOIN_TIMEOUT_S = 2.0


class LinkError(Exception):
	pass


class LinkOpenError(LinkError):
	pass


class LinkStateError(LinkError):
	"""Used before start(), after stop(), or started a second time."""


class LinkLostError(LinkError):
	"""The port failed while open; the worker has already closed it."""


class SerialPort(Protocol):
	@property
	def in_waiting(self) -> int: ...

	def read(self, size: int = 1) -> bytes: ...

	def write(self, data: bytes) -> int | None: ...

	def close(self) -> None: ...


def open_serial_port(port: str) -> SerialPort:
	# serial_for_url opens "COM3" as a normal port and also accepts pyserial URLs such as "loop://".
	return serial.serial_for_url(port, baudrate=BAUD_RATE, timeout=READ_TIMEOUT_S, write_timeout=WRITE_TIMEOUT_S)


@dataclass(frozen=True)
class ReceivedFrame:
	frame: ReplyFrame
	received_s: float


@dataclass(frozen=True)
class _LinkEnd:
	failure: BaseException | None


class _State(Enum):
	NEW = auto()
	OPEN = auto()
	CLOSED = auto()
	LOST = auto()


class SerialWorker:
	"""Single use: one start(), one stop(). A reconnect makes a new worker."""

	def __init__(
		self,
		port: str,
		opener: Callable[[str], SerialPort] = open_serial_port,
		clock: Callable[[], float] = time.perf_counter,
	):
		self._port_name = port
		self._opener = opener
		self._clock = clock
		self._parser = ReplyParser()
		self._received: queue.Queue[ReceivedFrame | _LinkEnd] = queue.Queue()
		# Guards _state, _serial and _failure, and is held for the whole write() so the reader
		# never closes the port under a write.
		self._lock = threading.Lock()
		self._state = _State.NEW
		self._serial: SerialPort | None = None
		self._failure: BaseException | None = None
		self._stopping = threading.Event()
		self._thread: threading.Thread | None = None

	def __enter__(self) -> "SerialWorker":
		self.start()
		return self

	def __exit__(self, exc_type, exc_value, traceback) -> None:
		self.stop()

	@property
	def port(self) -> str:
		return self._port_name

	@property
	def is_open(self) -> bool:
		return self._state is _State.OPEN

	@property
	def abandoned_frames(self) -> int:
		return self._parser.abandoned_frames

	@property
	def bad_crc_frames(self) -> int:
		return self._parser.bad_crc_frames

	def start(self) -> None:
		with self._lock:
			if self._state is not _State.NEW:
				raise LinkStateError(f"{self._port_name}: a SerialWorker can be started only once")
			try:
				self._serial = self._opener(self._port_name)
			except (serial.SerialException, OSError) as error:
				self._state = _State.CLOSED
				self._received.put(_LinkEnd(None))
				raise LinkOpenError(f"cannot open {self._port_name}: {error}") from error
			self._state = _State.OPEN
			self._thread = threading.Thread(target=self._read_loop, name=f"serial-{self._port_name}", daemon=True)
			self._thread.start()

	def stop(self) -> None:
		with self._lock:
			if self._state is _State.NEW:
				self._received.put(_LinkEnd(None))
			if self._state in (_State.NEW, _State.OPEN):
				self._state = _State.CLOSED
			self._stopping.set()
			self._cancel_read()
			thread = self._thread

		if thread is None or thread is threading.current_thread():
			return
		thread.join(JOIN_TIMEOUT_S)
		if thread.is_alive():
			logger.warning("%s: reader thread did not stop, closing the port from stop()", self._port_name)
			with self._lock:
				self._close_port()

	def send(self, data: bytes) -> float:
		"""Writes the bytes now and returns the time the write finished."""
		with self._lock:
			self._raise_unless_open()
			assert self._serial is not None
			try:
				self._serial.write(data)
			except (serial.SerialException, OSError) as error:
				self._lose(error)
				raise LinkLostError(f"{self._port_name} lost: {error}") from error
			return self._clock()

	def receive(self, timeout_s: float) -> ReceivedFrame | None:
		"""The next reply, or None after timeout_s. Raises once the link is closed or lost, after
		every reply received before that has been handed out."""
		if self._state is _State.NEW:
			raise LinkStateError(f"{self._port_name} is not open")
		try:
			item = self._received.get(timeout=timeout_s)
		except queue.Empty:
			return None
		if isinstance(item, _LinkEnd):
			self._received.put(item)
			raise self._end_error(item)
		return item

	def _read_loop(self) -> None:
		port = self._serial
		assert port is not None
		try:
			while not self._stopping.is_set():
				data = port.read(max(1, port.in_waiting))
				if data:
					now_s = self._clock()
					for frame in self._parser.feed(data, now_s):
						self._received.put(ReceivedFrame(frame, now_s))
		except Exception as error:
			with self._lock:
				self._lose(error)
		finally:
			with self._lock:
				self._close_port()
				end = _LinkEnd(self._failure if self._state is _State.LOST else None)
			self._received.put(end)

	def _lose(self, error: BaseException) -> None:
		if self._state is not _State.OPEN:
			return
		logger.error("%s: link lost: %r", self._port_name, error)
		self._state = _State.LOST
		self._failure = error
		self._stopping.set()
		self._cancel_read()

	def _raise_unless_open(self) -> None:
		if self._state is _State.LOST:
			raise LinkLostError(f"{self._port_name} lost: {self._failure}") from self._failure
		if self._state is not _State.OPEN:
			raise LinkStateError(f"{self._port_name} is not open")

	def _end_error(self, end: _LinkEnd) -> LinkError:
		if end.failure is None:
			return LinkStateError(f"{self._port_name} is closed")
		error = LinkLostError(f"{self._port_name} lost: {end.failure}")
		error.__cause__ = end.failure
		return error

	def _cancel_read(self) -> None:
		cancel = getattr(self._serial, "cancel_read", None)
		if cancel is not None:
			cancel()

	def _close_port(self) -> None:
		if self._serial is None:
			return
		try:
			self._serial.close()
		except Exception:
			logger.exception("%s: error while closing the port", self._port_name)
		self._serial = None

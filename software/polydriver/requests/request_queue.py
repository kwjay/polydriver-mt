"""Sends requests over one link, one at a time (protocol.md section 3).

Every new request takes the next sequence number; a retry resends the same bytes, so the board
answers it from its stored reply instead of running the command twice. Retries happen on silence
and on a BAD_CRC NACK carrying this request's SEQ; every other NACK is final. A reply whose SEQ is
not the request in flight is late (an answer to an earlier attempt) and is dropped and counted.
"""

import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Protocol, TypeVar

from ..protocol.constants import NackReason, Reply
from ..protocol.link_layer import ReplyFrame, SequenceCounter, build_frame
from ..protocol.serial_worker import LinkError, ReceivedFrame
from .base import Request, RequestError, nack_reason

logger = logging.getLogger(__name__)

REPLY_TIMEOUT_S = 0.2
RETRIES = 3
# How often a wait for a reply checks whether stop() was called.
STOP_CHECK_S = 0.05
JOIN_TIMEOUT_S = 2.0

Result = TypeVar("Result")


class RequestTimeoutError(RequestError):
	pass


class QueueStateError(RequestError):
	pass


class QueueClosedError(QueueStateError):
	pass


class Transport(Protocol):
	def send(self, data: bytes) -> float: ...

	def receive(self, timeout_s: float) -> ReceivedFrame | None: ...


@dataclass(frozen=True)
class Exchange:
	"""One finished request, for link-quality logging (chapter 6.2) and for timing a command on the
	board. Times are the transport's clock. The board runs a command once, on the first attempt that
	reaches it, so it ran between first_sent_s and replied_s."""

	request: Request
	sequence: int
	attempts: int
	first_sent_s: float | None
	sent_s: float | None
	replied_s: float | None
	result: Any = None
	error: BaseException | None = None

	@property
	def round_trip_s(self) -> float | None:
		if self.sent_s is None or self.replied_s is None:
			return None
		return self.replied_s - self.sent_s


@dataclass(frozen=True)
class LinkStats:
	frames_sent: int = 0
	retries: int = 0
	reply_timeouts: int = 0
	late_replies: int = 0
	nacks: dict[NackReason | int, int] = field(default_factory=dict)


@dataclass
class _Counts:
	frames_sent: int = 0
	retries: int = 0
	reply_timeouts: int = 0
	late_replies: int = 0
	nacks: Counter = field(default_factory=Counter)


@dataclass
class _Item:
	request: Request
	future: Future
	timed: bool = False


class _State(Enum):
	NEW = auto()
	OPEN = auto()
	CLOSED = auto()
	LOST = auto()


class _Stopped(Exception):
	pass


class RequestQueue:
	"""Single use, like SerialWorker. Requests submitted before start() wait for it."""

	def __init__(
		self,
		transport: Transport,
		*,
		reply_timeout_s: float = REPLY_TIMEOUT_S,
		retries: int = RETRIES,
		sequence: SequenceCounter | None = None,
		clock: Callable[[], float] = time.perf_counter,
		observer: Callable[[Exchange], None] | None = None,
	):
		if reply_timeout_s <= 0:
			raise ValueError("reply_timeout_s must be > 0")
		if retries < 0:
			raise ValueError("retries must be >= 0")
		self._transport = transport
		self._reply_timeout_s = reply_timeout_s
		self._retries = retries
		self._sequence = sequence or SequenceCounter()
		self._clock = clock
		self._observer = observer

		self._lock = threading.Condition()
		self._pending: deque[_Item] = deque()
		self._state = _State.NEW
		self._failure: BaseException | None = None
		self._thread: threading.Thread | None = None

		self._counts = _Counts()

	def __enter__(self) -> "RequestQueue":
		self.start()
		return self

	def __exit__(self, exc_type, exc_value, traceback) -> None:
		self.stop()

	@property
	def is_open(self) -> bool:
		return self._state is _State.OPEN

	@property
	def pending_count(self) -> int:
		with self._lock:
			return len(self._pending)

	@property
	def stats(self) -> LinkStats:
		with self._lock:
			counts = self._counts
			return LinkStats(
				frames_sent=counts.frames_sent,
				retries=counts.retries,
				reply_timeouts=counts.reply_timeouts,
				late_replies=counts.late_replies,
				nacks=dict(counts.nacks),
			)

	def start(self) -> None:
		with self._lock:
			if self._state is not _State.NEW:
				raise QueueStateError("a RequestQueue can be started only once")
			self._state = _State.OPEN
			self._thread = threading.Thread(target=self._run, name="request-queue", daemon=True)
			self._thread.start()

	def stop(self) -> None:
		"""Fails every request not yet answered (C2.12) and returns within about STOP_CHECK_S."""
		with self._lock:
			if self._state in (_State.NEW, _State.OPEN):
				self._state = _State.CLOSED
			self._lock.notify_all()
			thread = self._thread
		if thread is not None and thread is not threading.current_thread():
			thread.join(JOIN_TIMEOUT_S)
			if thread.is_alive():
				logger.warning("request queue thread did not stop")
		self._fail_waiting()

	def submit(self, request: Request[Result]) -> "Future[Result]":
		return self._submit(request, timed=False)

	def submit_timed(self, request: Request) -> "Future[Exchange]":
		"""Like submit(), but the future gives the whole Exchange: the result or the request's error
		(NACK, timeout) together with its times. It raises only when the link is lost or stopped."""
		return self._submit(request, timed=True)

	def _submit(self, request: Request, timed: bool) -> Future:
		future: Future = Future()
		with self._lock:
			self._raise_unless_accepting()
			item = _Item(request, future, timed)
			if request.URGENT:
				self._pending.insert(self._urgent_count(), item)
			else:
				self._pending.append(item)
			self._lock.notify_all()
		return future

	def call(self, request: Request[Result], timeout_s: float | None = None) -> Result:
		return self.submit(request).result(timeout_s)

	def _urgent_count(self) -> int:
		count = 0
		for item in self._pending:
			if not item.request.URGENT:
				break
			count += 1
		return count

	def _raise_unless_accepting(self) -> None:
		if self._state is _State.LOST and self._failure is not None:
			raise type(self._failure)(*self._failure.args) from self._failure
		if self._state not in (_State.NEW, _State.OPEN):
			raise QueueClosedError("the request queue is closed")

	# --- Queue thread -------------------------------------------------------------

	def _run(self) -> None:
		while True:
			with self._lock:
				self._lock.wait_for(lambda: self._pending or self._state is not _State.OPEN)
				if self._state is not _State.OPEN:
					break
				item = self._pending.popleft()
			if not item.future.set_running_or_notify_cancel():
				continue
			try:
				self._execute(item)
			except LinkError as error:
				self._lose(error)
				_settle(item.future, error=error)
				break
			except _Stopped:
				_settle(item.future, error=QueueClosedError(
					f"stopped before {item.request.COMMAND.name} was answered; it may have run on the board"
				))
				break
			except Exception as error:
				logger.exception("request queue: %r failed unexpectedly", item.request)
				_settle(item.future, error=error)
		self._fail_waiting()

	def _execute(self, item: _Item) -> None:
		request = item.request
		sequence = self._sequence.next_sequence()
		frame = build_frame(request.COMMAND, sequence, request.encode())
		attempts = 0
		first_sent_s: float | None = None
		sent_s: float | None = None
		while True:
			attempts += 1
			sent_s = self._transport.send(frame)
			if first_sent_s is None:
				first_sent_s = sent_s
			with self._lock:
				self._counts.frames_sent += 1
				if attempts > 1:
					self._counts.retries += 1
			received = self._await_reply(sequence)

			if received is not None and _is_nack(received.frame) and len(received.frame.payload) == 1:
				with self._lock:
					self._counts.nacks[nack_reason(received.frame.payload[0])] += 1
			retry = received is None or _is_bad_crc_nack(received.frame)
			if retry and attempts <= self._retries:
				continue

			if received is None:
				error: BaseException = RequestTimeoutError(
					f"{request.COMMAND.name} got no reply after {attempts} attempts"
				)
				self._finish(item, sequence, attempts, first_sent_s, sent_s, None, error=error)
				return
			try:
				result = request.handle(received.frame)
			except RequestError as error:
				self._finish(item, sequence, attempts, first_sent_s, sent_s, received.received_s, error=error)
				return
			self._finish(item, sequence, attempts, first_sent_s, sent_s, received.received_s, result=result)
			return

	def _await_reply(self, sequence: int) -> ReceivedFrame | None:
		deadline_s = self._clock() + self._reply_timeout_s
		while True:
			if self._state is not _State.OPEN:
				raise _Stopped()
			remaining_s = deadline_s - self._clock()
			if remaining_s <= 0:
				with self._lock:
					self._counts.reply_timeouts += 1
				return None
			received = self._transport.receive(min(remaining_s, STOP_CHECK_S))
			if received is None:
				continue
			if received.frame.sequence != sequence:
				with self._lock:
					self._counts.late_replies += 1
				continue
			return received

	def _finish(self, item: _Item, sequence: int, attempts: int, first_sent_s: float | None, sent_s: float | None,
				replied_s: float | None, result: Any = None, error: BaseException | None = None) -> None:
		exchange = Exchange(item.request, sequence, attempts, first_sent_s, sent_s, replied_s, result, error)
		if item.timed:
			_settle(item.future, result=exchange)
		else:
			_settle(item.future, result=result, error=error)
		if self._observer is None:
			return
		try:
			self._observer(exchange)
		except Exception:
			logger.exception("request queue: observer failed")

	def _lose(self, error: BaseException) -> None:
		with self._lock:
			if self._state is _State.OPEN:
				self._state = _State.LOST
				self._failure = error

	def _fail_waiting(self) -> None:
		with self._lock:
			waiting = list(self._pending)
			self._pending.clear()
			failure = self._failure
		for item in waiting:
			if failure is not None:
				error: BaseException = type(failure)(*failure.args)
				error.__cause__ = failure
			else:
				error = QueueClosedError(f"the request queue stopped before {item.request.COMMAND.name} was sent")
			_settle(item.future, error=error)


def _is_nack(frame: ReplyFrame) -> bool:
	return frame.command == Reply.NACK


def _is_bad_crc_nack(frame: ReplyFrame) -> bool:
	return _is_nack(frame) and frame.payload == bytes([NackReason.BAD_CRC])


def _settle(future: Future, result: Any = None, error: BaseException | None = None) -> None:
	try:
		if error is not None:
			future.set_exception(error)
		else:
			future.set_result(result)
	except InvalidStateError:
		pass  # cancelled by the caller meanwhile

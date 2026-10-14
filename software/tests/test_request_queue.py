import queue
import struct
import threading
import time
from concurrent.futures import CancelledError

import pytest

from polydriver.protocol import constants as c
from polydriver.protocol.constants import BOARD_ID, ETX, STX, Command, NackReason, Reply
from polydriver.protocol.link_layer import ReplyFrame, ReplyParser, SequenceCounter, crc8
from polydriver.protocol.serial_worker import LinkLostError, LinkStateError, ReceivedFrame, SerialWorker
from polydriver.requests import (
	Estop, GetVersion, JobSegment, RequestNackError, RequestStatus, SetPwm, SetSpeed, Stop, UnexpectedReplyError,
)
from polydriver.requests.request_queue import (
	Exchange, QueueClosedError, QueueStateError, RequestQueue, RequestTimeoutError,
)

TIMEOUT_S = 0.05
FIXED_START = 40


class FixedStart:
	def randrange(self, stop: int) -> int:
		return FIXED_START


def reply_bytes(command: int, sequence: int, payload: bytes = b"") -> bytes:
	body = bytes([BOARD_ID, command, len(payload), sequence]) + payload
	return bytes([STX]) + body + bytes([crc8(body), ETX])


def version_payload() -> bytes:
	return bytes([3, 3, 0, 0, 32])


class FakeTransport:
	"""Stands in for SerialWorker. respond(frame) returns the replies the board sends to one request
	(a list of ReplyFrame), or [] for silence."""

	def __init__(self, respond=None):
		self.respond = respond or (lambda frame: [ReplyFrame(Reply.ACK, frame.sequence, b"")])
		self.sent: list[ReplyFrame] = []
		self.raw: list[bytes] = []
		self._replies: queue.Queue = queue.Queue()
		self._parser = ReplyParser()
		self.send_error: Exception | None = None
		self.clock_s = 0.0

	def send(self, data: bytes) -> float:
		if self.send_error is not None:
			raise self.send_error
		[frame] = self._parser.feed(data, 0.0)
		self.sent.append(frame)
		self.raw.append(data)
		for reply in self.respond(frame):
			self.push(reply)
		self.clock_s += 1.0
		return self.clock_s

	def push(self, frame_or_error) -> None:
		self._replies.put(frame_or_error)

	def receive(self, timeout_s: float) -> ReceivedFrame | None:
		try:
			item = self._replies.get(timeout=timeout_s)
		except queue.Empty:
			return None
		if isinstance(item, Exception):
			self._replies.put(item)
			raise item
		return ReceivedFrame(item, self.clock_s + 0.25)

	@property
	def commands(self) -> list[int]:
		return [frame.command for frame in self.sent]


def make_queue(transport, **options) -> RequestQueue:
	options.setdefault("reply_timeout_s", TIMEOUT_S)
	options.setdefault("sequence", SequenceCounter(FixedStart()))
	return RequestQueue(transport, **options)


@pytest.fixture
def transport():
	return FakeTransport()


@pytest.fixture
def requests(transport):
	q = make_queue(transport)
	q.start()
	yield q
	q.stop()


def wait_until(predicate, timeout_s: float = 1.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.002)
	return predicate()


# --- One exchange -----------------------------------------------------------------

def test_a_request_is_sent_and_its_reply_decoded():
	transport = FakeTransport(lambda f: [ReplyFrame(Reply.VERSION, f.sequence, version_payload())])
	with make_queue(transport) as requests:
		version = requests.call(GetVersion())
	assert version.max_segments == 32
	assert transport.sent == [ReplyFrame(Command.GET_VERSION, FIXED_START, b"")]


def test_an_ack_request_returns_none(requests, transport):
	assert requests.call(SetSpeed(50.0)) is None
	assert transport.sent[0].payload == struct.pack("<f", 50.0)


def test_each_new_request_takes_the_next_sequence_number(requests, transport):
	for _ in range(3):
		requests.call(SetPwm(10))
	assert [f.sequence for f in transport.sent] == [FIXED_START, FIXED_START + 1, FIXED_START + 2]


def test_the_sequence_wraps_after_255(transport):
	class Start255:
		def randrange(self, stop):
			return 255

	with make_queue(transport, sequence=SequenceCounter(Start255())) as requests:
		requests.call(Stop())
		requests.call(Stop())
	assert [f.sequence for f in transport.sent] == [255, 0]


def test_the_default_sequence_start_is_random():
	starts = set()
	for _ in range(20):
		transport = FakeTransport()
		with RequestQueue(transport, reply_timeout_s=TIMEOUT_S) as requests:
			requests.call(Stop())
		starts.add(transport.sent[0].sequence)
	assert len(starts) > 1


def test_requests_go_one_at_a_time_in_submission_order(transport):
	held = []
	transport.respond = lambda f: held.append(f) or []
	with make_queue(transport, reply_timeout_s=1.0) as requests:
		first = requests.submit(SetPwm(1))
		second = requests.submit(SetPwm(2))
		assert wait_until(lambda: len(transport.sent) == 1)
		time.sleep(0.03)
		assert len(transport.sent) == 1
		transport.push(ReplyFrame(Reply.ACK, held[0].sequence, b""))
		first.result(1.0)
		assert wait_until(lambda: len(transport.sent) == 2)
		transport.push(ReplyFrame(Reply.ACK, held[1].sequence, b""))
		second.result(1.0)
	assert [f.payload for f in transport.sent] == [bytes([1]), bytes([2])]


# --- NACKs and wrong replies ----------------------------------------------------------

def test_a_nack_is_final_and_names_the_reason(transport):
	transport.respond = lambda f: [ReplyFrame(Reply.NACK, f.sequence, bytes([NackReason.WRONG_STATE]))]
	with make_queue(transport) as requests:
		with pytest.raises(RequestNackError) as raised:
			requests.call(SetSpeed(10.0))
		stats = requests.stats
	assert raised.value.reason is NackReason.WRONG_STATE
	assert len(transport.sent) == 1
	assert stats.nacks == {NackReason.WRONG_STATE: 1}
	assert stats.retries == 0


def test_a_bad_crc_nack_for_this_request_is_retried_at_once_with_the_same_bytes(transport):
	answers = iter([
		lambda f: [ReplyFrame(Reply.NACK, f.sequence, bytes([NackReason.BAD_CRC]))],
		lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")],
	])
	transport.respond = lambda f: next(answers)(f)
	with make_queue(transport, reply_timeout_s=1.0) as requests:
		started_s = time.perf_counter()
		requests.call(SetSpeed(10.0))
		elapsed_s = time.perf_counter() - started_s
		stats = requests.stats
	assert transport.raw[0] == transport.raw[1]
	assert elapsed_s < 0.5
	assert stats.retries == 1
	assert stats.nacks == {NackReason.BAD_CRC: 1}


def test_bad_crc_nacks_until_the_budget_is_spent_fail_with_bad_crc(transport):
	transport.respond = lambda f: [ReplyFrame(Reply.NACK, f.sequence, bytes([NackReason.BAD_CRC]))]
	with make_queue(transport, retries=2) as requests:
		with pytest.raises(RequestNackError) as raised:
			requests.call(Stop())
	assert raised.value.reason is NackReason.BAD_CRC
	assert len(transport.sent) == 3


def test_a_bad_crc_nack_with_another_sequence_is_ignored(transport):
	answers = iter([
		lambda f: [ReplyFrame(Reply.NACK, (f.sequence + 7) % 256, bytes([NackReason.BAD_CRC]))],
		lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")],
	])
	transport.respond = lambda f: next(answers)(f)
	with make_queue(transport) as requests:
		requests.call(Stop())
		stats = requests.stats
	assert len(transport.sent) == 2
	assert stats.late_replies == 1
	assert stats.reply_timeouts == 1


def test_an_unexpected_reply_code_fails_the_request(transport):
	transport.respond = lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")]
	with make_queue(transport) as requests:
		with pytest.raises(UnexpectedReplyError):
			requests.call(RequestStatus())


# --- Timeouts, retries and late replies ---------------------------------------------

def test_silence_is_retried_with_the_same_bytes_then_answered(transport):
	answers = iter([lambda f: [], lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")]])
	transport.respond = lambda f: next(answers)(f)
	with make_queue(transport) as requests:
		requests.call(JobSegment(0, 25.0, 1500))
		stats = requests.stats
	assert len(transport.raw) == 2
	assert transport.raw[0] == transport.raw[1]
	assert stats.retries == 1
	assert stats.reply_timeouts == 1


def test_silence_past_the_retry_budget_raises_a_timeout(transport):
	transport.respond = lambda f: []
	with make_queue(transport, retries=3) as requests:
		with pytest.raises(RequestTimeoutError, match="SET_SPEED"):
			requests.call(SetSpeed(1.0))
		stats = requests.stats
	assert len(transport.sent) == 4
	assert stats.reply_timeouts == 4
	assert stats.retries == 3


def test_zero_retries_sends_once(transport):
	transport.respond = lambda f: []
	with make_queue(transport, retries=0) as requests:
		with pytest.raises(RequestTimeoutError):
			requests.call(Stop())
	assert len(transport.sent) == 1


def test_a_late_reply_to_an_earlier_request_does_not_answer_the_next_one(transport):
	# The first request is answered twice (its retry's reply comes after the original one):
	# the duplicate arrives while the second request waits and must not complete it.
	def respond(frame):
		if frame.command == Command.SET_PWM:
			ack = ReplyFrame(Reply.ACK, frame.sequence, b"")
			return [ack, ack]
		return [ReplyFrame(Reply.VERSION, frame.sequence, version_payload())]

	transport.respond = respond
	with make_queue(transport) as requests:
		requests.call(SetPwm(5))
		assert requests.call(GetVersion()).protocol_version == 3
		stats = requests.stats
	assert stats.late_replies == 1


def test_a_reply_with_another_sequence_is_dropped_and_counted(transport):
	transport.respond = lambda f: [
		ReplyFrame(Reply.ACK, (f.sequence - 1) % 256, b""),
		ReplyFrame(Reply.ACK, f.sequence, b""),
	]
	with make_queue(transport) as requests:
		requests.call(Stop())
		stats = requests.stats
	assert stats.late_replies == 1
	assert stats.retries == 0


def test_bad_values_never_reach_the_queue(requests, transport):
	with pytest.raises(ValueError):
		requests.submit(SetPwm(300))
	assert transport.sent == []


# --- Urgent requests ------------------------------------------------------------------

def test_stop_jumps_ahead_of_waiting_requests(transport):
	held = []
	transport.respond = lambda f: held.append(f) or []
	with make_queue(transport, reply_timeout_s=1.0) as requests:
		requests.submit(SetPwm(1))
		assert wait_until(lambda: len(transport.sent) == 1)
		requests.submit(SetPwm(2))
		requests.submit(SetPwm(3))
		requests.submit(Stop())
		requests.submit(Estop())
		transport.respond = lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")]
		transport.push(ReplyFrame(Reply.ACK, held[0].sequence, b""))
		assert wait_until(lambda: len(transport.sent) == 5)
	assert transport.commands == [Command.SET_PWM, Command.STOP, Command.ESTOP, Command.SET_PWM, Command.SET_PWM]
	assert [f.payload for f in transport.sent if f.command == Command.SET_PWM] == [bytes([1]), bytes([2]), bytes([3])]


# --- Stopping (C2.12) and losing the link ---------------------------------------------

def test_stop_fails_waiting_and_in_flight_requests(transport):
	transport.respond = lambda f: []
	requests = make_queue(transport, reply_timeout_s=5.0)
	requests.start()
	in_flight = requests.submit(SetPwm(1))
	waiting = requests.submit(SetPwm(2))
	assert wait_until(lambda: len(transport.sent) == 1)
	started_s = time.perf_counter()
	requests.stop()
	assert time.perf_counter() - started_s < 1.0
	with pytest.raises(QueueClosedError, match="may have"):
		in_flight.result(1.0)
	with pytest.raises(QueueClosedError):
		waiting.result(1.0)
	assert len(transport.sent) == 1


def test_submit_after_stop_is_refused(requests):
	requests.stop()
	with pytest.raises(QueueClosedError):
		requests.submit(Stop())


def test_requests_submitted_before_start_are_sent_after_start(transport):
	requests = make_queue(transport)
	future = requests.submit(Stop())
	requests.start()
	assert future.result(1.0) is None
	requests.stop()


def test_stop_before_start_fails_what_was_submitted(transport):
	requests = make_queue(transport)
	future = requests.submit(Stop())
	requests.stop()
	with pytest.raises(QueueClosedError):
		future.result(0.1)


def test_a_queue_starts_once(requests):
	with pytest.raises(QueueStateError):
		requests.start()


def test_stop_twice_is_harmless(requests):
	requests.stop()
	requests.stop()


def test_a_lost_link_fails_the_request_in_flight_and_every_waiting_one(transport):
	transport.respond = lambda f: []
	with make_queue(transport, reply_timeout_s=5.0) as requests:
		in_flight = requests.submit(SetPwm(1))
		waiting = requests.submit(SetPwm(2))
		assert wait_until(lambda: len(transport.sent) == 1)
		transport.push(LinkLostError("COM5 lost: device unplugged"))
		for future in (in_flight, waiting):
			with pytest.raises(LinkLostError, match="unplugged"):
				future.result(1.0)
		with pytest.raises(LinkLostError, match="unplugged"):
			requests.submit(Stop())


def test_a_failed_send_fails_everything_as_well(transport):
	transport.send_error = LinkStateError("COM5 is closed")
	with make_queue(transport) as requests:
		with pytest.raises(LinkStateError):
			requests.call(Stop())
		with pytest.raises(LinkStateError):
			requests.submit(Stop())


def test_a_cancelled_request_is_not_sent(transport):
	held = []
	transport.respond = lambda f: held.append(f) or []
	with make_queue(transport, reply_timeout_s=1.0) as requests:
		requests.submit(SetPwm(1))
		assert wait_until(lambda: len(transport.sent) == 1)
		cancelled = requests.submit(SetPwm(2))
		after = requests.submit(SetPwm(3))
		assert cancelled.cancel()
		transport.respond = lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")]
		transport.push(ReplyFrame(Reply.ACK, held[0].sequence, b""))
		after.result(1.0)
	assert [f.payload for f in transport.sent] == [bytes([1]), bytes([3])]
	with pytest.raises(CancelledError):
		cancelled.result()


# --- What gets recorded ---------------------------------------------------------------

def test_every_exchange_is_reported_with_its_timing(transport):
	exchanges: list[Exchange] = []
	answers = iter([lambda f: [], lambda f: [ReplyFrame(Reply.ACK, f.sequence, b"")]])
	transport.respond = lambda f: next(answers)(f)
	with make_queue(transport, observer=exchanges.append) as requests:
		requests.call(SetSpeed(5.0))
	[exchange] = exchanges
	assert exchange.request == SetSpeed(5.0)
	assert exchange.sequence == FIXED_START
	assert exchange.attempts == 2
	assert exchange.sent_s == 2.0
	assert exchange.replied_s == 2.25
	assert exchange.round_trip_s == 0.25
	assert exchange.error is None


def test_a_failed_exchange_is_reported_with_its_error(transport):
	exchanges: list[Exchange] = []
	transport.respond = lambda f: []
	with make_queue(transport, retries=0, observer=exchanges.append) as requests:
		with pytest.raises(RequestTimeoutError):
			requests.call(Stop())
	assert isinstance(exchanges[0].error, RequestTimeoutError)
	assert exchanges[0].replied_s is None
	assert exchanges[0].round_trip_s is None


def test_an_observer_that_raises_does_not_break_the_queue(transport):
	def broken(exchange):
		raise RuntimeError("observer bug")

	with make_queue(transport, observer=broken) as requests:
		requests.call(Stop())
		requests.call(Stop())
	assert len(transport.sent) == 2


def test_stats_count_frames_sent(requests):
	requests.call(Stop())
	requests.call(Stop())
	assert requests.stats.frames_sent == 2


# --- With the real SerialWorker ------------------------------------------------------------

class BoardPort:
	"""A pyserial-like port that answers like the firmware: ACK, or STATUS for REQ_STAT, with the
	request's SEQ."""

	def __init__(self):
		self._condition = threading.Condition()
		self._incoming = bytearray()
		self._parser = ReplyParser()

	@property
	def in_waiting(self) -> int:
		with self._condition:
			return len(self._incoming)

	def read(self, size: int = 1) -> bytes:
		with self._condition:
			self._condition.wait_for(lambda: self._incoming, timeout=0.05)
			data = bytes(self._incoming[:size])
			del self._incoming[:size]
			return data

	def write(self, data: bytes) -> int:
		for frame in self._parser.feed(data, 0.0):
			if frame.command == Command.REQ_STAT:
				payload = struct.pack(c.STATUS_FORMAT, 12.5, 12.0, 90, 0x04, 1000, 77, 0, 10010, 0, 0, 0, 0xFF, 0)
				answer = reply_bytes(Reply.STATUS, frame.sequence, payload)
			else:
				answer = reply_bytes(Reply.ACK, frame.sequence)
			with self._condition:
				self._incoming.extend(answer)
				self._condition.notify_all()
		return len(data)

	def cancel_read(self) -> None:
		with self._condition:
			self._condition.notify_all()

	def close(self) -> None:
		pass


def test_end_to_end_through_the_serial_worker():
	port = BoardPort()
	with SerialWorker("COM_FAKE", opener=lambda name: port) as worker, RequestQueue(worker) as requests:
		assert requests.call(SetSpeed(12.5)) is None
		status = requests.call(RequestStatus())
	assert status.raw_speed_hz == 12.5
	assert status.duty == 90
	assert status.edge_count == 77

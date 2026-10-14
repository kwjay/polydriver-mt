import threading
import time

import pytest
import serial

from polydriver.protocol.constants import BAUD_RATE, BOARD_ID, ETX, STX, Command, Reply
from polydriver.protocol.link_layer import ReplyFrame, build_frame, crc8
from polydriver.protocol.serial_worker import (
	READ_TIMEOUT_S, WRITE_TIMEOUT_S, LinkLostError, LinkOpenError, LinkStateError, ReceivedFrame,
	SerialWorker, open_serial_port,
)

FIXED_TIME_S = 12.5


def reply_frame(command: int, sequence: int, payload: bytes = b"") -> bytes:
	body = bytes([BOARD_ID, command, len(payload), sequence]) + payload
	return bytes([STX]) + body + bytes([crc8(body), ETX])


def wait_until(predicate, timeout_s: float = 1.0) -> bool:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(0.005)
	return predicate()


class FakePort:
	"""Behaves like a pyserial port opened with a read timeout: read() blocks until data,
	cancel_read() or READ_TIMEOUT_S."""

	def __init__(self):
		self._condition = threading.Condition()
		self._incoming = bytearray()
		self._cancelled = False
		self._read_error: Exception | None = None
		self.write_error: Exception | None = None
		self.written: list[bytes] = []
		self.close_count = 0

	@property
	def in_waiting(self) -> int:
		with self._condition:
			return len(self._incoming)

	def read(self, size: int = 1) -> bytes:
		with self._condition:
			self._condition.wait_for(
				lambda: self._incoming or self._cancelled or self._read_error, timeout=READ_TIMEOUT_S,
			)
			if self._read_error is not None:
				raise self._read_error
			self._cancelled = False
			data = bytes(self._incoming[:size])
			del self._incoming[:size]
			return data

	def write(self, data: bytes) -> int:
		if self.write_error is not None:
			raise self.write_error
		self.written.append(bytes(data))
		return len(data)

	def cancel_read(self) -> None:
		with self._condition:
			self._cancelled = True
			self._condition.notify_all()

	def close(self) -> None:
		self.close_count += 1

	@property
	def closed(self) -> bool:
		return self.close_count > 0

	def feed(self, data: bytes) -> None:
		with self._condition:
			self._incoming.extend(data)
			self._condition.notify_all()

	def fail_read(self, error: Exception) -> None:
		with self._condition:
			self._read_error = error
			self._condition.notify_all()


@pytest.fixture
def port():
	return FakePort()


@pytest.fixture
def worker(port):
	worker = SerialWorker("COM_FAKE", opener=lambda name: port, clock=lambda: FIXED_TIME_S)
	yield worker
	worker.stop()


# --- Opening --------------------------------------------------------------------

def test_start_opens_the_named_port(port):
	opened = []
	worker = SerialWorker("COM7", opener=lambda name: opened.append(name) or port)
	worker.start()
	worker.stop()
	assert opened == ["COM7"]


def test_the_default_opener_uses_the_protocol_settings():
	opened = open_serial_port("loop://")
	try:
		assert opened.baudrate == BAUD_RATE
		assert opened.timeout == READ_TIMEOUT_S
		assert opened.write_timeout == WRITE_TIMEOUT_S
	finally:
		opened.close()


def test_a_port_that_cannot_be_opened_raises_link_open_error():
	def refuse(name):
		raise serial.SerialException("could not open port 'COM9': access denied")

	worker = SerialWorker("COM9", opener=refuse)
	with pytest.raises(LinkOpenError, match="COM9") as raised:
		worker.start()
	assert isinstance(raised.value.__cause__, serial.SerialException)
	assert not worker.is_open
	with pytest.raises(LinkStateError):
		worker.receive(0.01)


def test_a_worker_starts_only_once(worker):
	worker.start()
	with pytest.raises(LinkStateError):
		worker.start()
	worker.stop()
	with pytest.raises(LinkStateError):
		worker.start()


# --- Sending --------------------------------------------------------------------

def test_send_writes_at_once_and_returns_the_send_time(worker, port):
	worker.start()
	assert worker.send(b"\x02\x01\x40") == FIXED_TIME_S
	assert port.written == [b"\x02\x01\x40"]


def test_send_does_not_wait_for_the_reader(port):
	worker = SerialWorker("COM_FAKE", opener=lambda name: port)
	worker.start()
	time.sleep(0.01)
	started_s = time.perf_counter()
	worker.send(b"x")
	worker.stop()
	assert time.perf_counter() - started_s < READ_TIMEOUT_S


def test_send_before_start_is_refused(worker):
	with pytest.raises(LinkStateError):
		worker.send(b"x")


def test_send_after_stop_is_refused(worker):
	worker.start()
	worker.stop()
	with pytest.raises(LinkStateError):
		worker.send(b"x")


# --- Receiving ------------------------------------------------------------------

def test_a_reply_is_parsed_and_time_stamped(worker, port):
	worker.start()
	port.feed(reply_frame(Reply.ACK, 9))
	assert worker.receive(1.0) == ReceivedFrame(frame=ReplyFrame(Reply.ACK, 9, b""), received_s=FIXED_TIME_S)


def test_a_reply_split_across_reads_is_reassembled(worker, port):
	worker.start()
	data = reply_frame(Reply.ID, 3, bytes([2]))
	port.feed(data[:4])
	assert wait_until(lambda: port.in_waiting == 0)
	assert worker.receive(0.02) is None
	port.feed(data[4:])
	assert worker.receive(1.0).frame == ReplyFrame(Reply.ID, 3, bytes([2]))


def test_replies_come_out_in_order(worker, port):
	worker.start()
	port.feed(reply_frame(Reply.ACK, 1) + reply_frame(Reply.ACK, 2) + reply_frame(Reply.ACK, 3))
	assert [worker.receive(1.0).frame.sequence for _ in range(3)] == [1, 2, 3]


def test_receive_returns_none_when_nothing_arrives(worker):
	worker.start()
	assert worker.receive(0.02) is None


def test_receive_before_start_is_refused(worker):
	with pytest.raises(LinkStateError):
		worker.receive(0.01)


def test_the_parser_counters_are_visible(worker, port):
	worker.start()
	damaged = bytearray(reply_frame(Reply.ACK, 1))
	damaged[2] = Reply.NACK
	port.feed(bytes(damaged) + bytes([STX, BOARD_ID, Reply.STATUS, 200]))
	assert wait_until(lambda: worker.bad_crc_frames == 1 and worker.abandoned_frames == 1)


# --- Stopping -------------------------------------------------------------------

def test_stop_closes_the_port_and_ends_the_thread(worker, port):
	worker.start()
	worker.stop()
	assert port.close_count == 1
	assert not worker.is_open
	assert not any(t.name == "serial-COM_FAKE" for t in threading.enumerate())


def test_stop_twice_and_stop_before_start_are_harmless(worker, port):
	worker.stop()
	worker.stop()
	assert port.close_count == 0
	with pytest.raises(LinkStateError):
		worker.receive(0.01)
	with pytest.raises(LinkStateError):
		worker.start()


def test_stop_wakes_a_waiting_receive(worker):
	worker.start()
	outcome = []

	def wait_for_reply():
		started_s = time.perf_counter()
		try:
			worker.receive(5.0)
		except LinkStateError:
			outcome.append(time.perf_counter() - started_s)

	waiter = threading.Thread(target=wait_for_reply)
	waiter.start()
	time.sleep(0.02)
	worker.stop()
	waiter.join(2.0)
	assert outcome and outcome[0] < 1.0


def test_receive_after_stop_reports_the_link_closed(worker):
	worker.start()
	worker.stop()
	for _ in range(2):
		with pytest.raises(LinkStateError):
			worker.receive(0.01)


def test_context_manager_opens_and_closes(port):
	with SerialWorker("COM_FAKE", opener=lambda name: port) as worker:
		assert worker.is_open
	assert not worker.is_open
	assert port.closed


def test_context_manager_closes_on_an_exception(port):
	with pytest.raises(ValueError):
		with SerialWorker("COM_FAKE", opener=lambda name: port):
			raise ValueError("boom")
	assert port.closed


# --- Failures (C2.13: the port must be closed without anyone calling stop) -------

def test_a_read_failure_closes_the_port_and_is_reported(worker, port):
	worker.start()
	port.fail_read(serial.SerialException("device unplugged"))
	with pytest.raises(LinkLostError, match="device unplugged") as raised:
		worker.receive(1.0)
	assert isinstance(raised.value.__cause__, serial.SerialException)
	assert port.close_count == 1
	assert not worker.is_open


def test_a_write_failure_closes_the_port_and_is_reported(worker, port):
	worker.start()
	port.write_error = serial.SerialTimeoutException("Write timeout")
	with pytest.raises(LinkLostError, match="Write timeout"):
		worker.send(b"x")
	assert wait_until(lambda: port.closed)
	with pytest.raises(LinkLostError):
		worker.receive(1.0)
	assert port.close_count == 1


def test_after_a_loss_every_call_reports_it(worker, port):
	worker.start()
	port.fail_read(serial.SerialException("device unplugged"))
	for _ in range(2):
		with pytest.raises(LinkLostError):
			worker.receive(1.0)
	with pytest.raises(LinkLostError):
		worker.send(b"x")
	worker.stop()
	with pytest.raises(LinkLostError):
		worker.receive(0.01)


def test_replies_received_before_a_failure_are_delivered_first(worker, port):
	worker.start()
	port.feed(reply_frame(Reply.ACK, 1))
	assert wait_until(lambda: port.in_waiting == 0)
	port.fail_read(serial.SerialException("device unplugged"))
	assert worker.receive(1.0).frame.sequence == 1
	with pytest.raises(LinkLostError):
		worker.receive(1.0)


def test_an_unexpected_error_in_the_reader_also_closes_the_port(worker, port):
	worker.start()
	port.fail_read(ValueError("bug in a driver"))
	with pytest.raises(LinkLostError, match="bug in a driver"):
		worker.receive(1.0)
	assert port.close_count == 1


# --- Real pyserial ----------------------------------------------------------------

def test_round_trip_through_a_pyserial_loopback_port():
	with SerialWorker("loop://") as worker:
		sent_s = worker.send(build_frame(Command.GET_VERSION, 7))
		received = worker.receive(1.0)
	assert received.frame == ReplyFrame(Command.GET_VERSION, 7, b"")
	assert received.received_s >= sent_s

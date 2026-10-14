import threading
import time

import pytest
import serial

from polydriver.devices.link import BoardNotRespondingError, Link, ProtocolVersionError
from polydriver.protocol.constants import BOARD_ID, ETX, STX, Command, Reply
from polydriver.protocol.link_layer import ReplyParser, crc8
from polydriver.protocol.serial_worker import LinkOpenError
from polydriver.requests import SetPwm, Stop
from polydriver.requests.request_queue import QueueClosedError

FAST = {"boot_wait_s": 0.5, "reply_timeout_s": 0.02, "retries": 1}


def reply_bytes(command: int, sequence: int, payload: bytes = b"") -> bytes:
	body = bytes([BOARD_ID, command, len(payload), sequence]) + payload
	return bytes([STX]) + body + bytes([crc8(body), ETX])


class BoardPort:
	"""A port with a board behind it. It stays silent until boot_s has passed (the Uno's
	bootloader), then answers GET_VERSION with protocol_version and everything else with ACK."""

	def __init__(self, protocol_version: int = 3, boot_s: float = 0.0):
		self.protocol_version = protocol_version
		self.booted_at_s = time.monotonic() + boot_s
		self.closed = False
		self.requests: list[int] = []
		self._condition = threading.Condition()
		self._incoming = bytearray()
		self._parser = ReplyParser()

	@property
	def in_waiting(self) -> int:
		with self._condition:
			return len(self._incoming)

	def read(self, size: int = 1) -> bytes:
		with self._condition:
			self._condition.wait_for(lambda: self._incoming, timeout=0.02)
			data = bytes(self._incoming[:size])
			del self._incoming[:size]
			return data

	def write(self, data: bytes) -> int:
		if time.monotonic() < self.booted_at_s:
			return len(data)
		for frame in self._parser.feed(data, 0.0):
			self.requests.append(frame.command)
			if frame.command == Command.GET_VERSION:
				answer = reply_bytes(Reply.VERSION, frame.sequence, bytes([self.protocol_version, 3, 0, 0, 32]))
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
		self.closed = True


def test_open_checks_the_version_and_keeps_it():
	port = BoardPort()
	with Link.open("COM5", opener=lambda name: port, **FAST) as link:
		assert link.port == "COM5"
		assert link.version.protocol_version == 3
		assert link.version.max_segments == 32
		assert link.is_open
		assert link.call(SetPwm(10)) is None
	assert port.closed
	assert not link.is_open


def test_open_waits_for_a_board_that_is_still_in_its_bootloader():
	port = BoardPort(boot_s=0.2)
	started_s = time.monotonic()
	with Link.open("COM5", opener=lambda name: port, **FAST) as link:
		assert link.version.protocol_version == 3
	assert time.monotonic() - started_s >= 0.2
	assert port.requests[0] == Command.GET_VERSION


def test_a_board_that_never_answers_is_reported_and_the_port_closed():
	port = BoardPort(boot_s=60.0)
	with pytest.raises(BoardNotRespondingError, match="COM5"):
		Link.open("COM5", opener=lambda name: port, **FAST)
	assert port.closed
	assert not any(t.name in ("serial-COM5", "request-queue") for t in threading.enumerate())


def test_another_protocol_version_is_refused_and_the_port_closed():
	port = BoardPort(protocol_version=2)
	with pytest.raises(ProtocolVersionError, match="2"):
		Link.open("COM5", opener=lambda name: port, **FAST)
	assert port.closed


def test_a_port_that_cannot_be_opened_leaves_nothing_running():
	def refuse(name):
		raise serial.SerialException("could not open port 'COM9'")

	with pytest.raises(LinkOpenError):
		Link.open("COM9", opener=refuse, **FAST)
	assert not any(t.name in ("serial-COM9", "request-queue") for t in threading.enumerate())


def test_close_is_safe_twice_and_refuses_later_requests():
	link = Link.open("COM5", opener=lambda name: BoardPort(), **FAST)
	link.close()
	link.close()
	with pytest.raises(QueueClosedError):
		link.call(Stop())


def test_stats_include_both_ends_of_the_host():
	with Link.open("COM5", opener=lambda name: BoardPort(), **FAST) as link:
		link.call(Stop())
		stats = link.stats
	assert stats.frames_sent >= 2
	assert stats.host_bad_crc_frames == 0
	assert stats.host_abandoned_frames == 0


def test_the_observer_sees_every_exchange():
	exchanges = []
	with Link.open("COM5", opener=lambda name: BoardPort(), observer=exchanges.append, **FAST) as link:
		link.call(Stop())
	assert [e.request.COMMAND for e in exchanges][-1] == Command.STOP
	assert all(e.round_trip_s is not None for e in exchanges)

"""Framing of protocol v3 (docs/protocol.md sections 2-3): CRC-8, request frames, reply parser, SEQ.

The parser mirrors firmware/polydriver/lib/core/FrameParser.cpp, with the reply payload limit.
"""

import random
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from .constants import BOARD_ID, ETX, MAX_REPLY_PAYLOAD, MAX_REQUEST_PAYLOAD, STX

CRC8_INITIAL = 0x00
CRC8_POLYNOMIAL = 0x8C
SEQUENCE_COUNT = 256

# The board sends a whole reply in one burst (39 B max = 3.4 ms), but the host sees bytes only
# when its reader thread wakes, after USB latency and Windows' 15.6 ms scheduler tick. The
# board's 10 ms would abandon good replies here.
REPLY_INTER_BYTE_TIMEOUT_S = 0.1


class FrameBuildError(ValueError):
	pass


@dataclass(frozen=True)
class ReplyFrame:
	command: int
	sequence: int
	payload: bytes


def crc8(data: bytes) -> int:
	crc = CRC8_INITIAL
	for byte in data:
		for _ in range(8):
			mix = (crc ^ byte) & 0x01
			crc >>= 1
			if mix:
				crc ^= CRC8_POLYNOMIAL
			byte >>= 1
	return crc


def build_frame(command: int, sequence: int, payload: bytes = b"") -> bytes:
	if not 0 <= command <= 0xFF:
		raise FrameBuildError(f"command {command} does not fit in one byte")
	if not 0 <= sequence <= 0xFF:
		raise FrameBuildError(f"sequence {sequence} does not fit in one byte")
	if len(payload) > MAX_REQUEST_PAYLOAD:
		raise FrameBuildError(f"payload of {len(payload)} B exceeds MAX_REQUEST_PAYLOAD ({MAX_REQUEST_PAYLOAD} B)")
	body = bytes([BOARD_ID, command, len(payload), sequence]) + payload
	return bytes([STX]) + body + bytes([crc8(body), ETX])


class _State(Enum):
	WAIT_STX = auto()
	READ_ID = auto()
	READ_CMD = auto()
	READ_LEN = auto()
	READ_SEQ = auto()
	READ_PAYLOAD = auto()
	READ_CRC = auto()
	READ_ETX = auto()


class ReplyParser:
	def __init__(self, inter_byte_timeout_s: float = REPLY_INTER_BYTE_TIMEOUT_S):
		self._inter_byte_timeout_s = inter_byte_timeout_s
		self._state = _State.WAIT_STX
		self._command = 0
		self._length = 0
		self._sequence = 0
		self._payload = bytearray()
		self._received_crc = 0
		self._last_chunk_s = 0.0
		self._abandoned_frames = 0
		self._bad_crc_frames = 0

	@property
	def abandoned_frames(self) -> int:
		return self._abandoned_frames

	@property
	def bad_crc_frames(self) -> int:
		return self._bad_crc_frames

	def feed(self, data: bytes, now_s: float) -> list[ReplyFrame]:
		if self._state is not _State.WAIT_STX and now_s - self._last_chunk_s > self._inter_byte_timeout_s:
			self._abandon()
		self._last_chunk_s = now_s

		frames = []
		for byte in data:
			frame = self._feed_byte(byte)
			if frame is not None:
				frames.append(frame)
		return frames

	def _feed_byte(self, byte: int) -> ReplyFrame | None:
		match self._state:
			case _State.WAIT_STX:
				if byte == STX:
					self._state = _State.READ_ID

			case _State.READ_ID:
				if byte == BOARD_ID:
					self._state = _State.READ_CMD
				elif byte != STX:
					self._state = _State.WAIT_STX

			case _State.READ_CMD:
				self._command = byte
				self._state = _State.READ_LEN

			case _State.READ_LEN:
				if byte > MAX_REPLY_PAYLOAD:
					self._abandon()
				else:
					self._length = byte
					self._state = _State.READ_SEQ

			case _State.READ_SEQ:
				self._sequence = byte
				self._payload.clear()
				self._state = _State.READ_PAYLOAD if self._length > 0 else _State.READ_CRC

			case _State.READ_PAYLOAD:
				self._payload.append(byte)
				if len(self._payload) >= self._length:
					self._state = _State.READ_CRC

			case _State.READ_CRC:
				self._received_crc = byte
				self._state = _State.READ_ETX

			case _State.READ_ETX:
				return self._finish(byte)
		return None

	def _finish(self, byte: int) -> ReplyFrame | None:
		if byte != ETX:
			self._abandon()
			if byte == STX:
				self._state = _State.READ_ID
			return None

		self._state = _State.WAIT_STX
		body = bytes([BOARD_ID, self._command, self._length, self._sequence]) + self._payload
		if crc8(body) != self._received_crc:
			self._bad_crc_frames += 1
			return None
		return ReplyFrame(command=self._command, sequence=self._sequence, payload=bytes(self._payload))

	def _abandon(self) -> None:
		self._abandoned_frames += 1
		self._state = _State.WAIT_STX


class RandomSource(Protocol):
	def randrange(self, stop: int) -> int: ...


class SequenceCounter:
	"""One per link. Starts at a random value so a reconnecting host is not taken for a retry."""

	def __init__(self, source: RandomSource | None = None):
		self._next = (source or random.SystemRandom()).randrange(SEQUENCE_COUNT)

	def next_sequence(self) -> int:
		sequence = self._next
		self._next = (self._next + 1) % SEQUENCE_COUNT
		return sequence

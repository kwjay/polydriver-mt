"""A request is one protocol command: it encodes its payload and turns the board's reply into a
result or a named error. Retrying and sequence numbers belong to RequestQueue.

Requests check only what the frame cannot carry (integer ranges, enum values, floats beyond f32).
Everything the board judges (NaN, negative speed, alpha 0, ...) is sent and answered with a NACK,
so each rule lives in one place (decided 2026-10-10).
"""

import struct
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from ..protocol.constants import REPLY_PAYLOAD_LENGTH, REQUEST_FORMAT, Command, NackReason, Reply
from ..protocol.link_layer import ReplyFrame

Result = TypeVar("Result")


class RequestError(Exception):
	pass


class RequestValueError(RequestError, ValueError):
	pass


class RequestNackError(RequestError):
	def __init__(self, command: Command, reason: NackReason | int):
		self.command = command
		self.reason = reason
		reason_name = reason.name if isinstance(reason, NackReason) else f"unknown reason 0x{reason:02X}"
		super().__init__(f"{command.name} refused: {reason_name}")


class UnexpectedReplyError(RequestError):
	"""The board answered with another reply code: it believed it was answering something else."""

	def __init__(self, command: Command, expected: Reply, got: int):
		self.command = command
		self.expected = expected
		self.got = got
		super().__init__(f"{command.name} expects {expected.name} or NACK, got reply code 0x{got:02X}")


class PayloadFormatError(RequestError):
	"""A reply payload this host cannot decode: firmware and host speak different protocols."""


def nack_reason(value: int) -> NackReason | int:
	try:
		return NackReason(value)
	except ValueError:
		return value


@dataclass(frozen=True)
class Request(Generic[Result]):
	COMMAND: ClassVar[Command]
	REPLY: ClassVar[Reply] = Reply.ACK
	URGENT: ClassVar[bool] = False

	def __post_init__(self) -> None:
		self.encode()

	def values(self) -> tuple:
		return ()

	def encode(self) -> bytes:
		try:
			return struct.pack(REQUEST_FORMAT[self.COMMAND], *self.values())
		except (struct.error, OverflowError, ValueError) as error:
			raise RequestValueError(f"{self!r} cannot be sent: {error}") from error

	def handle(self, frame: ReplyFrame) -> Result:
		if frame.command == Reply.NACK:
			self._check_length(Reply.NACK, frame.payload)
			raise RequestNackError(self.COMMAND, nack_reason(frame.payload[0]))
		if frame.command != self.REPLY:
			raise UnexpectedReplyError(self.COMMAND, self.REPLY, frame.command)
		self._check_length(self.REPLY, frame.payload)
		return self.decode(frame.payload)

	def decode(self, payload: bytes) -> Result:
		return None  # type: ignore[return-value]  # ACK replies carry nothing

	def _check_length(self, reply: Reply, payload: bytes) -> None:
		expected = REPLY_PAYLOAD_LENGTH[reply]
		if len(payload) != expected:
			raise PayloadFormatError(
				f"{reply.name} payload is {len(payload)} B, protocol v3 says {expected} B: "
				"firmware and host protocol versions differ"
			)

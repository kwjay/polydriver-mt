"""Requests that load and run a job on the board (protocol.md section 6)."""

from dataclasses import dataclass
from typing import ClassVar

from ..protocol.constants import Command, Reply, SegmentFlag
from .base import Request
from .reports import JobInfo


@dataclass(frozen=True)
class JobClear(Request[None]):
	COMMAND: ClassVar[Command] = Command.JOB_CLEAR


@dataclass(frozen=True)
class JobSegment(Request[None]):
	"""value is a speed in Hz, or a duty 0-255 when duty is set."""

	COMMAND: ClassVar[Command] = Command.JOB_SEGMENT
	index: int
	value: float
	duration_ms: int
	ramp: bool = False
	duty: bool = False

	def values(self) -> tuple:
		flags = (SegmentFlag.RAMP if self.ramp else 0) | (SegmentFlag.DUTY if self.duty else 0)
		return (self.index, int(flags), self.value, self.duration_ms)


@dataclass(frozen=True)
class JobCommit(Request[JobInfo]):
	COMMAND: ClassVar[Command] = Command.JOB_COMMIT
	REPLY: ClassVar[Reply] = Reply.JOB_INFO
	segment_count: int
	total_ms: int

	def values(self) -> tuple:
		return (self.segment_count, self.total_ms)

	def decode(self, payload: bytes) -> JobInfo:
		return JobInfo.decode(payload)


@dataclass(frozen=True)
class JobRun(Request[None]):
	COMMAND: ClassVar[Command] = Command.JOB_RUN

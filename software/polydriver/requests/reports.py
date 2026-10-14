"""Decoded data replies. Field names and order are STATUS_FIELDS etc. in protocol/constants.py."""

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import TypeVar

from ..protocol.constants import (
	JOB_INFO_FORMAT, NO_SEGMENT, SETTINGS_FORMAT, STATUS_FORMAT, VERSION_FORMAT, JobState, Mode, Role,
	StatusFlag,
)
from .base import PayloadFormatError

Enumeration = TypeVar("Enumeration", bound=IntEnum)


def decode_enum(enum_type: type[Enumeration], value: int, what: str) -> Enumeration:
	try:
		return enum_type(value)
	except ValueError:
		raise PayloadFormatError(f"unknown {what} {value}: firmware and host protocol versions differ") from None


@dataclass(frozen=True)
class StatusReport:
	raw_speed_hz: float
	filtered_speed_hz: float
	duty: int
	flags: StatusFlag
	device_time_us: int
	edge_count: int
	control_resyncs: int
	longest_loop_interval_us: int
	dropped_frames: int
	bad_crc_frames: int
	job_state: JobState
	segment_index: int | None
	job_elapsed_ms: int

	@classmethod
	def decode(cls, payload: bytes) -> "StatusReport":
		(raw, filtered, duty, flags, device_time, edges, resyncs, longest, dropped, bad_crc,
		 job_state, segment, elapsed) = struct.unpack(STATUS_FORMAT, payload)
		return cls(
			raw_speed_hz=raw,
			filtered_speed_hz=filtered,
			duty=duty,
			flags=StatusFlag(flags),
			device_time_us=device_time,
			edge_count=edges,
			control_resyncs=resyncs,
			longest_loop_interval_us=longest,
			dropped_frames=dropped,
			bad_crc_frames=bad_crc,
			job_state=decode_enum(JobState, job_state, "job state"),
			segment_index=None if segment == NO_SEGMENT else segment,
			job_elapsed_ms=elapsed,
		)


@dataclass(frozen=True)
class SettingsReport:
	kp: float
	ki: float
	kd: float
	target_speed_hz: float
	comms_timeout_ms: int
	mode: Mode
	duty_setpoint: int
	alpha: float
	feedforward_offset_duty: float
	feedforward_gain_duty_per_hz: float

	@classmethod
	def decode(cls, payload: bytes) -> "SettingsReport":
		(kp, ki, kd, target, timeout, mode, duty, alpha, offset, gain) = struct.unpack(SETTINGS_FORMAT, payload)
		return cls(
			kp=kp,
			ki=ki,
			kd=kd,
			target_speed_hz=target,
			comms_timeout_ms=timeout,
			mode=decode_enum(Mode, mode, "mode"),
			duty_setpoint=duty,
			alpha=alpha,
			feedforward_offset_duty=offset,
			feedforward_gain_duty_per_hz=gain,
		)


@dataclass(frozen=True)
class VersionReport:
	protocol_version: int
	firmware_major: int
	firmware_minor: int
	firmware_patch: int
	max_segments: int

	@classmethod
	def decode(cls, payload: bytes) -> "VersionReport":
		return cls(*struct.unpack(VERSION_FORMAT, payload))


@dataclass(frozen=True)
class JobInfo:
	job_state: JobState
	segment_count: int
	total_ms: int

	@classmethod
	def decode(cls, payload: bytes) -> "JobInfo":
		job_state, count, total = struct.unpack(JOB_INFO_FORMAT, payload)
		return cls(job_state=decode_enum(JobState, job_state, "job state"), segment_count=count, total_ms=total)


def decode_role(payload: bytes) -> Role:
	return decode_enum(Role, payload[0], "role")

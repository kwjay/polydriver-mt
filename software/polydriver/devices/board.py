"""One dispenser board: typed calls over a Link (C6.2)."""

import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from ..config import BoardSettings
from ..protocol.constants import Mode, Role, StatusFlag
from ..requests import (
	ClearEstop, Estop, GetId, GetVersion, JobClear, JobCommit, JobInfo, JobRun, JobSegment, Request,
	RequestSettings, RequestStatus, SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetPwm,
	SetSpeed, SettingsReport, StatusReport, Stop, VersionReport,
)
from .link import DeviceError

# One control tick. Shorter segments are stretched or skipped by the board, so the host never
# sends them (decided 2026-10-11).
MIN_SEGMENT_MS = 10

WRAP_US = 2**32

Result = TypeVar("Result")


class JobUploadError(DeviceError):
	pass


class SettingsMismatchError(DeviceError):
	"""SETTINGS read back after apply_settings() differs from what was sent."""


class Requester(Protocol):
	version: VersionReport

	def call(self, request: Request[Result], timeout_s: float | None = None) -> Result: ...


@dataclass(frozen=True)
class Segment:
	"""value is a speed in Hz, or a duty 0-255 when duty is set."""

	value: float
	duration_ms: int
	ramp: bool = False
	duty: bool = False


@dataclass(frozen=True)
class BoardStatus:
	report: StatusReport
	device_time_us: int
	device_restarted: bool


class DeviceClock:
	"""Turns the board's 32-bit micros() (wraps every ~71.6 min) into a running count.

	A step backwards of more than half the range is a wrap; a smaller one means the board
	restarted, and the count continues from where it was. Needs a sample at least every ~35 min.
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._last_raw_us: int | None = None
		self._total_us = 0
		self._restarts = 0

	@property
	def restarts(self) -> int:
		return self._restarts

	def unwrap(self, raw_us: int) -> int:
		with self._lock:
			if self._last_raw_us is None:
				self._total_us = raw_us
			else:
				forward_us = (raw_us - self._last_raw_us) % WRAP_US
				if forward_us < WRAP_US // 2:
					self._total_us += forward_us
				else:
					self._restarts += 1
					self._total_us += raw_us
			self._last_raw_us = raw_us
			return self._total_us


class Board:
	def __init__(self, link: Requester):
		self._link = link
		self._clock = DeviceClock()
		self._lock = threading.Lock()
		self._seen_configured = False
		self._extra_restarts = 0

	@property
	def link(self) -> Requester:
		return self._link

	@property
	def device_restarts(self) -> int:
		return self._clock.restarts + self._extra_restarts

	# --- Reads --------------------------------------------------------------------

	def version(self) -> VersionReport:
		return self._link.call(GetVersion())

	def role(self) -> Role:
		return self._link.call(GetId())

	def settings(self) -> SettingsReport:
		return self._link.call(RequestSettings())

	def status(self) -> BoardStatus:
		report = self._link.call(RequestStatus())
		restarts_before = self._clock.restarts
		device_time_us = self._clock.unwrap(report.device_time_us)
		restarted = self._clock.restarts != restarts_before
		with self._lock:
			unconfigured = StatusFlag.UNCONFIGURED in report.flags
			if unconfigured and self._seen_configured and not restarted:
				self._extra_restarts += 1
				restarted = True
			if restarted or unconfigured:
				self._seen_configured = not unconfigured
			else:
				self._seen_configured = True
		return BoardStatus(report, device_time_us, restarted)

	# --- Settings -----------------------------------------------------------------

	def set_role(self, role: Role) -> None:
		self._link.call(SetId(role))

	def set_mode(self, mode: Mode) -> None:
		self._link.call(SetMode(mode))

	def set_pid(self, kp: float, ki: float, kd: float) -> None:
		self._link.call(SetPid(kp, ki, kd))

	def set_filter(self, alpha: float) -> None:
		self._link.call(SetFilter(alpha))

	def set_feedforward(self, offset_duty: float, gain_duty_per_hz: float) -> None:
		self._link.call(SetFeedforward(offset_duty, gain_duty_per_hz))

	def set_comms_timeout_ms(self, timeout_ms: int) -> None:
		self._link.call(SetCommsTimeout(timeout_ms))

	def apply_settings(self, settings: BoardSettings, comms_timeout_ms: int) -> SettingsReport:
		"""Sends the config in the decided order (2026-10-12) and checks it with SETTINGS. Without gains
		SET_PID is skipped, so the board stays UNCONFIGURED."""
		self.set_comms_timeout_ms(comms_timeout_ms)
		if settings.has_gains:
			self.set_pid(settings.kp, settings.ki, settings.kd)
		self.set_filter(settings.alpha)
		self.set_feedforward(settings.ff_offset_duty, settings.ff_gain_duty_per_hz)
		report = self.settings()

		expected = {
			"alpha": (settings.alpha, report.alpha),
			"ff_offset_duty": (settings.ff_offset_duty, report.feedforward_offset_duty),
			"ff_gain_duty_per_hz": (settings.ff_gain_duty_per_hz, report.feedforward_gain_duty_per_hz),
		}
		if settings.has_gains:
			expected |= {"kp": (settings.kp, report.kp), "ki": (settings.ki, report.ki), "kd": (settings.kd, report.kd)}
		wrong = [f"{name} sent {sent:g}, board has {held:g}" for name, (sent, held) in expected.items() if _f32(sent) != held]
		if report.comms_timeout_ms != comms_timeout_ms:
			wrong.append(f"comms timeout sent {comms_timeout_ms} ms, board has {report.comms_timeout_ms} ms")
		if wrong:
			raise SettingsMismatchError("the board did not keep: " + "; ".join(wrong))
		return report

	# --- Motor --------------------------------------------------------------------

	def set_speed(self, target_speed_hz: float) -> None:
		self._link.call(SetSpeed(target_speed_hz))

	def set_pwm(self, duty: int) -> None:
		self._link.call(SetPwm(duty))

	def stop(self) -> None:
		self._link.call(Stop())

	def estop(self) -> None:
		self._link.call(Estop())

	def clear_estop(self) -> None:
		self._link.call(ClearEstop())

	# --- Jobs ---------------------------------------------------------------------

	def upload_job(self, segments: Sequence[Segment]) -> JobInfo:
		limit = self._link.version.max_segments
		if not segments:
			raise JobUploadError("the job is empty")
		if len(segments) > limit:
			raise JobUploadError(f"the job has {len(segments)} segments, the board holds {limit} segments")
		for index, segment in enumerate(segments):
			if segment.duration_ms < MIN_SEGMENT_MS:
				raise JobUploadError(
					f"segment {index} lasts {segment.duration_ms} ms, shorter than one {MIN_SEGMENT_MS} ms control tick"
				)
		self._link.call(JobClear())
		for index, segment in enumerate(segments):
			self._link.call(JobSegment(index, segment.value, segment.duration_ms, ramp=segment.ramp, duty=segment.duty))
		total_ms = sum(segment.duration_ms for segment in segments)
		return self._link.call(JobCommit(len(segments), total_ms))

	def run_job(self) -> None:
		self._link.call(JobRun())

	def clear_job(self) -> None:
		self._link.call(JobClear())


def _f32(value: float) -> float:
	"""The value as the board stores it (IEEE-754 single precision)."""
	return struct.unpack("<f", struct.pack("<f", value))[0]

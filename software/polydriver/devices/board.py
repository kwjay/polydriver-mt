"""One dispenser board: typed calls over a Link (C6.2)."""

import logging
import math
import struct
import threading
import time
from collections.abc import Callable, Sequence
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

logger = logging.getLogger(__name__)

# One control tick. Shorter segments are stretched or skipped by the board, so the host never
# sends them (decided 2026-10-11).
MIN_SEGMENT_MS = 10
MAX_DUTY = 255

WRAP_US = 2**32
# How far the board's step between two STATUS replies may differ from the host's: the reply's
# travel time and the poll jitter are milliseconds, a restart loses the whole boot time (> 1 s).
MAX_STEP_MISMATCH_US = 500_000
# The two crystals drift apart; 188 ppm measured on 2026-10-15.
CLOCK_TOLERANCE = 0.01

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
	"""Turns the board's 32-bit micros() (wraps every ~71.6 min) into a running count, and notices
	when the board restarted (micros() starts again from 0); the count then goes on from where it was.

	With the host time since the last sample, the board's step must match it (within
	MAX_STEP_MISMATCH_US plus CLOCK_TOLERANCE of it); any other step is a restart. Without it, a
	step of more than half the range counts as a restart, which misses restarts after ~36 min of
	uptime, so the Board always passes the host time.
	"""

	def __init__(self):
		self._lock = threading.Lock()
		self._last_raw_us: int | None = None
		self._total_us = 0
		self._restarts = 0

	@property
	def restarts(self) -> int:
		return self._restarts

	def unwrap(self, raw_us: int, host_elapsed_us: float | None = None) -> int:
		with self._lock:
			if self._last_raw_us is None:
				self._total_us = raw_us
			else:
				forward_us = (raw_us - self._last_raw_us) % WRAP_US
				if host_elapsed_us is None:
					restarted = forward_us >= WRAP_US // 2
				else:
					wraps = max(0, round((host_elapsed_us - forward_us) / WRAP_US))
					forward_us += wraps * WRAP_US
					allowed_us = MAX_STEP_MISMATCH_US + CLOCK_TOLERANCE * host_elapsed_us
					restarted = abs(forward_us - host_elapsed_us) > allowed_us
				if restarted:
					self._restarts += 1
					self._total_us += raw_us
				else:
					self._total_us += forward_us
			self._last_raw_us = raw_us
			return self._total_us


class Board:
	def __init__(self, link: Requester, clock: Callable[[], float] = time.perf_counter,
				on_change: Callable[[str], None] | None = None):
		"""on_change hears what each successful command changed on the board (not apply_settings,
		which its caller reports as one change)."""
		self._link = link
		self._on_change = on_change
		self._host_clock = clock
		self._last_status_host_s: float | None = None
		self._clock = DeviceClock()
		self._lock = threading.Lock()
		self._status_lock = threading.Lock()
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
		# One at a time, so the device times are unwrapped in the order the board sent them.
		with self._status_lock:
			return self._status()

	def _status(self) -> BoardStatus:
		report = self._link.call(RequestStatus())
		with self._lock:
			now_s = self._host_clock()
			elapsed_us = None if self._last_status_host_s is None else (now_s - self._last_status_host_s) * 1e6
			self._last_status_host_s = now_s
			restarts_before = self._clock.restarts
			device_time_us = self._clock.unwrap(report.device_time_us, elapsed_us)
			restarted = self._clock.restarts != restarts_before
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
		self._change(SetId(role), f"role stored: {role.name.lower()}")

	def set_mode(self, mode: Mode) -> None:
		self._change(SetMode(mode), f"mode {Mode(mode).name}")

	def set_pid(self, kp: float, ki: float, kd: float) -> None:
		self._change(SetPid(kp, ki, kd), f"pid {kp:g} {ki:g} {kd:g}")

	def set_filter(self, alpha: float) -> None:
		self._change(SetFilter(alpha), f"filter alpha {alpha:g}")

	def set_feedforward(self, offset_duty: float, gain_duty_per_hz: float) -> None:
		self._change(SetFeedforward(offset_duty, gain_duty_per_hz),
					f"feedforward {offset_duty:g} duty + {gain_duty_per_hz:g} duty/Hz")

	def set_comms_timeout_ms(self, timeout_ms: int) -> None:
		self._change(SetCommsTimeout(timeout_ms), "comms timeout " + (f"{timeout_ms} ms" if timeout_ms else "off"))

	def apply_settings(self, settings: BoardSettings, comms_timeout_ms: int) -> SettingsReport:
		"""Sends the config in the decided order (2026-10-12) and checks it with SETTINGS. Without gains
		SET_PID is skipped, so the board stays UNCONFIGURED."""
		self._link.call(SetCommsTimeout(comms_timeout_ms))
		if settings.has_gains:
			self._link.call(SetPid(settings.kp, settings.ki, settings.kd))
		self._link.call(SetFilter(settings.alpha))
		self._link.call(SetFeedforward(settings.ff_offset_duty, settings.ff_gain_duty_per_hz))
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
		self._change(SetSpeed(target_speed_hz), f"speed {target_speed_hz:g} Hz")

	def set_pwm(self, duty: int) -> None:
		self._change(SetPwm(duty), f"duty {duty}")

	def stop(self) -> None:
		self._change(Stop(), "STOP")

	def estop(self) -> None:
		self._change(Estop(), "ESTOP")

	def clear_estop(self) -> None:
		self._change(ClearEstop(), "ESTOP cleared")

	# --- Jobs ---------------------------------------------------------------------

	def upload_job(self, segments: Sequence[Segment]) -> JobInfo:
		limit = self._link.version.max_segments
		if not segments:
			raise JobUploadError("the job is empty")
		if len(segments) > limit:
			raise JobUploadError(f"the job has {len(segments)} segments, the board holds {limit} segments")
		if len({segment.duty for segment in segments}) > 1:
			raise JobUploadError("the job mixes speed and duty segments; the board runs one kind per job")
		for index, segment in enumerate(segments):
			if segment.duration_ms < MIN_SEGMENT_MS:
				raise JobUploadError(
					f"segment {index} lasts {segment.duration_ms} ms, shorter than one {MIN_SEGMENT_MS} ms control tick"
				)
			if not (math.isfinite(segment.value) and segment.value >= 0):
				raise JobUploadError(f"segment {index} has the value {segment.value}; it must be a number >= 0")
			if segment.duty and segment.value > MAX_DUTY:
				raise JobUploadError(f"segment {index} has duty {segment.value:g}; a duty is 0 to {MAX_DUTY}")
		self._link.call(JobClear())
		for index, segment in enumerate(segments):
			self._link.call(JobSegment(index, segment.value, segment.duration_ms, ramp=segment.ramp, duty=segment.duty))
		total_ms = sum(segment.duration_ms for segment in segments)
		info = self._link.call(JobCommit(len(segments), total_ms))
		self._changed(f"job uploaded: {len(segments)} segments, {total_ms} ms")
		return info

	def run_job(self) -> None:
		self._change(JobRun(), "job started")

	def clear_job(self) -> None:
		self._change(JobClear(), "job cleared")

	# --- Reporting changes ------------------------------------------------------------

	def _change(self, request: Request, description: str) -> None:
		self._link.call(request)
		self._changed(description)

	def _changed(self, description: str) -> None:
		if self._on_change is None:
			return
		try:
			self._on_change(description)
		except Exception:
			logger.exception("board: reporting %r failed", description)


def _f32(value: float) -> float:
	"""The value as the board stores it (IEEE-754 single precision)."""
	return struct.unpack("<f", struct.pack("<f", value))[0]

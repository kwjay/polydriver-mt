"""A simulated board behind the same serial interface as a real one (C8.3).

It speaks protocol v3 as firmware 3.0.0 does: CommandDispatcher, DispenserController, PIDRegulator,
EMAFilter, JobProgram, JobRunner, FrameParser and InputCapture are mirrored here rule for rule. A
simple motor model turns the screw. Fault switches: corrupted replies or requests, dropped replies, a
jammed screw, a lost link and a clock running fast.

The board's time follows the host clock. The board catches up in 10 ms control ticks whenever it is
written to or looked at, so it needs no thread of its own and a manual clock steps it exactly.

	bench = SimBench.pair()
	session = Session(replace(config, ports=bench.ports), link_factory=bench.link_factory)
"""

import math
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import serial

from ..devices.link import Link
from ..jobs.check import CLOCK_HZ, EDGE_TIMEOUT_OVERFLOWS, PRESCALER, TIMER_PERIOD_TICKS
from ..protocol.constants import (
	BOARD_ID, ETX, INTER_BYTE_TIMEOUT_MS, JOB_INFO_FORMAT, MAX_DUTY, MAX_REQUEST_PAYLOAD, NO_SEGMENT,
	PROTOCOL_VERSION, REQUEST_FORMAT, REQUEST_PAYLOAD_LENGTH, SETTINGS_FORMAT, STATUS_FORMAT, STX, VERSION_FORMAT,
	Command, JobState, Mode, NackReason, Reply, Role, SegmentFlag, StatusFlag,
)
from ..protocol.link_layer import crc8
from ..protocol.serial_worker import READ_TIMEOUT_S

FIRMWARE_VERSION = (3, 0, 0)
CONTROL_PERIOD_US = 10_000
CONTROL_DT_S = 0.01
MAX_SEGMENTS = 32
DEFAULT_ALPHA = 1.0
PID_OUTPUT_MAX = 255.0
FLT_MAX = 3.4028234663852886e38
U32 = 2**32
US_PER_TIMER_TICK = PRESCALER * 1e6 / CLOCK_HZ
EDGE_TIMEOUT_TICKS = TIMER_PERIOD_TICKS * EDGE_TIMEOUT_OVERFLOWS
# Below this the decaying screw counts as standing, so an idle board can skip its ticks.
STANDING_HZ = 1e-6

REFUSED_WHILE_RUNNING = frozenset({
	Command.SET_SPEED, Command.SET_PWM, Command.SET_MODE, Command.SET_ID,
	Command.JOB_CLEAR, Command.JOB_SEGMENT, Command.JOB_COMMIT,
})


@dataclass(frozen=True)
class MotorModel:
	"""The screw's speed follows the duty with a first-order lag of lag_s: nothing up to dead_duty,
	then hz_per_duty for every step above it. The defaults are the simavr bridge's motor."""

	dead_duty: int = 128
	hz_per_duty: float = 0.6
	lag_s: float = 0.3

	def __post_init__(self) -> None:
		if not 0 <= self.dead_duty <= MAX_DUTY:
			raise ValueError(f"dead_duty = {self.dead_duty!r} must be 0 to {MAX_DUTY}")
		if not (math.isfinite(self.hz_per_duty) and self.hz_per_duty >= 0):
			raise ValueError(f"hz_per_duty = {self.hz_per_duty!r} must be a finite number >= 0")
		if not (math.isfinite(self.lag_s) and self.lag_s > 0):
			raise ValueError(f"lag_s = {self.lag_s!r} must be a finite number > 0")

	def steady_hz(self, duty: int) -> float:
		return max(0, duty - self.dead_duty) * self.hz_per_duty


@dataclass(frozen=True)
class SimState:
	"""What the board is doing, for tests and for watching a simulation."""

	time_us: int
	role: Role
	mode: Mode
	target_speed_hz: float
	duty: int
	motor_hz: float
	edge_count: int
	job_state: JobState
	job_elapsed_ms: int
	comms_lost: bool
	estopped: bool


def _in_float_range(value: float) -> bool:
	return 0.0 <= value <= FLT_MAX


class _Pid:
	def __init__(self):
		self.kp = self.ki = self.kd = 0.0
		self.reset()

	def reset(self) -> None:
		self.integral = 0.0
		self.previous = 0.0
		self.has_previous = False

	def set_tunings(self, kp: float, ki: float, kd: float) -> bool:
		if not all(_in_float_range(gain) for gain in (kp, ki, kd)):
			return False
		self.kp, self.ki, self.kd = kp, ki, kd
		return True

	def calculate(self, setpoint: float, measurement: float, dt_s: float, feedforward: float) -> float:
		error = setpoint - measurement
		p_term = self.kp * error
		d_term = -self.kd * (measurement - self.previous) / dt_s if self.has_previous else 0.0
		self.previous = measurement
		self.has_previous = True

		others = feedforward + p_term + d_term
		candidate = self.integral + self.ki * error * dt_s
		if error > 0:
			room = PID_OUTPUT_MAX - others
			if candidate > room:
				candidate = self.integral if self.integral > room else room
		elif error < 0:
			room = -others
			if candidate < room:
				candidate = self.integral if self.integral < room else room
		self.integral = _clamp(candidate, -PID_OUTPUT_MAX, PID_OUTPUT_MAX)
		return _clamp(others + self.integral, 0.0, PID_OUTPUT_MAX)


def _clamp(value: float, low: float, high: float) -> float:
	# NaN fails both comparisons and maps to low, as in the firmware.
	if value > high:
		return high
	if value >= low:
		return value
	return low


class _Ema:
	def __init__(self):
		self.alpha = DEFAULT_ALPHA
		self.reset()

	def reset(self) -> None:
		self.initialized = False
		self.last = 0.0

	def set_alpha(self, alpha: float) -> bool:
		if not 0.0 < alpha <= 1.0:
			return False
		self.alpha = alpha
		return True

	def filter(self, sample: float) -> float:
		if not self.initialized:
			self.last, self.initialized = sample, True
		else:
			self.last = self.alpha * sample + (1.0 - self.alpha) * self.last
		return self.last


class _Controller:
	"""DispenserController; the setters answer None for OK or the NACK reason."""

	def __init__(self):
		self.pid = _Pid()
		self.ema = _Ema()
		self.mode = Mode.CLOSED_LOOP
		self.target_hz = 0.0
		self.duty_setpoint = 0
		self.filtered_hz = 0.0
		self.output_duty = 0
		self.ff_offset_duty = 0.0
		self.ff_gain_duty_per_hz = 0.0
		self.comms_timeout_ms = 0
		self.last_activity_us = 0
		self.comms_lost = False
		self.estopped = False
		self.configured = False

	def set_target(self, target_hz: float) -> NackReason | None:
		if self.estopped:
			return NackReason.WRONG_STATE
		if self.mode is not Mode.CLOSED_LOOP:
			return NackReason.WRONG_MODE
		if not _in_float_range(target_hz):
			return NackReason.BAD_VALUE
		if target_hz > 0 and not self.configured:
			return NackReason.NOT_CONFIGURED
		self.target_hz = target_hz
		self.comms_lost = False
		if target_hz == 0:
			self._reset_regulation()
		return None

	def set_duty(self, duty: int) -> NackReason | None:
		if self.estopped:
			return NackReason.WRONG_STATE
		if self.mode is not Mode.OPEN_LOOP:
			return NackReason.WRONG_MODE
		self.duty_setpoint = duty
		self.comms_lost = False
		return None

	def set_mode(self, value: int) -> NackReason | None:
		if self.estopped:
			return NackReason.WRONG_STATE
		if value not in (Mode.CLOSED_LOOP, Mode.OPEN_LOOP):
			return NackReason.BAD_VALUE
		self.mode = Mode(value)
		self.target_hz = 0.0
		self.duty_setpoint = 0
		self._reset_regulation()
		return None

	def set_tunings(self, kp: float, ki: float, kd: float) -> NackReason | None:
		if not self.pid.set_tunings(kp, ki, kd):
			return NackReason.BAD_VALUE
		self.configured = True
		return None

	def set_alpha(self, alpha: float) -> NackReason | None:
		return None if self.ema.set_alpha(alpha) else NackReason.BAD_VALUE

	def set_feedforward(self, offset_duty: float, gain_duty_per_hz: float) -> NackReason | None:
		if not (0.0 <= offset_duty <= MAX_DUTY and _in_float_range(gain_duty_per_hz)):
			return NackReason.BAD_VALUE
		self.ff_offset_duty, self.ff_gain_duty_per_hz = offset_duty, gain_duty_per_hz
		return None

	def set_comms_timeout(self, timeout_ms: int, now_us: int) -> None:
		self.comms_timeout_ms = timeout_ms
		self.last_activity_us = now_us

	def stop(self) -> None:
		self.target_hz = 0.0
		self.duty_setpoint = 0
		self._reset_regulation()

	def estop(self) -> None:
		self.stop()
		self.estopped = True

	@property
	def motor_commanded(self) -> bool:
		return self.duty_setpoint > 0 if self.mode is Mode.OPEN_LOOP else self.target_hz > 0

	@property
	def regulating(self) -> bool:
		return self.mode is Mode.CLOSED_LOOP and self.target_hz > 0

	def update(self, raw_hz: float, now_us: int) -> int:
		self.filtered_hz = self.ema.filter(raw_hz)
		if self.comms_timeout_ms > 0 and self.motor_commanded:
			if now_us - self.last_activity_us >= self.comms_timeout_ms * 1000:
				self.stop()
				self.comms_lost = True
		if self.mode is Mode.OPEN_LOOP:
			self.output_duty = self.duty_setpoint
		elif self.target_hz > 0:
			feedforward = self.ff_offset_duty + self.ff_gain_duty_per_hz * self.target_hz
			self.output_duty = int(self.pid.calculate(self.target_hz, self.filtered_hz, CONTROL_DT_S, feedforward) + 0.5)
		else:
			self.output_duty = 0
		return self.output_duty

	def _reset_regulation(self) -> None:
		self.pid.reset()
		self.ema.reset()


@dataclass(frozen=True)
class _Segment:
	value: float
	duration_ms: int
	flags: int


class _Program:
	def __init__(self):
		self.clear()

	def clear(self) -> None:
		self.segments: list[_Segment] = []
		self.total_ms = 0
		self.state = JobState.EMPTY

	def add(self, index: int, flags: int, value: float, duration_ms: int) -> NackReason | None:
		if self.state not in (JobState.EMPTY, JobState.LOADING):
			return NackReason.WRONG_STATE
		if index >= MAX_SEGMENTS:
			return NackReason.JOB_FULL
		if index != len(self.segments):
			return NackReason.BAD_INDEX
		if flags & ~int(SegmentFlag.RAMP | SegmentFlag.DUTY):
			return NackReason.BAD_VALUE
		if not _in_float_range(value) or (flags & SegmentFlag.DUTY and value > MAX_DUTY):
			return NackReason.BAD_VALUE
		if duration_ms == 0 or duration_ms > U32 - 1 - self.total_ms:
			return NackReason.BAD_VALUE
		self.segments.append(_Segment(value, duration_ms, flags))
		self.total_ms += duration_ms
		self.state = JobState.LOADING
		return None

	def commit(self, count: int, total_ms: int) -> NackReason | None:
		if self.state is not JobState.LOADING:
			return NackReason.WRONG_STATE
		duty_types = {s.flags & SegmentFlag.DUTY for s in self.segments}
		if count != len(self.segments) or total_ms != self.total_ms or len(duty_types) > 1:
			return NackReason.BAD_JOB
		self.state = JobState.READY
		return None

	@property
	def is_duty_job(self) -> bool:
		return bool(self.segments) and bool(self.segments[0].flags & SegmentFlag.DUTY)

	def setpoint_at(self, elapsed_ms: int) -> tuple[float, int]:
		start_ms, previous = 0, 0.0
		for index, segment in enumerate(self.segments):
			end_ms = start_ms + segment.duration_ms
			if elapsed_ms < end_ms:
				if not segment.flags & SegmentFlag.RAMP:
					return segment.value, index
				fraction = (elapsed_ms - start_ms) / segment.duration_ms
				return previous + (segment.value - previous) * fraction, index
			previous, start_ms = segment.value, end_ms
		return 0.0, NO_SEGMENT


class _Runner:
	def __init__(self, program: _Program, controller: _Controller):
		self.program = program
		self.controller = controller
		self.phase = JobState.EMPTY
		self.last_tick_us = 0
		self.elapsed_ms = 0
		self.remainder_us = 0
		self.segment_index = NO_SEGMENT

	@property
	def running(self) -> bool:
		return self.phase is JobState.RUNNING

	def start(self, now_us: int) -> NackReason | None:
		if self.running or self.program.state is not JobState.READY or self.controller.estopped:
			return NackReason.WRONG_STATE
		duty_job = self.program.is_duty_job
		if self.controller.mode is not (Mode.OPEN_LOOP if duty_job else Mode.CLOSED_LOOP):
			return NackReason.WRONG_MODE
		if not duty_job and not self.controller.configured:
			return NackReason.NOT_CONFIGURED
		self.phase = JobState.RUNNING
		self.last_tick_us = now_us
		self.elapsed_ms = self.remainder_us = 0
		if not self._apply(0):
			self.controller.stop()
			self._finish(JobState.ABORTED)
			return NackReason.WRONG_STATE
		return None

	def tick(self, now_us: int) -> None:
		if not self.running:
			return
		if self.controller.comms_lost or self.controller.estopped:
			self._finish(JobState.ABORTED)
			return
		self.remainder_us += now_us - self.last_tick_us
		self.last_tick_us = now_us
		self.elapsed_ms += self.remainder_us // 1000
		self.remainder_us %= 1000
		if self.elapsed_ms >= self.program.total_ms:
			self.controller.stop()
			self.elapsed_ms = self.program.total_ms
			self._finish(JobState.DONE)
		elif not self._apply(self.elapsed_ms):
			self.controller.stop()
			self._finish(JobState.ABORTED)

	def abort(self) -> None:
		if self.running:
			self._finish(JobState.ABORTED)

	def reset(self) -> None:
		if self.running:
			return
		self.phase = JobState.EMPTY
		self.elapsed_ms = self.remainder_us = 0
		self.segment_index = NO_SEGMENT

	@property
	def _has_outcome(self) -> bool:
		return self.phase is not JobState.EMPTY and self.program.state is JobState.READY

	@property
	def state(self) -> JobState:
		return self.phase if self._has_outcome else self.program.state

	@property
	def reported_elapsed_ms(self) -> int:
		return self.elapsed_ms if self._has_outcome else 0

	def _apply(self, elapsed_ms: int) -> bool:
		setpoint, self.segment_index = self.program.setpoint_at(elapsed_ms)
		if self.program.is_duty_job:
			return self.controller.set_duty(int(setpoint + 0.5)) is None
		return self.controller.set_target(setpoint) is None

	def _finish(self, outcome: JobState) -> None:
		self.phase = outcome
		self.segment_index = NO_SEGMENT


class _Screw:
	"""The motor model and InputCapture: edges where the screw's turns cross whole numbers, timed
	in 4 us timer ticks; stalled once no edge came for two timer overflows."""

	def __init__(self, motor: MotorModel):
		self.motor = motor
		self.speed_hz = 0.0
		self.turns = 0.0
		self.edge_count = 0
		self.previous_edge_ticks = 0
		self.period_ticks = 0
		self.stalled = False

	def turn(self, start_us: int, end_us: int, duty: int, jammed: bool) -> None:
		if jammed:
			self.speed_hz = steady_hz = 0.0
		else:
			steady_hz = self.motor.steady_hz(duty)
		dt_s = (end_us - start_us) / 1e6
		decay = math.exp(-dt_s / self.motor.lag_s)
		turned = steady_hz * dt_s + (self.speed_hz - steady_hz) * self.motor.lag_s * (1 - decay)
		self.speed_hz = steady_hz + (self.speed_hz - steady_hz) * decay
		if steady_hz == 0 and self.speed_hz < STANDING_HZ:
			self.speed_hz = 0.0

		first_edge, last_edge = math.floor(self.turns) + 1, math.floor(self.turns + turned)

		def edge_ticks(edge: int) -> int:
			# The speed is taken as even within one tick.
			return int((start_us + (edge - self.turns) / turned * (end_us - start_us)) / US_PER_TIMER_TICK)

		overflow = (int(end_us / US_PER_TIMER_TICK) // TIMER_PERIOD_TICKS) * TIMER_PERIOD_TICKS
		if overflow > start_us / US_PER_TIMER_TICK and (last_edge < first_edge or edge_ticks(first_edge) >= overflow):
			if not self.stalled and overflow - self.previous_edge_ticks >= EDGE_TIMEOUT_TICKS:
				self.stalled = True
				self.period_ticks = 0
		for edge in range(max(first_edge, last_edge - 1), last_edge + 1):
			ticks = edge_ticks(edge)
			if self.stalled:
				self.stalled = False
			else:
				self.period_ticks = ticks - self.previous_edge_ticks
			self.previous_edge_ticks = ticks
		self.edge_count += max(0, last_edge - first_edge + 1)
		self.turns += turned

	@property
	def raw_speed_hz(self) -> float:
		if self.stalled or self.period_ticks == 0:
			return 0.0
		return CLOCK_HZ / (PRESCALER * self.period_ticks)


class _RequestParser:
	"""FrameParser: a request frame, a BAD_CRC (with its SEQ) or nothing, byte by byte."""

	def __init__(self):
		self.state = "stx"
		self.last_byte_ms = 0
		self.abandoned = 0
		self.bad_crc = 0
		self.command = self.length = self.sequence = 0
		self.payload = bytearray()
		self.received_crc = 0

	def feed(self, byte: int, now_ms: int) -> tuple[str, int, int, bytes] | None:
		if self.state != "stx" and now_ms - self.last_byte_ms > INTER_BYTE_TIMEOUT_MS:
			self._abandon()
		self.last_byte_ms = now_ms
		match self.state:
			case "stx":
				if byte == STX:
					self.state = "id"
			case "id":
				if byte == BOARD_ID:
					self.state = "cmd"
				elif byte != STX:
					self.state = "stx"
			case "cmd":
				self.command, self.state = byte, "len"
			case "len":
				if byte > MAX_REQUEST_PAYLOAD:
					self._abandon()
				else:
					self.length, self.state = byte, "seq"
			case "seq":
				self.sequence = byte
				self.payload = bytearray()
				self.state = "payload" if self.length else "crc"
			case "payload":
				self.payload.append(byte)
				if len(self.payload) >= self.length:
					self.state = "crc"
			case "crc":
				self.received_crc, self.state = byte, "etx"
			case "etx":
				return self._finish(byte)
		return None

	def _finish(self, byte: int) -> tuple[str, int, int, bytes] | None:
		if byte != ETX:
			self._abandon()
			if byte == STX:
				self.state = "id"
			return None
		self.state = "stx"
		body = bytes([BOARD_ID, self.command, self.length, self.sequence]) + self.payload
		if crc8(body) != self.received_crc:
			self.bad_crc = min(self.bad_crc + 1, 0xFFFF)
			return ("bad_crc", self.command, self.sequence, b"")
		return ("frame", self.command, self.sequence, bytes(self.payload))

	def _abandon(self) -> None:
		self.abandoned = min(self.abandoned + 1, 0xFFFF)
		self.state = "stx"


def _reply(code: Reply, sequence: int, payload: bytes = b"") -> bytes:
	body = bytes([BOARD_ID, code, len(payload), sequence]) + payload
	return bytes([STX]) + body + bytes([crc8(body), ETX])


def _nack(sequence: int, reason: NackReason) -> bytes:
	return _reply(Reply.NACK, sequence, bytes([reason]))


class SimPort:
	"""The board's end of the cable, as the SerialWorker sees a pyserial port."""

	def __init__(self, board: "SimBoard", name: str):
		self._board = board
		self._name = name
		self._condition = threading.Condition()
		self._buffer = bytearray()
		self._closed = False
		self._lost = False
		self._cancelled = False

	@property
	def in_waiting(self) -> int:
		with self._condition:
			self._raise_if_unusable()
			return len(self._buffer)

	def read(self, size: int = 1) -> bytes:
		deadline_s = time.monotonic() + READ_TIMEOUT_S
		with self._condition:
			while not (self._buffer or self._lost or self._closed or self._cancelled):
				remaining_s = deadline_s - time.monotonic()
				if remaining_s <= 0:
					break
				self._condition.wait(remaining_s)
			self._raise_if_unusable()
			self._cancelled = False
			data = bytes(self._buffer[:size])
			del self._buffer[:size]
			return data

	def write(self, data: bytes) -> int:
		with self._condition:
			self._raise_if_unusable()
		self._board._receive(self, bytes(data))
		return len(data)

	def cancel_read(self) -> None:
		with self._condition:
			self._cancelled = True
			self._condition.notify_all()

	def close(self) -> None:
		with self._condition:
			if self._closed:
				return
			self._closed = True
			self._condition.notify_all()
		self._board._port_closed(self)

	def _deliver(self, data: bytes) -> None:
		with self._condition:
			self._buffer += data
			self._condition.notify_all()

	def _lose(self) -> None:
		with self._condition:
			self._lost = True
			self._condition.notify_all()

	def _raise_if_unusable(self) -> None:
		if self._lost:
			raise serial.SerialException(f"{self._name}: the device was unplugged (simulated link loss)")
		if self._closed:
			raise serial.SerialException(f"{self._name}: the port is closed")


class SimBoard:
	"""One board: its stored role survives restart(); everything else starts over, as after a
	reset. Opening its port resets it, as the Uno's DTR line does, unless resets_on_open is off.

	Fault switches, set at any time: corrupt_replies and corrupt_requests flip the CRC of that many
	next replies or requests (the board NACKs a corrupted request with BAD_CRC); drop_replies runs
	that many next requests without answering them; jammed holds the screw still, so no edges come;
	lose_link() unplugs the cable until restore_link(); clock_error makes the board's clock run that
	fraction fast (0.005 is 0.5 %) or, below 0, slow.
	"""

	def __init__(
		self,
		role: Role = Role.NONE,
		*,
		motor: MotorModel = MotorModel(),
		clock: Callable[[], float] = time.perf_counter,
		clock_error: float = 0.0,
		resets_on_open: bool = True,
		boot_s: float = 0.0,
	):
		self._lock = threading.RLock()
		self._role = Role(role)
		self._motor = motor
		self._clock = clock
		self._clock_error = clock_error
		self._resets_on_open = resets_on_open
		self._boot_us = round(boot_s * 1e6)
		self._port: SimPort | None = None
		self._link_lost = False
		self.corrupt_replies = 0
		self.corrupt_requests = 0
		self.drop_replies = 0
		self.jammed = False
		self.restart()

	# --- Faults and the outside -------------------------------------------------------------

	def open(self, port_name: str = "simulated board") -> SimPort:
		with self._lock:
			if self._link_lost:
				raise serial.SerialException(f"could not open port {port_name!r}: the device was not found (unplugged)")
			if self._port is not None:
				raise serial.SerialException(f"could not open port {port_name!r}: the port is in use")
			if self._resets_on_open:
				self.restart()
			self._port = SimPort(self, port_name)
			return self._port

	def restart(self, watchdog: bool = False) -> None:
		with self._lock:
			self._base_host_s = self._clock()
			self._base_us = 0
			self._watchdog_reset = watchdog
			self._controller = _Controller()
			self._program = _Program()
			self._runner = _Runner(self._program, self._controller)
			self._screw = _Screw(self._motor)
			self._parser = _RequestParser()
			self._next_tick_us = CONTROL_PERIOD_US
			self._ticked = False
			self._last_request: tuple[int, int] | None = None
			self._last_reply = b""

	def lose_link(self) -> None:
		with self._lock:
			self._link_lost = True
			port, self._port = self._port, None
		if port is not None:
			port._lose()

	def restore_link(self) -> None:
		with self._lock:
			self._link_lost = False

	@property
	def link_lost(self) -> bool:
		return self._link_lost

	@property
	def role(self) -> Role:
		return self._role

	@property
	def clock_error(self) -> float:
		return self._clock_error

	@clock_error.setter
	def clock_error(self, error: float) -> None:
		with self._lock:
			now_us = self._now_us()
			self._base_host_s, self._base_us = self._clock(), now_us
			self._clock_error = error

	def state(self) -> SimState:
		with self._lock:
			now_us = self._now_us()
			self._advance(now_us)
			c, r = self._controller, self._runner
			return SimState(
				time_us=now_us, role=self._role, mode=c.mode, target_speed_hz=c.target_hz, duty=c.output_duty,
				motor_hz=self._screw.speed_hz, edge_count=self._screw.edge_count % U32, job_state=r.state,
				job_elapsed_ms=r.reported_elapsed_ms, comms_lost=c.comms_lost, estopped=c.estopped,
			)

	# --- Time -------------------------------------------------------------------------------

	def _now_us(self) -> int:
		return self._base_us + round((self._clock() - self._base_host_s) * 1e6 * (1 + self._clock_error))

	def _advance(self, now_us: int) -> None:
		while self._next_tick_us <= now_us:
			if self._standing():
				# Nothing would change in the ticks up to now.
				self._next_tick_us += (now_us - self._next_tick_us) // CONTROL_PERIOD_US * CONTROL_PERIOD_US
			self._tick(self._next_tick_us)
			self._next_tick_us += CONTROL_PERIOD_US

	def _standing(self) -> bool:
		c = self._controller
		return (self._screw.speed_hz == 0.0 and self._screw.stalled and c.output_duty == 0 and not c.motor_commanded
				and not self._runner.running and c.ema.initialized and c.filtered_hz == 0.0)

	def _tick(self, now_us: int) -> None:
		self._screw.turn(now_us - CONTROL_PERIOD_US, now_us, self._controller.output_duty, self.jammed)
		self._runner.tick(now_us)
		self._controller.update(self._screw.raw_speed_hz, now_us)
		self._ticked = True

	# --- Serial -----------------------------------------------------------------------------

	def _port_closed(self, port: SimPort) -> None:
		with self._lock:
			if self._port is port:
				self._port = None

	def _receive(self, port: SimPort, data: bytes) -> None:
		with self._lock:
			if port is not self._port:
				return
			now_us = self._now_us()
			self._advance(now_us)
			if now_us < self._boot_us:
				return
			if self.corrupt_requests > 0 and len(data) >= 2:
				self.corrupt_requests -= 1
				data = data[:-2] + bytes([data[-2] ^ 0xFF]) + data[-1:]
			for byte in data:
				event = self._parser.feed(byte, now_us // 1000)
				if event is None:
					continue
				reply = self._handle(event, now_us)
				if self.drop_replies > 0:
					self.drop_replies -= 1
					continue
				if self.corrupt_replies > 0:
					self.corrupt_replies -= 1
					reply = reply[:-2] + bytes([reply[-2] ^ 0xFF]) + reply[-1:]
				port._deliver(reply)

	def _handle(self, event: tuple[str, int, int, bytes], now_us: int) -> bytes:
		kind, command, sequence, payload = event
		if kind == "bad_crc":
			return _nack(sequence, NackReason.BAD_CRC)
		self._controller.last_activity_us = now_us
		if self._last_request == (command, sequence):
			return self._last_reply
		reply = self._execute(command, sequence, payload, now_us)
		self._last_request, self._last_reply = (command, sequence), reply
		return reply

	def _execute(self, code: int, sequence: int, payload: bytes, now_us: int) -> bytes:
		try:
			command = Command(code)
		except ValueError:
			return _nack(sequence, NackReason.UNKNOWN_COMMAND)
		if len(payload) != REQUEST_PAYLOAD_LENGTH[command]:
			return _nack(sequence, NackReason.BAD_LENGTH)
		if self._runner.running and command in REFUSED_WHILE_RUNNING:
			return _nack(sequence, NackReason.WRONG_STATE)

		values = struct.unpack(REQUEST_FORMAT[command], payload)
		c, program, runner = self._controller, self._program, self._runner
		match command:
			case Command.SET_SPEED:
				result = c.set_target(*values)
			case Command.SET_PID:
				result = c.set_tunings(*values)
			case Command.SET_FILTER:
				result = c.set_alpha(*values)
			case Command.SET_FEEDFORWARD:
				result = c.set_feedforward(*values)
			case Command.REQ_STAT:
				return _reply(Reply.STATUS, sequence, self._status(now_us))
			case Command.REQ_SETTINGS:
				return _reply(Reply.SETTINGS, sequence, struct.pack(
					SETTINGS_FORMAT, c.pid.kp, c.pid.ki, c.pid.kd, c.target_hz, c.comms_timeout_ms, c.mode,
					c.duty_setpoint, c.ema.alpha, c.ff_offset_duty, c.ff_gain_duty_per_hz,
				))
			case Command.GET_VERSION:
				return _reply(Reply.VERSION, sequence, struct.pack(VERSION_FORMAT, PROTOCOL_VERSION, *FIRMWARE_VERSION, MAX_SEGMENTS))
			case Command.GET_ID:
				return _reply(Reply.ID, sequence, bytes([self._role]))
			case Command.SET_ID:
				result = self._set_role(*values)
			case Command.SET_MODE:
				result = c.set_mode(*values)
			case Command.SET_PWM:
				result = c.set_duty(*values)
			case Command.SET_COMMS_TIMEOUT:
				c.set_comms_timeout(values[0], now_us)
				result = None
			case Command.STOP:
				runner.abort()
				c.stop()
				result = None
			case Command.ESTOP:
				runner.abort()
				c.estop()
				result = None
			case Command.CLEAR_ESTOP:
				c.estopped = False
				result = None
			case Command.JOB_CLEAR:
				runner.reset()
				program.clear()
				result = None
			case Command.JOB_SEGMENT:
				index, flags, value, duration_ms = values
				result = program.add(index, flags, value, duration_ms)
			case Command.JOB_COMMIT:
				result = program.commit(*values)
				if result is None:
					return _reply(Reply.JOB_INFO, sequence, struct.pack(
						JOB_INFO_FORMAT, program.state, len(program.segments), program.total_ms))
			case Command.JOB_RUN:
				result = runner.start(now_us)
		return _reply(Reply.ACK, sequence) if result is None else _nack(sequence, result)

	def _set_role(self, value: int) -> NackReason | None:
		if value > Role.COLOUR:
			return NackReason.BAD_VALUE
		self._role = Role(value)
		return None

	def _status(self, now_us: int) -> bytes:
		c, screw = self._controller, self._screw
		flags = StatusFlag(0)
		for flag, on in (
			(StatusFlag.ENCODER_STALLED, screw.stalled),
			(StatusFlag.REGULATING, c.regulating),
			(StatusFlag.COMMS_LOST, c.comms_lost),
			(StatusFlag.OPEN_LOOP, c.mode is Mode.OPEN_LOOP),
			(StatusFlag.ESTOP, c.estopped),
			(StatusFlag.WATCHDOG_RESET, self._watchdog_reset),
			(StatusFlag.UNCONFIGURED, not c.configured),
		):
			if on:
				flags |= flag
		longest_us = CONTROL_PERIOD_US if self._ticked else 0
		self._ticked = False
		return struct.pack(
			STATUS_FORMAT, screw.raw_speed_hz, c.filtered_hz, c.output_duty, flags, now_us % U32,
			screw.edge_count % U32, 0, longest_us, self._parser.abandoned, self._parser.bad_crc,
			self._runner.state, self._runner.segment_index, self._runner.reported_elapsed_ms,
		)


class SimBench:
	"""Simulated boards by port name, opened the way Link.open opens a serial port."""

	POLYMER_PORT = "SIM-POLYMER"
	COLOUR_PORT = "SIM-COLOUR"

	def __init__(self, boards: dict[str, SimBoard]):
		self._boards = dict(boards)

	@classmethod
	def pair(cls, **board_options) -> "SimBench":
		"""A polymer and a colour board, each made with the same options."""
		return cls({
			cls.POLYMER_PORT: SimBoard(Role.POLYMER, **board_options),
			cls.COLOUR_PORT: SimBoard(Role.COLOUR, **board_options),
		})

	def __getitem__(self, port: str) -> SimBoard:
		return self._boards[port]

	@property
	def ports(self) -> dict[Role, str]:
		"""The ports by the role their board has stored, for Config.ports."""
		return {board.role: port for port, board in self._boards.items() if board.role is not Role.NONE}

	def opener(self, port: str) -> SimPort:
		board = self._boards.get(port)
		if board is None:
			raise serial.SerialException(f"could not open port {port!r}: no simulated board there")
		return board.open(port)

	def link_factory(self, port: str) -> Link:
		return Link.open(port, opener=self.opener)

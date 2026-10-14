"""Requests that set how the motor is driven (protocol.md section 4)."""

from dataclasses import dataclass
from typing import ClassVar

from ..protocol.constants import Command, Mode
from .base import Request


@dataclass(frozen=True)
class SetSpeed(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_SPEED
	target_speed_hz: float

	def values(self) -> tuple:
		return (self.target_speed_hz,)


@dataclass(frozen=True)
class SetPid(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_PID
	kp: float
	ki: float
	kd: float

	def values(self) -> tuple:
		return (self.kp, self.ki, self.kd)


@dataclass(frozen=True)
class SetFilter(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_FILTER
	alpha: float

	def values(self) -> tuple:
		return (self.alpha,)


@dataclass(frozen=True)
class SetFeedforward(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_FEEDFORWARD
	offset_duty: float
	gain_duty_per_hz: float

	def values(self) -> tuple:
		return (self.offset_duty, self.gain_duty_per_hz)


@dataclass(frozen=True)
class SetMode(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_MODE
	mode: Mode

	def values(self) -> tuple:
		return (int(Mode(self.mode)),)


@dataclass(frozen=True)
class SetPwm(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_PWM
	duty: int

	def values(self) -> tuple:
		return (self.duty,)


@dataclass(frozen=True)
class SetCommsTimeout(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_COMMS_TIMEOUT
	timeout_ms: int

	def values(self) -> tuple:
		return (self.timeout_ms,)


@dataclass(frozen=True)
class Stop(Request[None]):
	COMMAND: ClassVar[Command] = Command.STOP
	URGENT: ClassVar[bool] = True


@dataclass(frozen=True)
class Estop(Request[None]):
	"""The latching software stop. Not an emergency stop in the safety sense (protocol.md)."""

	COMMAND: ClassVar[Command] = Command.ESTOP
	URGENT: ClassVar[bool] = True


@dataclass(frozen=True)
class ClearEstop(Request[None]):
	COMMAND: ClassVar[Command] = Command.CLEAR_ESTOP

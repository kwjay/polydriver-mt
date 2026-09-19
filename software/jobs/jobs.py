import struct
from dataclasses import dataclass

from comms.constants import (
    CMD_SET_SPEED,
    CMD_SET_PID,
    CMD_REQ_STAT,
    CMD_REQ_SETTINGS,
    RESP_ACK,
    RESP_NACK,
    RESP_STATUS,
    RESP_SETTINGS,
)
from comms.link_layer import ResponseFrame
from .base_job import BaseJob


class JobError(Exception):
    """Base class for errors raised while interpreting a job's response."""


class JobNackError(JobError):
    """The device rejected the frame outright (RESP_NACK), e.g. a CRC mismatch
    it detected on its end. Retrying the exact same bytes won't help, so
    JobManager treats this as a final failure rather than retrying it."""


class UnexpectedResponseError(JobError):
    """The device replied with a command byte this job doesn't expect."""

    def __init__(self, expected: tuple[int, ...], got: int):
        self.expected = expected
        self.got = got
        super().__init__(f"expected response command in {expected!r}, got 0x{got:02X}")


@dataclass(frozen=True)
class StatusReport:
    frequency: float
    pwm: int
    is_stalled: bool


@dataclass(frozen=True)
class SettingsReport:
    kp: float
    ki: float
    kd: float
    speed: float


class SetSpeedJob(BaseJob):
    def __init__(self, target_id: int, speed: float, timeout: float = 0.5, retries: int = 3):
        super().__init__(target_id, timeout, retries)
        self.speed = speed

    @property
    def command_id(self) -> int:
        return CMD_SET_SPEED

    def build_payload(self) -> bytes:
        return struct.pack("<f", self.speed)

    def handle_response(self, response_frame: ResponseFrame) -> bool:
        if response_frame.command == RESP_ACK:
            return True
        if response_frame.command == RESP_NACK:
            raise JobNackError(f"target {self.target_id} rejected SET_SPEED")
        raise UnexpectedResponseError((RESP_ACK, RESP_NACK), response_frame.command)

    def __repr__(self) -> str:
        return f"SetSpeedJob(target_id={self.target_id}, speed={self.speed})"


class SetPidJob(BaseJob):
    def __init__(
        self,
        target_id: int,
        kp: float,
        ki: float,
        kd: float,
        timeout: float = 0.5,
        retries: int = 3,
    ):
        super().__init__(target_id, timeout, retries)
        self.kp = kp
        self.ki = ki
        self.kd = kd

    @property
    def command_id(self) -> int:
        return CMD_SET_PID

    def build_payload(self) -> bytes:
        return struct.pack("<fff", self.kp, self.ki, self.kd)

    def handle_response(self, response_frame: ResponseFrame) -> bool:
        if response_frame.command == RESP_ACK:
            return True
        if response_frame.command == RESP_NACK:
            raise JobNackError(f"target {self.target_id} rejected SET_PID")
        raise UnexpectedResponseError((RESP_ACK, RESP_NACK), response_frame.command)

    def __repr__(self) -> str:
        return f"SetPidJob(target_id={self.target_id}, kp={self.kp}, ki={self.ki}, kd={self.kd})"


class RequestStatusJob(BaseJob):
    @property
    def command_id(self) -> int:
        return CMD_REQ_STAT

    def build_payload(self) -> bytes:
        return b""

    def handle_response(self, response_frame: ResponseFrame) -> StatusReport:
        if response_frame.command != RESP_STATUS:
            raise UnexpectedResponseError((RESP_STATUS,), response_frame.command)
        frequency, pwm, is_stalled = struct.unpack("<fBB", response_frame.payload)
        return StatusReport(frequency=frequency, pwm=pwm, is_stalled=bool(is_stalled))


class RequestSettingsJob(BaseJob):
    @property
    def command_id(self) -> int:
        return CMD_REQ_SETTINGS

    def build_payload(self) -> bytes:
        return b""

    def handle_response(self, response_frame: ResponseFrame) -> SettingsReport:
        if response_frame.command != RESP_SETTINGS:
            raise UnexpectedResponseError((RESP_SETTINGS,), response_frame.command)
        kp, ki, kd, speed = struct.unpack("<ffff", response_frame.payload)
        return SettingsReport(kp=kp, ki=ki, kd=kd, speed=speed)

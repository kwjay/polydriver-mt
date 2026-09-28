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
    STATUS_PAYLOAD_FORMAT,
    STATUS_PAYLOAD_LEN,
    StatusFlag,
)
from comms.link_layer import ResponseFrame
from .base_job import BaseJob


class JobError(Exception):
    ...


class JobNackError(JobError):
    ...


class PayloadFormatError(JobError):
    ...


class UnexpectedResponseError(JobError):

    def __init__(self, expected: tuple[int, ...], got: int):
        self.expected = expected
        self.got = got
        super().__init__(f"expected response command in {expected!r}, got 0x{got:02X}")


@dataclass(frozen=True)
class StatusReport:
    frequency: float
    pwm: int
    is_stalled: bool
    filtered_frequency: float = 0.0
    stall_fault: bool = False
    regulating: bool = False
    comms_lost: bool = False
    device_us: int = 0
    edge_count: int = 0
    missed_cycles: int = 0
    max_loop_interval_us: int = 0
    frame_timeouts: int = 0
    crc_errors: int = 0


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
        if len(response_frame.payload) != STATUS_PAYLOAD_LEN:
            raise PayloadFormatError(
                f"STATUS payload is {len(response_frame.payload)} bytes, expected {STATUS_PAYLOAD_LEN}"
                " - firmware and host protocol versions differ"
            )
        (raw, filtered, pwm, flags, device_us, edges,
         missed, max_interval, frame_timeouts, crc_errors) = struct.unpack(
            STATUS_PAYLOAD_FORMAT, response_frame.payload
        )
        flags = StatusFlag(flags)
        return StatusReport(
            frequency=raw,
            pwm=pwm,
            is_stalled=StatusFlag.ENCODER_STALLED in flags,
            filtered_frequency=filtered,
            stall_fault=StatusFlag.STALL_FAULT in flags,
            regulating=StatusFlag.REGULATING in flags,
            comms_lost=StatusFlag.COMMS_LOST in flags,
            device_us=device_us,
            edge_count=edges,
            missed_cycles=missed,
            max_loop_interval_us=max_interval,
            frame_timeouts=frame_timeouts,
            crc_errors=crc_errors,
        )


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

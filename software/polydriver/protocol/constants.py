"""Wire constants of protocol v3 (docs/protocol.md).

Must match firmware/polydriver/lib/core/ProtocolDefs.h; tests/test_firmware_sync.py checks it.
"""

import struct
from enum import IntEnum, IntFlag

PROTOCOL_VERSION = 3
BOARD_ID = 0x01
STX = 0x02
ETX = 0x03

# STX | ID | CMD | LEN | SEQ | payload | CRC | ETX
FRAME_OVERHEAD = 7
MAX_REQUEST_PAYLOAD = 16
MAX_REPLY_PAYLOAD = 32
MAX_FRAME_LENGTH = FRAME_OVERHEAD + MAX_REPLY_PAYLOAD

INTER_BYTE_TIMEOUT_MS = 10
BAUD_RATE = 115200

NO_SEGMENT = 0xFF
MAX_DUTY = 255


class Command(IntEnum):
	SET_SPEED = 0x10
	SET_PID = 0x20
	SET_FILTER = 0x22
	SET_FEEDFORWARD = 0x24
	REQ_STAT = 0x30
	REQ_SETTINGS = 0x32
	GET_VERSION = 0x40
	GET_ID = 0x42
	SET_ID = 0x44
	SET_MODE = 0x50
	SET_PWM = 0x52
	SET_COMMS_TIMEOUT = 0x54
	STOP = 0x60
	ESTOP = 0x62
	CLEAR_ESTOP = 0x64
	JOB_CLEAR = 0x70
	JOB_SEGMENT = 0x72
	JOB_COMMIT = 0x74
	JOB_RUN = 0x76


class Reply(IntEnum):
	ACK = 0x11
	NACK = 0x12
	STATUS = 0x31
	SETTINGS = 0x33
	VERSION = 0x41
	ID = 0x43
	JOB_INFO = 0x75


class NackReason(IntEnum):
	BAD_CRC = 0x01
	BAD_LENGTH = 0x02
	UNKNOWN_COMMAND = 0x03
	WRONG_MODE = 0x04
	WRONG_STATE = 0x05
	BAD_INDEX = 0x06
	JOB_FULL = 0x07
	BAD_JOB = 0x08
	BAD_VALUE = 0x09
	NOT_CONFIGURED = 0x0A


class Role(IntEnum):
	NONE = 0
	POLYMER = 1
	COLOUR = 2


class Mode(IntEnum):
	CLOSED_LOOP = 0
	OPEN_LOOP = 1


class JobState(IntEnum):
	EMPTY = 0
	LOADING = 1
	READY = 2
	RUNNING = 3
	DONE = 4
	ABORTED = 5


class StatusFlag(IntFlag):
	"""STATUS byte 9. 0x02 is reserved (STALL_FAULT in v2)."""

	ENCODER_STALLED = 0x01
	REGULATING = 0x04
	COMMS_LOST = 0x08
	OPEN_LOOP = 0x10
	ESTOP = 0x20
	WATCHDOG_RESET = 0x40
	UNCONFIGURED = 0x80


class SegmentFlag(IntFlag):
	"""JOB_SEGMENT byte 1."""

	RAMP = 0x01
	DUTY = 0x02


# Payload layouts, little-endian (struct format strings).
REQUEST_FORMAT: dict[Command, str] = {
	Command.SET_SPEED: "<f",
	Command.SET_PID: "<fff",
	Command.SET_FILTER: "<f",
	Command.SET_FEEDFORWARD: "<ff",
	Command.REQ_STAT: "<",
	Command.REQ_SETTINGS: "<",
	Command.GET_VERSION: "<",
	Command.GET_ID: "<",
	Command.SET_ID: "<B",
	Command.SET_MODE: "<B",
	Command.SET_PWM: "<B",
	Command.SET_COMMS_TIMEOUT: "<H",
	Command.STOP: "<",
	Command.ESTOP: "<",
	Command.CLEAR_ESTOP: "<",
	Command.JOB_CLEAR: "<",
	Command.JOB_SEGMENT: "<BBfI",
	Command.JOB_COMMIT: "<BI",
	Command.JOB_RUN: "<",
}

REQUEST_PAYLOAD_LENGTH: dict[Command, int] = {
	command: struct.calcsize(fmt) for command, fmt in REQUEST_FORMAT.items()
}

STATUS_FORMAT = "<ffBBIIHHHHBBI"
STATUS_FIELDS = (
	"raw_speed_hz", "filtered_speed_hz", "duty", "flags", "device_time_us", "edge_count",
	"control_resyncs", "longest_loop_interval_us", "dropped_frames", "bad_crc_frames",
	"job_state", "segment_index", "job_elapsed_ms",
)
SETTINGS_FORMAT = "<ffffHBBfff"
SETTINGS_FIELDS = (
	"kp", "ki", "kd", "target_speed_hz", "comms_timeout_ms", "mode", "duty_setpoint", "alpha",
	"feedforward_offset_duty", "feedforward_gain_duty_per_hz",
)
VERSION_FORMAT = "<BBBBB"
VERSION_FIELDS = ("protocol_version", "firmware_major", "firmware_minor", "firmware_patch", "max_segments")
JOB_INFO_FORMAT = "<BBI"
JOB_INFO_FIELDS = ("job_state", "segment_count", "total_ms")
NACK_FORMAT = "<B"
ID_FORMAT = "<B"

REPLY_FORMAT: dict[Reply, str] = {
	Reply.ACK: "<",
	Reply.NACK: NACK_FORMAT,
	Reply.STATUS: STATUS_FORMAT,
	Reply.SETTINGS: SETTINGS_FORMAT,
	Reply.VERSION: VERSION_FORMAT,
	Reply.ID: ID_FORMAT,
	Reply.JOB_INFO: JOB_INFO_FORMAT,
}

REPLY_PAYLOAD_LENGTH: dict[Reply, int] = {
	reply: struct.calcsize(fmt) for reply, fmt in REPLY_FORMAT.items()
}

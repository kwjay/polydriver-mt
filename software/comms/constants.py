from enum import Enum, IntEnum, IntFlag, auto

STX = 0x02
ETX = 0x03

CMD_SET_SPEED = 0x10
CMD_SET_PID = 0x20
CMD_REQ_STAT = 0x30 
CMD_REQ_SETTINGS = 0x32

RESP_ACK = 0x11
RESP_NACK = 0x12 
RESP_STATUS = 0x31
RESP_SETTINGS = 0x33

# Inbound to the board (requests). Must match MAX_PAYLOAD_LEN in protocol_handler.h.
MAX_PAYLOAD_LEN = 16
# Outbound from the board (responses); RESP_STATUS is the largest.
# Must match MAX_RESPONSE_PAYLOAD_LEN in protocol_handler.h.
MAX_RESPONSE_PAYLOAD_LEN = 26

# RESP_STATUS payload, little-endian. Mirrors the layout comment in protocol_handler.h:
# raw Hz, filtered Hz, pwm, flags, device micros, edge count, loop resyncs,
# longest loop interval [us], abandoned frames, bad-CRC frames received.
STATUS_PAYLOAD_FORMAT = "<ffBBIIHHHH"
STATUS_PAYLOAD_LEN = 26


# Mirrors StatusFlag in protocol_handler.h.
class StatusFlag(IntFlag):
	ENCODER_STALLED = 0x01
	STALL_FAULT = 0x02
	REGULATING = 0x04
	COMMS_LOST = 0x08

class RxState(Enum):
	WAIT_STX = auto()
	READ_ID = auto()
	READ_CMD = auto()
	READ_LEN = auto()
	READ_PAYLOAD = auto()
	READ_CRC = auto()
	WAIT_ETX = auto()


# Mirrors NackReason in firmware/polydriver/lib/algorithms/protocol_handler.h.
class NackReason(IntEnum):
	BAD_CRC = 0x01
	BAD_LENGTH = 0x02
	UNKNOWN_COMMAND = 0x03

from enum import Enum, IntEnum, auto

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

MAX_PAYLOAD_LEN = 16

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

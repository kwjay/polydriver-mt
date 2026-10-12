#ifndef PROTOCOL_DEFS_H
#define PROTOCOL_DEFS_H

#include <stdint.h>

// Wire constants of protocol v3, docs/protocol.md.
// Must match software/polydriver/protocol/constants.py.
namespace ProtocolDefs {

constexpr uint8_t PROTOCOL_VERSION = 3;
constexpr uint8_t BOARD_ID = 0x01;
constexpr uint8_t STX = 0x02;
constexpr uint8_t ETX = 0x03;

// STX | ID | CMD | LEN | SEQ | payload | CRC | ETX
constexpr uint8_t FRAME_OVERHEAD = 7;
constexpr uint8_t MAX_REQUEST_PAYLOAD = 16;
constexpr uint8_t MAX_REPLY_PAYLOAD = 32;
constexpr uint8_t MAX_FRAME_LENGTH = FRAME_OVERHEAD + MAX_REPLY_PAYLOAD;

// About 115 byte times at 115200 baud.
constexpr uint16_t INTER_BYTE_TIMEOUT_MS = 10;

enum class Command : uint8_t {
  SET_SPEED = 0x10,
  SET_PID = 0x20,
  SET_FILTER = 0x22,
  SET_FEEDFORWARD = 0x24,
  REQ_STAT = 0x30,
  REQ_SETTINGS = 0x32,
  GET_VERSION = 0x40,
  GET_ID = 0x42,
  SET_ID = 0x44,
  SET_MODE = 0x50,
  SET_PWM = 0x52,
  SET_COMMS_TIMEOUT = 0x54,
  STOP = 0x60,
  ESTOP = 0x62,
  CLEAR_ESTOP = 0x64,
  JOB_CLEAR = 0x70,
  JOB_SEGMENT = 0x72,
  JOB_COMMIT = 0x74,
  JOB_RUN = 0x76
};

enum class Reply : uint8_t {
  ACK = 0x11,
  NACK = 0x12,
  STATUS = 0x31,
  SETTINGS = 0x33,
  VERSION = 0x41,
  ID = 0x43,
  JOB_INFO = 0x75
};

enum class NackReason : uint8_t {
  BAD_CRC = 0x01,
  BAD_LENGTH = 0x02,
  UNKNOWN_COMMAND = 0x03,
  WRONG_MODE = 0x04,
  WRONG_STATE = 0x05,
  BAD_INDEX = 0x06,
  JOB_FULL = 0x07,
  BAD_JOB = 0x08,
  BAD_VALUE = 0x09,
  NOT_CONFIGURED = 0x0A
};

enum class Role : uint8_t {
  NONE = 0,
  POLYMER = 1,
  COLOUR = 2
};

// STATUS byte 9. 0x02 is reserved (STALL_FAULT in v2).
namespace StatusFlag {
constexpr uint8_t ENCODER_STALLED = 0x01;
constexpr uint8_t REGULATING = 0x04;
constexpr uint8_t COMMS_LOST = 0x08;
constexpr uint8_t OPEN_LOOP = 0x10;
constexpr uint8_t ESTOP = 0x20;
constexpr uint8_t WATCHDOG_RESET = 0x40;
constexpr uint8_t UNCONFIGURED = 0x80;
}

// JOB_SEGMENT byte 1.
namespace SegmentFlag {
constexpr uint8_t RAMP = 0x01;
constexpr uint8_t DUTY = 0x02;
}

namespace PayloadLength {
constexpr uint8_t SET_SPEED = 4;
constexpr uint8_t SET_PID = 12;
constexpr uint8_t SET_FILTER = 4;
constexpr uint8_t SET_FEEDFORWARD = 8;
constexpr uint8_t SET_ID = 1;
constexpr uint8_t SET_MODE = 1;
constexpr uint8_t SET_PWM = 1;
constexpr uint8_t SET_COMMS_TIMEOUT = 2;
constexpr uint8_t JOB_SEGMENT = 10;
constexpr uint8_t JOB_COMMIT = 5;

constexpr uint8_t NACK = 1;
constexpr uint8_t STATUS = 32;
constexpr uint8_t SETTINGS = 32;
constexpr uint8_t VERSION = 5;
constexpr uint8_t ID = 1;
constexpr uint8_t JOB_INFO = 6;
}

// Expected payload length of a request; false for an opcode this protocol does not define.
inline bool requestPayloadLength(uint8_t opcode, uint8_t& length) {
  switch (static_cast<Command>(opcode)) {
    case Command::SET_SPEED: length = PayloadLength::SET_SPEED; return true;
    case Command::SET_PID: length = PayloadLength::SET_PID; return true;
    case Command::SET_FILTER: length = PayloadLength::SET_FILTER; return true;
    case Command::SET_FEEDFORWARD: length = PayloadLength::SET_FEEDFORWARD; return true;
    case Command::SET_ID: length = PayloadLength::SET_ID; return true;
    case Command::SET_MODE: length = PayloadLength::SET_MODE; return true;
    case Command::SET_PWM: length = PayloadLength::SET_PWM; return true;
    case Command::SET_COMMS_TIMEOUT: length = PayloadLength::SET_COMMS_TIMEOUT; return true;
    case Command::JOB_SEGMENT: length = PayloadLength::JOB_SEGMENT; return true;
    case Command::JOB_COMMIT: length = PayloadLength::JOB_COMMIT; return true;
    case Command::REQ_STAT:
    case Command::REQ_SETTINGS:
    case Command::GET_VERSION:
    case Command::GET_ID:
    case Command::STOP:
    case Command::ESTOP:
    case Command::CLEAR_ESTOP:
    case Command::JOB_CLEAR:
    case Command::JOB_RUN:
      length = 0;
      return true;
  }
  return false;
}

}

#endif

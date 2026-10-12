#include "FrameParser.h"
#include "Crc8.h"

using namespace ProtocolDefs;

ParseEvent FrameParser::feed(uint8_t byte, uint32_t nowMs) {
  if (state != State::WAIT_STX && nowMs - lastByteMs > INTER_BYTE_TIMEOUT_MS) abandon();
  lastByteMs = nowMs;

  switch (state) {
    case State::WAIT_STX:
      if (byte == STX) startFrame();
      return ParseEvent::NONE;

    case State::READ_ID:
      if (byte == BOARD_ID) {
        runningCrc = Crc8::update(Crc8::INITIAL, byte);
        state = State::READ_CMD;
      } else if (byte != STX) {
        state = State::WAIT_STX;
      }
      return ParseEvent::NONE;

    case State::READ_CMD:
      frame.command = byte;
      runningCrc = Crc8::update(runningCrc, byte);
      state = State::READ_LEN;
      return ParseEvent::NONE;

    case State::READ_LEN:
      if (byte > MAX_REQUEST_PAYLOAD) {
        abandon();
        return ParseEvent::NONE;
      }
      frame.length = byte;
      runningCrc = Crc8::update(runningCrc, byte);
      state = State::READ_SEQ;
      return ParseEvent::NONE;

    case State::READ_SEQ:
      frame.sequence = byte;
      runningCrc = Crc8::update(runningCrc, byte);
      payloadIndex = 0;
      state = (frame.length > 0) ? State::READ_PAYLOAD : State::READ_CRC;
      return ParseEvent::NONE;

    case State::READ_PAYLOAD:
      frame.payload[payloadIndex++] = byte;
      runningCrc = Crc8::update(runningCrc, byte);
      if (payloadIndex >= frame.length) state = State::READ_CRC;
      return ParseEvent::NONE;

    case State::READ_CRC:
      receivedCrc = byte;
      state = State::READ_ETX;
      return ParseEvent::NONE;

    case State::READ_ETX:
      return finish(byte);
  }
  return ParseEvent::NONE;
}

void FrameParser::startFrame() {
  state = State::READ_ID;
}

void FrameParser::abandon() {
  countUp(abandonedFrames);
  state = State::WAIT_STX;
}

ParseEvent FrameParser::finish(uint8_t byte) {
  if (byte != ETX) {
    abandon();
    if (byte == STX) startFrame();
    return ParseEvent::NONE;
  }

  state = State::WAIT_STX;
  if (receivedCrc != runningCrc) {
    countUp(badCrcFrames);
    return ParseEvent::BAD_CRC;
  }
  return ParseEvent::FRAME;
}

void FrameParser::countUp(uint16_t& counter) {
  if (counter < 0xFFFF) counter++;
}

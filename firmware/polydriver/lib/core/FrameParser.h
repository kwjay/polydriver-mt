#ifndef FRAME_PARSER_H
#define FRAME_PARSER_H

#include <stdint.h>
#include "ProtocolDefs.h"

struct Frame {
  uint8_t command;
  uint8_t sequence;
  uint8_t length;
  uint8_t payload[ProtocolDefs::MAX_REQUEST_PAYLOAD];
};

enum class ParseEvent : uint8_t {
  NONE,
  FRAME,
  BAD_CRC
};

class FrameParser {
public:
  ParseEvent feed(uint8_t byte, uint32_t nowMs);

  const Frame& getFrame() const { return frame; }
  uint16_t getAbandonedFrames() const { return abandonedFrames; }
  uint16_t getBadCrcFrames() const { return badCrcFrames; }

private:
  enum class State : uint8_t {
    WAIT_STX,
    READ_ID,
    READ_CMD,
    READ_LEN,
    READ_SEQ,
    READ_PAYLOAD,
    READ_CRC,
    READ_ETX
  };

  void startFrame();
  void abandon();
  ParseEvent finish(uint8_t byte);
  static void countUp(uint16_t& counter);

  State state{State::WAIT_STX};
  Frame frame{};
  uint8_t payloadIndex{0};
  uint8_t runningCrc{0};
  uint8_t receivedCrc{0};
  uint32_t lastByteMs{0};
  uint16_t abandonedFrames{0};
  uint16_t badCrcFrames{0};
};

#endif

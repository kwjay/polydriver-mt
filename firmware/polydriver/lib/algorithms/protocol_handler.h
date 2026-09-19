#ifndef PROTOCOL_HANDLER_H
#define PROTOCOL_HANDLER_H

#include <stdint.h>

constexpr uint8_t MY_ID = 0x01;
constexpr uint8_t STX   = 0x02;
constexpr uint8_t ETX   = 0x03;

// Must match MAX_PAYLOAD_LEN in software/comms/constants.py.
constexpr uint8_t MAX_PAYLOAD_LEN = 16;
constexpr uint8_t MAX_RESPONSE_PAYLOAD_LEN = 16;

// STX | ID | CMD | LEN | ...payload... | CRC | ETX
constexpr uint8_t FRAME_OVERHEAD = 6;

constexpr uint8_t CMD_SET_SPEED     = 0x10;
constexpr uint8_t CMD_SET_PID       = 0x20;
constexpr uint8_t CMD_REQ_STAT      = 0x30;
constexpr uint8_t CMD_REQ_SETTINGS  = 0x32;

constexpr uint8_t RESP_ACK       = 0x11;
constexpr uint8_t RESP_NACK      = 0x12;
constexpr uint8_t RESP_STATUS    = 0x31;
constexpr uint8_t RESP_SETTINGS  = 0x33;

// RESP_NACK payload byte. Mirrored in software/comms/constants.py.
enum class NackReason : uint8_t {
  BAD_CRC         = 0x01,  // checksum over ID|CMD|LEN|payload did not match
  BAD_LENGTH      = 0x02,  // opcode understood, payload the wrong size for it
  UNKNOWN_COMMAND = 0x03   // opcode not implemented by this firmware
};

enum class CommandEvent {
  NONE,
  SPEED_UPDATED,
  PID_UPDATED,
  TELEMETRY_REQUESTED,
  SYNC_REQUESTED
};

struct PidSettings {
  float kp{0.0f};
  float ki{0.0f};
  float kd{0.0f};
};

enum class RxState{
  WAIT_STX,
  READ_ID,
  READ_CMD,
  READ_LEN,
  READ_PAYLOAD,
  READ_CRC,
  WAIT_ETX
};

typedef void (*TxCallback)(const uint8_t* data, uint8_t len);

class ProtocolHandler {
private:
  RxState currentState = RxState::WAIT_STX;
  uint8_t rxId{0};
  uint8_t rxCommand{0};
  uint8_t rxLength{0};
  uint8_t rxPayload[MAX_PAYLOAD_LEN]{0};
  uint8_t rxIndex{0};
  uint8_t rxCrc{0};

  float parsedSpeed{0.0f};
  PidSettings parsedPid;

  TxCallback txFunc{nullptr};

  uint8_t calculateCRC8(const uint8_t* data, uint8_t len);
  void sendResponse(uint8_t cmd, const uint8_t* payload, uint8_t len);
  void sendNack(NackReason reason);
  CommandEvent processFrame();

public:
  ProtocolHandler() = default;
  void setTxCallback(TxCallback callback) { txFunc = callback; }

  CommandEvent processByte(uint8_t b);
  void sendTelemetry(float frequency, uint8_t pwm, uint8_t isStalled);
  const float& getSpeed() const { return parsedSpeed; }
  const PidSettings& getPidSettings() const { return  parsedPid; }
  void sendSettings(float kp, float ki, float kd, float speed);
};

#endif

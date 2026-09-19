#include "protocol_handler.h"
#ifdef ARDUINO
  #include <Arduino.h>
#else
  #include <cstring>
  #include <stdint.h>
#endif

uint8_t ProtocolHandler::calculateCRC8(const uint8_t* data, uint8_t len) {
  uint8_t crc = 0x00;
  for (uint8_t i = 0; i < len; i++) {
    uint8_t extract = data[i];
    for (uint8_t tempI = 8; tempI; tempI--) {
      uint8_t sum = (crc ^ extract) & 0x01;
      crc >>= 1;
      if (sum) crc ^= 0x8C;
      extract >>= 1;
    }
  }
  return crc;
}

void ProtocolHandler::sendResponse(uint8_t cmd, const uint8_t* payload, uint8_t len) {
  if (len > MAX_RESPONSE_PAYLOAD_LEN) return;

  uint8_t crcData[3 + MAX_RESPONSE_PAYLOAD_LEN];
  crcData[0] = MY_ID;
  crcData[1] = cmd;
  crcData[2] = len;
  for(uint8_t i = 0; i < len; i++) crcData[3+i] = payload[i];

  uint8_t crc = calculateCRC8(crcData, 3 + len);
  uint8_t txBuffer[FRAME_OVERHEAD + MAX_RESPONSE_PAYLOAD_LEN];
  uint8_t idx = 0;

  txBuffer[idx++] = STX;
  for(uint8_t i = 0; i < 3 + len; i++) txBuffer[idx++] = crcData[i];
  txBuffer[idx++] = crc;
  txBuffer[idx++] = ETX;

  if (txFunc != nullptr) {
    txFunc(txBuffer, idx);
  }
}

void ProtocolHandler::sendNack(NackReason reason) {
  uint8_t err = static_cast<uint8_t>(reason);
  sendResponse(RESP_NACK, &err, 1);
}

CommandEvent ProtocolHandler::processFrame() {
  uint8_t crcBuffer[3 + MAX_PAYLOAD_LEN];
  crcBuffer[0] = rxId;
  crcBuffer[1] = rxCommand;
  crcBuffer[2] = rxLength;
  memcpy(&crcBuffer[3], rxPayload, rxLength);

  if (calculateCRC8(crcBuffer, 3 + rxLength) != rxCrc) {
    sendNack(NackReason::BAD_CRC);
    return CommandEvent::NONE;
  }

  union FloatBytes { float fVal; uint8_t bVal[4]; } converter;
  CommandEvent resultEvent = CommandEvent::NONE;

  switch (rxCommand) {
    case CMD_SET_SPEED:
      if (rxLength == 4) {
        memcpy(converter.bVal, rxPayload, 4);
        parsedSpeed = converter.fVal;
        sendResponse(RESP_ACK, nullptr, 0);
        resultEvent = CommandEvent::SPEED_UPDATED;
      } else {
        sendNack(NackReason::BAD_LENGTH);
      }
      break;

    case CMD_SET_PID:
      if (rxLength == 12) {
        FloatBytes kp, ki, kd;
        memcpy(kp.bVal, &rxPayload[0], 4);
        memcpy(ki.bVal, &rxPayload[4], 4);
        memcpy(kd.bVal, &rxPayload[8], 4);

        parsedPid.kp = kp.fVal;
        parsedPid.ki = ki.fVal;
        parsedPid.kd = kd.fVal;

        sendResponse(RESP_ACK, nullptr, 0);
        resultEvent = CommandEvent::PID_UPDATED;
      } else {
        sendNack(NackReason::BAD_LENGTH);
      }
      break;

    case CMD_REQ_STAT:
      if (rxLength == 0) {
        resultEvent = CommandEvent::TELEMETRY_REQUESTED;
      } else {
        sendNack(NackReason::BAD_LENGTH);
      }
      break;

    case CMD_REQ_SETTINGS:
      if (rxLength == 0) {
        resultEvent = CommandEvent::SYNC_REQUESTED;
      } else {
        sendNack(NackReason::BAD_LENGTH);
      }
      break;

    default:
      sendNack(NackReason::UNKNOWN_COMMAND);
      break;
  }

  return resultEvent;
}

CommandEvent ProtocolHandler::processByte(uint8_t b) {
  if (timeFunc != nullptr) {
    uint32_t now = timeFunc();
    // Unsigned subtraction, so a millis() rollover is not a false timeout.
    if (currentState != RxState::WAIT_STX && (now - lastByteMs) > frameTimeoutMs) {
      currentState = RxState::WAIT_STX;
      frameTimeouts++;
    }
    lastByteMs = now;
  }

  CommandEvent eventOccurred = CommandEvent::NONE;
    switch (currentState) {
      case RxState::WAIT_STX:
        if (b == STX) currentState = RxState::READ_ID;
        break;
      case RxState::READ_ID:
        rxId = b;
        currentState = (b == MY_ID) ? RxState::READ_CMD : RxState::WAIT_STX;
        break;
      case RxState::READ_CMD:
        rxCommand = b;
        currentState = RxState::READ_LEN;
        break;
      case RxState::READ_LEN:
        rxLength = b;
        rxIndex = 0;
        if (rxLength > MAX_PAYLOAD_LEN) {
          currentState = RxState::WAIT_STX;
        } else {
          currentState = (rxLength > 0) ? RxState::READ_PAYLOAD : RxState::READ_CRC;
        }
        break;
      case RxState::READ_PAYLOAD:
        if (rxIndex < MAX_PAYLOAD_LEN) rxPayload[rxIndex++] = b;
        if (rxIndex >= rxLength) currentState = RxState::READ_CRC;
        break;
      case RxState::READ_CRC:
        rxCrc = b;
        currentState = RxState::WAIT_ETX;
        break;
      case RxState::WAIT_ETX:
        if (b == ETX) {
            eventOccurred = processFrame();
        }
        currentState = RxState::WAIT_STX;
        break;
    }
  return eventOccurred;
}

void ProtocolHandler::sendTelemetry(float frequency, uint8_t pwm, uint8_t isStalled) {
  uint8_t payload[6];
  union { float f; uint8_t b[4]; } freqUnion;
  freqUnion.f = frequency;
  memcpy(&payload[0], freqUnion.b, 4);
  payload[4] = pwm;
  payload[5] = isStalled;
  sendResponse(RESP_STATUS, payload, 6);
}

void ProtocolHandler::sendSettings(float kp, float ki, float kd, float speed) {
  uint8_t payload[16];
  union { float f; uint8_t b[4]; } conv;
  conv.f = kp;
  memcpy(&payload[0], conv.b, 4);
  conv.f = ki;
  memcpy(&payload[4], conv.b, 4);
  conv.f = kd;
  memcpy(&payload[8], conv.b, 4);
  conv.f = speed;
  memcpy(&payload[12], conv.b, 4);

  sendResponse(RESP_SETTINGS, payload, 16);
}

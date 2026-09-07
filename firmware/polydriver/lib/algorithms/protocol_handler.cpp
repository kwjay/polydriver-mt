#include "protocol_handler.h"
#include <cstring>

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
  uint8_t crcData[20];
  crcData[0] = MY_ID;
  crcData[1] = cmd;
  crcData[2] = len;
  for(uint8_t i = 0; i < len; i++) crcData[3+i] = payload[i];

  uint8_t crc = calculateCRC8(crcData, 3 + len);
  uint8_t txBuffer[24]; 
  uint8_t idx = 0;
  
  txBuffer[idx++] = STX;
  for(uint8_t i = 0; i < 3 + len; i++) txBuffer[idx++] = crcData[i];
  txBuffer[idx++] = crc;
  txBuffer[idx++] = ETX;

  if (txFunc != nullptr) {
    txFunc(txBuffer, idx);
  }
}

CommandEvent ProtocolHandler::processFrame() {
  uint8_t crcBuffer[20];
  crcBuffer[0] = MY_ID;
  crcBuffer[1] = rxCommand;
  crcBuffer[2] = rxLength;
  memcpy(&crcBuffer[3], rxPayload, rxLength);

  if (calculateCRC8(crcBuffer, 3 + rxLength) != rxCrc) {
    uint8_t err = 0x01;
    sendResponse(RESP_NACK, &err, 1);
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
      }
      break;

    case CMD_REQ_STAT:
      resultEvent = CommandEvent::TELEMETRY_REQUESTED;
      break;
    case CMD_REQ_SETTINGS:
      resultEvent = CommandEvent::SYNC_REQUESTED;
      break;
  }
  
  return resultEvent;
}

CommandEvent ProtocolHandler::processByte(uint8_t b) {
  CommandEvent eventOccurred = CommandEvent::NONE;
      switch (currentState) {
        case RxState::WAIT_STX:
            if (b == STX) currentState = RxState::READ_ID;
            break;
        case RxState::READ_ID:
            currentState = (b == MY_ID) ? RxState::READ_CMD : RxState::WAIT_STX;
            break;
        case RxState::READ_CMD:
            rxCommand = b;
            currentState = RxState::READ_LEN;
            break;
        case RxState::READ_LEN:
            rxLength = b;
            rxIndex = 0;
            currentState = (rxLength > 0) ? RxState::READ_PAYLOAD : RxState::READ_CRC;
            break;
        case RxState::READ_PAYLOAD:
            if (rxIndex < sizeof(rxPayload)) rxPayload[rxIndex++] = b;
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

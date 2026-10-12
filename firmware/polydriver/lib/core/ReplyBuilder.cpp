#include "ReplyBuilder.h"
#include "Crc8.h"
#include "LittleEndian.h"

using namespace ProtocolDefs;
using LittleEndian::putFloat;
using LittleEndian::putU16;
using LittleEndian::putU32;

namespace {

uint16_t saturateU16(uint32_t value) {
  return value > 0xFFFFUL ? 0xFFFF : static_cast<uint16_t>(value);
}

}

namespace ReplyBuilder {

bool build(uint8_t replyCode, uint8_t sequence, const uint8_t* payload, uint8_t length,
           ReplyFrame& out) {
  if (length > MAX_REPLY_PAYLOAD) {
    out.length = 0;
    return false;
  }

  uint8_t n = 0;
  out.bytes[n++] = STX;
  out.bytes[n++] = BOARD_ID;
  out.bytes[n++] = replyCode;
  out.bytes[n++] = length;
  out.bytes[n++] = sequence;
  for (uint8_t i = 0; i < length; i++) out.bytes[n++] = payload[i];
  out.bytes[n] = Crc8::compute(&out.bytes[1], static_cast<uint8_t>(n - 1));
  n++;
  out.bytes[n++] = ETX;
  out.length = n;
  return true;
}

void ack(uint8_t sequence, ReplyFrame& out) {
  build(static_cast<uint8_t>(Reply::ACK), sequence, nullptr, 0, out);
}

void nack(uint8_t sequence, NackReason reason, ReplyFrame& out) {
  const uint8_t payload[PayloadLength::NACK] = {static_cast<uint8_t>(reason)};
  build(static_cast<uint8_t>(Reply::NACK), sequence, payload, PayloadLength::NACK, out);
}

void status(uint8_t sequence, const StatusReport& report, ReplyFrame& out) {
  uint8_t payload[PayloadLength::STATUS];
  putFloat(&payload[0], report.rawSpeedHz);
  putFloat(&payload[4], report.filteredSpeedHz);
  payload[8] = report.duty;
  payload[9] = report.flags;
  putU32(&payload[10], report.deviceTimeUs);
  putU32(&payload[14], report.edgeCount);
  putU16(&payload[18], report.controlResyncs);
  putU16(&payload[20], saturateU16(report.longestLoopIntervalUs));
  putU16(&payload[22], report.droppedFrames);
  putU16(&payload[24], report.badCrcFrames);
  payload[26] = report.jobState;
  payload[27] = report.segmentIndex;
  putU32(&payload[28], report.jobElapsedMs);
  build(static_cast<uint8_t>(Reply::STATUS), sequence, payload, PayloadLength::STATUS, out);
}

void settings(uint8_t sequence, const SettingsReport& report, ReplyFrame& out) {
  uint8_t payload[PayloadLength::SETTINGS];
  putFloat(&payload[0], report.kp);
  putFloat(&payload[4], report.ki);
  putFloat(&payload[8], report.kd);
  putFloat(&payload[12], report.targetSpeedHz);
  putU16(&payload[16], report.commsTimeoutMs);
  payload[18] = report.mode;
  payload[19] = report.dutySetpoint;
  putFloat(&payload[20], report.alpha);
  putFloat(&payload[24], report.feedforwardOffsetDuty);
  putFloat(&payload[28], report.feedforwardGainDutyPerHz);
  build(static_cast<uint8_t>(Reply::SETTINGS), sequence, payload, PayloadLength::SETTINGS, out);
}

void version(uint8_t sequence, const FirmwareVersion& firmware, uint8_t maxSegments,
             ReplyFrame& out) {
  const uint8_t payload[PayloadLength::VERSION] = {
      PROTOCOL_VERSION, firmware.major, firmware.minor, firmware.patch, maxSegments};
  build(static_cast<uint8_t>(Reply::VERSION), sequence, payload, PayloadLength::VERSION, out);
}

void id(uint8_t sequence, Role role, ReplyFrame& out) {
  const uint8_t payload[PayloadLength::ID] = {static_cast<uint8_t>(role)};
  build(static_cast<uint8_t>(Reply::ID), sequence, payload, PayloadLength::ID, out);
}

void jobInfo(uint8_t sequence, uint8_t jobState, uint8_t segmentCount, uint32_t totalMs,
             ReplyFrame& out) {
  uint8_t payload[PayloadLength::JOB_INFO];
  payload[0] = jobState;
  payload[1] = segmentCount;
  putU32(&payload[2], totalMs);
  build(static_cast<uint8_t>(Reply::JOB_INFO), sequence, payload, PayloadLength::JOB_INFO, out);
}

}

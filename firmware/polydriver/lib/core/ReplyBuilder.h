#ifndef REPLY_BUILDER_H
#define REPLY_BUILDER_H

#include <stdint.h>
#include "ProtocolDefs.h"

struct ReplyFrame {
  uint8_t bytes[ProtocolDefs::MAX_FRAME_LENGTH];
  uint8_t length;
};

// STATUS payload, protocol.md section 5.
struct StatusReport {
  float rawSpeedHz;
  float filteredSpeedHz;
  uint8_t duty;
  uint8_t flags;
  uint32_t deviceTimeUs;
  uint32_t edgeCount;
  uint16_t controlResyncs;
  uint32_t longestLoopIntervalUs;
  uint16_t droppedFrames;
  uint16_t badCrcFrames;
  uint8_t jobState;
  uint8_t segmentIndex;
  uint32_t jobElapsedMs;
};

// SETTINGS payload, protocol.md section 5.
struct SettingsReport {
  float kp;
  float ki;
  float kd;
  float targetSpeedHz;
  uint16_t commsTimeoutMs;
  uint8_t mode;
  uint8_t dutySetpoint;
  float alpha;
  float feedforwardOffsetDuty;
  float feedforwardGainDutyPerHz;
};

struct FirmwareVersion {
  uint8_t major;
  uint8_t minor;
  uint8_t patch;
};

namespace ReplyBuilder {

bool build(uint8_t replyCode, uint8_t sequence, const uint8_t* payload, uint8_t length,
           ReplyFrame& out);

void ack(uint8_t sequence, ReplyFrame& out);
void nack(uint8_t sequence, ProtocolDefs::NackReason reason, ReplyFrame& out);
void status(uint8_t sequence, const StatusReport& report, ReplyFrame& out);
void settings(uint8_t sequence, const SettingsReport& report, ReplyFrame& out);
void version(uint8_t sequence, const FirmwareVersion& firmware, uint8_t maxSegments,
             ReplyFrame& out);
void id(uint8_t sequence, ProtocolDefs::Role role, ReplyFrame& out);
void jobInfo(uint8_t sequence, uint8_t jobState, uint8_t segmentCount, uint32_t totalMs,
             ReplyFrame& out);

}

#endif

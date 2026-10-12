#include <unity.h>
#include <string.h>
#include "ReplyBuilder.h"
#include "FrameParser.h"
#include "Crc8.h"

using namespace ProtocolDefs;

void setUp(void) {}
void tearDown(void) {}

static const uint8_t HEADER = 5;  // STX ID CMD LEN SEQ

static void assertWellFormed(const ReplyFrame& frame, Reply code, uint8_t sequence,
                             uint8_t payloadLength) {
  TEST_ASSERT_EQUAL_UINT8(FRAME_OVERHEAD + payloadLength, frame.length);
  TEST_ASSERT_EQUAL_HEX8(STX, frame.bytes[0]);
  TEST_ASSERT_EQUAL_HEX8(BOARD_ID, frame.bytes[1]);
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(code), frame.bytes[2]);
  TEST_ASSERT_EQUAL_UINT8(payloadLength, frame.bytes[3]);
  TEST_ASSERT_EQUAL_UINT8(sequence, frame.bytes[4]);
  const uint8_t crcAt = static_cast<uint8_t>(HEADER + payloadLength);
  TEST_ASSERT_EQUAL_HEX8(Crc8::compute(&frame.bytes[1], static_cast<uint8_t>(crcAt - 1)),
                         frame.bytes[crcAt]);
  TEST_ASSERT_EQUAL_HEX8(ETX, frame.bytes[crcAt + 1]);
}

static const uint8_t* payloadOf(const ReplyFrame& frame) { return &frame.bytes[HEADER]; }

static uint16_t u16At(const uint8_t* p) {
  return static_cast<uint16_t>(p[0] | (p[1] << 8));
}

static uint32_t u32At(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

static float floatAt(const uint8_t* p) {
  const uint32_t bits = u32At(p);
  float value;
  memcpy(&value, &bits, sizeof(value));
  return value;
}

// --- Framing --------------------------------------------------------------

void test_reply_ack_is_an_empty_frame() {
  ReplyFrame frame;
  ReplyBuilder::ack(7, frame);
  assertWellFormed(frame, Reply::ACK, 7, 0);
}

void test_reply_ack_exact_bytes() {
  ReplyFrame frame;
  ReplyBuilder::ack(0, frame);
  const uint8_t header[] = {0x02, 0x01, 0x11, 0x00, 0x00};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(header, frame.bytes, 5);
  TEST_ASSERT_EQUAL_HEX8(0x03, frame.bytes[6]);
}

void test_reply_nack_carries_the_reason() {
  ReplyFrame frame;
  ReplyBuilder::nack(200, NackReason::NOT_CONFIGURED, frame);
  assertWellFormed(frame, Reply::NACK, 200, 1);
  TEST_ASSERT_EQUAL_HEX8(0x0A, payloadOf(frame)[0]);
}

void test_reply_build_refuses_an_oversized_payload() {
  uint8_t payload[MAX_REPLY_PAYLOAD + 1] = {0};
  ReplyFrame frame;
  TEST_ASSERT_FALSE(ReplyBuilder::build(0x31, 1, payload, MAX_REPLY_PAYLOAD + 1, frame));
  TEST_ASSERT_EQUAL_UINT8(0, frame.length);
  TEST_ASSERT_TRUE(ReplyBuilder::build(0x31, 1, payload, MAX_REPLY_PAYLOAD, frame));
  TEST_ASSERT_EQUAL_UINT8(MAX_FRAME_LENGTH, frame.length);
}

void test_reply_frames_are_readable_by_the_parser() {
  ReplyFrame frame;
  ReplyBuilder::jobInfo(33, 2, 5, 12345, frame);
  FrameParser parser;
  ParseEvent last = ParseEvent::NONE;
  for (uint8_t i = 0; i < frame.length; i++) last = parser.feed(frame.bytes[i], i);
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(ParseEvent::FRAME), static_cast<uint8_t>(last));
  TEST_ASSERT_EQUAL_HEX8(0x75, parser.getFrame().command);
  TEST_ASSERT_EQUAL_UINT8(33, parser.getFrame().sequence);
  TEST_ASSERT_EQUAL_UINT8(6, parser.getFrame().length);
}

// --- Payloads -------------------------------------------------------------

void test_reply_floats_are_little_endian_ieee754() {
  StatusReport report{};
  report.rawSpeedHz = 100.0f;
  ReplyFrame frame;
  ReplyBuilder::status(1, report, frame);
  const uint8_t expected[] = {0x00, 0x00, 0xC8, 0x42};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, payloadOf(frame), 4);
}

void test_reply_status_layout() {
  StatusReport report{};
  report.rawSpeedHz = 12.5f;
  report.filteredSpeedHz = 11.25f;
  report.duty = 180;
  report.flags = 0x85;
  report.deviceTimeUs = 0x11223344UL;
  report.edgeCount = 0xA0B0C0D0UL;
  report.controlResyncs = 0x0102;
  report.longestLoopIntervalUs = 10250;
  report.droppedFrames = 0x0304;
  report.badCrcFrames = 0x0506;
  report.jobState = 3;
  report.segmentIndex = 4;
  report.jobElapsedMs = 987654UL;

  ReplyFrame frame;
  ReplyBuilder::status(9, report, frame);
  assertWellFormed(frame, Reply::STATUS, 9, 32);
  const uint8_t* p = payloadOf(frame);
  TEST_ASSERT_EQUAL_FLOAT(12.5f, floatAt(&p[0]));
  TEST_ASSERT_EQUAL_FLOAT(11.25f, floatAt(&p[4]));
  TEST_ASSERT_EQUAL_UINT8(180, p[8]);
  TEST_ASSERT_EQUAL_HEX8(0x85, p[9]);
  TEST_ASSERT_EQUAL_HEX32(0x11223344UL, u32At(&p[10]));
  TEST_ASSERT_EQUAL_HEX8(0x44, p[10]);
  TEST_ASSERT_EQUAL_HEX32(0xA0B0C0D0UL, u32At(&p[14]));
  TEST_ASSERT_EQUAL_HEX16(0x0102, u16At(&p[18]));
  TEST_ASSERT_EQUAL_UINT16(10250, u16At(&p[20]));
  TEST_ASSERT_EQUAL_HEX16(0x0304, u16At(&p[22]));
  TEST_ASSERT_EQUAL_HEX16(0x0506, u16At(&p[24]));
  TEST_ASSERT_EQUAL_UINT8(3, p[26]);
  TEST_ASSERT_EQUAL_UINT8(4, p[27]);
  TEST_ASSERT_EQUAL_UINT32(987654UL, u32At(&p[28]));
}

void test_reply_status_saturates_the_loop_interval() {
  StatusReport report{};
  report.longestLoopIntervalUs = 70000UL;
  ReplyFrame frame;
  ReplyBuilder::status(1, report, frame);
  TEST_ASSERT_EQUAL_UINT16(0xFFFF, u16At(&payloadOf(frame)[20]));

  report.longestLoopIntervalUs = 0xFFFFUL;
  ReplyBuilder::status(1, report, frame);
  TEST_ASSERT_EQUAL_UINT16(0xFFFF, u16At(&payloadOf(frame)[20]));
}

void test_reply_settings_layout() {
  SettingsReport report{};
  report.kp = 1.5f;
  report.ki = 0.25f;
  report.kd = 0.05f;
  report.targetSpeedHz = 42.0f;
  report.commsTimeoutMs = 3000;
  report.mode = 1;
  report.dutySetpoint = 77;
  report.alpha = 0.2f;
  report.feedforwardOffsetDuty = 128.0f;
  report.feedforwardGainDutyPerHz = 0.75f;

  ReplyFrame frame;
  ReplyBuilder::settings(11, report, frame);
  assertWellFormed(frame, Reply::SETTINGS, 11, 32);
  const uint8_t* p = payloadOf(frame);
  TEST_ASSERT_EQUAL_FLOAT(1.5f, floatAt(&p[0]));
  TEST_ASSERT_EQUAL_FLOAT(0.25f, floatAt(&p[4]));
  TEST_ASSERT_EQUAL_FLOAT(0.05f, floatAt(&p[8]));
  TEST_ASSERT_EQUAL_FLOAT(42.0f, floatAt(&p[12]));
  TEST_ASSERT_EQUAL_UINT16(3000, u16At(&p[16]));
  TEST_ASSERT_EQUAL_UINT8(1, p[18]);
  TEST_ASSERT_EQUAL_UINT8(77, p[19]);
  TEST_ASSERT_EQUAL_FLOAT(0.2f, floatAt(&p[20]));
  TEST_ASSERT_EQUAL_FLOAT(128.0f, floatAt(&p[24]));
  TEST_ASSERT_EQUAL_FLOAT(0.75f, floatAt(&p[28]));
}

void test_reply_version_layout() {
  ReplyFrame frame;
  ReplyBuilder::version(5, FirmwareVersion{1, 2, 3}, 32, frame);
  assertWellFormed(frame, Reply::VERSION, 5, 5);
  const uint8_t expected[] = {3, 1, 2, 3, 32};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, payloadOf(frame), 5);
}

void test_reply_id_layout() {
  ReplyFrame frame;
  ReplyBuilder::id(6, Role::COLOUR, frame);
  assertWellFormed(frame, Reply::ID, 6, 1);
  TEST_ASSERT_EQUAL_UINT8(2, payloadOf(frame)[0]);
}

void test_reply_job_info_layout() {
  ReplyFrame frame;
  ReplyBuilder::jobInfo(8, 2, 17, 0x00012345UL, frame);
  assertWellFormed(frame, Reply::JOB_INFO, 8, 6);
  const uint8_t expected[] = {2, 17, 0x45, 0x23, 0x01, 0x00};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, payloadOf(frame), 6);
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_reply_ack_is_an_empty_frame);
  RUN_TEST(test_reply_ack_exact_bytes);
  RUN_TEST(test_reply_nack_carries_the_reason);
  RUN_TEST(test_reply_build_refuses_an_oversized_payload);
  RUN_TEST(test_reply_frames_are_readable_by_the_parser);
  RUN_TEST(test_reply_floats_are_little_endian_ieee754);
  RUN_TEST(test_reply_status_layout);
  RUN_TEST(test_reply_status_saturates_the_loop_interval);
  RUN_TEST(test_reply_settings_layout);
  RUN_TEST(test_reply_version_layout);
  RUN_TEST(test_reply_id_layout);
  RUN_TEST(test_reply_job_info_layout);
  return UNITY_END();
}

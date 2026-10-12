#include <unity.h>
#include "ProtocolDefs.h"
#include "DispenserController.h"
#include "JobProgram.h"

using namespace ProtocolDefs;

// The core classes use the wire values directly; a mismatch must not even compile.
static_assert(static_cast<uint8_t>(ControlMode::CLOSED_LOOP) == 0, "mode 0 is closed loop");
static_assert(static_cast<uint8_t>(ControlMode::OPEN_LOOP) == 1, "mode 1 is open loop");
static_assert(static_cast<uint8_t>(JobState::EMPTY) == 0, "job state EMPTY");
static_assert(static_cast<uint8_t>(JobState::LOADING) == 1, "job state LOADING");
static_assert(static_cast<uint8_t>(JobState::READY) == 2, "job state READY");
static_assert(static_cast<uint8_t>(JobState::RUNNING) == 3, "job state RUNNING");
static_assert(static_cast<uint8_t>(JobState::DONE) == 4, "job state DONE");
static_assert(static_cast<uint8_t>(JobState::ABORTED) == 5, "job state ABORTED");
static_assert(JobProgram::FLAG_RAMP == SegmentFlag::RAMP, "segment flag RAMP");
static_assert(JobProgram::FLAG_DUTY == SegmentFlag::DUTY, "segment flag DUTY");
static_assert(JobProgram::NO_SEGMENT == 0xFF, "no segment is 0xFF in STATUS");
static_assert(JobProgram::MAX_SEGMENTS == 32, "VERSION reports 32 segments");

void setUp(void) {}
void tearDown(void) {}

static uint8_t op(Command command) { return static_cast<uint8_t>(command); }
static uint8_t code(Reply reply) { return static_cast<uint8_t>(reply); }
static uint8_t reason(NackReason nack) { return static_cast<uint8_t>(nack); }

void test_protocol_frame_constants() {
  TEST_ASSERT_EQUAL_UINT8(3, PROTOCOL_VERSION);
  TEST_ASSERT_EQUAL_HEX8(0x01, BOARD_ID);
  TEST_ASSERT_EQUAL_HEX8(0x02, STX);
  TEST_ASSERT_EQUAL_HEX8(0x03, ETX);
  TEST_ASSERT_EQUAL_UINT8(7, FRAME_OVERHEAD);
  TEST_ASSERT_EQUAL_UINT8(16, MAX_REQUEST_PAYLOAD);
  TEST_ASSERT_EQUAL_UINT8(32, MAX_REPLY_PAYLOAD);
  TEST_ASSERT_EQUAL_UINT8(39, MAX_FRAME_LENGTH);
  TEST_ASSERT_EQUAL_UINT16(10, INTER_BYTE_TIMEOUT_MS);
}

void test_protocol_opcodes_match_the_spec() {
  TEST_ASSERT_EQUAL_HEX8(0x10, op(Command::SET_SPEED));
  TEST_ASSERT_EQUAL_HEX8(0x20, op(Command::SET_PID));
  TEST_ASSERT_EQUAL_HEX8(0x22, op(Command::SET_FILTER));
  TEST_ASSERT_EQUAL_HEX8(0x24, op(Command::SET_FEEDFORWARD));
  TEST_ASSERT_EQUAL_HEX8(0x30, op(Command::REQ_STAT));
  TEST_ASSERT_EQUAL_HEX8(0x32, op(Command::REQ_SETTINGS));
  TEST_ASSERT_EQUAL_HEX8(0x40, op(Command::GET_VERSION));
  TEST_ASSERT_EQUAL_HEX8(0x42, op(Command::GET_ID));
  TEST_ASSERT_EQUAL_HEX8(0x44, op(Command::SET_ID));
  TEST_ASSERT_EQUAL_HEX8(0x50, op(Command::SET_MODE));
  TEST_ASSERT_EQUAL_HEX8(0x52, op(Command::SET_PWM));
  TEST_ASSERT_EQUAL_HEX8(0x54, op(Command::SET_COMMS_TIMEOUT));
  TEST_ASSERT_EQUAL_HEX8(0x60, op(Command::STOP));
  TEST_ASSERT_EQUAL_HEX8(0x62, op(Command::ESTOP));
  TEST_ASSERT_EQUAL_HEX8(0x64, op(Command::CLEAR_ESTOP));
  TEST_ASSERT_EQUAL_HEX8(0x70, op(Command::JOB_CLEAR));
  TEST_ASSERT_EQUAL_HEX8(0x72, op(Command::JOB_SEGMENT));
  TEST_ASSERT_EQUAL_HEX8(0x74, op(Command::JOB_COMMIT));
  TEST_ASSERT_EQUAL_HEX8(0x76, op(Command::JOB_RUN));
}

void test_protocol_reply_codes_match_the_spec() {
  TEST_ASSERT_EQUAL_HEX8(0x11, code(Reply::ACK));
  TEST_ASSERT_EQUAL_HEX8(0x12, code(Reply::NACK));
  TEST_ASSERT_EQUAL_HEX8(0x31, code(Reply::STATUS));
  TEST_ASSERT_EQUAL_HEX8(0x33, code(Reply::SETTINGS));
  TEST_ASSERT_EQUAL_HEX8(0x41, code(Reply::VERSION));
  TEST_ASSERT_EQUAL_HEX8(0x43, code(Reply::ID));
  TEST_ASSERT_EQUAL_HEX8(0x75, code(Reply::JOB_INFO));
}

void test_protocol_nack_reasons_match_the_spec() {
  TEST_ASSERT_EQUAL_HEX8(0x01, reason(NackReason::BAD_CRC));
  TEST_ASSERT_EQUAL_HEX8(0x02, reason(NackReason::BAD_LENGTH));
  TEST_ASSERT_EQUAL_HEX8(0x03, reason(NackReason::UNKNOWN_COMMAND));
  TEST_ASSERT_EQUAL_HEX8(0x04, reason(NackReason::WRONG_MODE));
  TEST_ASSERT_EQUAL_HEX8(0x05, reason(NackReason::WRONG_STATE));
  TEST_ASSERT_EQUAL_HEX8(0x06, reason(NackReason::BAD_INDEX));
  TEST_ASSERT_EQUAL_HEX8(0x07, reason(NackReason::JOB_FULL));
  TEST_ASSERT_EQUAL_HEX8(0x08, reason(NackReason::BAD_JOB));
  TEST_ASSERT_EQUAL_HEX8(0x09, reason(NackReason::BAD_VALUE));
  TEST_ASSERT_EQUAL_HEX8(0x0A, reason(NackReason::NOT_CONFIGURED));
}

void test_protocol_status_flags_match_the_spec() {
  TEST_ASSERT_EQUAL_HEX8(0x01, StatusFlag::ENCODER_STALLED);
  TEST_ASSERT_EQUAL_HEX8(0x04, StatusFlag::REGULATING);
  TEST_ASSERT_EQUAL_HEX8(0x08, StatusFlag::COMMS_LOST);
  TEST_ASSERT_EQUAL_HEX8(0x10, StatusFlag::OPEN_LOOP);
  TEST_ASSERT_EQUAL_HEX8(0x20, StatusFlag::ESTOP);
  TEST_ASSERT_EQUAL_HEX8(0x40, StatusFlag::WATCHDOG_RESET);
  TEST_ASSERT_EQUAL_HEX8(0x80, StatusFlag::UNCONFIGURED);

  const uint8_t all = StatusFlag::ENCODER_STALLED | StatusFlag::REGULATING | StatusFlag::COMMS_LOST |
                      StatusFlag::OPEN_LOOP | StatusFlag::ESTOP | StatusFlag::WATCHDOG_RESET |
                      StatusFlag::UNCONFIGURED;
  TEST_ASSERT_EQUAL_HEX8(0xFD, all);
}

void test_protocol_roles_match_the_spec() {
  TEST_ASSERT_EQUAL_UINT8(0, static_cast<uint8_t>(Role::NONE));
  TEST_ASSERT_EQUAL_UINT8(1, static_cast<uint8_t>(Role::POLYMER));
  TEST_ASSERT_EQUAL_UINT8(2, static_cast<uint8_t>(Role::COLOUR));
}

void test_protocol_request_payload_lengths() {
  struct Expected {
    Command command;
    uint8_t length;
  };
  const Expected expected[] = {
    {Command::SET_SPEED, 4},       {Command::SET_PID, 12},     {Command::SET_FILTER, 4},
    {Command::SET_FEEDFORWARD, 8}, {Command::REQ_STAT, 0},     {Command::REQ_SETTINGS, 0},
    {Command::GET_VERSION, 0},     {Command::GET_ID, 0},       {Command::SET_ID, 1},
    {Command::SET_MODE, 1},        {Command::SET_PWM, 1},      {Command::SET_COMMS_TIMEOUT, 2},
    {Command::STOP, 0},            {Command::ESTOP, 0},        {Command::CLEAR_ESTOP, 0},
    {Command::JOB_CLEAR, 0},       {Command::JOB_SEGMENT, 10}, {Command::JOB_COMMIT, 5},
    {Command::JOB_RUN, 0},
  };
  for (const Expected& e : expected) {
    uint8_t length = 0xEE;
    TEST_ASSERT_TRUE(requestPayloadLength(op(e.command), length));
    TEST_ASSERT_EQUAL_UINT8(e.length, length);
    TEST_ASSERT_TRUE(length <= MAX_REQUEST_PAYLOAD);
  }
}

void test_protocol_unknown_opcodes_have_no_length() {
  uint8_t length = 0;
  TEST_ASSERT_FALSE(requestPayloadLength(0x00, length));
  TEST_ASSERT_FALSE(requestPayloadLength(0x11, length));
  TEST_ASSERT_FALSE(requestPayloadLength(0x31, length));
  TEST_ASSERT_FALSE(requestPayloadLength(0x26, length));
  TEST_ASSERT_FALSE(requestPayloadLength(0xFF, length));

  uint8_t known = 0;
  for (uint16_t opcode = 0; opcode <= 0xFF; opcode++) {
    if (requestPayloadLength(static_cast<uint8_t>(opcode), length)) known++;
  }
  TEST_ASSERT_EQUAL_UINT8(19, known);
}

void test_protocol_reply_payloads_fit() {
  TEST_ASSERT_EQUAL_UINT8(1, PayloadLength::NACK);
  TEST_ASSERT_EQUAL_UINT8(32, PayloadLength::STATUS);
  TEST_ASSERT_EQUAL_UINT8(32, PayloadLength::SETTINGS);
  TEST_ASSERT_EQUAL_UINT8(5, PayloadLength::VERSION);
  TEST_ASSERT_EQUAL_UINT8(1, PayloadLength::ID);
  TEST_ASSERT_EQUAL_UINT8(6, PayloadLength::JOB_INFO);
  TEST_ASSERT_TRUE(PayloadLength::STATUS <= MAX_REPLY_PAYLOAD);
  TEST_ASSERT_TRUE(PayloadLength::SETTINGS <= MAX_REPLY_PAYLOAD);
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_protocol_frame_constants);
  RUN_TEST(test_protocol_opcodes_match_the_spec);
  RUN_TEST(test_protocol_reply_codes_match_the_spec);
  RUN_TEST(test_protocol_nack_reasons_match_the_spec);
  RUN_TEST(test_protocol_status_flags_match_the_spec);
  RUN_TEST(test_protocol_roles_match_the_spec);
  RUN_TEST(test_protocol_request_payload_lengths);
  RUN_TEST(test_protocol_unknown_opcodes_have_no_length);
  RUN_TEST(test_protocol_reply_payloads_fit);
  return UNITY_END();
}

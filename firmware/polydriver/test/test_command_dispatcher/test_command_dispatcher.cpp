#include <unity.h>
#include <limits>
#include "CommandDispatcher.h"
#include "LittleEndian.h"

using namespace ProtocolDefs;

static constexpr uint32_t MS = 1000UL;
static constexpr float DT_S = 0.01f;

void setUp(void) {}
void tearDown(void) {}

class FakeRoleStorage : public RoleStorage {
public:
  uint8_t loadRole() override { return stored; }
  void saveRole(uint8_t role) override {
    stored = role;
    saves++;
  }
  uint8_t stored{0};
  int saves{0};
};

struct Payload {
  uint8_t bytes[MAX_REQUEST_PAYLOAD]{};
  uint8_t length{0};
  Payload& u8(uint8_t value) {
    bytes[length++] = value;
    return *this;
  }
  Payload& u16(uint16_t value) {
    LittleEndian::putU16(&bytes[length], value);
    length = static_cast<uint8_t>(length + 2);
    return *this;
  }
  Payload& u32(uint32_t value) {
    LittleEndian::putU32(&bytes[length], value);
    length = static_cast<uint8_t>(length + 4);
    return *this;
  }
  Payload& f32(float value) {
    LittleEndian::putFloat(&bytes[length], value);
    length = static_cast<uint8_t>(length + 4);
    return *this;
  }
};

struct Board {
  DispenserController controller;
  JobProgram program;
  JobRunner runner{program, controller};
  FrameParser parser;
  PeriodicTimer timer{10000};
  FakeRoleStorage storage;
  CommandDispatcher dispatcher{controller, program, runner, parser, timer, storage};
  BoardInputs inputs{0.0f, 0, false, false};
  uint8_t nextSequence{0};
  ReplyFrame reply{};

  ReplyFrame& sendWithSequence(uint8_t sequence, Command command, const Payload& payload,
                               uint32_t nowUs = 0) {
    Frame frame{};
    frame.command = static_cast<uint8_t>(command);
    frame.sequence = sequence;
    frame.length = payload.length;
    for (uint8_t i = 0; i < payload.length; i++) frame.payload[i] = payload.bytes[i];
    TEST_ASSERT_TRUE(dispatcher.handle(ParseEvent::FRAME, frame, nowUs, inputs, reply));
    return reply;
  }

  ReplyFrame& send(Command command, const Payload& payload = Payload(), uint32_t nowUs = 0) {
    return sendWithSequence(nextSequence++, command, payload, nowUs);
  }

  void configure() { assertAck(send(Command::SET_PID, Payload().f32(1.0f).f32(0.0f).f32(0.0f))); }

  static void assertAck(const ReplyFrame& frame) {
    TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::ACK), frame.bytes[2]);
  }
};

static uint8_t replyCode(const ReplyFrame& frame) { return frame.bytes[2]; }
static uint8_t replySequence(const ReplyFrame& frame) { return frame.bytes[4]; }
static const uint8_t* replyPayload(const ReplyFrame& frame) { return &frame.bytes[5]; }

static void assertAck(const ReplyFrame& frame) { Board::assertAck(frame); }

static void assertNack(NackReason reason, const ReplyFrame& frame) {
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::NACK), replyCode(frame));
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(reason), replyPayload(frame)[0]);
}

static void loadSpeedJob(Board& board, float hz, uint32_t durationMs) {
  assertAck(board.send(Command::JOB_CLEAR));
  assertAck(board.send(Command::JOB_SEGMENT, Payload().u8(0).u8(0).f32(hz).u32(durationMs)));
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::JOB_INFO),
                         replyCode(board.send(Command::JOB_COMMIT, Payload().u8(1).u32(durationMs))));
}

// --- Framing and errors ---------------------------------------------------

void test_dispatcher_no_event_no_reply() {
  Board board;
  Frame frame{};
  TEST_ASSERT_FALSE(board.dispatcher.handle(ParseEvent::NONE, frame, 0, board.inputs, board.reply));
}

void test_dispatcher_bad_crc_gets_a_nack_with_its_sequence() {
  Board board;
  Frame frame{};
  frame.command = static_cast<uint8_t>(Command::SET_SPEED);
  frame.sequence = 77;
  TEST_ASSERT_TRUE(board.dispatcher.handle(ParseEvent::BAD_CRC, frame, 0, board.inputs, board.reply));
  assertNack(NackReason::BAD_CRC, board.reply);
  TEST_ASSERT_EQUAL_UINT8(77, replySequence(board.reply));
}

void test_dispatcher_a_bad_crc_is_not_remembered_for_retries() {
  Board board;
  Frame frame{};
  frame.command = static_cast<uint8_t>(Command::GET_VERSION);
  frame.sequence = 5;
  board.dispatcher.handle(ParseEvent::BAD_CRC, frame, 0, board.inputs, board.reply);
  ReplyFrame& reply = board.sendWithSequence(5, Command::GET_VERSION, Payload());
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::VERSION), replyCode(reply));
}

void test_dispatcher_unknown_opcode() {
  Board board;
  Frame frame{};
  frame.command = 0xEE;
  frame.sequence = 1;
  board.dispatcher.handle(ParseEvent::FRAME, frame, 0, board.inputs, board.reply);
  assertNack(NackReason::UNKNOWN_COMMAND, board.reply);
}

void test_dispatcher_wrong_payload_length() {
  Board board;
  assertNack(NackReason::BAD_LENGTH, board.send(Command::SET_SPEED, Payload().u8(1).u8(2).u8(3)));
  assertNack(NackReason::BAD_LENGTH, board.send(Command::REQ_STAT, Payload().u8(0)));
  assertNack(NackReason::BAD_LENGTH, board.send(Command::STOP, Payload().u8(0)));
}

void test_dispatcher_every_reply_echoes_the_sequence() {
  Board board;
  TEST_ASSERT_EQUAL_UINT8(201, replySequence(board.sendWithSequence(201, Command::GET_ID, Payload())));
  TEST_ASSERT_EQUAL_UINT8(9, replySequence(board.sendWithSequence(9, Command::SET_SPEED, Payload())));
}

// --- Retries --------------------------------------------------------------

void test_dispatcher_a_retry_is_answered_but_not_run_twice() {
  Board board;
  const Payload segment = Payload().u8(0).u8(0).f32(10.0f).u32(1000);
  assertAck(board.sendWithSequence(50, Command::JOB_SEGMENT, segment));
  assertAck(board.sendWithSequence(50, Command::JOB_SEGMENT, segment));
  TEST_ASSERT_EQUAL_UINT8(1, board.program.getCount());
}

void test_dispatcher_same_command_new_sequence_runs_again() {
  Board board;
  const Payload segment = Payload().u8(0).u8(0).f32(10.0f).u32(1000);
  assertAck(board.sendWithSequence(50, Command::JOB_SEGMENT, segment));
  assertNack(NackReason::BAD_INDEX, board.sendWithSequence(51, Command::JOB_SEGMENT, segment));
}

void test_dispatcher_same_sequence_other_command_runs() {
  Board board;
  assertAck(board.sendWithSequence(60, Command::JOB_CLEAR, Payload()));
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::ID),
                         replyCode(board.sendWithSequence(60, Command::GET_ID, Payload())));
}

void test_dispatcher_a_retry_repeats_a_nack() {
  Board board;
  assertNack(NackReason::NOT_CONFIGURED,
             board.sendWithSequence(70, Command::SET_SPEED, Payload().f32(50.0f)));
  board.controller.setTunings(1.0f, 0.0f, 0.0f);
  assertNack(NackReason::NOT_CONFIGURED,
             board.sendWithSequence(70, Command::SET_SPEED, Payload().f32(50.0f)));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, board.controller.getTargetSpeedHz());
}

// --- Settings commands ----------------------------------------------------

void test_dispatcher_set_speed_needs_tunings_first() {
  Board board;
  assertNack(NackReason::NOT_CONFIGURED, board.send(Command::SET_SPEED, Payload().f32(50.0f)));
  board.configure();
  assertAck(board.send(Command::SET_SPEED, Payload().f32(50.0f)));
  TEST_ASSERT_EQUAL_FLOAT(50.0f, board.controller.getTargetSpeedHz());
}

void test_dispatcher_set_speed_rejects_nan() {
  Board board;
  board.configure();
  assertNack(NackReason::BAD_VALUE,
             board.send(Command::SET_SPEED, Payload().f32(std::numeric_limits<float>::quiet_NaN())));
}

void test_dispatcher_set_pid() {
  Board board;
  assertAck(board.send(Command::SET_PID, Payload().f32(1.5f).f32(0.25f).f32(0.05f)));
  TEST_ASSERT_EQUAL_FLOAT(1.5f, board.controller.getKp());
  TEST_ASSERT_EQUAL_FLOAT(0.25f, board.controller.getKi());
  TEST_ASSERT_EQUAL_FLOAT(0.05f, board.controller.getKd());
  TEST_ASSERT_TRUE(board.controller.isConfigured());
  assertNack(NackReason::BAD_VALUE, board.send(Command::SET_PID, Payload().f32(-1.0f).f32(0.0f).f32(0.0f)));
}

void test_dispatcher_set_filter_and_feedforward() {
  Board board;
  assertAck(board.send(Command::SET_FILTER, Payload().f32(0.3f)));
  TEST_ASSERT_EQUAL_FLOAT(0.3f, board.controller.getAlpha());
  assertNack(NackReason::BAD_VALUE, board.send(Command::SET_FILTER, Payload().f32(0.0f)));

  assertAck(board.send(Command::SET_FEEDFORWARD, Payload().f32(120.0f).f32(0.5f)));
  TEST_ASSERT_EQUAL_FLOAT(120.0f, board.controller.getFeedforwardOffsetDuty());
  TEST_ASSERT_EQUAL_FLOAT(0.5f, board.controller.getFeedforwardGainDutyPerHz());
  assertNack(NackReason::BAD_VALUE, board.send(Command::SET_FEEDFORWARD, Payload().f32(300.0f).f32(0.5f)));
}

void test_dispatcher_open_loop_duty() {
  Board board;
  assertNack(NackReason::WRONG_MODE, board.send(Command::SET_PWM, Payload().u8(255)));
  assertAck(board.send(Command::SET_MODE, Payload().u8(1)));
  assertAck(board.send(Command::SET_PWM, Payload().u8(255)));
  TEST_ASSERT_EQUAL_UINT8(255, board.controller.getDutySetpoint());
  assertNack(NackReason::WRONG_MODE, board.send(Command::SET_SPEED, Payload().f32(10.0f)));
}

void test_dispatcher_unknown_mode_is_a_bad_value() {
  Board board;
  assertNack(NackReason::BAD_VALUE, board.send(Command::SET_MODE, Payload().u8(5)));
}

void test_dispatcher_comms_timeout_is_kept_alive_by_any_frame() {
  Board board;
  board.configure();
  assertAck(board.send(Command::SET_COMMS_TIMEOUT, Payload().u16(3000), 0));
  TEST_ASSERT_EQUAL_UINT16(3000, board.controller.getCommsTimeoutMs());
  board.send(Command::SET_SPEED, Payload().f32(50.0f), 0);

  board.send(Command::REQ_STAT, Payload(), 2900 * MS);
  TEST_ASSERT_TRUE(board.controller.update(0.0f, 5800 * MS, DT_S) > 0);
  TEST_ASSERT_FALSE(board.controller.isCommsLost());

  TEST_ASSERT_EQUAL_UINT8(0, board.controller.update(0.0f, 5900 * MS, DT_S));
  TEST_ASSERT_TRUE(board.controller.isCommsLost());
}

void test_dispatcher_a_retry_also_keeps_the_link_alive() {
  Board board;
  board.configure();
  board.send(Command::SET_COMMS_TIMEOUT, Payload().u16(3000), 0);
  board.sendWithSequence(90, Command::REQ_STAT, Payload(), 1000 * MS);
  board.sendWithSequence(90, Command::REQ_STAT, Payload(), 3500 * MS);
  board.send(Command::SET_SPEED, Payload().f32(50.0f), 3500 * MS);
  TEST_ASSERT_TRUE(board.controller.update(0.0f, 6000 * MS, DT_S) > 0);
}

// --- STOP and ESTOP -------------------------------------------------------

void test_dispatcher_stop_during_a_job_aborts_it_for_good() {
  Board board;
  board.configure();
  board.program.addSegment(0, JobProgram::FLAG_RAMP, 100.0f, 1000);
  board.program.commit(1, 1000);
  assertAck(board.send(Command::JOB_RUN, Payload(), 0));
  board.runner.tick(100 * MS);
  TEST_ASSERT_TRUE(board.controller.getTargetSpeedHz() > 0.0f);

  assertAck(board.send(Command::STOP, Payload(), 105 * MS));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(JobState::ABORTED),
                          static_cast<uint8_t>(board.runner.getState()));
  board.runner.tick(110 * MS);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, board.controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT8(0, board.controller.update(0.0f, 110 * MS, DT_S));
}

void test_dispatcher_estop_latches_and_clears() {
  Board board;
  board.configure();
  loadSpeedJob(board, 50.0f, 1000);
  assertAck(board.send(Command::JOB_RUN));
  assertAck(board.send(Command::ESTOP));
  TEST_ASSERT_TRUE(board.controller.isEstopped());
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(JobState::ABORTED),
                          static_cast<uint8_t>(board.runner.getState()));

  assertNack(NackReason::WRONG_STATE, board.send(Command::SET_SPEED, Payload().f32(10.0f)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::SET_MODE, Payload().u8(1)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::JOB_RUN));
  assertAck(board.send(Command::SET_PID, Payload().f32(2.0f).f32(0.0f).f32(0.0f)));

  assertAck(board.send(Command::CLEAR_ESTOP));
  TEST_ASSERT_FALSE(board.controller.isEstopped());
  TEST_ASSERT_EQUAL_FLOAT(0.0f, board.controller.getTargetSpeedHz());
  assertAck(board.send(Command::SET_SPEED, Payload().f32(10.0f)));
}

// --- Jobs -----------------------------------------------------------------

void test_dispatcher_loads_commits_and_runs_a_job() {
  Board board;
  board.configure();
  assertAck(board.send(Command::JOB_CLEAR));
  assertAck(board.send(Command::JOB_SEGMENT, Payload().u8(0).u8(0).f32(10.0f).u32(1000)));
  assertAck(board.send(Command::JOB_SEGMENT, Payload().u8(1).u8(JobProgram::FLAG_RAMP).f32(20.0f).u32(500)));

  ReplyFrame& info = board.send(Command::JOB_COMMIT, Payload().u8(2).u32(1500));
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::JOB_INFO), replyCode(info));
  const uint8_t expected[] = {2, 2, 0xDC, 0x05, 0x00, 0x00};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, replyPayload(info), 6);

  assertAck(board.send(Command::JOB_RUN, Payload(), 0));
  TEST_ASSERT_TRUE(board.runner.isRunning());
  TEST_ASSERT_EQUAL_FLOAT(10.0f, board.controller.getTargetSpeedHz());
}

void test_dispatcher_job_errors_map_to_their_nacks() {
  Board board;
  assertNack(NackReason::BAD_INDEX, board.send(Command::JOB_SEGMENT, Payload().u8(3).u8(0).f32(1.0f).u32(10)));
  assertNack(NackReason::JOB_FULL, board.send(Command::JOB_SEGMENT, Payload().u8(40).u8(0).f32(1.0f).u32(10)));
  assertNack(NackReason::BAD_VALUE, board.send(Command::JOB_SEGMENT, Payload().u8(0).u8(0x04).f32(1.0f).u32(10)));
  assertAck(board.send(Command::JOB_SEGMENT, Payload().u8(0).u8(0).f32(1.0f).u32(10)));
  assertNack(NackReason::BAD_JOB, board.send(Command::JOB_COMMIT, Payload().u8(2).u32(10)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::JOB_RUN));
}

void test_dispatcher_job_run_checks_mode_and_tunings() {
  Board board;
  loadSpeedJob(board, 50.0f, 1000);
  assertNack(NackReason::NOT_CONFIGURED, board.send(Command::JOB_RUN));
  board.send(Command::SET_MODE, Payload().u8(1));
  assertNack(NackReason::WRONG_MODE, board.send(Command::JOB_RUN));
}

void test_dispatcher_refuses_changes_while_a_job_runs() {
  Board board;
  board.configure();
  loadSpeedJob(board, 50.0f, 1000);
  assertAck(board.send(Command::JOB_RUN));

  assertNack(NackReason::WRONG_STATE, board.send(Command::SET_SPEED, Payload().f32(10.0f)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::SET_PWM, Payload().u8(10)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::SET_MODE, Payload().u8(1)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::SET_ID, Payload().u8(1)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::JOB_CLEAR));
  assertNack(NackReason::WRONG_STATE, board.send(Command::JOB_SEGMENT, Payload().u8(1).u8(0).f32(1.0f).u32(10)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::JOB_COMMIT, Payload().u8(1).u32(1000)));
  assertNack(NackReason::WRONG_STATE, board.send(Command::JOB_RUN));

  assertAck(board.send(Command::SET_PID, Payload().f32(2.0f).f32(0.0f).f32(0.0f)));
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::STATUS), replyCode(board.send(Command::REQ_STAT)));
  TEST_ASSERT_TRUE(board.runner.isRunning());
}

// --- Reports --------------------------------------------------------------

void test_dispatcher_status_report() {
  Board board;
  board.inputs = BoardInputs{12.5f, 4242, true, true};
  board.controller.update(12.5f, 0, DT_S);
  ReplyFrame& status = board.send(Command::REQ_STAT, Payload(), 0x00ABCDEFUL);
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::STATUS), replyCode(status));
  const uint8_t* p = replyPayload(status);
  TEST_ASSERT_EQUAL_FLOAT(12.5f, LittleEndian::getFloat(&p[0]));
  TEST_ASSERT_EQUAL_FLOAT(12.5f, LittleEndian::getFloat(&p[4]));
  TEST_ASSERT_EQUAL_UINT8(0, p[8]);
  TEST_ASSERT_EQUAL_HEX8(StatusFlag::ENCODER_STALLED | StatusFlag::WATCHDOG_RESET | StatusFlag::UNCONFIGURED, p[9]);
  TEST_ASSERT_EQUAL_HEX32(0x00ABCDEFUL, LittleEndian::getU32(&p[10]));
  TEST_ASSERT_EQUAL_UINT32(4242, LittleEndian::getU32(&p[14]));
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(JobState::EMPTY), p[26]);
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, p[27]);
}

void test_dispatcher_status_flags_follow_the_state() {
  Board board;
  board.configure();
  board.send(Command::SET_SPEED, Payload().f32(10.0f));
  TEST_ASSERT_EQUAL_HEX8(StatusFlag::REGULATING, replyPayload(board.send(Command::REQ_STAT))[9]);

  board.send(Command::SET_MODE, Payload().u8(1));
  TEST_ASSERT_EQUAL_HEX8(StatusFlag::OPEN_LOOP, replyPayload(board.send(Command::REQ_STAT))[9]);

  board.send(Command::ESTOP);
  TEST_ASSERT_EQUAL_HEX8(StatusFlag::OPEN_LOOP | StatusFlag::ESTOP,
                         replyPayload(board.send(Command::REQ_STAT))[9]);
}

void test_dispatcher_status_reports_the_comms_lost_flag() {
  Board board;
  board.configure();
  board.send(Command::SET_COMMS_TIMEOUT, Payload().u16(100), 0);
  board.send(Command::SET_SPEED, Payload().f32(10.0f), 0);
  board.controller.update(0.0f, 200 * MS, DT_S);
  TEST_ASSERT_EQUAL_HEX8(StatusFlag::COMMS_LOST,
                         replyPayload(board.send(Command::REQ_STAT, Payload(), 200 * MS))[9]);
}

void test_dispatcher_status_takes_the_longest_loop_interval() {
  Board board;
  board.timer.due(0);
  board.timer.due(10 * MS);
  board.timer.due(22500);
  const uint8_t* first = replyPayload(board.send(Command::REQ_STAT));
  TEST_ASSERT_EQUAL_UINT16(12500, LittleEndian::getU16(&first[20]));
  const uint8_t* second = replyPayload(board.send(Command::REQ_STAT));
  TEST_ASSERT_EQUAL_UINT16(0, LittleEndian::getU16(&second[20]));
}

void test_dispatcher_status_carries_the_parser_counters() {
  Board board;
  const uint8_t garbage[] = {STX, BOARD_ID, 0x30, 0x00, 0x01, 0x00, 0x7E};
  for (uint8_t i = 0; i < sizeof(garbage); i++) board.parser.feed(garbage[i], i);
  const uint8_t badCrc[] = {STX, BOARD_ID, 0x30, 0x00, 0x01, 0x55, ETX};
  for (uint8_t i = 0; i < sizeof(badCrc); i++) board.parser.feed(badCrc[i], 100 + i);
  const uint8_t* p = replyPayload(board.send(Command::REQ_STAT));
  TEST_ASSERT_EQUAL_UINT16(1, LittleEndian::getU16(&p[22]));
  TEST_ASSERT_EQUAL_UINT16(1, LittleEndian::getU16(&p[24]));
}

void test_dispatcher_settings_report() {
  Board board;
  board.send(Command::SET_PID, Payload().f32(1.5f).f32(0.25f).f32(0.05f));
  board.send(Command::SET_FILTER, Payload().f32(0.2f));
  board.send(Command::SET_FEEDFORWARD, Payload().f32(128.0f).f32(0.75f));
  board.send(Command::SET_COMMS_TIMEOUT, Payload().u16(3000));
  board.send(Command::SET_SPEED, Payload().f32(42.0f));

  ReplyFrame& settings = board.send(Command::REQ_SETTINGS);
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::SETTINGS), replyCode(settings));
  const uint8_t* p = replyPayload(settings);
  TEST_ASSERT_EQUAL_FLOAT(1.5f, LittleEndian::getFloat(&p[0]));
  TEST_ASSERT_EQUAL_FLOAT(0.25f, LittleEndian::getFloat(&p[4]));
  TEST_ASSERT_EQUAL_FLOAT(0.05f, LittleEndian::getFloat(&p[8]));
  TEST_ASSERT_EQUAL_FLOAT(42.0f, LittleEndian::getFloat(&p[12]));
  TEST_ASSERT_EQUAL_UINT16(3000, LittleEndian::getU16(&p[16]));
  TEST_ASSERT_EQUAL_UINT8(0, p[18]);
  TEST_ASSERT_EQUAL_UINT8(0, p[19]);
  TEST_ASSERT_EQUAL_FLOAT(0.2f, LittleEndian::getFloat(&p[20]));
  TEST_ASSERT_EQUAL_FLOAT(128.0f, LittleEndian::getFloat(&p[24]));
  TEST_ASSERT_EQUAL_FLOAT(0.75f, LittleEndian::getFloat(&p[28]));
}

void test_dispatcher_version() {
  Board board;
  ReplyFrame& version = board.send(Command::GET_VERSION);
  TEST_ASSERT_EQUAL_HEX8(static_cast<uint8_t>(Reply::VERSION), replyCode(version));
  const uint8_t expected[] = {3, FIRMWARE_VERSION.major, FIRMWARE_VERSION.minor,
                              FIRMWARE_VERSION.patch, 32};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, replyPayload(version), 5);
}

// --- Role -----------------------------------------------------------------

void test_dispatcher_role_is_loaded_at_begin() {
  Board board;
  board.storage.stored = 2;
  board.dispatcher.begin();
  TEST_ASSERT_EQUAL_UINT8(2, replyPayload(board.send(Command::GET_ID))[0]);
}

void test_dispatcher_unprogrammed_storage_means_no_role() {
  Board board;
  board.storage.stored = 0xFF;
  board.dispatcher.begin();
  TEST_ASSERT_EQUAL_UINT8(0, replyPayload(board.send(Command::GET_ID))[0]);
}

void test_dispatcher_set_id_saves_only_a_change() {
  Board board;
  board.dispatcher.begin();
  assertAck(board.send(Command::SET_ID, Payload().u8(1)));
  TEST_ASSERT_EQUAL_UINT8(1, board.storage.stored);
  assertAck(board.send(Command::SET_ID, Payload().u8(1)));
  TEST_ASSERT_EQUAL_INT(1, board.storage.saves);
  TEST_ASSERT_EQUAL_UINT8(1, replyPayload(board.send(Command::GET_ID))[0]);
  assertNack(NackReason::BAD_VALUE, board.send(Command::SET_ID, Payload().u8(3)));
  TEST_ASSERT_EQUAL_UINT8(1, board.storage.stored);
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_dispatcher_no_event_no_reply);
  RUN_TEST(test_dispatcher_bad_crc_gets_a_nack_with_its_sequence);
  RUN_TEST(test_dispatcher_a_bad_crc_is_not_remembered_for_retries);
  RUN_TEST(test_dispatcher_unknown_opcode);
  RUN_TEST(test_dispatcher_wrong_payload_length);
  RUN_TEST(test_dispatcher_every_reply_echoes_the_sequence);
  RUN_TEST(test_dispatcher_a_retry_is_answered_but_not_run_twice);
  RUN_TEST(test_dispatcher_same_command_new_sequence_runs_again);
  RUN_TEST(test_dispatcher_same_sequence_other_command_runs);
  RUN_TEST(test_dispatcher_a_retry_repeats_a_nack);
  RUN_TEST(test_dispatcher_set_speed_needs_tunings_first);
  RUN_TEST(test_dispatcher_set_speed_rejects_nan);
  RUN_TEST(test_dispatcher_set_pid);
  RUN_TEST(test_dispatcher_set_filter_and_feedforward);
  RUN_TEST(test_dispatcher_open_loop_duty);
  RUN_TEST(test_dispatcher_unknown_mode_is_a_bad_value);
  RUN_TEST(test_dispatcher_comms_timeout_is_kept_alive_by_any_frame);
  RUN_TEST(test_dispatcher_a_retry_also_keeps_the_link_alive);
  RUN_TEST(test_dispatcher_stop_during_a_job_aborts_it_for_good);
  RUN_TEST(test_dispatcher_estop_latches_and_clears);
  RUN_TEST(test_dispatcher_loads_commits_and_runs_a_job);
  RUN_TEST(test_dispatcher_job_errors_map_to_their_nacks);
  RUN_TEST(test_dispatcher_job_run_checks_mode_and_tunings);
  RUN_TEST(test_dispatcher_refuses_changes_while_a_job_runs);
  RUN_TEST(test_dispatcher_status_report);
  RUN_TEST(test_dispatcher_status_flags_follow_the_state);
  RUN_TEST(test_dispatcher_status_reports_the_comms_lost_flag);
  RUN_TEST(test_dispatcher_status_takes_the_longest_loop_interval);
  RUN_TEST(test_dispatcher_status_carries_the_parser_counters);
  RUN_TEST(test_dispatcher_settings_report);
  RUN_TEST(test_dispatcher_version);
  RUN_TEST(test_dispatcher_role_is_loaded_at_begin);
  RUN_TEST(test_dispatcher_unprogrammed_storage_means_no_role);
  RUN_TEST(test_dispatcher_set_id_saves_only_a_change);
  return UNITY_END();
}

#include <unity.h>
#include "JobRunner.h"

static constexpr uint8_t STEP = 0;
static constexpr uint8_t RAMP = JobProgram::FLAG_RAMP;
static constexpr uint8_t DUTY = JobProgram::FLAG_DUTY;
static constexpr uint32_t MS = 1000UL;
static constexpr float DT_S = 0.01f;

void setUp(void) {}
void tearDown(void) {}

static void assertResult(JobResult expected, JobResult actual) {
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(expected), static_cast<uint8_t>(actual));
}

static void assertState(JobState expected, const JobRunner& runner) {
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(expected), static_cast<uint8_t>(runner.getState()));
}

static void configure(DispenserController& controller) {
  controller.setTunings(1.0f, 0.0f, 0.0f);
}

static void loadSpeedStep(JobProgram& program, float hz, uint32_t durationMs) {
  program.addSegment(0, STEP, hz, durationMs);
  TEST_ASSERT_TRUE(program.commit(1, durationMs) == JobResult::OK);
}

// --- Starting -------------------------------------------------------------

void test_job_runner_idle_reports_the_program_state() {
  JobProgram program;
  DispenserController controller;
  JobRunner runner(program, controller);
  assertState(JobState::EMPTY, runner);
  program.addSegment(0, STEP, 10.0f, 1000);
  assertState(JobState::LOADING, runner);
  program.commit(1, 1000);
  assertState(JobState::READY, runner);
  TEST_ASSERT_FALSE(runner.isRunning());
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, runner.getSegmentIndex());
}

void test_job_runner_start_needs_a_ready_program() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  JobRunner runner(program, controller);
  assertResult(JobResult::WRONG_STATE, runner.start(0));
  program.addSegment(0, STEP, 10.0f, 1000);
  assertResult(JobResult::WRONG_STATE, runner.start(0));
  TEST_ASSERT_FALSE(runner.isRunning());
}

void test_job_runner_start_refuses_while_running() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  assertResult(JobResult::OK, runner.start(0));
  assertResult(JobResult::WRONG_STATE, runner.start(5 * MS));
}

void test_job_runner_start_refuses_during_estop() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  controller.estop();
  JobRunner runner(program, controller);
  assertResult(JobResult::WRONG_STATE, runner.start(0));
  assertState(JobState::READY, runner);
}

void test_job_runner_speed_job_needs_closed_loop() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  controller.setMode(ControlMode::OPEN_LOOP);
  JobRunner runner(program, controller);
  assertResult(JobResult::WRONG_MODE, runner.start(0));
}

void test_job_runner_duty_job_needs_open_loop() {
  JobProgram program;
  DispenserController controller;
  program.addSegment(0, DUTY, 100.0f, 1000);
  program.commit(1, 1000);
  JobRunner runner(program, controller);
  assertResult(JobResult::WRONG_MODE, runner.start(0));
}

void test_job_runner_speed_job_needs_tunings() {
  JobProgram program;
  DispenserController controller;
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  assertResult(JobResult::NOT_CONFIGURED, runner.start(0));
}

void test_job_runner_duty_job_runs_without_tunings() {
  JobProgram program;
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  program.addSegment(0, DUTY, 100.0f, 1000);
  program.commit(1, 1000);
  JobRunner runner(program, controller);
  assertResult(JobResult::OK, runner.start(0));
  TEST_ASSERT_EQUAL_UINT8(100, controller.getDutySetpoint());
}

void test_job_runner_start_applies_the_first_setpoint_at_once() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  assertResult(JobResult::OK, runner.start(0));
  assertState(JobState::RUNNING, runner);
  TEST_ASSERT_EQUAL_FLOAT(50.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT8(0, runner.getSegmentIndex());
  TEST_ASSERT_EQUAL_UINT32(0, runner.getElapsedMs());
}

void test_job_runner_start_clears_comms_lost() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  controller.setCommsTimeoutMs(3000, 0);
  controller.setTargetSpeedHz(10.0f);
  controller.update(0.0f, 3000 * MS, DT_S);
  TEST_ASSERT_TRUE(controller.isCommsLost());

  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  controller.onHostActivity(3001 * MS);
  assertResult(JobResult::OK, runner.start(3001 * MS));
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

// --- Running --------------------------------------------------------------

void test_job_runner_follows_a_ramp_tick_by_tick() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  program.addSegment(0, RAMP, 100.0f, 1000);
  program.commit(1, 1000);
  JobRunner runner(program, controller);
  runner.start(0);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());

  for (uint32_t t = 10; t < 1000; t += 10) {
    runner.tick(t * MS);
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, static_cast<float>(t) / 10.0f, controller.getTargetSpeedHz());
    TEST_ASSERT_EQUAL_UINT32(t, runner.getElapsedMs());
  }
}

void test_job_runner_reports_the_current_segment() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  program.addSegment(0, STEP, 10.0f, 100);
  program.addSegment(1, STEP, 20.0f, 100);
  program.commit(2, 200);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(50 * MS);
  TEST_ASSERT_EQUAL_UINT8(0, runner.getSegmentIndex());
  runner.tick(150 * MS);
  TEST_ASSERT_EQUAL_UINT8(1, runner.getSegmentIndex());
  TEST_ASSERT_EQUAL_FLOAT(20.0f, controller.getTargetSpeedHz());
}

void test_job_runner_done_at_the_total_time() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 100);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(90 * MS);
  assertState(JobState::RUNNING, runner);

  runner.tick(105 * MS);
  assertState(JobState::DONE, runner);
  TEST_ASSERT_FALSE(runner.isRunning());
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT32(100, runner.getElapsedMs());
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, runner.getSegmentIndex());

  runner.tick(200 * MS);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT32(100, runner.getElapsedMs());
}

void test_job_runner_duty_job_rounds_to_the_nearest_duty() {
  JobProgram program;
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  program.addSegment(0, DUTY, 127.6f, 100);
  program.addSegment(1, DUTY, 127.4f, 100);
  program.commit(2, 200);
  JobRunner runner(program, controller);
  runner.start(0);
  TEST_ASSERT_EQUAL_UINT8(128, controller.getDutySetpoint());
  runner.tick(150 * MS);
  TEST_ASSERT_EQUAL_UINT8(127, controller.getDutySetpoint());
}

void test_job_runner_duty_job_ends_with_duty_zero() {
  JobProgram program;
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  program.addSegment(0, DUTY, 255.0f, 100);
  program.commit(1, 100);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(100 * MS);
  assertState(JobState::DONE, runner);
  TEST_ASSERT_EQUAL_UINT8(0, controller.getDutySetpoint());
}

// --- Time -----------------------------------------------------------------

void test_job_runner_a_late_tick_does_not_lose_time() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(10 * MS);
  runner.tick(20 * MS);
  runner.tick(55 * MS);
  TEST_ASSERT_EQUAL_UINT32(55, runner.getElapsedMs());
}

void test_job_runner_keeps_sub_millisecond_remainders() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(10500);
  TEST_ASSERT_EQUAL_UINT32(10, runner.getElapsedMs());
  runner.tick(21000);
  TEST_ASSERT_EQUAL_UINT32(21, runner.getElapsedMs());
}

void test_job_runner_runs_across_a_micros_wrap() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  const uint32_t start = 0xFFFFFFFFUL - 5 * MS;
  runner.start(start);
  runner.tick(static_cast<uint32_t>(start + 10 * MS));
  TEST_ASSERT_EQUAL_UINT32(10, runner.getElapsedMs());
  assertState(JobState::RUNNING, runner);
  runner.tick(static_cast<uint32_t>(start + 1000 * MS));
  assertState(JobState::DONE, runner);
}

void test_job_runner_a_segment_shorter_than_a_tick() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  program.addSegment(0, STEP, 100.0f, 1000);
  program.addSegment(1, STEP, 200.0f, 4);
  program.addSegment(2, STEP, 150.0f, 1000);
  program.commit(3, 2004);

  JobRunner hit(program, controller);
  hit.start(0);
  hit.tick(1002 * MS);
  TEST_ASSERT_EQUAL_FLOAT(200.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT8(1, hit.getSegmentIndex());
  hit.tick(1012 * MS);
  TEST_ASSERT_EQUAL_FLOAT(150.0f, controller.getTargetSpeedHz());
  hit.abort();

  JobRunner miss(program, controller);
  miss.start(0);
  miss.tick(998 * MS);
  TEST_ASSERT_EQUAL_UINT8(0, miss.getSegmentIndex());
  miss.tick(1008 * MS);
  TEST_ASSERT_EQUAL_FLOAT(150.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT8(2, miss.getSegmentIndex());
  miss.tick(2004 * MS);
  assertState(JobState::DONE, miss);
  TEST_ASSERT_EQUAL_UINT32(2004, miss.getElapsedMs());
}

// --- Ending early ---------------------------------------------------------

void test_job_runner_abort_holds_the_elapsed_time() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(300 * MS);
  controller.stop();
  runner.abort();
  assertState(JobState::ABORTED, runner);
  TEST_ASSERT_EQUAL_UINT32(300, runner.getElapsedMs());
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, runner.getSegmentIndex());

  runner.tick(400 * MS);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
}

void test_job_runner_comms_timeout_aborts_and_the_motor_stays_stopped() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  controller.setCommsTimeoutMs(3000, 0);
  loadSpeedStep(program, 50.0f, 10000);
  JobRunner runner(program, controller);
  runner.start(0);

  uint32_t t = 0;
  for (; t < 3000; t += 10) {
    runner.tick(t * MS);
    controller.update(0.0f, t * MS, DT_S);
  }
  runner.tick(t * MS);
  controller.update(0.0f, t * MS, DT_S);
  TEST_ASSERT_TRUE(controller.isCommsLost());

  for (t += 10; t < 3100; t += 10) {
    runner.tick(t * MS);
    TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, t * MS, DT_S));
  }
  assertState(JobState::ABORTED, runner);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_TRUE(controller.isCommsLost());
}

void test_job_runner_estop_between_ticks_aborts() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  runner.start(0);
  controller.estop();
  runner.tick(10 * MS);
  assertState(JobState::ABORTED, runner);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
}

void test_job_runner_a_refused_setpoint_aborts_and_stops() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 1000);
  JobRunner runner(program, controller);
  runner.start(0);
  controller.setMode(ControlMode::OPEN_LOOP);
  runner.tick(10 * MS);
  assertState(JobState::ABORTED, runner);
  TEST_ASSERT_EQUAL_UINT8(0, controller.getDutySetpoint());
}

// --- Again and after ------------------------------------------------------

void test_job_runner_runs_again_after_done_and_after_abort() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 100);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(100 * MS);
  assertState(JobState::DONE, runner);

  assertResult(JobResult::OK, runner.start(200 * MS));
  TEST_ASSERT_EQUAL_UINT32(0, runner.getElapsedMs());
  TEST_ASSERT_EQUAL_FLOAT(50.0f, controller.getTargetSpeedHz());
  runner.abort();
  assertState(JobState::ABORTED, runner);

  assertResult(JobResult::OK, runner.start(300 * MS));
  assertState(JobState::RUNNING, runner);
}

void test_job_runner_clearing_the_program_drops_the_old_outcome() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 100);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(100 * MS);
  assertState(JobState::DONE, runner);

  runner.reset();
  program.clear();
  assertState(JobState::EMPTY, runner);
  TEST_ASSERT_EQUAL_UINT32(0, runner.getElapsedMs());

  loadSpeedStep(program, 20.0f, 300);
  assertState(JobState::READY, runner);
  TEST_ASSERT_EQUAL_UINT32(0, runner.getElapsedMs());
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, runner.getSegmentIndex());

  assertResult(JobResult::OK, runner.start(500 * MS));
  runner.tick(510 * MS);
  TEST_ASSERT_EQUAL_FLOAT(20.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT32(10, runner.getElapsedMs());
}

void test_job_runner_reset_after_abort_reports_the_next_job_ready() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 100);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(40 * MS);
  runner.abort();
  assertState(JobState::ABORTED, runner);

  runner.reset();
  program.clear();
  loadSpeedStep(program, 20.0f, 300);
  assertState(JobState::READY, runner);
  TEST_ASSERT_EQUAL_UINT32(0, runner.getElapsedMs());
}

void test_job_runner_reset_while_running_does_nothing() {
  JobProgram program;
  DispenserController controller;
  configure(controller);
  loadSpeedStep(program, 50.0f, 100);
  JobRunner runner(program, controller);
  runner.start(0);
  runner.tick(40 * MS);

  runner.reset();
  assertState(JobState::RUNNING, runner);
  TEST_ASSERT_EQUAL_UINT32(40, runner.getElapsedMs());
  runner.tick(100 * MS);
  assertState(JobState::DONE, runner);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_job_runner_idle_reports_the_program_state);
  RUN_TEST(test_job_runner_start_needs_a_ready_program);
  RUN_TEST(test_job_runner_start_refuses_while_running);
  RUN_TEST(test_job_runner_start_refuses_during_estop);
  RUN_TEST(test_job_runner_speed_job_needs_closed_loop);
  RUN_TEST(test_job_runner_duty_job_needs_open_loop);
  RUN_TEST(test_job_runner_speed_job_needs_tunings);
  RUN_TEST(test_job_runner_duty_job_runs_without_tunings);
  RUN_TEST(test_job_runner_start_applies_the_first_setpoint_at_once);
  RUN_TEST(test_job_runner_start_clears_comms_lost);
  RUN_TEST(test_job_runner_follows_a_ramp_tick_by_tick);
  RUN_TEST(test_job_runner_reports_the_current_segment);
  RUN_TEST(test_job_runner_done_at_the_total_time);
  RUN_TEST(test_job_runner_duty_job_rounds_to_the_nearest_duty);
  RUN_TEST(test_job_runner_duty_job_ends_with_duty_zero);
  RUN_TEST(test_job_runner_a_late_tick_does_not_lose_time);
  RUN_TEST(test_job_runner_keeps_sub_millisecond_remainders);
  RUN_TEST(test_job_runner_runs_across_a_micros_wrap);
  RUN_TEST(test_job_runner_a_segment_shorter_than_a_tick);
  RUN_TEST(test_job_runner_abort_holds_the_elapsed_time);
  RUN_TEST(test_job_runner_comms_timeout_aborts_and_the_motor_stays_stopped);
  RUN_TEST(test_job_runner_estop_between_ticks_aborts);
  RUN_TEST(test_job_runner_a_refused_setpoint_aborts_and_stops);
  RUN_TEST(test_job_runner_runs_again_after_done_and_after_abort);
  RUN_TEST(test_job_runner_clearing_the_program_drops_the_old_outcome);
  RUN_TEST(test_job_runner_reset_after_abort_reports_the_next_job_ready);
  RUN_TEST(test_job_runner_reset_while_running_does_nothing);
  return UNITY_END();
}

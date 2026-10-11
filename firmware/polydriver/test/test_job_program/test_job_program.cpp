#include <unity.h>
#include <limits>
#include "JobProgram.h"

static constexpr uint8_t STEP = 0;
static constexpr uint8_t RAMP = JobProgram::FLAG_RAMP;
static constexpr uint8_t DUTY = JobProgram::FLAG_DUTY;

void setUp(void) {}
void tearDown(void) {}

static void assertResult(JobResult expected, JobResult actual) {
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(expected), static_cast<uint8_t>(actual));
}

static void assertState(JobState expected, const JobProgram& program) {
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(expected), static_cast<uint8_t>(program.getState()));
}

// --- Loading --------------------------------------------------------------

void test_job_program_starts_empty() {
  JobProgram program;
  assertState(JobState::EMPTY, program);
  TEST_ASSERT_EQUAL_UINT8(0, program.getCount());
  TEST_ASSERT_EQUAL_UINT32(0, program.getTotalMs());
}

void test_job_program_load_and_commit() {
  JobProgram program;
  assertResult(JobResult::OK, program.addSegment(0, STEP, 10.0f, 1000));
  assertState(JobState::LOADING, program);
  assertResult(JobResult::OK, program.addSegment(1, RAMP, 20.0f, 500));
  TEST_ASSERT_EQUAL_UINT8(2, program.getCount());
  TEST_ASSERT_EQUAL_UINT32(1500, program.getTotalMs());

  assertResult(JobResult::OK, program.commit(2, 1500));
  assertState(JobState::READY, program);
  TEST_ASSERT_FALSE(program.isDutyJob());
  TEST_ASSERT_EQUAL_FLOAT(20.0f, program.getSegment(1).value);
  TEST_ASSERT_EQUAL_UINT32(500, program.getSegment(1).durationMs);
  TEST_ASSERT_EQUAL_UINT8(RAMP, program.getSegment(1).flags);
}

void test_job_program_duty_job_is_recognised() {
  JobProgram program;
  program.addSegment(0, DUTY, 100.0f, 1000);
  program.addSegment(1, DUTY | RAMP, 255.0f, 1000);
  assertResult(JobResult::OK, program.commit(2, 2000));
  TEST_ASSERT_TRUE(program.isDutyJob());
}

void test_job_program_index_must_be_the_next_one() {
  JobProgram program;
  assertResult(JobResult::BAD_INDEX, program.addSegment(1, STEP, 10.0f, 1000));
  program.addSegment(0, STEP, 10.0f, 1000);
  assertResult(JobResult::BAD_INDEX, program.addSegment(0, STEP, 10.0f, 1000));
  assertResult(JobResult::BAD_INDEX, program.addSegment(2, STEP, 10.0f, 1000));
  TEST_ASSERT_EQUAL_UINT8(1, program.getCount());
}

void test_job_program_limit_is_32_segments() {
  JobProgram program;
  for (uint8_t i = 0; i < JobProgram::MAX_SEGMENTS; i++) {
    assertResult(JobResult::OK, program.addSegment(i, STEP, 1.0f, 10));
  }
  assertResult(JobResult::JOB_FULL, program.addSegment(32, STEP, 1.0f, 10));
  TEST_ASSERT_EQUAL_UINT8(32, program.getCount());
}

void test_job_program_index_past_the_limit_is_full_not_bad_index() {
  JobProgram program;
  program.addSegment(0, STEP, 1.0f, 10);
  assertResult(JobResult::JOB_FULL, program.addSegment(40, STEP, 1.0f, 10));
}

void test_job_program_rejects_bad_values() {
  JobProgram program;
  const float nan = std::numeric_limits<float>::quiet_NaN();
  const float inf = std::numeric_limits<float>::infinity();
  assertResult(JobResult::BAD_VALUE, program.addSegment(0, 0x04, 10.0f, 1000));
  assertResult(JobResult::BAD_VALUE, program.addSegment(0, STEP, -1.0f, 1000));
  assertResult(JobResult::BAD_VALUE, program.addSegment(0, STEP, nan, 1000));
  assertResult(JobResult::BAD_VALUE, program.addSegment(0, STEP, inf, 1000));
  assertResult(JobResult::BAD_VALUE, program.addSegment(0, DUTY, 255.5f, 1000));
  assertResult(JobResult::BAD_VALUE, program.addSegment(0, STEP, 10.0f, 0));
  assertState(JobState::EMPTY, program);
  TEST_ASSERT_EQUAL_UINT8(0, program.getCount());
}

void test_job_program_speed_above_255_is_allowed() {
  JobProgram program;
  assertResult(JobResult::OK, program.addSegment(0, STEP, 300.0f, 1000));
}

void test_job_program_short_segments_are_accepted() {
  JobProgram program;
  assertResult(JobResult::OK, program.addSegment(0, STEP, 10.0f, 1));
}

void test_job_program_rejects_a_total_time_overflow() {
  JobProgram program;
  assertResult(JobResult::OK, program.addSegment(0, STEP, 1.0f, 0xFFFFFFF0UL));
  assertResult(JobResult::BAD_VALUE, program.addSegment(1, STEP, 1.0f, 0x10));
  assertResult(JobResult::OK, program.addSegment(1, STEP, 1.0f, 0x0F));
  TEST_ASSERT_EQUAL_UINT32(0xFFFFFFFFUL, program.getTotalMs());
}

void test_job_program_commit_refuses_a_mismatch_and_stays_loading() {
  JobProgram program;
  program.addSegment(0, STEP, 10.0f, 1000);
  program.addSegment(1, STEP, 20.0f, 1000);
  assertResult(JobResult::BAD_JOB, program.commit(3, 2000));
  assertResult(JobResult::BAD_JOB, program.commit(2, 1999));
  assertState(JobState::LOADING, program);
  assertResult(JobResult::OK, program.commit(2, 2000));
}

void test_job_program_commit_refuses_mixed_segment_types() {
  JobProgram program;
  program.addSegment(0, STEP, 10.0f, 1000);
  program.addSegment(1, DUTY, 100.0f, 1000);
  assertResult(JobResult::BAD_JOB, program.commit(2, 2000));
  assertState(JobState::LOADING, program);
}

void test_job_program_commit_needs_loading() {
  JobProgram program;
  assertResult(JobResult::WRONG_STATE, program.commit(0, 0));
  program.addSegment(0, STEP, 10.0f, 1000);
  program.commit(1, 1000);
  assertResult(JobResult::WRONG_STATE, program.commit(1, 1000));
}

void test_job_program_ready_refuses_more_segments_until_cleared() {
  JobProgram program;
  program.addSegment(0, STEP, 10.0f, 1000);
  program.commit(1, 1000);
  assertResult(JobResult::WRONG_STATE, program.addSegment(1, STEP, 10.0f, 1000));

  program.clear();
  assertState(JobState::EMPTY, program);
  TEST_ASSERT_EQUAL_UINT32(0, program.getTotalMs());
  assertResult(JobResult::OK, program.addSegment(0, STEP, 5.0f, 100));
}

// --- Setpoints ------------------------------------------------------------

void test_job_program_step_holds_its_value() {
  JobProgram program;
  program.addSegment(0, STEP, 10.0f, 1000);
  program.addSegment(1, STEP, 20.0f, 1000);
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(10.0f, program.setpointAt(0, index));
  TEST_ASSERT_EQUAL_UINT8(0, index);
  TEST_ASSERT_EQUAL_FLOAT(10.0f, program.setpointAt(999, index));
  TEST_ASSERT_EQUAL_FLOAT(20.0f, program.setpointAt(1500, index));
  TEST_ASSERT_EQUAL_UINT8(1, index);
}

void test_job_program_boundary_belongs_to_the_next_segment() {
  JobProgram program;
  program.addSegment(0, STEP, 10.0f, 1000);
  program.addSegment(1, STEP, 20.0f, 1000);
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(20.0f, program.setpointAt(1000, index));
  TEST_ASSERT_EQUAL_UINT8(1, index);
}

void test_job_program_first_ramp_starts_from_zero() {
  JobProgram program;
  program.addSegment(0, RAMP, 100.0f, 1000);
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, program.setpointAt(0, index));
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 25.0f, program.setpointAt(250, index));
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 50.0f, program.setpointAt(500, index));
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 99.9f, program.setpointAt(999, index));
}

void test_job_program_ramp_starts_from_the_previous_value() {
  JobProgram program;
  program.addSegment(0, STEP, 40.0f, 1000);
  program.addSegment(1, RAMP, 20.0f, 1000);
  program.addSegment(2, RAMP, 60.0f, 2000);
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(40.0f, program.setpointAt(1000, index));
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 30.0f, program.setpointAt(1500, index));
  TEST_ASSERT_EQUAL_UINT8(1, index);
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 20.0f, program.setpointAt(2000, index));
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 40.0f, program.setpointAt(3000, index));
  TEST_ASSERT_EQUAL_UINT8(2, index);
}

void test_job_program_past_the_end_is_zero_and_no_segment() {
  JobProgram program;
  program.addSegment(0, STEP, 10.0f, 1000);
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, program.setpointAt(1000, index));
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, index);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, program.setpointAt(0xFFFFFFFFUL, index));
}

void test_job_program_empty_program_has_no_setpoint() {
  JobProgram program;
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, program.setpointAt(0, index));
  TEST_ASSERT_EQUAL_UINT8(JobProgram::NO_SEGMENT, index);
}

void test_job_program_short_segment_is_found_inside_its_window() {
  JobProgram program;
  program.addSegment(0, STEP, 100.0f, 1000);
  program.addSegment(1, STEP, 200.0f, 4);
  program.addSegment(2, STEP, 150.0f, 1000);
  uint8_t index = 0;
  TEST_ASSERT_EQUAL_FLOAT(200.0f, program.setpointAt(1002, index));
  TEST_ASSERT_EQUAL_UINT8(1, index);
  TEST_ASSERT_EQUAL_FLOAT(150.0f, program.setpointAt(1004, index));
  TEST_ASSERT_EQUAL_UINT8(2, index);
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_job_program_starts_empty);
  RUN_TEST(test_job_program_load_and_commit);
  RUN_TEST(test_job_program_duty_job_is_recognised);
  RUN_TEST(test_job_program_index_must_be_the_next_one);
  RUN_TEST(test_job_program_limit_is_32_segments);
  RUN_TEST(test_job_program_index_past_the_limit_is_full_not_bad_index);
  RUN_TEST(test_job_program_rejects_bad_values);
  RUN_TEST(test_job_program_speed_above_255_is_allowed);
  RUN_TEST(test_job_program_short_segments_are_accepted);
  RUN_TEST(test_job_program_rejects_a_total_time_overflow);
  RUN_TEST(test_job_program_commit_refuses_a_mismatch_and_stays_loading);
  RUN_TEST(test_job_program_commit_refuses_mixed_segment_types);
  RUN_TEST(test_job_program_commit_needs_loading);
  RUN_TEST(test_job_program_ready_refuses_more_segments_until_cleared);
  RUN_TEST(test_job_program_step_holds_its_value);
  RUN_TEST(test_job_program_boundary_belongs_to_the_next_segment);
  RUN_TEST(test_job_program_first_ramp_starts_from_zero);
  RUN_TEST(test_job_program_ramp_starts_from_the_previous_value);
  RUN_TEST(test_job_program_past_the_end_is_zero_and_no_segment);
  RUN_TEST(test_job_program_empty_program_has_no_setpoint);
  RUN_TEST(test_job_program_short_segment_is_found_inside_its_window);
  return UNITY_END();
}

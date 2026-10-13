#include <unity.h>
#include <limits>
#include <float.h>
#include "PIDRegulator.h"

void setUp(void) {}
void tearDown(void) {}

void test_pid_default_gains_are_zero() {
  PIDRegulator pid;
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.getKp());
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.getKi());
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.getKd());
}

void test_pid_unconfigured_never_drives_the_output() {
  PIDRegulator pid;
  for (int i = 0; i < 100; ++i) {
    TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(100.0f, static_cast<float>(i % 7), 0.01f));
  }
}

void test_pid_zero_error() {
  PIDRegulator pid(1.0f, 0.1f, 0.05f);
  float output = pid.calculate(100.0f, 100.0f, 1.0f);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
}

void test_pid_proportional_term() {
  PIDRegulator pid(1.0f, 0.0f, 0.0f);
  float output = pid.calculate(100.0f, 90.0f, 1.0f);
  TEST_ASSERT_EQUAL_FLOAT(10.0f, output);
}

void test_pid_integral_term() {
  PIDRegulator pid(0.0f, 1.0f, 0.0f);
  float output1 = pid.calculate(100.0f, 90.0f, 1.0f);
  float output2 = pid.calculate(100.0f, 90.0f, 1.0f);
  TEST_ASSERT_EQUAL_FLOAT(10.0f, output1);
  TEST_ASSERT_EQUAL_FLOAT(20.0f, output2);
}

void test_pid_integral_is_clamped_to_the_output_range() {
  PIDRegulator pid(0.0f, 1.0f, 0.0f);
  float output = 0.0f;
  for (int i = 0; i < 100; ++i) {
    output = pid.calculate(100.0f, 90.0f, 1.0f);
  }
  TEST_ASSERT_EQUAL_FLOAT(255.0f, output);
}

void test_pid_derivative_term() {
  PIDRegulator pid(0.0f, 0.0f, 1.0f);
  float output1 = pid.calculate(100.0f, 90.0f, 1.0f);
  float output2 = pid.calculate(100.0f, 85.0f, 1.0f);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, output1);
  TEST_ASSERT_EQUAL_FLOAT(5.0f, output2);
}

void test_pid_derivative_uses_dt() {
  PIDRegulator pid(0.0f, 0.0f, 0.1f);
  pid.calculate(100.0f, 90.0f, 0.01f);
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 10.0f, pid.calculate(100.0f, 89.0f, 0.01f));
}

void test_pid_setpoint_step_causes_no_derivative_kick() {
  PIDRegulator pid(0.0f, 0.0f, 0.1f);
  pid.calculate(100.0f, 100.0f, 0.01f);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(200.0f, 100.0f, 0.01f));
}

void test_pid_start_from_rest_causes_no_derivative_kick() {
  PIDRegulator pid(0.0f, 0.0f, 0.1f);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(100.0f, 0.0f, 0.01f));
}

void test_pid_zero_dt() {
  PIDRegulator pid(1.0f, 1.0f, 1.0f);
  float output = pid.calculate(100.0f, 90.0f, 0.0f);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
}

void test_pid_lower_bound_clamping() {
  PIDRegulator pid(1.0f, 1.0f, 1.0f);
  float output = pid.calculate(90.0f, 100.0f, 1.0f);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
}

void test_pid_set_tunings() {
  PIDRegulator pid;
  TEST_ASSERT_TRUE(pid.setTunings(1.5f, 2.5f, 3.5f));
  TEST_ASSERT_EQUAL_FLOAT(1.5f, pid.getKp());
  TEST_ASSERT_EQUAL_FLOAT(2.5f, pid.getKi());
  TEST_ASSERT_EQUAL_FLOAT(3.5f, pid.getKd());
}

void test_pid_rejects_negative_nan_or_infinite_gains_and_keeps_the_old_ones() {
  const float nan = std::numeric_limits<float>::quiet_NaN();
  PIDRegulator pid(1.0f, 2.0f, 3.0f);
  TEST_ASSERT_FALSE(pid.setTunings(-0.1f, 2.0f, 3.0f));
  TEST_ASSERT_FALSE(pid.setTunings(1.0f, -0.1f, 3.0f));
  TEST_ASSERT_FALSE(pid.setTunings(1.0f, 2.0f, -0.1f));
  TEST_ASSERT_FALSE(pid.setTunings(nan, 2.0f, 3.0f));
  TEST_ASSERT_FALSE(pid.setTunings(1.0f, nan, 3.0f));
  TEST_ASSERT_FALSE(pid.setTunings(1.0f, 2.0f, nan));
  const float inf = std::numeric_limits<float>::infinity();
  TEST_ASSERT_FALSE(pid.setTunings(inf, 2.0f, 3.0f));
  TEST_ASSERT_FALSE(pid.setTunings(1.0f, inf, 3.0f));
  TEST_ASSERT_FALSE(pid.setTunings(1.0f, 2.0f, inf));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, pid.getKp());
  TEST_ASSERT_EQUAL_FLOAT(2.0f, pid.getKi());
  TEST_ASSERT_EQUAL_FLOAT(3.0f, pid.getKd());
}

void test_pid_accepts_all_zero_gains() {
  PIDRegulator pid(1.0f, 2.0f, 3.0f);
  TEST_ASSERT_TRUE(pid.setTunings(0.0f, 0.0f, 0.0f));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(100.0f, 0.0f, 0.01f));
}

void test_pid_feedforward_alone_sets_the_output() {
  PIDRegulator pid;
  TEST_ASSERT_EQUAL_FLOAT(128.0f, pid.calculate(50.0f, 50.0f, 0.01f, 128.0f));
  TEST_ASSERT_EQUAL_FLOAT(255.0f, pid.calculate(50.0f, 50.0f, 0.01f, 300.0f));
}

void test_pid_integral_can_pull_below_the_feedforward() {
  PIDRegulator pid(0.0f, 1.0f, 0.0f);
  TEST_ASSERT_EQUAL_FLOAT(90.0f, pid.calculate(100.0f, 110.0f, 1.0f, 100.0f));
}

void test_pid_no_windup_while_saturated_high() {
  PIDRegulator pid(0.0f, 1.0f, 0.0f);
  for (int i = 0; i < 10; ++i) {
    TEST_ASSERT_EQUAL_FLOAT(255.0f, pid.calculate(100.0f, 0.0f, 1.0f, 250.0f));
  }
  // The integral only grew to the 5 that 255 needed, so the first negative error acts at once.
  TEST_ASSERT_EQUAL_FLOAT(245.0f, pid.calculate(100.0f, 110.0f, 1.0f, 250.0f));
}

void test_pid_no_windup_while_saturated_low() {
  PIDRegulator pid(0.0f, 1.0f, 0.0f);
  for (int i = 0; i < 10; ++i) {
    TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(100.0f, 110.0f, 1.0f));
  }
  TEST_ASSERT_EQUAL_FLOAT(10.0f, pid.calculate(100.0f, 90.0f, 1.0f));
}

void test_pid_a_large_p_term_does_not_drain_the_integral() {
  PIDRegulator pid(1.0f, 1.0f, 0.0f);
  TEST_ASSERT_EQUAL_FLOAT(20.0f, pid.calculate(100.0f, 90.0f, 1.0f));
  TEST_ASSERT_EQUAL_FLOAT(255.0f, pid.calculate(400.0f, 90.0f, 1.0f));
  // The integral kept its 10 while P alone saturated: 10 + 10 + 10.
  TEST_ASSERT_EQUAL_FLOAT(30.0f, pid.calculate(100.0f, 90.0f, 1.0f));
}

void test_pid_a_large_negative_p_term_does_not_fill_the_integral() {
  PIDRegulator pid(1.0f, 1.0f, 0.0f);
  TEST_ASSERT_EQUAL_FLOAT(180.0f, pid.calculate(100.0f, 110.0f, 1.0f, 200.0f));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(100.0f, 400.0f, 1.0f, 200.0f));
  // The integral kept its -10 while P alone pulled the output to 0: 200 - 10 - 20.
  TEST_ASSERT_EQUAL_FLOAT(170.0f, pid.calculate(100.0f, 110.0f, 1.0f, 200.0f));
}

void test_pid_negative_integral_is_limited() {
  PIDRegulator pid(0.0f, 1.0f, 0.0f);
  for (int i = 0; i < 100; ++i) pid.calculate(0.0f, 10.0f, 1.0f, 255.0f);
  // Integral stops at -255: one step back up gives 255 - 255 + 10.
  TEST_ASSERT_EQUAL_FLOAT(10.0f, pid.calculate(10.0f, 0.0f, 1.0f, 255.0f));
}

void test_pid_output_stays_a_number_in_range_even_with_extreme_gains() {
  PIDRegulator pid(FLT_MAX, 0.0f, FLT_MAX);
  pid.calculate(0.0f, 0.0f, 0.01f);
  // P = +inf, D = -inf: their sum is NaN, which must not reach the duty.
  const float output = pid.calculate(1.0e30f, 1.0e30f * 0.5f, 0.01f);
  TEST_ASSERT_FALSE(output != output);
  TEST_ASSERT_TRUE(output >= 0.0f && output <= 255.0f);
  const float next = pid.calculate(10.0f, 10.0f, 0.01f);
  TEST_ASSERT_FALSE(next != next);
  TEST_ASSERT_TRUE(next >= 0.0f && next <= 255.0f);
}

void test_pid_reset() {
  PIDRegulator pid(0.0f, 1.0f, 1.0f);
  pid.calculate(100.0f, 90.0f, 1.0f);
  pid.reset();
  float output = pid.calculate(100.0f, 90.0f, 1.0f);
  TEST_ASSERT_EQUAL_FLOAT(10.0f, output);
}

void test_pid_reset_forgets_the_previous_measurement() {
  PIDRegulator pid(0.0f, 0.0f, 1.0f);
  pid.calculate(100.0f, 90.0f, 1.0f);
  pid.reset();
  TEST_ASSERT_EQUAL_FLOAT(0.0f, pid.calculate(100.0f, 50.0f, 1.0f));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_pid_default_gains_are_zero);
  RUN_TEST(test_pid_unconfigured_never_drives_the_output);
  RUN_TEST(test_pid_zero_error);
  RUN_TEST(test_pid_proportional_term);
  RUN_TEST(test_pid_integral_term);
  RUN_TEST(test_pid_integral_is_clamped_to_the_output_range);
  RUN_TEST(test_pid_derivative_term);
  RUN_TEST(test_pid_derivative_uses_dt);
  RUN_TEST(test_pid_setpoint_step_causes_no_derivative_kick);
  RUN_TEST(test_pid_start_from_rest_causes_no_derivative_kick);
  RUN_TEST(test_pid_zero_dt);
  RUN_TEST(test_pid_lower_bound_clamping);
  RUN_TEST(test_pid_set_tunings);
  RUN_TEST(test_pid_rejects_negative_nan_or_infinite_gains_and_keeps_the_old_ones);
  RUN_TEST(test_pid_accepts_all_zero_gains);
  RUN_TEST(test_pid_feedforward_alone_sets_the_output);
  RUN_TEST(test_pid_integral_can_pull_below_the_feedforward);
  RUN_TEST(test_pid_no_windup_while_saturated_high);
  RUN_TEST(test_pid_no_windup_while_saturated_low);
  RUN_TEST(test_pid_a_large_p_term_does_not_drain_the_integral);
  RUN_TEST(test_pid_a_large_negative_p_term_does_not_fill_the_integral);
  RUN_TEST(test_pid_negative_integral_is_limited);
  RUN_TEST(test_pid_output_stays_a_number_in_range_even_with_extreme_gains);
  RUN_TEST(test_pid_reset);
  RUN_TEST(test_pid_reset_forgets_the_previous_measurement);
  return UNITY_END();
}

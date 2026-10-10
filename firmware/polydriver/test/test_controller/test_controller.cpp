#include <unity.h>
#include <limits>
#include "DispenserController.h"

static constexpr float DT_S = 0.01f;
static constexpr uint16_t TIMEOUT_MS = 3000;
static constexpr uint32_t TIMEOUT_US = 3000000UL;

void setUp(void) {}
void tearDown(void) {}

static void configure(DispenserController& controller) {
  TEST_ASSERT_TRUE(controller.setTunings(1.0f, 0.0f, 0.0f) == ControlResult::OK);
}

static void assertResult(ControlResult expected, ControlResult actual) {
  TEST_ASSERT_EQUAL_UINT8(static_cast<uint8_t>(expected), static_cast<uint8_t>(actual));
}

// --- Boot state -------------------------------------------------------------

void test_controller_boots_idle_unconfigured_closed_loop() {
  DispenserController controller;
  TEST_ASSERT_FALSE(controller.isRegulating());
  TEST_ASSERT_FALSE(controller.isConfigured());
  TEST_ASSERT_FALSE(controller.isEstopped());
  TEST_ASSERT_FALSE(controller.isCommsLost());
  TEST_ASSERT_TRUE(controller.getMode() == ControlMode::CLOSED_LOOP);
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getKp());
  TEST_ASSERT_EQUAL_FLOAT(1.0f, controller.getAlpha());
  TEST_ASSERT_EQUAL_UINT16(0, controller.getCommsTimeoutMs());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));
}

// --- Configuration (UNCONFIGURED) -------------------------------------------

void test_controller_refuses_a_positive_target_before_tunings() {
  DispenserController controller;
  assertResult(ControlResult::NOT_CONFIGURED, controller.setTargetSpeedHz(100.0f));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_FALSE(controller.isRegulating());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));
}

void test_controller_accepts_a_zero_target_before_tunings() {
  DispenserController controller;
  assertResult(ControlResult::OK, controller.setTargetSpeedHz(0.0f));
}

void test_controller_first_accepted_tunings_configure_it() {
  DispenserController controller;
  assertResult(ControlResult::OK, controller.setTunings(1.5f, 0.25f, 0.05f));
  TEST_ASSERT_TRUE(controller.isConfigured());
  TEST_ASSERT_EQUAL_FLOAT(1.5f, controller.getKp());
  TEST_ASSERT_EQUAL_FLOAT(0.25f, controller.getKi());
  TEST_ASSERT_EQUAL_FLOAT(0.05f, controller.getKd());
}

void test_controller_rejected_tunings_do_not_configure_it() {
  DispenserController controller;
  assertResult(ControlResult::BAD_VALUE, controller.setTunings(-1.0f, 0.0f, 0.0f));
  assertResult(ControlResult::BAD_VALUE,
               controller.setTunings(std::numeric_limits<float>::quiet_NaN(), 0.0f, 0.0f));
  TEST_ASSERT_FALSE(controller.isConfigured());
}

void test_controller_all_zero_tunings_configure_it() {
  DispenserController controller;
  assertResult(ControlResult::OK, controller.setTunings(0.0f, 0.0f, 0.0f));
  TEST_ASSERT_TRUE(controller.isConfigured());
  assertResult(ControlResult::OK, controller.setTargetSpeedHz(100.0f));
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));
}

// --- Closed loop --------------------------------------------------------------

void test_controller_arms_on_a_positive_target() {
  DispenserController controller;
  configure(controller);
  assertResult(ControlResult::OK, controller.setTargetSpeedHz(100.0f));
  TEST_ASSERT_TRUE(controller.isRegulating());
  TEST_ASSERT_EQUAL_FLOAT(100.0f, controller.getTargetSpeedHz());
}

void test_controller_rejects_a_negative_or_nan_target() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(50.0f);
  assertResult(ControlResult::BAD_VALUE, controller.setTargetSpeedHz(-1.0f));
  assertResult(ControlResult::BAD_VALUE,
               controller.setTargetSpeedHz(std::numeric_limits<float>::quiet_NaN()));
  TEST_ASSERT_EQUAL_FLOAT(50.0f, controller.getTargetSpeedHz());
}

void test_controller_drives_output_up_when_below_setpoint() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(100.0f);
  uint8_t duty = controller.update(0.0f, 0, DT_S);
  TEST_ASSERT_EQUAL_UINT8(100, duty);
  TEST_ASSERT_EQUAL_UINT8(duty, controller.getOutputDuty());
}

void test_controller_output_is_clamped_to_the_pwm_range() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(1000.0f);
  TEST_ASSERT_EQUAL_UINT8(255, controller.update(0.0f, 0, DT_S));
}

void test_controller_zero_target_disarms_and_commands_zero() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(100.0f);
  controller.update(0.0f, 0, DT_S);

  controller.setTargetSpeedHz(0.0f);
  TEST_ASSERT_FALSE(controller.isRegulating());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 10000, DT_S));
  TEST_ASSERT_EQUAL_UINT8(0, controller.getOutputDuty());
}

void test_controller_zero_target_resets_the_integral() {
  DispenserController controller;
  controller.setTunings(0.0f, 1.0f, 0.0f);
  controller.setTargetSpeedHz(100.0f);
  for (int i = 0; i < 10; ++i) controller.update(0.0f, 0, DT_S);

  controller.setTargetSpeedHz(0.0f);
  controller.setTargetSpeedHz(100.0f);
  // One fresh step: ki * error * dt = 1 * 100 * 0.01.
  TEST_ASSERT_EQUAL_UINT8(1, controller.update(0.0f, 0, DT_S));
}

void test_controller_exposes_the_filtered_measurement() {
  DispenserController controller;
  configure(controller);
  assertResult(ControlResult::OK, controller.setAlpha(0.2f));
  controller.setTargetSpeedHz(100.0f);

  controller.update(50.0f, 0, DT_S);
  TEST_ASSERT_EQUAL_FLOAT(50.0f, controller.getFilteredFrequencyHz());

  controller.update(100.0f, 10000, DT_S);
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 60.0f, controller.getFilteredFrequencyHz());
}

void test_controller_rejects_an_invalid_alpha() {
  DispenserController controller;
  assertResult(ControlResult::BAD_VALUE, controller.setAlpha(0.0f));
  assertResult(ControlResult::BAD_VALUE, controller.setAlpha(1.5f));
  TEST_ASSERT_EQUAL_FLOAT(1.0f, controller.getAlpha());
}

// --- Open loop and modes ------------------------------------------------------

void test_controller_open_loop_outputs_the_duty_without_tunings() {
  DispenserController controller;
  assertResult(ControlResult::OK, controller.setMode(ControlMode::OPEN_LOOP));
  assertResult(ControlResult::OK, controller.setDuty(120));
  TEST_ASSERT_EQUAL_UINT8(120, controller.getDutySetpoint());
  TEST_ASSERT_EQUAL_UINT8(120, controller.update(0.0f, 0, DT_S));
  TEST_ASSERT_FALSE(controller.isRegulating());
}

void test_controller_duty_is_refused_in_closed_loop() {
  DispenserController controller;
  assertResult(ControlResult::WRONG_MODE, controller.setDuty(120));
  TEST_ASSERT_EQUAL_UINT8(0, controller.getDutySetpoint());
}

void test_controller_target_is_refused_in_open_loop() {
  DispenserController controller;
  configure(controller);
  controller.setMode(ControlMode::OPEN_LOOP);
  assertResult(ControlResult::WRONG_MODE, controller.setTargetSpeedHz(100.0f));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
}

void test_controller_switching_mode_stops_and_resets() {
  DispenserController controller;
  controller.setTunings(0.0f, 1.0f, 0.0f);
  controller.setTargetSpeedHz(100.0f);
  for (int i = 0; i < 10; ++i) controller.update(0.0f, 0, DT_S);

  assertResult(ControlResult::OK, controller.setMode(ControlMode::OPEN_LOOP));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));

  controller.setDuty(200);
  assertResult(ControlResult::OK, controller.setMode(ControlMode::CLOSED_LOOP));
  TEST_ASSERT_EQUAL_UINT8(0, controller.getDutySetpoint());

  controller.setTargetSpeedHz(100.0f);
  TEST_ASSERT_EQUAL_UINT8(1, controller.update(0.0f, 0, DT_S));
}

void test_controller_rejects_an_unknown_mode() {
  DispenserController controller;
  assertResult(ControlResult::BAD_VALUE, controller.setMode(static_cast<ControlMode>(7)));
  TEST_ASSERT_TRUE(controller.getMode() == ControlMode::CLOSED_LOOP);
}

// --- STOP and ESTOP -----------------------------------------------------------

void test_controller_stop_zeroes_target_and_duty_and_keeps_the_mode() {
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  controller.setDuty(150);
  controller.stop();
  TEST_ASSERT_EQUAL_UINT8(0, controller.getDutySetpoint());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));
  TEST_ASSERT_TRUE(controller.getMode() == ControlMode::OPEN_LOOP);

  DispenserController closed;
  configure(closed);
  closed.setTargetSpeedHz(100.0f);
  closed.stop();
  TEST_ASSERT_FALSE(closed.isRegulating());
  TEST_ASSERT_EQUAL_UINT8(0, closed.update(0.0f, 0, DT_S));
}

void test_controller_estop_stops_and_latches() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(100.0f);
  controller.estop();
  TEST_ASSERT_TRUE(controller.isEstopped());
  TEST_ASSERT_FALSE(controller.isRegulating());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));

  assertResult(ControlResult::WRONG_STATE, controller.setTargetSpeedHz(100.0f));
  assertResult(ControlResult::WRONG_STATE, controller.setMode(ControlMode::OPEN_LOOP));
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 10000, DT_S));
}

void test_controller_estop_refuses_duty_in_open_loop() {
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  controller.setDuty(150);
  controller.estop();
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));
  assertResult(ControlResult::WRONG_STATE, controller.setDuty(150));
}

void test_controller_tunings_are_accepted_during_estop() {
  DispenserController controller;
  controller.estop();
  assertResult(ControlResult::OK, controller.setTunings(1.0f, 0.0f, 0.0f));
  assertResult(ControlResult::OK, controller.setAlpha(0.5f));
}

void test_controller_clear_estop_releases_but_leaves_the_motor_stopped() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(100.0f);
  controller.estop();
  controller.clearEstop();
  TEST_ASSERT_FALSE(controller.isEstopped());
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getTargetSpeedHz());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, 0, DT_S));

  assertResult(ControlResult::OK, controller.setTargetSpeedHz(100.0f));
  TEST_ASSERT_EQUAL_UINT8(100, controller.update(0.0f, 10000, DT_S));
}

// --- Comms timeout ------------------------------------------------------------

void test_controller_comms_timeout_is_off_by_default() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(100.0f);
  TEST_ASSERT_EQUAL_UINT8(100, controller.update(0.0f, TIMEOUT_US * 100, DT_S));
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_stops_when_the_host_goes_silent() {
  DispenserController controller;
  configure(controller);
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.setTargetSpeedHz(100.0f);

  TEST_ASSERT_TRUE(controller.update(0.0f, TIMEOUT_US - 10000, DT_S) > 0);
  TEST_ASSERT_FALSE(controller.isCommsLost());

  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, TIMEOUT_US, DT_S));
  TEST_ASSERT_TRUE(controller.isCommsLost());
  TEST_ASSERT_FALSE(controller.isRegulating());

  controller.onHostActivity(TIMEOUT_US + 10000);
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, TIMEOUT_US + 20000, DT_S));
}

void test_controller_comms_loss_also_stops_open_loop() {
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.setDuty(150);
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, TIMEOUT_US, DT_S));
  TEST_ASSERT_TRUE(controller.isCommsLost());
  TEST_ASSERT_EQUAL_UINT8(0, controller.getDutySetpoint());
}

void test_controller_keeps_running_while_the_host_polls() {
  DispenserController controller;
  configure(controller);
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.setTargetSpeedHz(100.0f);

  for (uint32_t t = 0; t <= 10000000UL; t += 10000) {
    if (t % 500000UL == 0) controller.onHostActivity(t);
    controller.update(100.0f, t, DT_S);
  }
  TEST_ASSERT_TRUE(controller.isRegulating());
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_setting_the_timeout_restarts_the_timer() {
  DispenserController controller;
  configure(controller);
  controller.setTargetSpeedHz(100.0f);
  controller.update(0.0f, 0, DT_S);

  controller.setCommsTimeoutMs(TIMEOUT_MS, 10000000UL);
  TEST_ASSERT_TRUE(controller.update(0.0f, 10010000UL, DT_S) > 0);
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_zero_timeout_turns_it_off() {
  DispenserController controller;
  configure(controller);
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.setCommsTimeoutMs(0, 0);
  TEST_ASSERT_EQUAL_UINT16(0, controller.getCommsTimeoutMs());
  controller.setTargetSpeedHz(100.0f);
  TEST_ASSERT_TRUE(controller.update(0.0f, TIMEOUT_US * 10, DT_S) > 0);
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_new_setpoint_clears_comms_lost() {
  DispenserController controller;
  configure(controller);
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.setTargetSpeedHz(100.0f);
  controller.update(0.0f, TIMEOUT_US, DT_S);
  TEST_ASSERT_TRUE(controller.isCommsLost());

  controller.onHostActivity(TIMEOUT_US + 10000);
  assertResult(ControlResult::OK, controller.setTargetSpeedHz(100.0f));
  TEST_ASSERT_FALSE(controller.isCommsLost());
  TEST_ASSERT_TRUE(controller.update(0.0f, TIMEOUT_US + 20000, DT_S) > 0);
}

void test_controller_new_duty_clears_comms_lost() {
  DispenserController controller;
  controller.setMode(ControlMode::OPEN_LOOP);
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.setDuty(150);
  controller.update(0.0f, TIMEOUT_US, DT_S);
  TEST_ASSERT_TRUE(controller.isCommsLost());

  controller.onHostActivity(TIMEOUT_US + 10000);
  assertResult(ControlResult::OK, controller.setDuty(150));
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_silence_while_idle_is_not_a_comms_loss() {
  DispenserController controller;
  controller.setCommsTimeoutMs(TIMEOUT_MS, 0);
  controller.update(0.0f, TIMEOUT_US * 5, DT_S);
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_comms_timeout_survives_a_micros_wrap() {
  DispenserController controller;
  configure(controller);
  const uint32_t start = 0xFFFFFFFFUL - 1000000UL;
  controller.setCommsTimeoutMs(TIMEOUT_MS, start);
  controller.setTargetSpeedHz(100.0f);
  TEST_ASSERT_TRUE(controller.update(0.0f, static_cast<uint32_t>(start + 2000000UL), DT_S) > 0);
  TEST_ASSERT_FALSE(controller.isCommsLost());
  controller.update(0.0f, static_cast<uint32_t>(start + TIMEOUT_US), DT_S);
  TEST_ASSERT_TRUE(controller.isCommsLost());
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_controller_boots_idle_unconfigured_closed_loop);
  RUN_TEST(test_controller_refuses_a_positive_target_before_tunings);
  RUN_TEST(test_controller_accepts_a_zero_target_before_tunings);
  RUN_TEST(test_controller_first_accepted_tunings_configure_it);
  RUN_TEST(test_controller_rejected_tunings_do_not_configure_it);
  RUN_TEST(test_controller_all_zero_tunings_configure_it);
  RUN_TEST(test_controller_arms_on_a_positive_target);
  RUN_TEST(test_controller_rejects_a_negative_or_nan_target);
  RUN_TEST(test_controller_drives_output_up_when_below_setpoint);
  RUN_TEST(test_controller_output_is_clamped_to_the_pwm_range);
  RUN_TEST(test_controller_zero_target_disarms_and_commands_zero);
  RUN_TEST(test_controller_zero_target_resets_the_integral);
  RUN_TEST(test_controller_exposes_the_filtered_measurement);
  RUN_TEST(test_controller_rejects_an_invalid_alpha);
  RUN_TEST(test_controller_open_loop_outputs_the_duty_without_tunings);
  RUN_TEST(test_controller_duty_is_refused_in_closed_loop);
  RUN_TEST(test_controller_target_is_refused_in_open_loop);
  RUN_TEST(test_controller_switching_mode_stops_and_resets);
  RUN_TEST(test_controller_rejects_an_unknown_mode);
  RUN_TEST(test_controller_stop_zeroes_target_and_duty_and_keeps_the_mode);
  RUN_TEST(test_controller_estop_stops_and_latches);
  RUN_TEST(test_controller_estop_refuses_duty_in_open_loop);
  RUN_TEST(test_controller_tunings_are_accepted_during_estop);
  RUN_TEST(test_controller_clear_estop_releases_but_leaves_the_motor_stopped);
  RUN_TEST(test_controller_comms_timeout_is_off_by_default);
  RUN_TEST(test_controller_stops_when_the_host_goes_silent);
  RUN_TEST(test_controller_comms_loss_also_stops_open_loop);
  RUN_TEST(test_controller_keeps_running_while_the_host_polls);
  RUN_TEST(test_controller_setting_the_timeout_restarts_the_timer);
  RUN_TEST(test_controller_zero_timeout_turns_it_off);
  RUN_TEST(test_controller_new_setpoint_clears_comms_lost);
  RUN_TEST(test_controller_new_duty_clears_comms_lost);
  RUN_TEST(test_controller_silence_while_idle_is_not_a_comms_loss);
  RUN_TEST(test_controller_comms_timeout_survives_a_micros_wrap);
  return UNITY_END();
}

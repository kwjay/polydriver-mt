#include <unity.h>
#include "dispenser_controller.h"

static constexpr float DT = 0.01f;
static constexpr uint32_t STALL_US = 1500000UL;

void setUp(void) {}
void tearDown(void) {}

void test_controller_starts_idle_and_commands_nothing() {
  DispenserController controller;
  TEST_ASSERT_FALSE(controller.isRegulating());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, true, 0, DT));
}

void test_controller_arms_on_a_positive_target() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);
  TEST_ASSERT_TRUE(controller.isRegulating());
  TEST_ASSERT_EQUAL_FLOAT(100.0f, controller.getTargetSpeed());
}

void test_controller_drives_output_up_when_below_setpoint() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);
  uint8_t duty = controller.update(0.0f, false, 0, DT);
  TEST_ASSERT_TRUE(duty > 0);
}

void test_controller_output_is_clamped_to_the_pwm_range() {
  DispenserController controller;
  controller.setTargetSpeed(1000.0f);
  uint8_t duty = controller.update(0.0f, false, 0, DT);
  TEST_ASSERT_EQUAL_UINT8(255, duty);
}

void test_controller_zero_target_disarms_and_commands_zero() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);
  controller.update(0.0f, false, 0, DT);

  controller.setTargetSpeed(0.0f);
  TEST_ASSERT_FALSE(controller.isRegulating());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, false, 10000, DT));
  TEST_ASSERT_EQUAL_FLOAT(0.0f, controller.getFilteredFrequency());
}

void test_controller_exposes_the_filtered_measurement() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);

  // The filter seeds on its first sample, then approaches the input.
  controller.update(50.0f, false, 0, DT);
  TEST_ASSERT_EQUAL_FLOAT(50.0f, controller.getFilteredFrequency());

  controller.update(100.0f, false, 10000, DT);
  TEST_ASSERT_FLOAT_WITHIN(0.01f, 60.0f, controller.getFilteredFrequency());
}

void test_controller_latches_a_fault_after_a_sustained_stall() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);

  TEST_ASSERT_TRUE(controller.update(0.0f, true, 0, DT) > 0);
  TEST_ASSERT_FALSE(controller.isFaulted());

  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, true, STALL_US, DT));
  TEST_ASSERT_TRUE(controller.isFaulted());
}

void test_controller_fault_persists_until_a_restart() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);
  controller.update(0.0f, true, 0, DT);
  controller.update(0.0f, true, STALL_US, DT);
  TEST_ASSERT_TRUE(controller.isFaulted());

  // A spinning screw does not clear it on its own.
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(90.0f, false, STALL_US + 10000, DT));
  TEST_ASSERT_TRUE(controller.isFaulted());

  controller.setTargetSpeed(0.0f);
  TEST_ASSERT_TRUE(controller.isFaulted());

  controller.setTargetSpeed(100.0f);
  TEST_ASSERT_FALSE(controller.isFaulted());
  TEST_ASSERT_TRUE(controller.update(0.0f, false, STALL_US + 20000, DT) > 0);
}

void test_controller_new_setpoint_while_running_does_not_clear_the_fault() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);
  controller.update(0.0f, true, 0, DT);
  controller.update(0.0f, true, STALL_US, DT);
  TEST_ASSERT_TRUE(controller.isFaulted());

  controller.setTargetSpeed(120.0f);
  TEST_ASSERT_TRUE(controller.isFaulted());
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, false, STALL_US + 10000, DT));
}

void test_controller_streamed_setpoints_do_not_postpone_the_stall_fault() {
  DispenserController controller;
  controller.setTargetSpeed(100.0f);

  uint32_t t = 0;
  for (int i = 0; i < 15; i++) {
    controller.update(0.0f, true, t, DT);
    TEST_ASSERT_FALSE(controller.isFaulted());
    controller.setTargetSpeed(100.0f + static_cast<float>(i));
    t += 100000UL;
  }

  controller.update(0.0f, true, STALL_US, DT);
  TEST_ASSERT_TRUE(controller.isFaulted());
}

void test_controller_does_not_fault_while_idle() {
  DispenserController controller;
  controller.update(0.0f, true, 0, DT);
  controller.update(0.0f, true, STALL_US * 4, DT);
  TEST_ASSERT_FALSE(controller.isFaulted());
}

void test_controller_passes_tunings_through() {
  DispenserController controller;
  controller.setTunings(1.5f, 0.25f, 0.05f);
  TEST_ASSERT_EQUAL_FLOAT(1.5f, controller.getKp());
  TEST_ASSERT_EQUAL_FLOAT(0.25f, controller.getKi());
  TEST_ASSERT_EQUAL_FLOAT(0.05f, controller.getKd());
}

static constexpr uint32_t COMMS_US = DispenserController::COMMS_TIMEOUT_US;

void test_controller_stops_when_the_host_goes_silent() {
  DispenserController controller;
  controller.onHostActivity(0);
  controller.setTargetSpeed(100.0f);

  TEST_ASSERT_TRUE(controller.update(0.0f, false, COMMS_US - 10000, DT) > 0);
  TEST_ASSERT_FALSE(controller.isCommsLost());

  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, false, COMMS_US, DT));
  TEST_ASSERT_TRUE(controller.isCommsLost());
  TEST_ASSERT_FALSE(controller.isRegulating());

  controller.onHostActivity(COMMS_US + 10000);
  TEST_ASSERT_EQUAL_UINT8(0, controller.update(0.0f, false, COMMS_US + 20000, DT));
}

void test_controller_keeps_running_while_the_host_polls() {
  DispenserController controller;
  controller.onHostActivity(0);
  controller.setTargetSpeed(100.0f);

  for (uint32_t t = 0; t <= 10000000UL; t += 10000) {
    if (t % 500000UL == 0) controller.onHostActivity(t);
    controller.update(100.0f, false, t, DT);
  }
  TEST_ASSERT_TRUE(controller.isRegulating());
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_new_setpoint_clears_comms_lost() {
  DispenserController controller;
  controller.onHostActivity(0);
  controller.setTargetSpeed(100.0f);
  controller.update(0.0f, false, COMMS_US, DT);
  TEST_ASSERT_TRUE(controller.isCommsLost());

  controller.onHostActivity(COMMS_US + 10000);
  controller.setTargetSpeed(100.0f);
  TEST_ASSERT_FALSE(controller.isCommsLost());
  TEST_ASSERT_TRUE(controller.update(0.0f, false, COMMS_US + 20000, DT) > 0);
}

void test_controller_silence_while_idle_is_not_a_comms_loss() {
  DispenserController controller;
  controller.onHostActivity(0);
  controller.update(0.0f, true, COMMS_US * 5, DT);
  TEST_ASSERT_FALSE(controller.isCommsLost());
}

void test_controller_comms_timeout_survives_a_micros_wrap() {
  DispenserController controller;
  const uint32_t start = 0xFFFFFFFFUL - 1000000UL;
  controller.onHostActivity(start);
  controller.setTargetSpeed(100.0f);
  TEST_ASSERT_TRUE(controller.update(0.0f, false, static_cast<uint32_t>(start + 2000000UL), DT) > 0);
  TEST_ASSERT_FALSE(controller.isCommsLost());
  controller.update(0.0f, false, start + COMMS_US, DT);
  TEST_ASSERT_TRUE(controller.isCommsLost());
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_controller_starts_idle_and_commands_nothing);
  RUN_TEST(test_controller_arms_on_a_positive_target);
  RUN_TEST(test_controller_drives_output_up_when_below_setpoint);
  RUN_TEST(test_controller_output_is_clamped_to_the_pwm_range);
  RUN_TEST(test_controller_zero_target_disarms_and_commands_zero);
  RUN_TEST(test_controller_exposes_the_filtered_measurement);
  RUN_TEST(test_controller_latches_a_fault_after_a_sustained_stall);
  RUN_TEST(test_controller_fault_persists_until_a_restart);
  RUN_TEST(test_controller_new_setpoint_while_running_does_not_clear_the_fault);
  RUN_TEST(test_controller_streamed_setpoints_do_not_postpone_the_stall_fault);
  RUN_TEST(test_controller_does_not_fault_while_idle);
  RUN_TEST(test_controller_passes_tunings_through);
  RUN_TEST(test_controller_stops_when_the_host_goes_silent);
  RUN_TEST(test_controller_keeps_running_while_the_host_polls);
  RUN_TEST(test_controller_new_setpoint_clears_comms_lost);
  RUN_TEST(test_controller_silence_while_idle_is_not_a_comms_loss);
  RUN_TEST(test_controller_comms_timeout_survives_a_micros_wrap);
  return UNITY_END();
}

#include <Arduino.h>
#include <unity.h>
#include "pwm_generator.h"
#include "input_capture.h"

PWMGenerator pwm;
InputCapture encoder;

ISR(TIMER1_CAPT_vect) {
  encoder.handleInputCapture();
}

ISR(TIMER1_OVF_vect) {
encoder.handleTimerOverflow();
}

void setUp() {

}

void tearDown() {

}

void test_pwm_clamping() {
  pwm.setDutyCycle(300); 
  TEST_ASSERT_EQUAL_UINT8(255, pwm.getDutyCycle()); 
  pwm.setDutyCycle(-50); 
  TEST_ASSERT_EQUAL_UINT8(0, pwm.getDutyCycle()); 
  pwm.setDutyCycle(128); 
  TEST_ASSERT_EQUAL_UINT8(128, pwm.getDutyCycle()); 
}

void test_hardware_loopback_frequency() {
  pwm.setDutyCycle(128); 
  delay(100); 
  TEST_ASSERT_FALSE(encoder.getIsStalled()); 
  float freq = encoder.getSignalFrequency();
  TEST_ASSERT_FLOAT_WITHIN(5000.0f, 31250.0f, freq);
}

void test_stalled_signal_timeout() {
  pwm.setDutyCycle(0); 
  delay(600); 
  TEST_ASSERT_TRUE(encoder.getIsStalled()); 
  TEST_ASSERT_EQUAL_FLOAT(0.0f, encoder.getSignalFrequency()); 
}

void setup() {  
  UNITY_BEGIN();
  pwm.init();
  encoder.init();
  
  RUN_TEST(test_pwm_clamping);
  RUN_TEST(test_hardware_loopback_frequency);
  RUN_TEST(test_stalled_signal_timeout);
  UNITY_END();
}

void loop() {}
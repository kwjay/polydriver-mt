#include "pwm_generator.h"

void PWMGenerator::init() {
  /* Setting timer 2 to work in PWM mode at pin 3 */
  pinMode(PWM_PIN, OUTPUT);
  TCCR2A = _BV(COM2B1) | _BV(WGM20); // PWM 0 to OCR2A
  TCCR2B = _BV(WGM22) | _BV(CS20); // Prescaler set to 1
  OCR2A = MAX_PWM;
  OCR2B = MIN_PWM;
}

void PWMGenerator::setDutyCycle(int16_t value) {
  if (value > MAX_PWM) {
    currentPWMValue = MAX_PWM;
  } else if (value < MIN_PWM) {
    currentPWMValue = MIN_PWM;
  } else {
    currentPWMValue = value;
  }

  OCR2B = currentPWMValue;
}

uint8_t PWMGenerator::getDutyCycle() const {
  return currentPWMValue;
}
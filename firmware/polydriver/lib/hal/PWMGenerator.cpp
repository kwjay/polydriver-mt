#include "PWMGenerator.h"
#include <util/atomic.h>

void PWMGenerator::begin(PwmPrescaler prescaler) {
  duty = 0;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    TCCR2B = 0;
    TCNT2 = 0;
    OCR2A = TOP;
    OCR2B = 0;
    // Mode 5: phase-correct PWM, TOP = OCR2A. OC2B non-inverting: duty 0 = always low,
    // duty 255 = always high, no glitch at either end.
    TCCR2A = _BV(COM2B1) | _BV(WGM20);
    TCCR2B = _BV(WGM22) | static_cast<uint8_t>(prescaler);
  }
  pinMode(PWM_PIN, OUTPUT);
}

void PWMGenerator::setDuty(uint8_t newDuty) {
  duty = newDuty;
  // OCR2B is double-buffered in PWM mode: the new value takes effect at TOP, never mid-period.
  OCR2B = newDuty;
}

#include "input_capture.h"

void InputCapture::init() {
  pinMode(ICP_PIN, INPUT);
  TCCR1A = 0;
  TCCR1B = 0;
  TCNT1 = 0;
  // On rising edge, noise canceller enabled, prescaler 64
  TCCR1B = _BV(ICES1) | _BV(ICNC1) | _BV(CS11) | _BV(CS10); 
  // Input capture and interrupt on timer overflow
  TIMSK1 = _BV(ICIE1) | _BV(TOIE1); 
  TIFR1 = _BV(ICF1) | _BV(TOV1); 
}

void InputCapture::handleInputCapture() {
  uint16_t capture = ICR1;
  uint32_t currentOverflow = overflowCount;
  edgeCount++;

  if ((TIFR1 & _BV(TOV1)) && (capture < 0x7FFF)) {
    currentOverflow++;
    TIFR1 = _BV(TOV1);
    overflowCount = currentOverflow;
  }

  uint32_t currentTimestamp = capture_math::reconstructTimestamp(currentOverflow, capture);
  if (!isStalled) {
    period = currentTimestamp - previousTimestamp;
  } else {
    isStalled = false;
  }
  previousTimestamp = currentTimestamp;
}

void InputCapture::handleTimerOverflow() {
  overflowCount++;
  if (!isStalled && capture_math::edgeTimedOut(overflowCount, previousTimestamp)) {
    isStalled = true;
    period = 0;
  }
}

float InputCapture::getSignalFrequency() const {
  uint32_t safePeriod;
  bool safeStalled;

  noInterrupts();
  safePeriod = period;
  safeStalled = isStalled;
  interrupts();
  if (safeStalled) return 0.0f;
  return capture_math::frequencyFromPeriod(safePeriod);
}

uint32_t InputCapture::getEdgeCount() const {
  uint32_t safeCount;
  noInterrupts();
  safeCount = edgeCount;
  interrupts();
  return safeCount;
}

bool InputCapture::getIsStalled() const {
  bool safeStalled;
  noInterrupts();
  safeStalled = isStalled;
  interrupts();
  return safeStalled;
}
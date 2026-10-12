#include "InputCapture.h"
#include <util/atomic.h>
#include "CaptureMath.h"

void InputCapture::begin() {
  pinMode(ICP_PIN, INPUT);
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1 = 0;
    // Capture on rising edge, noise canceller on, prescaler 64 (4 us per tick).
    TCCR1B = _BV(ICES1) | _BV(ICNC1) | _BV(CS11) | _BV(CS10);
    // Clear stale flags (write 1 to clear) before enabling the capture and overflow interrupts.
    TIFR1 = _BV(ICF1) | _BV(TOV1);
    TIMSK1 = _BV(ICIE1) | _BV(TOIE1);
  }
}

void InputCapture::handleCapture() {
  const uint16_t captureTicks = ICR1;
  uint32_t overflows = overflowCount;

  // The capture vector outranks the overflow vector. If the timer wrapped just before this edge,
  // TOV1 is still pending and the overflow is not counted yet; a small ICR1 means the edge came
  // after the wrap, so count it here and clear the flag so the overflow ISR does not count it again.
  if ((TIFR1 & _BV(TOV1)) && captureTicks < 0x8000) {
    overflows++;
    overflowCount = overflows;
    TIFR1 = _BV(TOV1);
  }

  const uint32_t timestampTicks = CaptureMath::timestampTicks(overflows, captureTicks);
  if (stalled) {
    stalled = false;
  } else {
    periodTicks = timestampTicks - previousTimestampTicks;
  }
  previousTimestampTicks = timestampTicks;
  edgeCount++;
}

void InputCapture::handleOverflow() {
  overflowCount++;
  if (!stalled && CaptureMath::edgeTimedOut(overflowCount, previousTimestampTicks)) {
    stalled = true;
    periodTicks = 0;
  }
}

CaptureReading InputCapture::read() const {
  uint32_t period;
  uint32_t edges;
  bool isStalled;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    period = periodTicks;
    edges = edgeCount;
    isStalled = stalled;
  }
  const float frequencyHz = isStalled ? 0.0f : CaptureMath::frequencyFromPeriodHz(period);
  return CaptureReading{frequencyHz, edges, isStalled};
}

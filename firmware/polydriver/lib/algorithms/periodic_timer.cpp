#include "periodic_timer.h"

void PeriodicTimer::reset(uint32_t nowMicros) {
  nextDueUs = nowMicros + period;
  lastFireUs = nowMicros;
  lastInterval = 0;
  started = true;
}

uint32_t PeriodicTimer::takeMaxIntervalUs() {
  uint32_t result = maxInterval;
  maxInterval = 0;
  return result;
}

bool PeriodicTimer::due(uint32_t nowMicros) {
  if (!started) {
    reset(nowMicros);
    return false;
  }

  if (static_cast<int32_t>(nowMicros - nextDueUs) < 0) return false;

  lastInterval = nowMicros - lastFireUs;
  lastFireUs = nowMicros;
  if (lastInterval > maxInterval) maxInterval = lastInterval;

  nextDueUs += period;

  if (static_cast<int32_t>(nowMicros - nextDueUs) >= 0) {
    nextDueUs = nowMicros + period;
    if (missedCycles < 0xFFFF) missedCycles++;
  }

  return true;
}

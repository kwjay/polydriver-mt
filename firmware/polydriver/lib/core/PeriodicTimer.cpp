#include "PeriodicTimer.h"

void PeriodicTimer::reset(uint32_t nowUs) {
  nextDueUs = nowUs + periodUs;
  lastFireUs = nowUs;
  lastIntervalUs = 0;
  started = true;
}

uint32_t PeriodicTimer::takeMaxIntervalUs() {
  uint32_t result = maxIntervalUs;
  maxIntervalUs = 0;
  return result;
}

bool PeriodicTimer::due(uint32_t nowUs) {
  if (!started) {
    reset(nowUs);
    return false;
  }

  if (static_cast<int32_t>(nowUs - nextDueUs) < 0) return false;

  lastIntervalUs = nowUs - lastFireUs;
  lastFireUs = nowUs;
  if (lastIntervalUs > maxIntervalUs) maxIntervalUs = lastIntervalUs;

  nextDueUs += periodUs;

  if (static_cast<int32_t>(nowUs - nextDueUs) >= 0) {
    nextDueUs = nowUs + periodUs;
    if (missedCycles < 0xFFFF) missedCycles++;
  }

  return true;
}
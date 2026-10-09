#include "stall_guard.h"

bool StallGuard::update(bool currentlyStalled, uint32_t nowMicros) {
  if (faulted) return false;

  if (!currentlyStalled) {
    stalling = false;
    return false;
  }

  if (!stalling) {
    stalling = true;
    stallStartMicros = nowMicros;
    return false;
  }

  if (nowMicros - stallStartMicros >= faultTimeoutUs) {
    faulted = true;
    return true;
  }
  return false;
}

void StallGuard::reset() {
  stalling = false;
  faulted = false;
}

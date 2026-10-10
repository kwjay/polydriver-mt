#include "EMAFilter.h"

float EMAFilter::filter(float sample) {
  if (!isInitialized) {
    lastOutput = sample;
    isInitialized = true;
    return lastOutput;
  }
  lastOutput = alpha * sample + oneMinusAlpha * lastOutput;
  return lastOutput;
}

void EMAFilter::reset() {
  isInitialized = false;
}

bool EMAFilter::setAlpha(float newAlpha) {
  if (!(newAlpha > 0.0f && newAlpha <= 1.0f)) return false;
  alpha = newAlpha;
  oneMinusAlpha = 1.0f - newAlpha;
  return true;
}

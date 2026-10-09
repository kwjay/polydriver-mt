#include "ema_filter.h"

float EMAFilter::filter(float currentValue) {
  if (!isInitialized) {
    emaPrevious = currentValue;
    isInitialized = true;
    return currentValue;
  }
  emaPrevious = (alpha * currentValue) + alphaDifference * emaPrevious;
  return emaPrevious;
}

void EMAFilter::reset() {
  isInitialized = false;
  emaPrevious = 0.0f;
}

void EMAFilter::setAlpha(float newAlpha) {
  if (newAlpha > 0.0f && newAlpha <= 1.0f) {
    alpha = newAlpha;
    alphaDifference = 1.0f - alpha;
  }
}
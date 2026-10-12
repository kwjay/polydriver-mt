#include "PIDRegulator.h"
#include <float.h>

PIDRegulator::PIDRegulator(float initialKp, float initialKi, float initialKd)
    : kp(initialKp), ki(initialKi), kd(initialKd) {}

float PIDRegulator::calculate(float setpoint, float measurement, float dtS, float feedforward) {
  if (dtS <= 0.0f) return 0.0f;
  const float error = setpoint - measurement;
  const float pTerm = kp * error;

  float dTerm = 0.0f;
  if (hasPreviousMeasurement) {
    dTerm = -kd * (measurement - previousMeasurement) / dtS;
  }
  previousMeasurement = measurement;
  hasPreviousMeasurement = true;

  const float otherTerms = feedforward + pTerm + dTerm;
  float candidate = integralTerm + ki * error * dtS;
  if (error > 0.0f) {
    const float roomAbove = OUTPUT_MAX - otherTerms;
    if (candidate > roomAbove) candidate = (integralTerm > roomAbove) ? integralTerm : roomAbove;
  } else if (error < 0.0f) {
    const float roomBelow = OUTPUT_MIN - otherTerms;
    if (candidate < roomBelow) candidate = (integralTerm < roomBelow) ? integralTerm : roomBelow;
  }
  integralTerm = clamp(candidate, -OUTPUT_MAX, OUTPUT_MAX);

  return clamp(otherTerms + integralTerm, OUTPUT_MIN, OUTPUT_MAX);
}

bool PIDRegulator::setTunings(float newKp, float newKi, float newKd) {
  if (!(isValidGain(newKp) && isValidGain(newKi) && isValidGain(newKd))) return false;
  kp = newKp;
  ki = newKi;
  kd = newKd;
  return true;
}

float PIDRegulator::clamp(float value, float low, float high) {
  if (value > high) return high;
  if (value < low) return low;
  return value;
}

bool PIDRegulator::isValidGain(float gain) {
  return gain >= 0.0f && gain <= FLT_MAX;
}

void PIDRegulator::reset() {
  integralTerm = 0.0f;
  previousMeasurement = 0.0f;
  hasPreviousMeasurement = false;
}

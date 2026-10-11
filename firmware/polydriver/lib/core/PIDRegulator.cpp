#include "PIDRegulator.h"
#include <float.h>

PIDRegulator::PIDRegulator(float initialKp, float initialKi, float initialKd)
    : kp(initialKp), ki(initialKi), kd(initialKd) {}

float PIDRegulator::calculate(float setpoint, float measurement, float dtS) {
  if (dtS <= 0.0f) return 0.0f;
  float error = setpoint - measurement;
  float pTerm = kp * error;

  integralTerm += ki * error * dtS;
  if (integralTerm > OUTPUT_MAX) integralTerm = OUTPUT_MAX;
  else if (integralTerm < OUTPUT_MIN) integralTerm = OUTPUT_MIN;

  float dTerm = 0.0f;
  if (hasPreviousMeasurement) {
    dTerm = -kd * (measurement - previousMeasurement) / dtS;
  }
  previousMeasurement = measurement;
  hasPreviousMeasurement = true;

  float output = pTerm + integralTerm + dTerm;
  if (output > OUTPUT_MAX) output = OUTPUT_MAX;
  else if (output < OUTPUT_MIN) output = OUTPUT_MIN;
  return output;
}

bool PIDRegulator::setTunings(float newKp, float newKi, float newKd) {
  if (!(isValidGain(newKp) && isValidGain(newKi) && isValidGain(newKd))) return false;
  kp = newKp;
  ki = newKi;
  kd = newKd;
  return true;
}

bool PIDRegulator::isValidGain(float gain) {
  return gain >= 0.0f && gain <= FLT_MAX;
}

void PIDRegulator::reset() {
  integralTerm = 0.0f;
  previousMeasurement = 0.0f;
  hasPreviousMeasurement = false;
}

#include "PIDRegulator.h"

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
  if (!(newKp >= 0.0f && newKi >= 0.0f && newKd >= 0.0f)) return false;
  kp = newKp;
  ki = newKi;
  kd = newKd;
  return true;
}

void PIDRegulator::reset() {
  integralTerm = 0.0f;
  previousMeasurement = 0.0f;
  hasPreviousMeasurement = false;
}

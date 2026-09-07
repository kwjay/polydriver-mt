#include "pid_regulator.h"
PIDRegulator::PIDRegulator(float p, float i, float d)
  : kp(p), ki(i), kd(d) {}

float PIDRegulator::calculate(float setpoint, float measuredValue, float dt) {
  if (dt <= 0.0f) return 0.0f;
  float error = setpoint - measuredValue;
  float pTerm = kp * error;

  integralTerm += (ki * error * dt);
  if (integralTerm > outMax) integralTerm = outMax;
  else if (integralTerm < outMin) integralTerm = outMin;

  float dError = (error - previousError) / dt;
  float dTerm = kd * dError;
  
  float output = pTerm + integralTerm + dTerm;
  if (output > outMax) output = outMax;
  else if (output < outMin) output = outMin;
  previousError = error;
  return output;
}

void PIDRegulator::setTunings(float p, float i, float d) {
  kp = p;
  ki = i;
  kd = d;
}

void PIDRegulator::reset() {
  integralTerm = 0.0f;
  previousError = 0.0f;
}
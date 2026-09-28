#include "dispenser_controller.h"

void DispenserController::setTargetSpeed(float hz) {
  targetSpeed = hz;
  regulating = (hz > 0.0f);
  stallGuard.reset();

  if (!regulating) {
    pid.reset();
    ema.reset();
    filteredFrequency = 0.0f;
  }
}

void DispenserController::setTunings(float kp, float ki, float kd) {
  pid.setTunings(kp, ki, kd);
}

uint8_t DispenserController::update(float rawFrequency, bool encoderStalled, uint32_t nowMicros, float dt) {
  filteredFrequency = ema.filter(rawFrequency);

  if (!regulating) return 0;

  if (stallGuard.update(encoderStalled, nowMicros)) {
    pid.reset();
    ema.reset();
  }

  if (stallGuard.isFaulted()) return 0;

  float output = pid.calculate(targetSpeed, filteredFrequency, dt);
  if (output < 0.0f) output = 0.0f;
  if (output > 255.0f) output = 255.0f;
  return static_cast<uint8_t>(output);
}

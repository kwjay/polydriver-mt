#include "DispenserController.h"
#include <float.h>

ControlResult DispenserController::setTargetSpeedHz(float targetHz) {
  if (estopped) return ControlResult::WRONG_STATE;
  if (mode != ControlMode::CLOSED_LOOP) return ControlResult::WRONG_MODE;
  if (!(targetHz >= 0.0f && targetHz <= FLT_MAX)) return ControlResult::BAD_VALUE;
  if (targetHz > 0.0f && !configured) return ControlResult::NOT_CONFIGURED;

  targetSpeedHz = targetHz;
  commsLost = false;
  if (targetHz == 0.0f) resetRegulation();
  return ControlResult::OK;
}

ControlResult DispenserController::setDuty(uint8_t duty) {
  if (estopped) return ControlResult::WRONG_STATE;
  if (mode != ControlMode::OPEN_LOOP) return ControlResult::WRONG_MODE;

  dutySetpoint = duty;
  commsLost = false;
  return ControlResult::OK;
}

ControlResult DispenserController::setMode(ControlMode newMode) {
  if (estopped) return ControlResult::WRONG_STATE;
  if (newMode != ControlMode::CLOSED_LOOP && newMode != ControlMode::OPEN_LOOP) {
    return ControlResult::BAD_VALUE;
  }

  mode = newMode;
  targetSpeedHz = 0.0f;
  dutySetpoint = 0;
  resetRegulation();
  return ControlResult::OK;
}

ControlResult DispenserController::setTunings(float kp, float ki, float kd) {
  if (!pid.setTunings(kp, ki, kd)) return ControlResult::BAD_VALUE;
  configured = true;
  return ControlResult::OK;
}

ControlResult DispenserController::setAlpha(float alpha) {
  if (!ema.setAlpha(alpha)) return ControlResult::BAD_VALUE;
  return ControlResult::OK;
}

ControlResult DispenserController::setFeedforward(float offsetDuty, float gainDutyPerHz) {
  if (!(offsetDuty >= 0.0f && offsetDuty <= 255.0f)) return ControlResult::BAD_VALUE;
  if (!(gainDutyPerHz >= 0.0f && gainDutyPerHz <= FLT_MAX)) return ControlResult::BAD_VALUE;
  feedforwardOffsetDuty = offsetDuty;
  feedforwardGainDutyPerHz = gainDutyPerHz;
  return ControlResult::OK;
}

void DispenserController::setCommsTimeoutMs(uint16_t timeoutMs, uint32_t nowUs) {
  commsTimeoutMs = timeoutMs;
  lastHostActivityUs = nowUs;
}

void DispenserController::onHostActivity(uint32_t nowUs) {
  lastHostActivityUs = nowUs;
}

void DispenserController::stop() {
  targetSpeedHz = 0.0f;
  dutySetpoint = 0;
  resetRegulation();
}

void DispenserController::estop() {
  stop();
  estopped = true;
}

void DispenserController::clearEstop() {
  estopped = false;
}

uint8_t DispenserController::update(float rawFrequencyHz, uint32_t nowUs, float dtS) {
  filteredFrequencyHz = ema.filter(rawFrequencyHz);

  if (commsTimeoutMs > 0 && isMotorCommanded()) {
    const uint32_t timeoutUs = static_cast<uint32_t>(commsTimeoutMs) * 1000UL;
    if (nowUs - lastHostActivityUs >= timeoutUs) {
      stop();
      commsLost = true;
    }
  }

  if (mode == ControlMode::OPEN_LOOP) {
    outputDuty = dutySetpoint;
  } else if (targetSpeedHz > 0.0f) {
    const float feedforward = feedforwardOffsetDuty + feedforwardGainDutyPerHz * targetSpeedHz;
    // calculate() returns 0-255, so rounding with + 0.5 stays below 256.
    outputDuty = static_cast<uint8_t>(pid.calculate(targetSpeedHz, filteredFrequencyHz, dtS, feedforward) + 0.5f);
  } else {
    outputDuty = 0;
  }
  return outputDuty;
}

bool DispenserController::isMotorCommanded() const {
  if (mode == ControlMode::OPEN_LOOP) return dutySetpoint > 0;
  return targetSpeedHz > 0.0f;
}

void DispenserController::resetRegulation() {
  pid.reset();
  ema.reset();
}

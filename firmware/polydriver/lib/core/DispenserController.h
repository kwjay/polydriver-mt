#ifndef DISPENSER_CONTROLLER_H
#define DISPENSER_CONTROLLER_H

#include <stdint.h>
#include "PIDRegulator.h"
#include "EMAFilter.h"

enum class ControlResult : uint8_t {
  OK,
  WRONG_MODE,
  WRONG_STATE,
  BAD_VALUE,
  NOT_CONFIGURED
};

enum class ControlMode : uint8_t {
  CLOSED_LOOP = 0,
  OPEN_LOOP = 1
};

class DispenserController {
public:
  DispenserController() = default;

  ControlResult setTargetSpeedHz(float targetHz);
  ControlResult setDuty(uint8_t duty);
  ControlResult setMode(ControlMode newMode);
  ControlResult setTunings(float kp, float ki, float kd);
  ControlResult setAlpha(float alpha);
  ControlResult setFeedforward(float offsetDuty, float gainDutyPerHz);
  void setCommsTimeoutMs(uint16_t timeoutMs, uint32_t nowUs);
  void onHostActivity(uint32_t nowUs);

  void stop();
  void estop();
  void clearEstop();

  uint8_t update(float rawFrequencyHz, uint32_t nowUs, float dtS);

  float getTargetSpeedHz() const { return targetSpeedHz; }
  float getFilteredFrequencyHz() const { return filteredFrequencyHz; }
  uint8_t getDutySetpoint() const { return dutySetpoint; }
  uint8_t getOutputDuty() const { return outputDuty; }
  ControlMode getMode() const { return mode; }
  float getKp() const { return pid.getKp(); }
  float getKi() const { return pid.getKi(); }
  float getKd() const { return pid.getKd(); }
  float getAlpha() const { return ema.getAlpha(); }
  float getFeedforwardOffsetDuty() const { return feedforwardOffsetDuty; }
  float getFeedforwardGainDutyPerHz() const { return feedforwardGainDutyPerHz; }
  uint16_t getCommsTimeoutMs() const { return commsTimeoutMs; }

  bool isRegulating() const { return mode == ControlMode::CLOSED_LOOP && targetSpeedHz > 0.0f; }
  bool isCommsLost() const { return commsLost; }
  bool isEstopped() const { return estopped; }
  bool isConfigured() const { return configured; }

private:
  bool isMotorCommanded() const;
  void resetRegulation();

  PIDRegulator pid;
  EMAFilter ema;

  ControlMode mode{ControlMode::CLOSED_LOOP};
  float targetSpeedHz{0.0f};
  uint8_t dutySetpoint{0};
  float filteredFrequencyHz{0.0f};
  uint8_t outputDuty{0};
  float feedforwardOffsetDuty{0.0f};
  float feedforwardGainDutyPerHz{0.0f};

  uint16_t commsTimeoutMs{0};
  uint32_t lastHostActivityUs{0};
  bool commsLost{false};
  bool estopped{false};
  bool configured{false};
};

#endif

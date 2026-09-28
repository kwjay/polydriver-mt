#ifndef DISPENSER_CONTROLLER_H
#define DISPENSER_CONTROLLER_H

#include <stdint.h>
#include "pid_regulator.h"
#include "ema_filter.h"
#include "stall_guard.h"

class DispenserController {
public:
  DispenserController() = default;

  void setTargetSpeed(float hz);
  void setTunings(float kp, float ki, float kd);
  void setAlpha(float alpha) { ema.setAlpha(alpha); }

  uint8_t update(float rawFrequency, bool encoderStalled, uint32_t nowMicros, float dt);

  float getTargetSpeed() const { return targetSpeed; }
  float getFilteredFrequency() const { return filteredFrequency; }
  bool isRegulating() const { return regulating; }
  bool isFaulted() const { return stallGuard.isFaulted(); }
  float getKp() const { return pid.getKp(); }
  float getKi() const { return pid.getKi(); }
  float getKd() const { return pid.getKd(); }

private:
  PIDRegulator pid;
  EMAFilter ema;
  StallGuard stallGuard;

  float targetSpeed{0.0f};
  float filteredFrequency{0.0f};
  bool regulating{false};
};

#endif

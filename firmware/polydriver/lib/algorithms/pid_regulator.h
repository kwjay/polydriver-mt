#ifndef PID_REGULATOR_H
#define PID_REGULATOR_H


class PIDRegulator {
  float kp{0.7f};
  float ki{0.005f};
  float kd{0.1f};

  float integralTerm{0.0f};
  float previousError{0.0f};

  static constexpr float outMin{0.0f};
  static constexpr float outMax{255.0f};
public:
  PIDRegulator() = default;
  PIDRegulator(float p, float i, float d);
  float calculate(float setpoint, float measuredValue, float dt);
  void setTunings(float p, float i, float d);
  void reset();
  float getKp() const { return kp; }
  float getKi() const { return ki; }
  float getKd() const { return kd; }
};

#endif
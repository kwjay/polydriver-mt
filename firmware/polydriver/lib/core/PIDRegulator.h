#ifndef PID_REGULATOR_H
#define PID_REGULATOR_H

class PIDRegulator {
public:
  PIDRegulator() = default;
  PIDRegulator(float initialKp, float initialKi, float initialKd);

  float calculate(float setpoint, float measurement, float dtS, float feedforward = 0.0f);
  bool setTunings(float newKp, float newKi, float newKd);
  void reset();

  float getKp() const { return kp; }
  float getKi() const { return ki; }
  float getKd() const { return kd; }

private:
  static bool isValidGain(float gain);
  static float clamp(float value, float low, float high);

  static constexpr float OUTPUT_MIN = 0.0f;
  static constexpr float OUTPUT_MAX = 255.0f;

  float kp{0.0f};
  float ki{0.0f};
  float kd{0.0f};

  float integralTerm{0.0f};
  float previousMeasurement{0.0f};
  bool hasPreviousMeasurement{false};
};

#endif

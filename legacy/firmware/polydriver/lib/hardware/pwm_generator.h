#ifndef PWM_GENERATOR_H
#define PWM_GENERATOR_H

#include <Arduino.h>
#include <stdint.h>

class PWMGenerator {
private:
  uint8_t currentPWMValue{0};
  static constexpr uint8_t PWM_PIN = 3;
  static constexpr int16_t MAX_PWM = 255;
  static constexpr int16_t MIN_PWM = 0;
public:
  void init();
  void setDutyCycle(int16_t value);
  [[nodiscard]] uint8_t getDutyCycle() const;
};
#endif
#ifndef PWM_GENERATOR_H
#define PWM_GENERATOR_H

#include <Arduino.h>

// Timer2 clock select bits CS22:0. Phase-correct PWM with TOP 255 runs at 16 MHz / (510 * N):
// 1 -> 31.37 kHz, 8 -> 3.92 kHz, 32 -> 980 Hz, 64 -> 490 Hz, 128 -> 245 Hz, 256 -> 123 Hz,
// 1024 -> 31 Hz.
enum class PwmPrescaler : uint8_t {
  DIV_1 = 1,
  DIV_8 = 2,
  DIV_32 = 3,
  DIV_64 = 4,
  DIV_128 = 5,
  DIV_256 = 6,
  DIV_1024 = 7
};

class PWMGenerator {
public:
  void begin(PwmPrescaler prescaler = PwmPrescaler::DIV_1);
  void setDuty(uint8_t newDuty);
  uint8_t getDuty() const { return duty; }

private:
  // OC2B is fixed to PD3, Arduino pin 3.
  static constexpr uint8_t PWM_PIN = 3;
  static constexpr uint8_t TOP = 255;

  uint8_t duty{0};
};

#endif

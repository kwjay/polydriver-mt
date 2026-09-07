#ifndef INPUT_CAPTURE_H
#define INPUT_CAPTURE_H

#include <Arduino.h>

class InputCapture {
private:
  static constexpr uint32_t TCNT_MAX_VALUE = 65536UL;
  static constexpr float CLOCK_SPEED = 16000000.0f;
  static constexpr uint8_t PRESCALER = 64;
  static constexpr uint8_t ICP_PIN = 8;
  static constexpr uint8_t TIMEOUT_OVERFLOWS = 2;

  volatile uint32_t previousTimestamp{0};
  volatile uint32_t overflowCount{0};
  volatile uint32_t period{0};
  volatile uint8_t overflowsSinceLastCapture{0};
  volatile bool isStalled{true};

public:
  void init();

  void handleInputCapture();
  void handleTimerOverflow();

  [[nodiscard]] float getSignalFrequency() const;
  [[nodiscard]] bool getIsStalled() const;
};

#endif
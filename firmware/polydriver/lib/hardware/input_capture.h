#ifndef INPUT_CAPTURE_H
#define INPUT_CAPTURE_H

#include <Arduino.h>
#include "capture_math.h"

class InputCapture {
private:
  static constexpr uint8_t ICP_PIN = 8;

  volatile uint32_t previousTimestamp{0};
  volatile uint32_t overflowCount{0};
  volatile uint32_t period{0};
  volatile uint32_t edgeCount{0};
  volatile bool isStalled{true};

public:
  void init();

  void handleInputCapture();
  void handleTimerOverflow();

  [[nodiscard]] float getSignalFrequency() const;
  [[nodiscard]] bool getIsStalled() const;
  [[nodiscard]] uint32_t getEdgeCount() const;
};

#endif
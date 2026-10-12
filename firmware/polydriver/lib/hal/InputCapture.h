#ifndef INPUT_CAPTURE_H
#define INPUT_CAPTURE_H

#include <Arduino.h>

struct CaptureReading {
  float frequencyHz;
  uint32_t edgeCount;
  bool stalled;
};

class InputCapture {
public:
  void begin();

  // Called from ISR(TIMER1_CAPT_vect) and ISR(TIMER1_OVF_vect) in main.cpp.
  void handleCapture();
  void handleOverflow();

  CaptureReading read() const;

private:
  // ICP1 is fixed to PB0, Arduino pin 8.
  static constexpr uint8_t ICP_PIN = 8;

  volatile uint32_t overflowCount{0};
  volatile uint32_t previousTimestampTicks{0};
  volatile uint32_t periodTicks{0};
  volatile uint32_t edgeCount{0};
  volatile bool stalled{true};
};

#endif

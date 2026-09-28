#ifndef CAPTURE_MATH_H
#define CAPTURE_MATH_H

#include <stdint.h>

namespace capture_math {

constexpr float CLOCK_SPEED = 16000000.0f;
constexpr uint8_t PRESCALER = 64;
constexpr uint8_t TIMEOUT_OVERFLOWS = 2;
constexpr uint32_t TCNT_MAX_VALUE = 65536UL;

inline uint32_t reconstructTimestamp(uint32_t overflowCount, uint16_t capture) {
  return (overflowCount << 16) | capture;
}

inline float frequencyFromPeriod(uint32_t periodTicks) {
  if (periodTicks == 0) return 0.0f;
  return CLOCK_SPEED / (static_cast<float>(PRESCALER) * static_cast<float>(periodTicks));
}

// Slowest signal still measurable: TIMEOUT_OVERFLOWS full timer periods.
inline float minMeasurableFrequency() {
  return frequencyFromPeriod(TCNT_MAX_VALUE * TIMEOUT_OVERFLOWS);
}

}

#endif

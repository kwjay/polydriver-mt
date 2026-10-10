#ifndef CAPTURE_MATH_H
#define CAPTURE_MATH_H

#include <stdint.h>

namespace CaptureMath {

constexpr float CLOCK_HZ = 16000000.0f;
constexpr uint8_t PRESCALER = 64;
constexpr uint8_t EDGE_TIMEOUT_OVERFLOWS = 2;
constexpr uint32_t TIMER_PERIOD_TICKS = 65536UL;

// 2 x 65536 ticks x 4 us = 524.288 ms without an edge
constexpr uint32_t EDGE_TIMEOUT_TICKS = TIMER_PERIOD_TICKS * EDGE_TIMEOUT_OVERFLOWS;

inline uint32_t timestampTicks(uint32_t overflowCount, uint16_t captureTicks) {
  return (overflowCount << 16) | captureTicks;
}

inline float frequencyFromPeriodHz(uint32_t periodTicks) {
  if (periodTicks == 0) return 0.0f;
  return CLOCK_HZ / (static_cast<float>(PRESCALER) * static_cast<float>(periodTicks));
}

inline bool edgeTimedOut(uint32_t overflowCount, uint32_t lastEdgeTicks) {
  return timestampTicks(overflowCount, 0) - lastEdgeTicks >= EDGE_TIMEOUT_TICKS;
}

inline float minMeasurableFrequencyHz() {
  return frequencyFromPeriodHz(EDGE_TIMEOUT_TICKS);
}

}

#endif

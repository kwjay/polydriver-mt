#ifndef PERIODIC_TIMER_H
#define PERIODIC_TIMER_H

#include <stdint.h>

class PeriodicTimer {
public:
  explicit PeriodicTimer(uint32_t timerPeriodUs) : periodUs(timerPeriodUs) {}

  bool due(uint32_t nowUs);
  void reset(uint32_t nowUs);

  uint32_t getPeriodUs() const { return periodUs; }
  uint32_t getLastIntervalUs() const { return lastIntervalUs; }
  uint16_t getMissedCycles() const { return missedCycles; }
  uint32_t takeMaxIntervalUs();

private:
  uint32_t periodUs;
  uint32_t nextDueUs{0};
  uint32_t lastFireUs{0};
  uint32_t lastIntervalUs{0};
  uint32_t maxIntervalUs{0};
  uint16_t missedCycles{0};
  bool started{false};
};

#endif
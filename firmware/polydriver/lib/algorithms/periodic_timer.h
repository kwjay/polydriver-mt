#ifndef PERIODIC_TIMER_H
#define PERIODIC_TIMER_H

#include <stdint.h>

class PeriodicTimer {
public:
  explicit PeriodicTimer(uint32_t periodUs) : period(periodUs) {}

  bool due(uint32_t nowMicros);
  void reset(uint32_t nowMicros);

  uint32_t getPeriodUs() const { return period; }
  uint32_t getLastIntervalUs() const { return lastInterval; }
  uint16_t getMissedCycles() const { return missedCycles; }

private:
  uint32_t period;
  uint32_t nextDueUs{0};
  uint32_t lastFireUs{0};
  uint32_t lastInterval{0};
  uint16_t missedCycles{0};
  bool started{false};
};

#endif

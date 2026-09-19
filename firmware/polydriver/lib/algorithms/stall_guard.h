#ifndef STALL_GUARD_H
#define STALL_GUARD_H

#include <stdint.h>


class StallGuard {
public:
  explicit StallGuard(uint32_t faultTimeoutUs = DEFAULT_FAULT_TIMEOUT_US) : faultTimeoutUs(faultTimeoutUs) {}

  bool update(bool currentlyStalled, uint32_t nowMicros);
  [[nodiscard]] bool isFaulted() const { return faulted; }

  void reset();

private:
  static constexpr uint32_t DEFAULT_FAULT_TIMEOUT_US = 1500000UL;
  uint32_t faultTimeoutUs;
  bool stalling{false};
  uint32_t stallStartMicros{0};
  bool faulted{false};
};

#endif

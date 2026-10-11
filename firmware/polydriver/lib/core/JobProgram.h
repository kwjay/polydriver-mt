#ifndef JOB_PROGRAM_H
#define JOB_PROGRAM_H

#include <stdint.h>

enum class JobResult : uint8_t {
  OK,
  WRONG_STATE,
  WRONG_MODE,
  NOT_CONFIGURED,
  BAD_INDEX,
  JOB_FULL,
  BAD_JOB,
  BAD_VALUE
};

// Values are the job states of protocol v3, section 9.
enum class JobState : uint8_t {
  EMPTY = 0,
  LOADING = 1,
  READY = 2,
  RUNNING = 3,
  DONE = 4,
  ABORTED = 5
};

struct JobSegment {
  float value;
  uint32_t durationMs;
  uint8_t flags;
};

class JobProgram {
public:
  static constexpr uint8_t MAX_SEGMENTS = 32;
  static constexpr uint8_t NO_SEGMENT = 0xFF;
  // Flag bits as in the JOB_SEGMENT payload: bit 0 RAMP, bit 1 DUTY.
  static constexpr uint8_t FLAG_RAMP = 0x01;
  static constexpr uint8_t FLAG_DUTY = 0x02;
  static constexpr float MAX_DUTY = 255.0f;

  void clear();
  JobResult addSegment(uint8_t index, uint8_t flags, float value, uint32_t durationMs);
  JobResult commit(uint8_t expectedCount, uint32_t expectedTotalMs);

  float setpointAt(uint32_t elapsedMs, uint8_t& segmentIndex) const;

  JobState getState() const { return state; }
  uint8_t getCount() const { return count; }
  uint32_t getTotalMs() const { return totalMs; }
  bool isDutyJob() const { return count > 0 && (segments[0].flags & FLAG_DUTY) != 0; }
  const JobSegment& getSegment(uint8_t index) const { return segments[index]; }

private:
  bool hasMixedTypes() const;

  JobSegment segments[MAX_SEGMENTS]{};
  uint8_t count{0};
  uint32_t totalMs{0};
  JobState state{JobState::EMPTY};
};

#endif

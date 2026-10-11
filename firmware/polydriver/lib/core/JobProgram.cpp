#include "JobProgram.h"
#include <float.h>

void JobProgram::clear() {
  count = 0;
  totalMs = 0;
  state = JobState::EMPTY;
}

JobResult JobProgram::addSegment(uint8_t index, uint8_t flags, float value, uint32_t durationMs) {
  if (state != JobState::EMPTY && state != JobState::LOADING) return JobResult::WRONG_STATE;
  if (index >= MAX_SEGMENTS) return JobResult::JOB_FULL;
  if (index != count) return JobResult::BAD_INDEX;

  if ((flags & ~(FLAG_RAMP | FLAG_DUTY)) != 0) return JobResult::BAD_VALUE;
  if (!(value >= 0.0f && value <= FLT_MAX)) return JobResult::BAD_VALUE;
  if ((flags & FLAG_DUTY) != 0 && value > MAX_DUTY) return JobResult::BAD_VALUE;
  if (durationMs == 0) return JobResult::BAD_VALUE;
  if (durationMs > UINT32_MAX - totalMs) return JobResult::BAD_VALUE;

  segments[count] = JobSegment{value, durationMs, flags};
  count++;
  totalMs += durationMs;
  state = JobState::LOADING;
  return JobResult::OK;
}

JobResult JobProgram::commit(uint8_t expectedCount, uint32_t expectedTotalMs) {
  if (state != JobState::LOADING) return JobResult::WRONG_STATE;
  if (expectedCount != count || expectedTotalMs != totalMs || hasMixedTypes()) {
    return JobResult::BAD_JOB;
  }

  state = JobState::READY;
  return JobResult::OK;
}

float JobProgram::setpointAt(uint32_t elapsedMs, uint8_t& segmentIndex) const {
  uint32_t startMs = 0;
  float previousValue = 0.0f;

  for (uint8_t i = 0; i < count; i++) {
    const JobSegment& segment = segments[i];
    const uint32_t endMs = startMs + segment.durationMs;

    if (elapsedMs < endMs) {
      segmentIndex = i;
      if ((segment.flags & FLAG_RAMP) == 0) return segment.value;

      const float fraction =
          static_cast<float>(elapsedMs - startMs) / static_cast<float>(segment.durationMs);
      return previousValue + (segment.value - previousValue) * fraction;
    }

    previousValue = segment.value;
    startMs = endMs;
  }

  segmentIndex = NO_SEGMENT;
  return 0.0f;
}

bool JobProgram::hasMixedTypes() const {
  const uint8_t firstType = segments[0].flags & FLAG_DUTY;
  for (uint8_t i = 1; i < count; i++) {
    if ((segments[i].flags & FLAG_DUTY) != firstType) return true;
  }
  return false;
}

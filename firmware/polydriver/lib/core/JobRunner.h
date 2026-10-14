#ifndef JOB_RUNNER_H
#define JOB_RUNNER_H

#include <stdint.h>
#include "JobProgram.h"
#include "DispenserController.h"

class JobRunner {
public:
  JobRunner(const JobProgram& jobProgram, DispenserController& dispenserController);

  JobResult start(uint32_t nowUs);
  void tick(uint32_t nowUs);
  void abort();
  void reset();

  bool isRunning() const { return phase == JobState::RUNNING; }
  JobState getState() const;
  uint8_t getSegmentIndex() const { return segmentIndex; }
  uint32_t getElapsedMs() const;

private:
  bool hasOutcome() const;
  void advanceTime(uint32_t nowUs);
  bool apply(float setpoint);
  void finish(JobState outcome);

  const JobProgram& program;
  DispenserController& controller;

  JobState phase{JobState::EMPTY};
  uint32_t lastTickUs{0};
  uint32_t elapsedMs{0};
  uint32_t elapsedRemainderUs{0};
  uint8_t segmentIndex{JobProgram::NO_SEGMENT};
};

#endif

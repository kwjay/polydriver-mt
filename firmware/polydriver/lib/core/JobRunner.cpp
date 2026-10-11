#include "JobRunner.h"

JobRunner::JobRunner(const JobProgram& jobProgram, DispenserController& dispenserController)
    : program(jobProgram), controller(dispenserController) {}

JobResult JobRunner::start(uint32_t nowUs) {
  if (isRunning()) return JobResult::WRONG_STATE;
  if (program.getState() != JobState::READY) return JobResult::WRONG_STATE;
  if (controller.isEstopped()) return JobResult::WRONG_STATE;

  const bool dutyJob = program.isDutyJob();
  const ControlMode needed = dutyJob ? ControlMode::OPEN_LOOP : ControlMode::CLOSED_LOOP;
  if (controller.getMode() != needed) return JobResult::WRONG_MODE;
  if (!dutyJob && !controller.isConfigured()) return JobResult::NOT_CONFIGURED;

  phase = JobState::RUNNING;
  lastTickUs = nowUs;
  elapsedMs = 0;
  elapsedRemainderUs = 0;

  if (!apply(program.setpointAt(0, segmentIndex))) {
    controller.stop();
    finish(JobState::ABORTED);
    return JobResult::WRONG_STATE;
  }
  return JobResult::OK;
}

void JobRunner::tick(uint32_t nowUs) {
  if (!isRunning()) return;

  if (controller.isCommsLost() || controller.isEstopped()) {
    finish(JobState::ABORTED);
    return;
  }

  advanceTime(nowUs);

  if (elapsedMs >= program.getTotalMs()) {
    controller.stop();
    elapsedMs = program.getTotalMs();
    finish(JobState::DONE);
    return;
  }

  if (!apply(program.setpointAt(elapsedMs, segmentIndex))) {
    controller.stop();
    finish(JobState::ABORTED);
  }
}

void JobRunner::abort() {
  if (isRunning()) finish(JobState::ABORTED);
}

JobState JobRunner::getState() const {
  return hasOutcome() ? phase : program.getState();
}

uint32_t JobRunner::getElapsedMs() const {
  return hasOutcome() ? elapsedMs : 0;
}

bool JobRunner::hasOutcome() const {
  return phase != JobState::EMPTY && program.getState() == JobState::READY;
}

void JobRunner::advanceTime(uint32_t nowUs) {
  elapsedRemainderUs += nowUs - lastTickUs;
  lastTickUs = nowUs;
  elapsedMs += elapsedRemainderUs / 1000UL;
  elapsedRemainderUs %= 1000UL;
}

bool JobRunner::apply(float setpoint) {
  if (program.isDutyJob()) {
    const uint8_t duty = static_cast<uint8_t>(setpoint + 0.5f);
    return controller.setDuty(duty) == ControlResult::OK;
  }
  return controller.setTargetSpeedHz(setpoint) == ControlResult::OK;
}

void JobRunner::finish(JobState outcome) {
  phase = outcome;
  segmentIndex = JobProgram::NO_SEGMENT;
}

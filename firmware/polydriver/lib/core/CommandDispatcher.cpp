#include "CommandDispatcher.h"
#include "LittleEndian.h"

using namespace ProtocolDefs;
using LittleEndian::getFloat;
using LittleEndian::getU16;
using LittleEndian::getU32;

CommandDispatcher::CommandDispatcher(DispenserController& dispenserController,
                                     JobProgram& jobProgram, JobRunner& jobRunner,
                                     const FrameParser& frameParser, PeriodicTimer& controlTimer,
                                     RoleStorage& storage)
    : controller(dispenserController),
      program(jobProgram),
      runner(jobRunner),
      parser(frameParser),
      timer(controlTimer),
      roleStorage(storage) {}

void CommandDispatcher::begin() {
  const uint8_t stored = roleStorage.loadRole();
  role = (stored <= static_cast<uint8_t>(Role::COLOUR)) ? static_cast<Role>(stored) : Role::NONE;
}

bool CommandDispatcher::handle(ParseEvent event, const Frame& frame, uint32_t nowUs,
                               const BoardInputs& inputs, ReplyFrame& reply) {
  if (event == ParseEvent::NONE) return false;

  if (event == ParseEvent::BAD_CRC) {
    ReplyBuilder::nack(frame.sequence, NackReason::BAD_CRC, reply);
    return true;
  }

  controller.onHostActivity(nowUs);

  if (hasLastReply && frame.command == lastCommand && frame.sequence == lastSequence) {
    reply = lastReply;
    return true;
  }

  execute(frame, nowUs, inputs, reply);
  hasLastReply = true;
  lastCommand = frame.command;
  lastSequence = frame.sequence;
  lastReply = reply;
  return true;
}

void CommandDispatcher::execute(const Frame& frame, uint32_t nowUs, const BoardInputs& inputs,
                                ReplyFrame& reply) {
  const uint8_t seq = frame.sequence;
  const uint8_t* p = frame.payload;

  uint8_t expectedLength = 0;
  if (!requestPayloadLength(frame.command, expectedLength)) {
    ReplyBuilder::nack(seq, NackReason::UNKNOWN_COMMAND, reply);
    return;
  }
  if (frame.length != expectedLength) {
    ReplyBuilder::nack(seq, NackReason::BAD_LENGTH, reply);
    return;
  }

  const Command command = static_cast<Command>(frame.command);
  if (runner.isRunning() && isRefusedWhileRunning(command)) {
    ReplyBuilder::nack(seq, NackReason::WRONG_STATE, reply);
    return;
  }

  switch (command) {
    case Command::SET_SPEED:
      replyTo(seq, controller.setTargetSpeedHz(getFloat(&p[0])), reply);
      return;
    case Command::SET_PID:
      replyTo(seq, controller.setTunings(getFloat(&p[0]), getFloat(&p[4]), getFloat(&p[8])), reply);
      return;
    case Command::SET_FILTER:
      replyTo(seq, controller.setAlpha(getFloat(&p[0])), reply);
      return;
    case Command::SET_FEEDFORWARD:
      replyTo(seq, controller.setFeedforward(getFloat(&p[0]), getFloat(&p[4])), reply);
      return;
    case Command::REQ_STAT:
      buildStatus(seq, nowUs, inputs, reply);
      return;
    case Command::REQ_SETTINGS:
      buildSettings(seq, reply);
      return;
    case Command::GET_VERSION:
      ReplyBuilder::version(seq, FIRMWARE_VERSION, JobProgram::MAX_SEGMENTS, reply);
      return;
    case Command::GET_ID:
      ReplyBuilder::id(seq, role, reply);
      return;
    case Command::SET_ID:
      setRole(frame, reply);
      return;
    case Command::SET_MODE:
      replyTo(seq, controller.setMode(static_cast<ControlMode>(p[0])), reply);
      return;
    case Command::SET_PWM:
      replyTo(seq, controller.setDuty(p[0]), reply);
      return;
    case Command::SET_COMMS_TIMEOUT:
      controller.setCommsTimeoutMs(getU16(&p[0]), nowUs);
      ReplyBuilder::ack(seq, reply);
      return;
    case Command::STOP:
      runner.abort();
      controller.stop();
      ReplyBuilder::ack(seq, reply);
      return;
    case Command::ESTOP:
      runner.abort();
      controller.estop();
      ReplyBuilder::ack(seq, reply);
      return;
    case Command::CLEAR_ESTOP:
      controller.clearEstop();
      ReplyBuilder::ack(seq, reply);
      return;
    case Command::JOB_CLEAR:
      runner.reset();
      program.clear();
      ReplyBuilder::ack(seq, reply);
      return;
    case Command::JOB_SEGMENT:
      replyTo(seq, program.addSegment(p[0], p[1], getFloat(&p[2]), getU32(&p[6])), reply);
      return;
    case Command::JOB_COMMIT: {
      const JobResult result = program.commit(p[0], getU32(&p[1]));
      if (result != JobResult::OK) {
        replyTo(seq, result, reply);
        return;
      }
      ReplyBuilder::jobInfo(seq, static_cast<uint8_t>(program.getState()), program.getCount(),
                            program.getTotalMs(), reply);
      return;
    }
    case Command::JOB_RUN:
      replyTo(seq, runner.start(nowUs), reply);
      return;
  }
  ReplyBuilder::nack(seq, NackReason::UNKNOWN_COMMAND, reply);
}

void CommandDispatcher::replyTo(uint8_t sequence, ControlResult result, ReplyFrame& reply) {
  if (result == ControlResult::OK) {
    ReplyBuilder::ack(sequence, reply);
  } else {
    ReplyBuilder::nack(sequence, toNack(result), reply);
  }
}

void CommandDispatcher::replyTo(uint8_t sequence, JobResult result, ReplyFrame& reply) {
  if (result == JobResult::OK) {
    ReplyBuilder::ack(sequence, reply);
  } else {
    ReplyBuilder::nack(sequence, toNack(result), reply);
  }
}

void CommandDispatcher::buildStatus(uint8_t sequence, uint32_t nowUs, const BoardInputs& inputs,
                                    ReplyFrame& reply) {
  StatusReport report{};
  report.rawSpeedHz = inputs.rawSpeedHz;
  report.filteredSpeedHz = controller.getFilteredFrequencyHz();
  report.duty = controller.getOutputDuty();
  report.flags = statusFlags(inputs);
  report.deviceTimeUs = nowUs;
  report.edgeCount = inputs.edgeCount;
  report.controlResyncs = timer.getMissedCycles();
  report.longestLoopIntervalUs = timer.takeMaxIntervalUs();
  report.droppedFrames = parser.getAbandonedFrames();
  report.badCrcFrames = parser.getBadCrcFrames();
  report.jobState = static_cast<uint8_t>(runner.getState());
  report.segmentIndex = runner.getSegmentIndex();
  report.jobElapsedMs = runner.getElapsedMs();
  ReplyBuilder::status(sequence, report, reply);
}

void CommandDispatcher::buildSettings(uint8_t sequence, ReplyFrame& reply) {
  SettingsReport report{};
  report.kp = controller.getKp();
  report.ki = controller.getKi();
  report.kd = controller.getKd();
  report.targetSpeedHz = controller.getTargetSpeedHz();
  report.commsTimeoutMs = controller.getCommsTimeoutMs();
  report.mode = static_cast<uint8_t>(controller.getMode());
  report.dutySetpoint = controller.getDutySetpoint();
  report.alpha = controller.getAlpha();
  report.feedforwardOffsetDuty = controller.getFeedforwardOffsetDuty();
  report.feedforwardGainDutyPerHz = controller.getFeedforwardGainDutyPerHz();
  ReplyBuilder::settings(sequence, report, reply);
}

void CommandDispatcher::setRole(const Frame& frame, ReplyFrame& reply) {
  const uint8_t requested = frame.payload[0];
  if (requested > static_cast<uint8_t>(Role::COLOUR)) {
    ReplyBuilder::nack(frame.sequence, NackReason::BAD_VALUE, reply);
    return;
  }
  if (requested != static_cast<uint8_t>(role)) {
    roleStorage.saveRole(requested);
    role = static_cast<Role>(requested);
  }
  ReplyBuilder::ack(frame.sequence, reply);
}

uint8_t CommandDispatcher::statusFlags(const BoardInputs& inputs) const {
  uint8_t flags = 0;
  if (inputs.encoderStalled) flags |= StatusFlag::ENCODER_STALLED;
  if (controller.isRegulating()) flags |= StatusFlag::REGULATING;
  if (controller.isCommsLost()) flags |= StatusFlag::COMMS_LOST;
  if (controller.getMode() == ControlMode::OPEN_LOOP) flags |= StatusFlag::OPEN_LOOP;
  if (controller.isEstopped()) flags |= StatusFlag::ESTOP;
  if (inputs.watchdogReset) flags |= StatusFlag::WATCHDOG_RESET;
  if (!controller.isConfigured()) flags |= StatusFlag::UNCONFIGURED;
  return flags;
}

bool CommandDispatcher::isRefusedWhileRunning(Command command) {
  switch (command) {
    case Command::SET_SPEED:
    case Command::SET_PWM:
    case Command::SET_MODE:
    case Command::SET_ID:
    case Command::JOB_CLEAR:
    case Command::JOB_SEGMENT:
    case Command::JOB_COMMIT:
      return true;
    default:
      return false;
  }
}

NackReason CommandDispatcher::toNack(ControlResult result) {
  switch (result) {
    case ControlResult::WRONG_MODE: return NackReason::WRONG_MODE;
    case ControlResult::WRONG_STATE: return NackReason::WRONG_STATE;
    case ControlResult::NOT_CONFIGURED: return NackReason::NOT_CONFIGURED;
    case ControlResult::BAD_VALUE:
    case ControlResult::OK:
      break;
  }
  return NackReason::BAD_VALUE;
}

NackReason CommandDispatcher::toNack(JobResult result) {
  switch (result) {
    case JobResult::WRONG_STATE: return NackReason::WRONG_STATE;
    case JobResult::WRONG_MODE: return NackReason::WRONG_MODE;
    case JobResult::NOT_CONFIGURED: return NackReason::NOT_CONFIGURED;
    case JobResult::BAD_INDEX: return NackReason::BAD_INDEX;
    case JobResult::JOB_FULL: return NackReason::JOB_FULL;
    case JobResult::BAD_JOB: return NackReason::BAD_JOB;
    case JobResult::BAD_VALUE:
    case JobResult::OK:
      break;
  }
  return NackReason::BAD_VALUE;
}

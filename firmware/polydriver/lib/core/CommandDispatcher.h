#ifndef COMMAND_DISPATCHER_H
#define COMMAND_DISPATCHER_H

#include <stdint.h>
#include "ProtocolDefs.h"
#include "FrameParser.h"
#include "ReplyBuilder.h"
#include "DispenserController.h"
#include "JobProgram.h"
#include "JobRunner.h"
#include "PeriodicTimer.h"
#include "RoleStorage.h"

constexpr FirmwareVersion FIRMWARE_VERSION{3, 0, 0};

// Values only the hardware layer knows, read once per call by main.cpp.
struct BoardInputs {
  float rawSpeedHz;
  uint32_t edgeCount;
  bool encoderStalled;
  bool watchdogReset;
};

class CommandDispatcher {
public:
  CommandDispatcher(DispenserController& dispenserController, JobProgram& jobProgram,
                    JobRunner& jobRunner, const FrameParser& frameParser,
                    PeriodicTimer& controlTimer, RoleStorage& storage);

  void begin();
  bool handle(ParseEvent event, const Frame& frame, uint32_t nowUs, const BoardInputs& inputs,
              ReplyFrame& reply);

  ProtocolDefs::Role getRole() const { return role; }

private:
  void execute(const Frame& frame, uint32_t nowUs, const BoardInputs& inputs, ReplyFrame& reply);
  void replyTo(uint8_t sequence, ControlResult result, ReplyFrame& reply);
  void replyTo(uint8_t sequence, JobResult result, ReplyFrame& reply);
  void buildStatus(uint8_t sequence, uint32_t nowUs, const BoardInputs& inputs, ReplyFrame& reply);
  void buildSettings(uint8_t sequence, ReplyFrame& reply);
  void setRole(const Frame& frame, ReplyFrame& reply);
  uint8_t statusFlags(const BoardInputs& inputs) const;

  static bool isRefusedWhileRunning(ProtocolDefs::Command command);
  static ProtocolDefs::NackReason toNack(ControlResult result);
  static ProtocolDefs::NackReason toNack(JobResult result);

  DispenserController& controller;
  JobProgram& program;
  JobRunner& runner;
  const FrameParser& parser;
  PeriodicTimer& timer;
  RoleStorage& roleStorage;

  ProtocolDefs::Role role{ProtocolDefs::Role::NONE};
  bool hasLastReply{false};
  uint8_t lastCommand{0};
  uint8_t lastSequence{0};
  ReplyFrame lastReply{};
};

#endif

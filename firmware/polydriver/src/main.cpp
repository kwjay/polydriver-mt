#include <Arduino.h>
#include "CommandDispatcher.h"
#include "EepromStorage.h"
#include "InputCapture.h"
#include "PWMGenerator.h"
#include "Watchdog.h"

constexpr uint32_t BAUD_RATE = 115200;
constexpr uint32_t CONTROL_PERIOD_US = 10000;
constexpr float CONTROL_DT_S = 0.01f;
constexpr PwmPrescaler PWM_PRESCALER = PwmPrescaler::DIV_1;

InputCapture encoder;
PWMGenerator pwm;
EepromStorage roleStorage;

DispenserController controller;
JobProgram program;
JobRunner runner(program, controller);
FrameParser parser;
PeriodicTimer controlTimer(CONTROL_PERIOD_US);
CommandDispatcher dispatcher(controller, program, runner, parser, controlTimer, roleStorage);

ISR(TIMER1_CAPT_vect) {
  encoder.handleCapture();
}

ISR(TIMER1_OVF_vect) {
  encoder.handleOverflow();
}

static BoardInputs readBoardInputs() {
  const CaptureReading reading = encoder.read();
  return BoardInputs{reading.frequencyHz, reading.edgeCount, reading.stalled,
                     Watchdog::lastResetWasWatchdog()};
}

static void serviceSerial() {
  while (Serial.available() > 0) {
    const uint8_t received = static_cast<uint8_t>(Serial.read());
    const ParseEvent event = parser.feed(received, millis());
    if (event == ParseEvent::NONE) continue;

    ReplyFrame reply;
    if (dispatcher.handle(event, parser.getFrame(), micros(), readBoardInputs(), reply)) {
      Serial.write(reply.bytes, reply.length);
    }
  }
}

static void runControlTick(uint32_t nowUs) {
  runner.tick(nowUs);
  const CaptureReading reading = encoder.read();
  pwm.setDuty(controller.update(reading.frequencyHz, nowUs, CONTROL_DT_S));
}

void setup() {
  pwm.begin(PWM_PRESCALER);
  encoder.begin();
  dispatcher.begin();
  Serial.begin(BAUD_RATE);
  controlTimer.reset(micros());
  Watchdog::begin();
}

void loop() {
  Watchdog::kick();
  serviceSerial();

  const uint32_t nowUs = micros();
  if (controlTimer.due(nowUs)) runControlTick(nowUs);
}

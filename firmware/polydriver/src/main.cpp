#include <Arduino.h>
#include <avr/wdt.h>
#include "input_capture.h"
#include "pwm_generator.h"
#include "protocol_handler.h"
#include "dispenser_controller.h"
#include "periodic_timer.h"

static constexpr uint32_t CONTROL_PERIOD_US = 10000;
static constexpr float CONTROL_DT_S = 0.01f;

InputCapture encoder;
PWMGenerator pwm;
ProtocolHandler protocol;
DispenserController controller;
PeriodicTimer controlTimer(CONTROL_PERIOD_US);

ISR(TIMER1_CAPT_vect) {
  encoder.handleInputCapture();
}

ISR(TIMER1_OVF_vect) {
  encoder.handleTimerOverflow();
}

void serialTransmit(const uint8_t* data, uint8_t len) {
  Serial.write(data, len);
}

void setup() {
  wdt_disable();
  Serial.begin(115200);
  protocol.setTxCallback(serialTransmit);
  protocol.setTimeSource(millis);
  encoder.init();
  pwm.init();
  // Watchdog: MCU reset after 500 ms without wdt_reset()
  wdt_enable(WDTO_500MS);
}

void loop() {
  wdt_reset();

  while (Serial.available() > 0) {
    CommandEvent event = protocol.processByte(Serial.read());
    if (event != CommandEvent::NONE) controller.onHostActivity(micros());

    switch (event) {
      case CommandEvent::SPEED_UPDATED:
        controller.setTargetSpeed(protocol.getSpeed());
        if (!controller.isRegulating()) pwm.setDutyCycle(0);
        break;

      case CommandEvent::PID_UPDATED: {
        const PidSettings& newPid = protocol.getPidSettings();
        controller.setTunings(newPid.kp, newPid.ki, newPid.kd);
        break;
      }

      case CommandEvent::TELEMETRY_REQUESTED: {
        TelemetrySnapshot t;
        t.rawFrequency = encoder.getSignalFrequency();
        t.filteredFrequency = controller.getFilteredFrequency();
        t.pwm = pwm.getDutyCycle();
        t.flags = (encoder.getIsStalled() ? StatusFlag::ENCODER_STALLED : 0) |
                  (controller.isFaulted() ? StatusFlag::STALL_FAULT : 0) |
                  (controller.isRegulating() ? StatusFlag::REGULATING : 0) |
                  (controller.isCommsLost() ? StatusFlag::COMMS_LOST : 0);
        t.deviceMicros = micros();
        t.edgeCount = encoder.getEdgeCount();
        t.missedCycles = controlTimer.getMissedCycles();
        t.maxLoopIntervalUs = controlTimer.takeMaxIntervalUs();
        protocol.sendTelemetry(t);
        break;
      }

      case CommandEvent::SYNC_REQUESTED:
        protocol.sendSettings(controller.getKp(), controller.getKi(),
                              controller.getKd(), controller.getTargetSpeed());
        break;

      default:
        break;
    }
  }

  uint32_t now = micros();
  if (controlTimer.due(now)) {
    pwm.setDutyCycle(controller.update(encoder.getSignalFrequency(),
                                       encoder.getIsStalled(),
                                       now, CONTROL_DT_S));
  }
}

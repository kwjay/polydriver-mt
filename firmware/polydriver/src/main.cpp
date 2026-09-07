#include <Arduino.h>
#include "input_capture.h"
#include "pid_regulator.h"
#include "pwm_generator.h"
#include "protocol_handler.h"
#include "ema_filter.h"

InputCapture encoder;
PIDRegulator pid;
PWMGenerator pwm;
ProtocolHandler protocol;
EMAFilter ema;

float targetSpeed = 0.0f;
bool regulateSignal = false;

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
  Serial.begin(115200); 
  protocol.setTxCallback(serialTransmit);
  encoder.init();
  pwm.init();
  ema.setAlpha(0.2f);
  pid.setTunings(pid.getKp(), pid.getKi(), pid.getKd());
}

unsigned long previousMicros = 0;
const unsigned long sampleTime = 10000;
void loop() {
  while (Serial.available() > 0) {
    uint8_t incomingByte = Serial.read();
    CommandEvent event = protocol.processByte(incomingByte);
    if (event != CommandEvent::NONE) {
        switch (event) {
          case CommandEvent::SPEED_UPDATED:
            targetSpeed = protocol.getSpeed();
            regulateSignal = (targetSpeed > 0.0f);
            
            if (!regulateSignal) {
              pid.reset();
              ema.reset();
              pwm.setDutyCycle(0);
            }
            break;
            
          case CommandEvent::PID_UPDATED: {
            const PidSettings& newPid = protocol.getPidSettings();
            pid.setTunings(newPid.kp, newPid.ki, newPid.kd);
            break;
          }
            
          case CommandEvent::TELEMETRY_REQUESTED: {
            float rawFreq = encoder.getSignalFrequency();
            uint8_t currentPwm = pwm.getDutyCycle();
            uint8_t stalledStatus = encoder.getIsStalled() ? 1 : 0;
            protocol.sendTelemetry(rawFreq, currentPwm, stalledStatus);
            break;
          }
            
          case CommandEvent::SYNC_REQUESTED:
            protocol.sendSettings(pid.getKp(), pid.getKi(), pid.getKd(), targetSpeed);
            break;
          default:
            break;
        }
      }
  }

  unsigned long currentMicros = micros();
  if (currentMicros - previousMicros >= sampleTime) {
    previousMicros = currentMicros;

    float rawFrequency = encoder.getSignalFrequency();
    float filteredFrequency = ema.filter(rawFrequency);

    if (regulateSignal) {
      float dt = static_cast<float>(sampleTime) / 1000000.0f;
      float pidOutput = pid.calculate(targetSpeed, filteredFrequency, dt);
      pwm.setDutyCycle(static_cast<int16_t>(pidOutput));
    }
  }

}

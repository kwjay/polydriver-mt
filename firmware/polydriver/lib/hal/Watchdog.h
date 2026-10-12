#ifndef WATCHDOG_H
#define WATCHDOG_H

#include <Arduino.h>

namespace Watchdog {

// System-reset mode, 500 ms (protocol.md section 10).
void begin();
void kick();

// True only if MCUSR still held WDRF at boot. The stock Uno bootloader (optiboot) clears MCUSR
// before the sketch starts, so on a stock Uno this is always false.
bool lastResetWasWatchdog();

}

#endif

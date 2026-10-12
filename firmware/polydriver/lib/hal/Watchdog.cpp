#include "Watchdog.h"
#include <avr/wdt.h>

namespace {

// Hardware exception to "no global mutable state": written in .init3, before the C runtime and
// before any object exists, so it cannot live in an object. .noinit keeps the runtime from
// clearing it afterwards.
uint8_t resetFlags __attribute__((section(".noinit")));

}

// After a watchdog reset the watchdog stays on with its shortest timeout (about 16 ms). It has to be
// switched off before the runtime and setup() run, or the board resets again in a loop.
void captureResetFlags() __attribute__((naked, used, section(".init3")));
void captureResetFlags() {
  resetFlags = MCUSR;
  MCUSR = 0;
  wdt_disable();
}

namespace Watchdog {

void begin() {
  wdt_enable(WDTO_500MS);
}

void kick() {
  wdt_reset();
}

bool lastResetWasWatchdog() {
  return (resetFlags & _BV(WDRF)) != 0;
}

}

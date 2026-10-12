#ifndef CRC8_H
#define CRC8_H

#include <stdint.h>

// CRC-8 Dallas/Maxim: reflected polynomial 0x8C, init 0x00, no final XOR (protocol.md section 2).
// Must match the routine in software/polydriver/protocol.
namespace Crc8 {

constexpr uint8_t INITIAL = 0x00;

inline uint8_t update(uint8_t crc, uint8_t byte) {
  for (uint8_t bit = 0; bit < 8; bit++) {
    const bool mix = ((crc ^ byte) & 0x01) != 0;
    crc >>= 1;
    if (mix) crc ^= 0x8C;
    byte >>= 1;
  }
  return crc;
}

inline uint8_t compute(const uint8_t* data, uint8_t length) {
  uint8_t crc = INITIAL;
  for (uint8_t i = 0; i < length; i++) crc = update(crc, data[i]);
  return crc;
}

}

#endif

#ifndef LITTLE_ENDIAN_H
#define LITTLE_ENDIAN_H

#include <stdint.h>
#include <string.h>

// Wire byte order (protocol.md section 1), independent of the CPU's own byte order.
namespace LittleEndian {

inline void putU16(uint8_t* destination, uint16_t value) {
  destination[0] = static_cast<uint8_t>(value);
  destination[1] = static_cast<uint8_t>(value >> 8);
}

inline void putU32(uint8_t* destination, uint32_t value) {
  for (uint8_t i = 0; i < 4; i++) destination[i] = static_cast<uint8_t>(value >> (8 * i));
}

inline void putFloat(uint8_t* destination, float value) {
  uint32_t bits;
  memcpy(&bits, &value, sizeof(bits));
  putU32(destination, bits);
}

inline uint16_t getU16(const uint8_t* source) {
  return static_cast<uint16_t>(source[0] | (source[1] << 8));
}

inline uint32_t getU32(const uint8_t* source) {
  return static_cast<uint32_t>(source[0]) | (static_cast<uint32_t>(source[1]) << 8) |
         (static_cast<uint32_t>(source[2]) << 16) | (static_cast<uint32_t>(source[3]) << 24);
}

inline float getFloat(const uint8_t* source) {
  const uint32_t bits = getU32(source);
  float value;
  memcpy(&value, &bits, sizeof(value));
  return value;
}

}

#endif

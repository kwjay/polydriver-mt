#ifndef ROLE_RECORD_H
#define ROLE_RECORD_H

#include <stdint.h>

// The role is stored as two bytes: the role and its bitwise inverse. Any mismatch, including
// unprogrammed EEPROM (0xFF 0xFF) or a write cut off between the two bytes, reads as INVALID,
// so a damaged record can never turn into a different valid role.
namespace RoleRecord {

constexpr uint8_t INVALID = 0xFF;

inline uint8_t checkByte(uint8_t role) {
  return static_cast<uint8_t>(~role);
}

inline uint8_t decode(uint8_t stored, uint8_t check) {
  return (check == checkByte(stored)) ? stored : INVALID;
}

}

#endif

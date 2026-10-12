#include "EepromStorage.h"
#include <avr/eeprom.h>
#include "RoleRecord.h"

namespace {

uint8_t* eepromAddress(uint16_t address) {
  return reinterpret_cast<uint8_t*>(address);
}

}

uint8_t EepromStorage::loadRole() {
  const uint8_t stored = eeprom_read_byte(eepromAddress(ROLE_ADDRESS));
  const uint8_t check = eeprom_read_byte(eepromAddress(CHECK_ADDRESS));
  return RoleRecord::decode(stored, check);
}

void EepromStorage::saveRole(uint8_t role) {
  // eeprom_update_byte only writes when the value differs. Each write blocks for about 3.4 ms.
  // Role first, check byte second: a power cut in between leaves a mismatch, which reads as
  // INVALID (no role) rather than as a wrong role.
  eeprom_update_byte(eepromAddress(ROLE_ADDRESS), role);
  eeprom_update_byte(eepromAddress(CHECK_ADDRESS), RoleRecord::checkByte(role));
}

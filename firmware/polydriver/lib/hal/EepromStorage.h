#ifndef EEPROM_STORAGE_H
#define EEPROM_STORAGE_H

#include <Arduino.h>
#include "RoleStorage.h"

class EepromStorage final : public RoleStorage {
public:
  uint8_t loadRole() override;
  void saveRole(uint8_t role) override;

private:
  // EEPROM byte addresses of the role record (RoleRecord.h).
  static constexpr uint16_t ROLE_ADDRESS = 0;
  static constexpr uint16_t CHECK_ADDRESS = 1;
};

#endif

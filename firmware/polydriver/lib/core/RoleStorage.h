#ifndef ROLE_STORAGE_H
#define ROLE_STORAGE_H

#include <stdint.h>

// Where the board role survives a power cycle: EEPROM on the Uno (lib/hal), a fake in tests.
class RoleStorage {
public:
  virtual uint8_t loadRole() = 0;
  virtual void saveRole(uint8_t role) = 0;

protected:
  ~RoleStorage() = default;
};

#endif

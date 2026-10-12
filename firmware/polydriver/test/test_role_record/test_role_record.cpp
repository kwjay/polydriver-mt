#include <unity.h>
#include "RoleRecord.h"
#include "ProtocolDefs.h"

void setUp(void) {}
void tearDown(void) {}

void test_role_record_valid_roles_round_trip() {
  for (uint8_t role = 0; role <= static_cast<uint8_t>(ProtocolDefs::Role::COLOUR); role++) {
    TEST_ASSERT_EQUAL_UINT8(role, RoleRecord::decode(role, RoleRecord::checkByte(role)));
  }
}

void test_role_record_check_byte_is_the_inverse() {
  TEST_ASSERT_EQUAL_HEX8(0xFF, RoleRecord::checkByte(0x00));
  TEST_ASSERT_EQUAL_HEX8(0xFE, RoleRecord::checkByte(0x01));
  TEST_ASSERT_EQUAL_HEX8(0xFD, RoleRecord::checkByte(0x02));
}

void test_role_record_unprogrammed_eeprom_is_invalid() {
  TEST_ASSERT_EQUAL_HEX8(RoleRecord::INVALID, RoleRecord::decode(0xFF, 0xFF));
}

void test_role_record_all_zero_is_invalid() {
  TEST_ASSERT_EQUAL_HEX8(RoleRecord::INVALID, RoleRecord::decode(0x00, 0x00));
}

void test_role_record_a_write_cut_after_the_role_byte_is_invalid() {
  // Old record "polymer" (01 FE), new role "colour" written, check byte not yet.
  TEST_ASSERT_EQUAL_HEX8(RoleRecord::INVALID, RoleRecord::decode(0x02, 0xFE));
}

void test_role_record_any_single_bit_flip_is_invalid() {
  for (uint8_t role = 0; role <= 2; role++) {
    const uint8_t check = RoleRecord::checkByte(role);
    for (uint8_t bit = 0; bit < 8; bit++) {
      const uint8_t mask = static_cast<uint8_t>(1u << bit);
      TEST_ASSERT_EQUAL_HEX8(RoleRecord::INVALID,
                             RoleRecord::decode(static_cast<uint8_t>(role ^ mask), check));
      TEST_ASSERT_EQUAL_HEX8(RoleRecord::INVALID,
                             RoleRecord::decode(role, static_cast<uint8_t>(check ^ mask)));
    }
  }
}

void test_role_record_invalid_is_not_a_valid_role() {
  TEST_ASSERT_TRUE(RoleRecord::INVALID > static_cast<uint8_t>(ProtocolDefs::Role::COLOUR));
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_role_record_valid_roles_round_trip);
  RUN_TEST(test_role_record_check_byte_is_the_inverse);
  RUN_TEST(test_role_record_unprogrammed_eeprom_is_invalid);
  RUN_TEST(test_role_record_all_zero_is_invalid);
  RUN_TEST(test_role_record_a_write_cut_after_the_role_byte_is_invalid);
  RUN_TEST(test_role_record_any_single_bit_flip_is_invalid);
  RUN_TEST(test_role_record_invalid_is_not_a_valid_role);
  return UNITY_END();
}

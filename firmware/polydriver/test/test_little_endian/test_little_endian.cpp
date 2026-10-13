#include <unity.h>
#include "LittleEndian.h"

void setUp(void) {}
void tearDown(void) {}

void test_little_endian_u16_byte_order() {
  uint8_t bytes[2];
  LittleEndian::putU16(bytes, 0x1234);
  TEST_ASSERT_EQUAL_HEX8(0x34, bytes[0]);
  TEST_ASSERT_EQUAL_HEX8(0x12, bytes[1]);
}

void test_little_endian_u16_high_bit_values() {
  const uint16_t values[] = {0x0000, 0x007F, 0x0080, 0x7FFF, 0x8000, 0xC350, 0xFF00, 0xFFFF};
  for (uint16_t value : values) {
    uint8_t bytes[2];
    LittleEndian::putU16(bytes, value);
    TEST_ASSERT_EQUAL_HEX16(value, LittleEndian::getU16(bytes));
  }
  const uint8_t raw[] = {0xFE, 0xFF};
  TEST_ASSERT_EQUAL_HEX16(0xFFFE, LittleEndian::getU16(raw));
}

void test_little_endian_u32_byte_order_and_high_bit_values() {
  uint8_t bytes[4];
  LittleEndian::putU32(bytes, 0x89ABCDEFUL);
  const uint8_t expected[] = {0xEF, 0xCD, 0xAB, 0x89};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, bytes, 4);
  TEST_ASSERT_EQUAL_HEX32(0x89ABCDEFUL, LittleEndian::getU32(bytes));
  LittleEndian::putU32(bytes, 0xFFFFFFFFUL);
  TEST_ASSERT_EQUAL_HEX32(0xFFFFFFFFUL, LittleEndian::getU32(bytes));
}

void test_little_endian_float_round_trip() {
  const float values[] = {0.0f, -0.0f, 1.0f, 100.0f, -2.5f, 0.2f, 3.4e38f, 1.0e-38f};
  for (float value : values) {
    uint8_t bytes[4];
    LittleEndian::putFloat(bytes, value);
    TEST_ASSERT_EQUAL_FLOAT(value, LittleEndian::getFloat(bytes));
  }
  uint8_t bytes[4];
  LittleEndian::putFloat(bytes, 100.0f);
  const uint8_t expected[] = {0x00, 0x00, 0xC8, 0x42};
  TEST_ASSERT_EQUAL_HEX8_ARRAY(expected, bytes, 4);
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_little_endian_u16_byte_order);
  RUN_TEST(test_little_endian_u16_high_bit_values);
  RUN_TEST(test_little_endian_u32_byte_order_and_high_bit_values);
  RUN_TEST(test_little_endian_float_round_trip);
  return UNITY_END();
}

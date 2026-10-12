#include <unity.h>
#include "Crc8.h"

void setUp(void) {}
void tearDown(void) {}

void test_crc8_check_value() {
  const uint8_t ascii[] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
  TEST_ASSERT_EQUAL_HEX8(0xA1, Crc8::compute(ascii, sizeof(ascii)));
}

void test_crc8_of_nothing_is_the_initial_value() {
  TEST_ASSERT_EQUAL_HEX8(0x00, Crc8::compute(nullptr, 0));
}

void test_crc8_byte_by_byte_equals_the_whole_buffer() {
  const uint8_t data[] = {0x01, 0x10, 0x04, 0x07, 0x00, 0x00, 0x48, 0x42};
  uint8_t crc = Crc8::INITIAL;
  for (uint8_t byte : data) crc = Crc8::update(crc, byte);
  TEST_ASSERT_EQUAL_HEX8(Crc8::compute(data, sizeof(data)), crc);
}

void test_crc8_detects_every_single_bit_error() {
  uint8_t data[] = {0x01, 0x20, 0x0C, 0x05, 0x11, 0x22, 0x33, 0x44};
  const uint8_t good = Crc8::compute(data, sizeof(data));
  for (uint8_t i = 0; i < sizeof(data); i++) {
    for (uint8_t bit = 0; bit < 8; bit++) {
      data[i] ^= static_cast<uint8_t>(1u << bit);
      TEST_ASSERT_NOT_EQUAL(good, Crc8::compute(data, sizeof(data)));
      data[i] ^= static_cast<uint8_t>(1u << bit);
    }
  }
}

int main(void) {
  UNITY_BEGIN();
  RUN_TEST(test_crc8_check_value);
  RUN_TEST(test_crc8_of_nothing_is_the_initial_value);
  RUN_TEST(test_crc8_byte_by_byte_equals_the_whole_buffer);
  RUN_TEST(test_crc8_detects_every_single_bit_error);
  return UNITY_END();
}

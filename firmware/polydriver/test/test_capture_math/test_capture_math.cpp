#include <unity.h>
#include "capture_math.h"

void setUp(void) {}
void tearDown(void) {}

void test_timestamp_is_reconstructed_from_overflows_and_capture() {
	TEST_ASSERT_EQUAL_UINT32(0x00000000UL, capture_math::reconstructTimestamp(0, 0x0000));
	TEST_ASSERT_EQUAL_UINT32(0x0000ABCDUL, capture_math::reconstructTimestamp(0, 0xABCD));
	TEST_ASSERT_EQUAL_UINT32(0x0003ABCDUL, capture_math::reconstructTimestamp(3, 0xABCD));
}

void test_consecutive_timestamps_span_an_overflow_correctly() {
	uint32_t before = capture_math::reconstructTimestamp(7, 0xFFF0);
	uint32_t after = capture_math::reconstructTimestamp(8, 0x0010);
	TEST_ASSERT_EQUAL_UINT32(0x20, after - before);
}

void test_zero_period_reports_no_signal() {
	TEST_ASSERT_EQUAL_FLOAT(0.0f, capture_math::frequencyFromPeriod(0));
}

void test_frequency_matches_the_timer_configuration() {
	// 16 MHz / 64 = 250 kHz tick rate, so 250 ticks is exactly 1 kHz.
	TEST_ASSERT_FLOAT_WITHIN(0.01f, 1000.0f, capture_math::frequencyFromPeriod(250));
	TEST_ASSERT_FLOAT_WITHIN(0.01f, 62.5f, capture_math::frequencyFromPeriod(4000));
}

void test_minimum_measurable_frequency() {
	// Two full 16-bit timer periods at 4us per tick is the stall threshold.
	TEST_ASSERT_FLOAT_WITHIN(0.001f, 1.907f, capture_math::minMeasurableFrequency());
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_timestamp_is_reconstructed_from_overflows_and_capture);
	RUN_TEST(test_consecutive_timestamps_span_an_overflow_correctly);
	RUN_TEST(test_zero_period_reports_no_signal);
	RUN_TEST(test_frequency_matches_the_timer_configuration);
	RUN_TEST(test_minimum_measurable_frequency);
	return UNITY_END();
}

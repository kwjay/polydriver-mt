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
	TEST_ASSERT_EQUAL_FLOAT(capture_math::frequencyFromPeriod(capture_math::STALL_TIMEOUT_TICKS),
	                        capture_math::minMeasurableFrequency());
}

void test_edge_just_before_an_overflow_times_out_two_periods_later() {
	uint32_t edge = capture_math::reconstructTimestamp(10, 0xFFFF);
	TEST_ASSERT_FALSE(capture_math::edgeTimedOut(11, edge));
	TEST_ASSERT_FALSE(capture_math::edgeTimedOut(12, edge));
	TEST_ASSERT_TRUE(capture_math::edgeTimedOut(13, edge));
}

void test_edge_just_after_an_overflow_times_out_two_periods_later() {
	uint32_t edge = capture_math::reconstructTimestamp(10, 0x0001);
	TEST_ASSERT_FALSE(capture_math::edgeTimedOut(11, edge));
	TEST_ASSERT_FALSE(capture_math::edgeTimedOut(12, edge));
	TEST_ASSERT_TRUE(capture_math::edgeTimedOut(13, edge));
}

void test_period_below_the_threshold_never_times_out_whatever_the_phase() {
	const uint32_t period = capture_math::TCNT_MAX_VALUE + capture_math::TCNT_MAX_VALUE / 2;
	for (uint32_t phase = 0; phase < capture_math::TCNT_MAX_VALUE; phase += 997) {
		uint32_t edge = capture_math::reconstructTimestamp(100, 0) + phase;
		uint32_t next = edge + period;
		for (uint32_t ovf = 101; (ovf << 16) < next; ovf++) {
			TEST_ASSERT_FALSE(capture_math::edgeTimedOut(ovf, edge));
		}
	}
}

void test_edge_timeout_survives_a_timestamp_wrap() {
	uint32_t edge = capture_math::reconstructTimestamp(0xFFFF, 0xFFF0);
	TEST_ASSERT_FALSE(capture_math::edgeTimedOut(0x10001, edge));
	TEST_ASSERT_TRUE(capture_math::edgeTimedOut(0x10002, edge));
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_timestamp_is_reconstructed_from_overflows_and_capture);
	RUN_TEST(test_consecutive_timestamps_span_an_overflow_correctly);
	RUN_TEST(test_zero_period_reports_no_signal);
	RUN_TEST(test_frequency_matches_the_timer_configuration);
	RUN_TEST(test_minimum_measurable_frequency);
	RUN_TEST(test_edge_just_before_an_overflow_times_out_two_periods_later);
	RUN_TEST(test_edge_just_after_an_overflow_times_out_two_periods_later);
	RUN_TEST(test_period_below_the_threshold_never_times_out_whatever_the_phase);
	RUN_TEST(test_edge_timeout_survives_a_timestamp_wrap);
	return UNITY_END();
}

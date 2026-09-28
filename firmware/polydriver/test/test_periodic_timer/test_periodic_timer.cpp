#include <unity.h>
#include "periodic_timer.h"

static constexpr uint32_t PERIOD = 10000;

void setUp(void) {}
void tearDown(void) {}

void test_timer_first_call_starts_the_schedule_without_firing() {
	PeriodicTimer timer(PERIOD);
	TEST_ASSERT_FALSE(timer.due(0));
}

void test_timer_does_not_fire_before_the_period_elapses() {
	PeriodicTimer timer(PERIOD);
	timer.due(0);
	TEST_ASSERT_FALSE(timer.due(PERIOD - 1));
	TEST_ASSERT_TRUE(timer.due(PERIOD));
}

void test_timer_fires_once_per_period() {
	PeriodicTimer timer(PERIOD);
	timer.due(0);
	uint16_t fires = 0;
	for (uint32_t t = 1; t <= 5 * PERIOD; t++) {
		if (timer.due(t)) fires++;
	}
	TEST_ASSERT_EQUAL_UINT16(5, fires);
}

void test_timer_late_cycle_does_not_shift_the_grid() {
	PeriodicTimer timer(PERIOD);
	timer.due(0);

	TEST_ASSERT_TRUE(timer.due(PERIOD + 500));

	// The next deadline is still on the original grid, not 500us later.
	TEST_ASSERT_FALSE(timer.due(2 * PERIOD - 1));
	TEST_ASSERT_TRUE(timer.due(2 * PERIOD));
	TEST_ASSERT_EQUAL_UINT16(0, timer.getMissedCycles());
}

void test_timer_resynchronises_when_more_than_a_period_behind() {
	PeriodicTimer timer(PERIOD);
	timer.due(0);

	TEST_ASSERT_TRUE(timer.due(25000));
	TEST_ASSERT_EQUAL_UINT16(1, timer.getMissedCycles());

	// After resync the next deadline is one period from the late call,
	// so no burst of back-to-back cycles follows.
	TEST_ASSERT_FALSE(timer.due(34999));
	TEST_ASSERT_TRUE(timer.due(35000));
}

void test_timer_reports_the_measured_interval() {
	PeriodicTimer timer(PERIOD);
	timer.due(0);
	timer.due(PERIOD + 250);
	TEST_ASSERT_EQUAL_UINT32(PERIOD + 250, timer.getLastIntervalUs());
	timer.due(2 * PERIOD);
	TEST_ASSERT_EQUAL_UINT32(PERIOD - 250, timer.getLastIntervalUs());
}

void test_timer_survives_a_micros_rollover() {
	PeriodicTimer timer(PERIOD);
	timer.due(0xFFFFFF00UL);
	TEST_ASSERT_FALSE(timer.due(9000));
	TEST_ASSERT_TRUE(timer.due(9744));
	TEST_ASSERT_EQUAL_UINT16(0, timer.getMissedCycles());
}

void test_timer_reset_rearms_from_the_given_moment() {
	PeriodicTimer timer(PERIOD);
	timer.due(0);
	timer.reset(5000);
	TEST_ASSERT_FALSE(timer.due(14999));
	TEST_ASSERT_TRUE(timer.due(15000));
}

int main(void) {
	UNITY_BEGIN();
	RUN_TEST(test_timer_first_call_starts_the_schedule_without_firing);
	RUN_TEST(test_timer_does_not_fire_before_the_period_elapses);
	RUN_TEST(test_timer_fires_once_per_period);
	RUN_TEST(test_timer_late_cycle_does_not_shift_the_grid);
	RUN_TEST(test_timer_resynchronises_when_more_than_a_period_behind);
	RUN_TEST(test_timer_reports_the_measured_interval);
	RUN_TEST(test_timer_survives_a_micros_rollover);
	RUN_TEST(test_timer_reset_rearms_from_the_given_moment);
	return UNITY_END();
}

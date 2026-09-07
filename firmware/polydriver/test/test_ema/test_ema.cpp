#include <unity.h> 
#include "ema_filter.h"

void setUp(void) {

}

void tearDown(void) {
}

void test_ema_filter_initialization(void) {
	EMAFilter filter;
	float result = filter.filter(10.0f);
	TEST_ASSERT_EQUAL_FLOAT(10.0f, result);
}

void test_ema_filter_smoothing(void) {
	EMAFilter filter;
	float result1 = filter.filter(10.0f);
	float result2 = filter.filter(20.0f);
	float expected = (0.2f * 20.0f) + (0.8f * 10.0f);
	TEST_ASSERT_EQUAL_FLOAT(expected, result2);
}

void test_ema_filter_reset(void) {
	EMAFilter filter;
	for (int i = 0; i < 5; ++i) {
		filter.filter(static_cast<float>(i * 10));
	}
	filter.reset();
	float result = filter.filter(20.0f);
	TEST_ASSERT_EQUAL_FLOAT(20.0f, result);
}

void test_ema_filter_set_alpha(void) {
	EMAFilter filter;
	filter.setAlpha(0.5f);
	float result1 = filter.filter(10.0f);
	float result2 = filter.filter(20.0f);
	float expected = (0.5f * 20.0f) + (0.5f * 10.0f);
	TEST_ASSERT_EQUAL_FLOAT(expected, result2);
}

void test_ema_filter_invalid_alpha(void) {
	EMAFilter filter;
	filter.setAlpha(-0.1f); 
	float result1 = filter.filter(10.0f);
	float result2 = filter.filter(20.0f);
	float expected = (0.2f * 20.0f) + (0.8f * 10.0f); 
	TEST_ASSERT_EQUAL_FLOAT(expected, result2);
}



int main() {
	UNITY_BEGIN();
	RUN_TEST(test_ema_filter_initialization);
	RUN_TEST(test_ema_filter_smoothing);
	RUN_TEST(test_ema_filter_reset);
	RUN_TEST(test_ema_filter_set_alpha);
	RUN_TEST(test_ema_filter_invalid_alpha);
	UNITY_END();
}
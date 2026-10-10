#include <unity.h>
#include <math.h>
#include "EMAFilter.h"

void setUp(void) {}
void tearDown(void) {}

void test_ema_filter_initialization(void) {
  EMAFilter filter;
  TEST_ASSERT_EQUAL_FLOAT(10.0f, filter.filter(10.0f));
}

void test_ema_filter_default_alpha_is_one(void) {
  EMAFilter filter;
  TEST_ASSERT_EQUAL_FLOAT(1.0f, EMAFilter::DEFAULT_ALPHA);
  TEST_ASSERT_EQUAL_FLOAT(EMAFilter::DEFAULT_ALPHA, filter.getAlpha());
}

void test_ema_filter_unconfigured_passes_samples_through(void) {
  EMAFilter filter;
  filter.filter(0.0f);
  TEST_ASSERT_EQUAL_FLOAT(20.0f, filter.filter(20.0f));
  TEST_ASSERT_EQUAL_FLOAT(7.5f, filter.filter(7.5f));
}

void test_ema_filter_smoothing(void) {
  EMAFilter filter;
  filter.setAlpha(0.2f);
  filter.filter(10.0f);
  float expected = (0.2f * 20.0f) + (0.8f * 10.0f);
  TEST_ASSERT_EQUAL_FLOAT(expected, filter.filter(20.0f));
}

void test_ema_filter_reset(void) {
  EMAFilter filter;
  for (int i = 0; i < 5; ++i) {
    filter.filter(static_cast<float>(i * 10));
  }
  filter.reset();
  TEST_ASSERT_EQUAL_FLOAT(20.0f, filter.filter(20.0f));
}

void test_ema_filter_set_alpha(void) {
  EMAFilter filter;
  TEST_ASSERT_TRUE(filter.setAlpha(0.5f));
  TEST_ASSERT_EQUAL_FLOAT(0.5f, filter.getAlpha());
  filter.filter(10.0f);
  float expected = (0.5f * 20.0f) + (0.5f * 10.0f);
  TEST_ASSERT_EQUAL_FLOAT(expected, filter.filter(20.0f));
}

void test_ema_filter_alpha_one_passes_samples_through(void) {
  EMAFilter filter;
  TEST_ASSERT_TRUE(filter.setAlpha(1.0f));
  filter.filter(10.0f);
  TEST_ASSERT_EQUAL_FLOAT(20.0f, filter.filter(20.0f));
  TEST_ASSERT_EQUAL_FLOAT(-5.0f, filter.filter(-5.0f));
}

void test_ema_filter_rejects_invalid_alpha_and_keeps_the_old_one(void) {
  EMAFilter filter;
  filter.setAlpha(0.2f);
  const float invalid[] = {-0.1f, 0.0f, 1.0001f, 2.0f, NAN, INFINITY};
  for (float alpha : invalid) {
    TEST_ASSERT_FALSE(filter.setAlpha(alpha));
    TEST_ASSERT_EQUAL_FLOAT(0.2f, filter.getAlpha());
  }
  filter.filter(10.0f);
  float expected = (0.2f * 20.0f) + (0.8f * 10.0f);
  TEST_ASSERT_EQUAL_FLOAT(expected, filter.filter(20.0f));
}

void test_ema_filter_reset_keeps_a_custom_alpha(void) {
  EMAFilter filter;
  filter.setAlpha(0.5f);
  filter.filter(100.0f);
  filter.reset();
  TEST_ASSERT_EQUAL_FLOAT(0.5f, filter.getAlpha());
  filter.filter(10.0f);
  TEST_ASSERT_EQUAL_FLOAT(15.0f, filter.filter(20.0f));
}

void test_ema_filter_step_response(void) {
  // After a 0 -> 1 step the output after n samples is 1 - (1 - alpha)^n.
  const float alphas[] = {0.2f, 0.5f, 0.05f};
  for (float alpha : alphas) {
    EMAFilter filter;
    filter.setAlpha(alpha);
    filter.filter(0.0f);
    for (int n = 1; n <= 20; ++n) {
      float expected = 1.0f - powf(1.0f - alpha, static_cast<float>(n));
      TEST_ASSERT_FLOAT_WITHIN(1e-5f, expected, filter.filter(1.0f));
    }
  }
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_ema_filter_initialization);
  RUN_TEST(test_ema_filter_default_alpha_is_one);
  RUN_TEST(test_ema_filter_unconfigured_passes_samples_through);
  RUN_TEST(test_ema_filter_smoothing);
  RUN_TEST(test_ema_filter_reset);
  RUN_TEST(test_ema_filter_set_alpha);
  RUN_TEST(test_ema_filter_alpha_one_passes_samples_through);
  RUN_TEST(test_ema_filter_rejects_invalid_alpha_and_keeps_the_old_one);
  RUN_TEST(test_ema_filter_reset_keeps_a_custom_alpha);
  RUN_TEST(test_ema_filter_step_response);
  return UNITY_END();
}

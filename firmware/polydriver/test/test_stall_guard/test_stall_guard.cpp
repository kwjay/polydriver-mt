#include <unity.h>
#include "stall_guard.h"

void setUp(void) {}
void tearDown(void) {}

void test_stall_guard_no_fault_when_never_stalled() {
  StallGuard guard(100);
  for (uint32_t t = 0; t <= 500; t += 50) {
    TEST_ASSERT_FALSE(guard.update(false, t));
  }
  TEST_ASSERT_FALSE(guard.isFaulted());
}

void test_stall_guard_no_fault_before_timeout_elapses() {
  StallGuard guard(100);
  TEST_ASSERT_FALSE(guard.update(true, 0));
  TEST_ASSERT_FALSE(guard.update(true, 50));
  TEST_ASSERT_FALSE(guard.update(true, 99));
  TEST_ASSERT_FALSE(guard.isFaulted());
}

void test_stall_guard_faults_once_timeout_elapses() {
  StallGuard guard(100);
  guard.update(true, 0);
  guard.update(true, 50);
  bool justFaulted = guard.update(true, 100);
  TEST_ASSERT_TRUE(justFaulted);
  TEST_ASSERT_TRUE(guard.isFaulted());
}

void test_stall_guard_only_reports_the_fault_edge_once() {
  StallGuard guard(100);
  guard.update(true, 0);
  guard.update(true, 100);
  bool reportedAgain = guard.update(true, 200);
  TEST_ASSERT_FALSE(reportedAgain);
  TEST_ASSERT_TRUE(guard.isFaulted());
}

void test_stall_guard_intermittent_stalls_do_not_accumulate() {
  StallGuard guard(100);
  for (int cycle = 0; cycle < 5; ++cycle) {
    uint32_t base = cycle * 1000;
    guard.update(true, base);
    guard.update(true, base + 80);
    guard.update(false, base + 90);
  }
  TEST_ASSERT_FALSE(guard.isFaulted());
}

void test_stall_guard_stays_latched_until_reset() {
  StallGuard guard(100);
  guard.update(true, 0);
  guard.update(true, 100);
  TEST_ASSERT_TRUE(guard.isFaulted());

  guard.update(false, 200);
  TEST_ASSERT_TRUE(guard.isFaulted());

  guard.reset();
  TEST_ASSERT_FALSE(guard.isFaulted());
}

void test_stall_guard_reset_requires_a_full_new_timeout_to_fault_again() {
  StallGuard guard(100);
  guard.update(true, 0);
  guard.update(true, 100);
  guard.reset();

  TEST_ASSERT_FALSE(guard.update(true, 150));
  TEST_ASSERT_FALSE(guard.update(true, 200));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_stall_guard_no_fault_when_never_stalled);
  RUN_TEST(test_stall_guard_no_fault_before_timeout_elapses);
  RUN_TEST(test_stall_guard_faults_once_timeout_elapses);
  RUN_TEST(test_stall_guard_only_reports_the_fault_edge_once);
  RUN_TEST(test_stall_guard_intermittent_stalls_do_not_accumulate);
  RUN_TEST(test_stall_guard_stays_latched_until_reset);
  RUN_TEST(test_stall_guard_reset_requires_a_full_new_timeout_to_fault_again);
  UNITY_END();
}

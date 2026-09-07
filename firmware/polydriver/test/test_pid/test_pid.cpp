#include <unity.h> 
#include "pid_regulator.h"

void setUp(void) {
}

void tearDown(void) {
}

void test_pid_zero_error() {
    PIDRegulator pid(1.0f, 0.1f, 0.05f);
    float output = pid.calculate(100.0f, 100.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
}

void test_pid_proportional_term() {
    PIDRegulator pid(1.0f, 0.0f, 0.0f);
    float output = pid.calculate(100.0f, 90.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(10.0f, output);
}

void test_pid_integral_term() {
    PIDRegulator pid(0.0f, 1.0f, 0.0f);
    float output1 = pid.calculate(100.0f, 90.0f, 1.0f);
    float output2 = pid.calculate(100.0f, 90.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(10.0f, output1);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, output2);
}

void test_pid_integral_anti_windup() {
    PIDRegulator pid(0.0f, 1.0f, 0.0f);
    float output = 0.0f;
    for (int i = 0; i < 100; ++i) {
        output = pid.calculate(100.0f, 90.0f, 1.0f);
    }
    TEST_ASSERT_EQUAL_FLOAT(255.0f, output);
}

void test_pid_derivative_term() {
    PIDRegulator pid(0.0f, 0.0f, 1.0f);
    float output1 = pid.calculate(100.0f, 90.0f, 1.0f);
    float output2 = pid.calculate(100.0f, 95.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(10.0f, output1);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, output2);
}

void test_pid_zero_dt() {
    PIDRegulator pid(1.0f, 1.0f, 1.0f);
    float output = pid.calculate(100.0f, 90.0f, 0.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
}

void test_pid_lower_bound_clamping() {
    PIDRegulator pid(1.0f, 1.0f, 1.0f);
    float output = pid.calculate(90.0f, 100.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, output);
}

void test_pid_set_tunings() {
    PIDRegulator pid(0.0f, 0.0f, 0.0f);
    pid.setTunings(1.5f, 2.5f, 3.5f);
    TEST_ASSERT_EQUAL_FLOAT(1.5f, pid.getKp());
    TEST_ASSERT_EQUAL_FLOAT(2.5f, pid.getKi());
    TEST_ASSERT_EQUAL_FLOAT(3.5f, pid.getKd());
}

void test_pid_reset() {
    PIDRegulator pid(0.0f, 1.0f, 1.0f);
    pid.calculate(100.0f, 90.0f, 1.0f);
    pid.reset();
    float output = pid.calculate(100.0f, 90.0f, 1.0f);
    TEST_ASSERT_EQUAL_FLOAT(20.0f, output);
}

int main() {
    UNITY_BEGIN();
    RUN_TEST(test_pid_zero_error);
    RUN_TEST(test_pid_proportional_term);
    RUN_TEST(test_pid_integral_term);
    RUN_TEST(test_pid_integral_anti_windup);
    RUN_TEST(test_pid_derivative_term);
    RUN_TEST(test_pid_zero_dt);
    RUN_TEST(test_pid_lower_bound_clamping);
    RUN_TEST(test_pid_set_tunings);
    RUN_TEST(test_pid_reset);
    UNITY_END();
}
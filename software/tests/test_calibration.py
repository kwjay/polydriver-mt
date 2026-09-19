import os
import tempfile
import unittest

from control.calibration import CalibrationError, CalibrationStore


class TestTargetCalibrationInterpolation(unittest.TestCase):
	def setUp(self):
		self.store = CalibrationStore()

	def test_rate_for_speed_needs_at_least_two_distinct_speeds(self):
		self.store.add_measurement(1, speed=20.0, grams=5.0, duration_s=10.0)
		with self.assertRaises(CalibrationError):
			self.store.rate_for_speed(1, 20.0)

	def test_rate_for_speed_interpolates_between_two_points(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)   # 0.0 g/s
		self.store.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)  # 2.0 g/s
		self.assertAlmostEqual(self.store.rate_for_speed(1, 20.0), 1.0)
		self.assertAlmostEqual(self.store.rate_for_speed(1, 10.0), 0.0)
		self.assertAlmostEqual(self.store.rate_for_speed(1, 30.0), 2.0)

	def test_speed_for_rate_is_the_inverse_lookup(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)
		self.assertAlmostEqual(self.store.speed_for_rate(1, 1.0), 20.0)

	def test_rate_for_speed_refuses_to_extrapolate_above_range(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)
		with self.assertRaises(CalibrationError):
			self.store.rate_for_speed(1, 31.0)

	def test_rate_for_speed_refuses_to_extrapolate_below_range(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)
		with self.assertRaises(CalibrationError):
			self.store.rate_for_speed(1, 9.0)

	def test_speed_for_rate_refuses_to_extrapolate(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)
		with self.assertRaises(CalibrationError):
			self.store.speed_for_rate(1, 3.0)

	def test_three_speeds_interpolate_on_the_correct_segment(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)   # 0.0 g/s
		self.store.add_measurement(1, speed=20.0, grams=10.0, duration_s=10.0)  # 1.0 g/s
		self.store.add_measurement(1, speed=40.0, grams=50.0, duration_s=10.0)  # 5.0 g/s
		self.assertAlmostEqual(self.store.rate_for_speed(1, 15.0), 0.5)
		self.assertAlmostEqual(self.store.rate_for_speed(1, 30.0), 3.0)

	def test_rejects_nonpositive_duration(self):
		with self.assertRaises(ValueError):
			self.store.add_measurement(1, speed=20.0, grams=10.0, duration_s=0.0)

	def test_rejects_negative_grams(self):
		with self.assertRaises(ValueError):
			self.store.add_measurement(1, speed=20.0, grams=-1.0, duration_s=10.0)

	def test_unknown_target_raises(self):
		with self.assertRaises(CalibrationError):
			self.store.rate_for_speed(99, 20.0)


class TestRepeatMeasurementsAreAveragedNotOverwritten(unittest.TestCase):
	def setUp(self):
		self.store = CalibrationStore()

	def test_a_second_measurement_at_the_same_speed_is_averaged_with_the_first(self):
		self.store.add_measurement(1, speed=20.0, grams=10.0, duration_s=10.0)  # 1.0 g/s
		point = self.store.add_measurement(1, speed=20.0, grams=20.0, duration_s=10.0)  # 2.0 g/s
		self.assertEqual(point.repeats, 2)
		self.assertAlmostEqual(point.rate_g_s, 1.5)  # (1.0 + 2.0) / 2

	def test_points_still_report_one_entry_per_distinct_speed(self):
		self.store.add_measurement(1, speed=20.0, grams=10.0, duration_s=10.0)
		self.store.add_measurement(1, speed=20.0, grams=12.0, duration_s=10.0)
		self.store.add_measurement(1, speed=30.0, grams=15.0, duration_s=10.0)
		points = self.store.points(1)
		self.assertEqual(sorted(p.speed for p in points), [20.0, 30.0])

	def test_a_single_measurement_has_zero_spread(self):
		point = self.store.add_measurement(1, speed=20.0, grams=10.0, duration_s=10.0)
		self.assertEqual(point.repeats, 1)
		self.assertEqual(point.spread_g_s, 0.0)

	def test_spread_reports_the_disagreement_between_repeats(self):
		self.store.add_measurement(1, speed=20.0, grams=9.0, duration_s=10.0)   # 0.9 g/s
		self.store.add_measurement(1, speed=20.0, grams=11.0, duration_s=10.0)  # 1.1 g/s
		point = self.store.add_measurement(1, speed=20.0, grams=10.0, duration_s=10.0)  # 1.0 g/s
		self.assertEqual(point.repeats, 3)
		self.assertAlmostEqual(point.rate_g_s, 1.0)
		self.assertAlmostEqual(point.spread_g_s, 0.2)  # 1.1 - 0.9

	def test_interpolation_uses_the_averaged_rate_not_any_single_repeat(self):
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=30.0, grams=18.0, duration_s=10.0)  # 1.8 g/s
		self.store.add_measurement(1, speed=30.0, grams=22.0, duration_s=10.0)  # 2.2 g/s -> avg 2.0 g/s
		self.assertAlmostEqual(self.store.rate_for_speed(1, 30.0), 2.0)


class TestDeadbandSpeed(unittest.TestCase):
	def setUp(self):
		self.store = CalibrationStore()

	def test_unknown_target_raises_calibration_error(self):
		with self.assertRaises(CalibrationError):
			self.store.deadband_speed(1)

	def test_lowest_producing_speed_is_reported(self):
		self.store.add_measurement(1, speed=5.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=20.0, grams=8.0, duration_s=10.0)
		self.assertEqual(self.store.deadband_speed(1), 20.0)

	def test_none_when_every_tested_speed_produced_nothing(self):
		self.store.add_measurement(1, speed=5.0, grams=0.0, duration_s=10.0)
		self.store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.assertIsNone(self.store.deadband_speed(1))


class TestCalibrationPersistence(unittest.TestCase):
	def setUp(self):
		fd, self.path = tempfile.mkstemp(suffix=".json")
		os.close(fd)
		os.remove(self.path)
		self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

	def test_save_and_load_round_trip(self):
		store = CalibrationStore()
		store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		store.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)
		store.add_measurement(2, speed=15.0, grams=5.0, duration_s=5.0)
		store.save(self.path)

		loaded = CalibrationStore.load(self.path)
		self.assertAlmostEqual(loaded.rate_for_speed(1, 20.0), 1.0)
		self.assertEqual(len(loaded.points(2)), 1)

	def test_repeats_survive_a_save_and_load_round_trip(self):
		store = CalibrationStore()
		store.add_measurement(1, speed=20.0, grams=9.0, duration_s=10.0)
		store.add_measurement(1, speed=20.0, grams=11.0, duration_s=10.0)
		store.save(self.path)

		loaded = CalibrationStore.load(self.path)
		point = loaded.points(1)[0]
		self.assertEqual(point.repeats, 2)
		self.assertAlmostEqual(point.rate_g_s, 1.0)

	def test_load_of_a_missing_file_returns_an_empty_store(self):
		store = CalibrationStore.load(self.path)
		with self.assertRaises(CalibrationError):
			store.points(1)

	def test_save_creates_missing_parent_directories(self):
		nested_path = os.path.join(os.path.dirname(self.path), "cal_nested", "subdir", "calibration.json")
		self.addCleanup(lambda: os.path.exists(nested_path) and os.remove(nested_path))

		store = CalibrationStore()
		store.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		store.save(nested_path)
		self.assertTrue(os.path.exists(nested_path))


if __name__ == "__main__":
	unittest.main()

import csv
import os
import tempfile
import unittest

from control.telemetry_logger import TelemetryLogger
from jobs.jobs import SettingsReport, StatusReport


class TestTelemetryLogger(unittest.TestCase):
	def setUp(self):
		fd, self.path = tempfile.mkstemp(suffix=".csv")
		os.close(fd)
		os.remove(self.path)  # let TelemetryLogger create it fresh
		self.addCleanup(lambda: os.path.exists(self.path) and os.remove(self.path))

	def _read_rows(self):
		with open(self.path, newline="") as f:
			return list(csv.DictReader(f))

	def test_creates_file_with_header(self):
		logger = TelemetryLogger(self.path)
		logger.close()

		with open(self.path, newline="") as f:
			header = next(csv.reader(f))
		self.assertEqual(header, TelemetryLogger.FIELDNAMES)

	def test_record_status_appends_a_row_per_call(self):
		logger = TelemetryLogger(self.path)
		logger.record_status(1, StatusReport(frequency=10.5, pwm=100, is_stalled=False), timestamp=1000.0)
		logger.record_status(2, StatusReport(frequency=20.0, pwm=200, is_stalled=True), timestamp=1000.5)
		logger.close()

		rows = self._read_rows()
		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["kind"], "status")
		self.assertEqual(rows[0]["target_id"], "1")
		self.assertEqual(rows[0]["frequency"], "10.5")
		self.assertEqual(rows[0]["pwm"], "100")
		self.assertEqual(rows[0]["is_stalled"], "0")
		# no target_speed was passed -> written as an empty cell, not "None".
		self.assertEqual(rows[0]["target_speed"], "")
		# a status row carries no settings data.
		self.assertEqual(rows[0]["kp"], "")
		self.assertEqual(rows[0]["firmware_speed"], "")
		self.assertEqual(rows[1]["target_id"], "2")
		self.assertEqual(rows[1]["is_stalled"], "1")

	def test_record_status_writes_the_commanded_target_speed_alongside_the_reading(self):
		logger = TelemetryLogger(self.path)
		logger.record_status(
			1, StatusReport(frequency=10.5, pwm=100, is_stalled=False), target_speed=33.0, timestamp=1000.0
		)
		logger.close()

		rows = self._read_rows()
		self.assertEqual(rows[0]["target_speed"], "33.0")

	def test_record_settings_appends_a_row_with_no_status_fields(self):
		logger = TelemetryLogger(self.path)
		logger.record_settings(1, SettingsReport(kp=1.0, ki=0.2, kd=0.05, speed=10.0), timestamp=2000.0)
		logger.close()

		rows = self._read_rows()
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["kind"], "settings")
		self.assertEqual(row["target_id"], "1")
		self.assertEqual(row["kp"], "1.0")
		self.assertEqual(row["ki"], "0.2")
		self.assertEqual(row["kd"], "0.05")
		self.assertEqual(row["firmware_speed"], "10.0")
		# a settings row carries no status data.
		self.assertEqual(row["frequency"], "")
		self.assertEqual(row["pwm"], "")
		self.assertEqual(row["is_stalled"], "")
		self.assertEqual(row["target_speed"], "")

	def test_status_and_settings_rows_interleave_in_one_file(self):
		logger = TelemetryLogger(self.path)
		logger.record_status(1, StatusReport(frequency=1.0, pwm=1, is_stalled=False), timestamp=1.0)
		logger.record_settings(1, SettingsReport(kp=1.0, ki=1.0, kd=1.0, speed=1.0), timestamp=2.0)
		logger.record_status(2, StatusReport(frequency=2.0, pwm=2, is_stalled=False), timestamp=3.0)
		logger.close()

		rows = self._read_rows()
		self.assertEqual([r["kind"] for r in rows], ["status", "settings", "status"])
		self.assertEqual([r["target_id"] for r in rows], ["1", "1", "2"])

	def test_reopening_an_existing_file_does_not_duplicate_the_header(self):
		logger = TelemetryLogger(self.path)
		logger.record_status(1, StatusReport(frequency=1.0, pwm=1, is_stalled=False), timestamp=1.0)
		logger.close()

		logger2 = TelemetryLogger(self.path)
		logger2.record_status(1, StatusReport(frequency=2.0, pwm=2, is_stalled=False), timestamp=2.0)
		logger2.close()

		with open(self.path, newline="") as f:
			lines = f.readlines()
		header_lines = [l for l in lines if l.startswith("timestamp,")]
		self.assertEqual(len(header_lines), 1)
		self.assertEqual(len(self._read_rows()), 2)

	def test_creates_missing_parent_directories(self):
		nested_path = os.path.join(os.path.dirname(self.path), "nested", "subdir", "run.csv")
		self.addCleanup(lambda: os.path.exists(nested_path) and os.remove(nested_path))

		logger = TelemetryLogger(nested_path)
		logger.record_status(1, StatusReport(frequency=1.0, pwm=1, is_stalled=False), timestamp=1.0)
		logger.close()

		self.assertTrue(os.path.exists(nested_path))


if __name__ == "__main__":
	unittest.main()

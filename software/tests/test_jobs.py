import struct
import unittest

from comms.constants import (
	CMD_SET_SPEED,
	CMD_SET_PID,
	CMD_REQ_STAT,
	CMD_REQ_SETTINGS,
	RESP_ACK,
	RESP_NACK,
	RESP_STATUS,
	RESP_SETTINGS,
)
from comms.link_layer import ResponseFrame
from jobs.jobs import (
	JobNackError,
	PayloadFormatError,
	UnexpectedResponseError,
	SetSpeedJob,
	SetPidJob,
	RequestStatusJob,
	RequestSettingsJob,
	StatusReport,
	SettingsReport,
)


def make_frame(source_id: int, command: int, payload: bytes = b"") -> ResponseFrame:
	return ResponseFrame(source_id=source_id, command=command, length=len(payload), payload=payload)


class TestSetSpeedJob(unittest.TestCase):
	def test_command_id_and_payload(self):
		job = SetSpeedJob(target_id=1, speed=42.5)
		self.assertEqual(job.command_id, CMD_SET_SPEED)
		self.assertEqual(job.build_payload(), struct.pack("<f", 42.5))

	def test_ack_response_returns_true(self):
		job = SetSpeedJob(target_id=1, speed=10.0)
		result = job.handle_response(make_frame(1, RESP_ACK))
		self.assertTrue(result)

	def test_nack_response_raises_job_nack_error(self):
		job = SetSpeedJob(target_id=1, speed=10.0)
		with self.assertRaises(JobNackError):
			job.handle_response(make_frame(1, RESP_NACK))

	def test_unexpected_response_raises(self):
		job = SetSpeedJob(target_id=1, speed=10.0)
		with self.assertRaises(UnexpectedResponseError):
			job.handle_response(make_frame(1, RESP_STATUS, b"\x00" * 6))


class TestSetPidJob(unittest.TestCase):
	def test_command_id_and_payload(self):
		job = SetPidJob(target_id=2, kp=1.0, ki=0.5, kd=0.1)
		self.assertEqual(job.command_id, CMD_SET_PID)
		self.assertEqual(job.build_payload(), struct.pack("<fff", 1.0, 0.5, 0.1))

	def test_ack_response_returns_true(self):
		job = SetPidJob(target_id=2, kp=1.0, ki=0.5, kd=0.1)
		self.assertTrue(job.handle_response(make_frame(2, RESP_ACK)))

	def test_nack_response_raises_job_nack_error(self):
		job = SetPidJob(target_id=2, kp=1.0, ki=0.5, kd=0.1)
		with self.assertRaises(JobNackError):
			job.handle_response(make_frame(2, RESP_NACK))

	def test_unexpected_response_raises(self):
		job = SetPidJob(target_id=2, kp=1.0, ki=0.5, kd=0.1)
		with self.assertRaises(UnexpectedResponseError):
			job.handle_response(make_frame(2, RESP_STATUS, b"\x00" * 6))


class TestRequestStatusJob(unittest.TestCase):
	def test_command_id_and_empty_payload(self):
		job = RequestStatusJob(target_id=3)
		self.assertEqual(job.command_id, CMD_REQ_STAT)
		self.assertEqual(job.build_payload(), b"")

	def test_parses_status_payload(self):
		job = RequestStatusJob(target_id=3)
		payload = struct.pack(
			"<ffBBIIHHHH", 123.5, 120.25, 200, 0x01, 0xA1B2C3D4, 0x01020304, 0x1234, 10250, 7, 3
		)
		report = job.handle_response(make_frame(3, RESP_STATUS, payload))

		self.assertIsInstance(report, StatusReport)
		self.assertAlmostEqual(report.frequency, 123.5)
		self.assertAlmostEqual(report.filtered_frequency, 120.25)
		self.assertEqual(report.pwm, 200)
		self.assertTrue(report.is_stalled)
		self.assertFalse(report.stall_fault)
		self.assertFalse(report.regulating)
		self.assertEqual(report.device_us, 0xA1B2C3D4)
		self.assertEqual(report.edge_count, 0x01020304)
		self.assertEqual(report.missed_cycles, 0x1234)
		self.assertEqual(report.max_loop_interval_us, 10250)
		self.assertEqual(report.frame_timeouts, 7)
		self.assertEqual(report.crc_errors, 3)

	def test_status_flags_are_decoded_independently(self):
		job = RequestStatusJob(target_id=3)
		payload = struct.pack("<ffBBIIHHHH", 0.0, 0.0, 0, 0x06, 0, 0, 0, 0, 0, 0)
		report = job.handle_response(make_frame(3, RESP_STATUS, payload))
		self.assertFalse(report.is_stalled)
		self.assertTrue(report.stall_fault)
		self.assertTrue(report.regulating)
		self.assertFalse(report.comms_lost)

	def test_comms_lost_flag_is_decoded(self):
		job = RequestStatusJob(target_id=3)
		payload = struct.pack("<ffBBIIHHHH", 0.0, 0.0, 0, 0x08, 0, 0, 0, 0, 0, 0)
		report = job.handle_response(make_frame(3, RESP_STATUS, payload))
		self.assertTrue(report.comms_lost)
		self.assertFalse(report.regulating)

	def test_a_status_payload_of_the_old_size_is_rejected_clearly(self):
		job = RequestStatusJob(target_id=3)
		with self.assertRaises(PayloadFormatError) as ctx:
			job.handle_response(make_frame(3, RESP_STATUS, struct.pack("<fBB", 1.0, 2, 0)))
		self.assertIn("protocol versions differ", str(ctx.exception))

	def test_unexpected_response_raises(self):
		job = RequestStatusJob(target_id=3)
		with self.assertRaises(UnexpectedResponseError):
			job.handle_response(make_frame(3, RESP_ACK))


class TestRequestSettingsJob(unittest.TestCase):
	def test_command_id_and_empty_payload(self):
		job = RequestSettingsJob(target_id=4)
		self.assertEqual(job.command_id, CMD_REQ_SETTINGS)
		self.assertEqual(job.build_payload(), b"")

	def test_parses_settings_payload(self):
		job = RequestSettingsJob(target_id=4)
		payload = struct.pack("<ffff", 2.0, 0.3, 0.05, 15.5)
		report = job.handle_response(make_frame(4, RESP_SETTINGS, payload))

		self.assertIsInstance(report, SettingsReport)
		self.assertAlmostEqual(report.kp, 2.0)
		self.assertAlmostEqual(report.ki, 0.3)
		self.assertAlmostEqual(report.kd, 0.05)
		self.assertAlmostEqual(report.speed, 15.5)

	def test_unexpected_response_raises(self):
		job = RequestSettingsJob(target_id=4)
		with self.assertRaises(UnexpectedResponseError):
			job.handle_response(make_frame(4, RESP_NACK))


if __name__ == "__main__":
	unittest.main()

import queue
import struct
import threading
import time
import unittest

from comms.constants import RESP_ACK, RESP_NACK, RESP_STATUS, RESP_SETTINGS
from comms.link_layer import ResponseFrame
from control.calibration import CalibrationError, CalibrationStore
from jobs.job_manager import JobManager
from control.session import PolydriverSession, SessionError


class _FakeWorker:
	def __init__(self):
		self.rx_queue: "queue.Queue" = queue.Queue()
		self.sent: list[tuple[int, int, bytes]] = []
		self.is_running = True

	def send_command(self, target_id: int, cmd: int, payload: bytes = b""):
		self.sent.append((target_id, cmd, payload))

	def stop(self):
		self.is_running = False


def wait_until(predicate, timeout=2.0, interval=0.01) -> bool:
	deadline = time.time() + timeout
	while time.time() < deadline:
		if predicate():
			return True
		time.sleep(interval)
	return predicate()


def auto_ack(worker, max_count=None):
	stop_event = threading.Event()

	def _loop():
		answered = 0
		while not stop_event.is_set():
			if max_count is not None and answered >= max_count:
				return
			if len(worker.sent) > answered:
				target_id, cmd, payload = worker.sent[answered]
				worker.rx_queue.put(ResponseFrame(source_id=target_id, command=RESP_ACK, length=0, payload=b""))
				answered += 1
			else:
				time.sleep(0.005)

	thread = threading.Thread(target=_loop, daemon=True)
	thread.start()
	return stop_event


class TestPolydriverSession(unittest.TestCase):
	def setUp(self):
		self.worker = _FakeWorker()
		self.manager = JobManager(self.worker, poll_interval=0.01)
		self.manager.start()
		self.session = PolydriverSession(poll_interval=10.0, job_timeout=0.05, job_retries=1)
		self.session._attach(self.worker, self.manager)
		self.addCleanup(self.session.disconnect)

	def test_set_speed_success(self):
		def respond():
			self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
			target_id, cmd, payload = self.worker.sent[0]
			self.worker.rx_queue.put(ResponseFrame(source_id=target_id, command=RESP_ACK, length=0, payload=b""))

		import threading
		threading.Thread(target=respond, daemon=True).start()

		self.session.set_speed(1, 12.5)
		self.assertIsNotNone(self.session.get_state(1))

	def test_set_speed_nack_raises_session_error(self):
		def respond():
			self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
			target_id, cmd, payload = self.worker.sent[0]
			self.worker.rx_queue.put(ResponseFrame(source_id=target_id, command=RESP_NACK, length=0, payload=b""))

		import threading
		threading.Thread(target=respond, daemon=True).start()

		with self.assertRaises(SessionError):
			self.session.set_speed(1, 12.5)

	def test_set_speed_no_response_times_out(self):
		with self.assertRaises(SessionError):
			self.session.set_speed(1, 12.5)

	def test_request_status_records_state_and_notifies_telemetry_sink(self):
		import struct

		recorded = []

		class _FakeSink:
			def record_status(self, target_id, report, target_speed=None):
				recorded.append((target_id, report, target_speed))

			def record_settings(self, target_id, report):
				pass

		self.session.attach_telemetry_sink(_FakeSink())

		def respond():
			self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
			target_id, cmd, payload = self.worker.sent[0]
			status_payload = struct.pack("<fBB", 42.0, 128, 0)
			self.worker.rx_queue.put(
				ResponseFrame(source_id=target_id, command=RESP_STATUS, length=len(status_payload), payload=status_payload)
			)

		import threading
		threading.Thread(target=respond, daemon=True).start()

		report = self.session.request_status(3)
		self.assertAlmostEqual(report.frequency, 42.0)
		self.assertEqual(report.pwm, 128)

		state = self.session.get_state(3)
		self.assertIsNotNone(state)
		assert state is not None
		self.assertEqual(state.last_status, report)
		self.assertEqual(recorded, [(3, report, None)])

	def test_set_speed_records_the_commanded_setpoint_for_later_status_reads(self):
		import struct
		import threading

		recorded = []

		class _FakeSink:
			def record_status(self, target_id, report, target_speed=None):
				recorded.append((target_id, report, target_speed))

			def record_settings(self, target_id, report):
				pass

		self.session.attach_telemetry_sink(_FakeSink())

		def ack_speed_then_status():
			self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
			target_id, cmd, payload = self.worker.sent[0]
			self.worker.rx_queue.put(ResponseFrame(source_id=target_id, command=RESP_ACK, length=0, payload=b""))

			self.assertTrue(wait_until(lambda: len(self.worker.sent) == 2))
			target_id, cmd, payload = self.worker.sent[1]
			status_payload = struct.pack("<fBB", 7.0, 64, 0)
			self.worker.rx_queue.put(
				ResponseFrame(source_id=target_id, command=RESP_STATUS, length=len(status_payload), payload=status_payload)
			)

		threading.Thread(target=ack_speed_then_status, daemon=True).start()

		self.session.set_speed(1, 33.0)
		state = self.session.get_state(1)
		assert state is not None
		self.assertEqual(state.last_commanded_speed, 33.0)

		self.session.request_status(1)
		self.assertEqual(len(recorded), 1)
		target_id, report, target_speed = recorded[0]
		self.assertEqual(target_speed, 33.0)

	def test_request_settings_records_state_and_notifies_telemetry_sink(self):
		import struct
		import threading

		status_calls = []
		settings_calls = []

		class _FakeSink:
			def record_status(self, target_id, report, target_speed=None):
				status_calls.append((target_id, report, target_speed))

			def record_settings(self, target_id, report):
				settings_calls.append((target_id, report))

		self.session.attach_telemetry_sink(_FakeSink())

		def respond():
			self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
			target_id, cmd, payload = self.worker.sent[0]
			settings_payload = struct.pack("<ffff", 1.0, 0.2, 0.05, 10.0)
			self.worker.rx_queue.put(
				ResponseFrame(
					source_id=target_id,
					command=RESP_SETTINGS,
					length=len(settings_payload),
					payload=settings_payload,
				)
			)

		threading.Thread(target=respond, daemon=True).start()

		report = self.session.request_settings(2)
		self.assertEqual(report.kp, 1.0)
		self.assertEqual(report.speed, 10.0)

		state = self.session.get_state(2)
		assert state is not None
		self.assertEqual(state.last_settings, report)
		self.assertEqual(settings_calls, [(2, report)])
		self.assertEqual(status_calls, [])

	def test_add_and_remove_target(self):
		self.session.add_target(5, name="left")
		self.assertEqual(len(self.session.targets()), 1)
		self.session.remove_target(5)
		self.assertEqual(self.session.targets(), [])

	def test_commands_before_connect_raise(self):
		fresh = PolydriverSession()
		with self.assertRaises(SessionError):
			fresh.set_speed(1, 1.0)


class TestPolydriverSessionPolling(unittest.TestCase):
	def test_poll_loop_updates_tracked_targets(self):
		import struct

		worker = _FakeWorker()
		manager = JobManager(worker, poll_interval=0.01)
		manager.start()
		session = PolydriverSession(poll_interval=0.03, job_timeout=0.05, job_retries=1)
		session._attach(worker, manager)
		self.addCleanup(session.disconnect)

		session.add_target(7)

		def respond_forever():
			answered = 0
			deadline = time.time() + 2.0
			while time.time() < deadline and answered < 2:
				if len(worker.sent) > answered:
					target_id, cmd, payload = worker.sent[answered]
					status_payload = struct.pack("<fBB", 5.0 + answered, 10, 0)
					worker.rx_queue.put(
						ResponseFrame(source_id=target_id, command=RESP_STATUS, length=len(status_payload), payload=status_payload)
					)
					answered += 1
				time.sleep(0.005)

		import threading
		responder = threading.Thread(target=respond_forever, daemon=True)
		responder.start()

		def has_status() -> bool:
			state = session.get_state(7)
			return state is not None and state.last_status is not None

		self.assertTrue(wait_until(has_status))
		self.assertTrue(wait_until(lambda: len(worker.sent) >= 2), "poller did not run more than once")


class TestPolydriverSessionTimedRuns(unittest.TestCase):
	def setUp(self):
		self.worker = _FakeWorker()
		self.manager = JobManager(self.worker, poll_interval=0.01)
		self.manager.start()
		self.session = PolydriverSession(poll_interval=10.0, job_timeout=0.05, job_retries=1)
		self.session._attach(self.worker, self.manager)
		self.addCleanup(self.session.disconnect)

	def test_run_for_sets_speed_immediately_and_returns_the_resolved_speed(self):
		auto_ack(self.worker)
		resolved = self.session.run_for(1, duration_s=0.05, speed=20.0)
		self.assertEqual(resolved, 20.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 1))
		_, _, payload = self.worker.sent[0]
		self.assertAlmostEqual(struct.unpack("<f", payload)[0], 20.0)

	def test_run_for_auto_stops_to_zero_after_the_duration(self):
		auto_ack(self.worker)
		self.session.run_for(1, duration_s=0.05, speed=20.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 2, timeout=2.0))
		_, _, payload = self.worker.sent[1]
		self.assertAlmostEqual(struct.unpack("<f", payload)[0], 0.0)

	def test_run_for_rejects_neither_speed_nor_rate(self):
		with self.assertRaises(ValueError):
			self.session.run_for(1, duration_s=1.0)

	def test_run_for_rejects_both_speed_and_rate(self):
		with self.assertRaises(ValueError):
			self.session.run_for(1, duration_s=1.0, speed=1.0, rate_g_s=1.0)

	def test_run_for_rejects_nonpositive_duration(self):
		with self.assertRaises(ValueError):
			self.session.run_for(1, duration_s=0.0, speed=1.0)

	def test_run_for_with_a_rate_resolves_speed_via_calibration(self):
		self.session.calibration.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.session.calibration.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)  # 2.0 g/s at speed 30

		auto_ack(self.worker)
		resolved = self.session.run_for(1, duration_s=0.05, rate_g_s=1.0)
		self.assertAlmostEqual(resolved, 20.0)

	def test_run_for_with_an_uncalibrated_rate_raises_and_does_not_command_anything(self):
		auto_ack(self.worker)
		with self.assertRaises(CalibrationError):
			self.session.run_for(1, duration_s=0.05, rate_g_s=1.0)
		self.assertEqual(self.worker.sent, [])

	def test_stop_run_cancels_before_the_duration_elapses(self):
		auto_ack(self.worker)
		self.session.run_for(1, duration_s=5.0, speed=20.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 1))

		started = time.time()
		cancelled = self.session.stop_run(1)
		elapsed = time.time() - started

		self.assertTrue(cancelled)
		self.assertLess(elapsed, 1.0, "stop_run should not block for anywhere near the full duration")
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 2))
		_, _, payload = self.worker.sent[1]
		self.assertAlmostEqual(struct.unpack("<f", payload)[0], 0.0)

	def test_stop_run_with_no_active_run_returns_false(self):
		self.assertFalse(self.session.stop_run(1))

	def test_a_new_run_for_supersedes_an_earlier_one_for_the_same_target(self):
		auto_ack(self.worker)
		self.session.run_for(1, duration_s=5.0, speed=20.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 1))
		resolved = self.session.run_for(1, duration_s=0.05, speed=40.0)
		self.assertEqual(resolved, 40.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 3))
		_, _, cancelled_payload = self.worker.sent[1]
		self.assertAlmostEqual(struct.unpack("<f", cancelled_payload)[0], 0.0)
		_, _, restarted_payload = self.worker.sent[2]
		self.assertAlmostEqual(struct.unpack("<f", restarted_payload)[0], 40.0)

		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 4))
		_, _, last_payload = self.worker.sent[-1]
		self.assertAlmostEqual(struct.unpack("<f", last_payload)[0], 0.0)

	def test_disconnect_stops_an_active_run_instead_of_abandoning_it(self):
		auto_ack(self.worker)
		self.session.run_for(1, duration_s=5.0, speed=20.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 1))

		self.session.disconnect()
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 2))
		_, _, payload = self.worker.sent[1]
		self.assertAlmostEqual(struct.unpack("<f", payload)[0], 0.0)


class TestPolydriverSessionCalibration(unittest.TestCase):
	def setUp(self):
		self.worker = _FakeWorker()
		self.manager = JobManager(self.worker, poll_interval=0.01)
		self.manager.start()
		self.session = PolydriverSession(poll_interval=10.0, job_timeout=0.05, job_retries=1)
		self.session._attach(self.worker, self.manager)
		self.addCleanup(self.session.disconnect)

	def test_record_calibration_point_is_reflected_in_calibration_points(self):
		point = self.session.record_calibration_point(1, speed=20.0, grams=10.0, duration_s=10.0)
		self.assertAlmostEqual(point.rate_g_s, 1.0)
		self.assertEqual(self.session.calibration_points(1), [point])

	def test_calibration_points_for_an_unknown_target_is_empty_not_an_error(self):
		self.assertEqual(self.session.calibration_points(99), [])

	def test_calibration_deadband_for_an_unknown_target_is_none_not_an_error(self):
		self.assertIsNone(self.session.calibration_deadband(99))

	def test_set_rate_resolves_speed_via_calibration_and_commands_it(self):
		self.session.calibration.add_measurement(1, speed=10.0, grams=0.0, duration_s=10.0)
		self.session.calibration.add_measurement(1, speed=30.0, grams=20.0, duration_s=10.0)  # 2.0 g/s at speed 30

		auto_ack(self.worker)
		resolved = self.session.set_rate(1, 1.0)
		self.assertAlmostEqual(resolved, 20.0)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) >= 1))

	def test_save_and_load_calibration_round_trips_through_the_session(self):
		import os
		import tempfile

		fd, path = tempfile.mkstemp(suffix=".json")
		os.close(fd)
		os.remove(path)
		self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

		self.session.record_calibration_point(1, speed=20.0, grams=10.0, duration_s=10.0)
		self.session.save_calibration(path)

		fresh = PolydriverSession()
		fresh.load_calibration(path)
		self.assertEqual(len(fresh.calibration_points(1)), 1)


if __name__ == "__main__":
	unittest.main()

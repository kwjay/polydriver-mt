import queue
import time
import unittest

from comms.constants import RESP_ACK, RESP_NACK, RESP_STATUS, RESP_SETTINGS
from comms.link_layer import ResponseFrame
from jobs.job_manager import JobManager
from control.session import PolydriverSession, SessionError


class _FakeWorker:
	"""Same shape SerialWorker exposes to JobManager: an rx_queue and a
	send_command(). Good enough to drive a PolydriverSession end to end
	without a real serial port."""

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


class TestPolydriverSession(unittest.TestCase):
	def setUp(self):
		self.worker = _FakeWorker()
		self.manager = JobManager(self.worker, poll_interval=0.01)
		self.manager.start()

		# poll_interval is set long on purpose: most tests drive commands
		# directly and don't want the background poller racing them for
		# worker.sent entries. test_poll_loop_updates_tracked_targets below
		# uses its own short-interval session instead.
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

		self.session.set_speed(1, 12.5)  # should not raise
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
			self.session.set_speed(1, 12.5)  # nothing ever answers -> timeout

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
		# no set_speed(3, ...) was ever issued, so nothing was commanded yet.
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
		# a settings read is not a status read - it must not also show up
		# as one.
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


if __name__ == "__main__":
	unittest.main()

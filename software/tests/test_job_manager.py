import queue
import time
import unittest

from comms.link_layer import ResponseFrame
from comms.serial_worker import SerialWorkerError
from jobs.base_job import BaseJob
from jobs.job_manager import JobManager, JobTimeoutError, LinkLostError

FAKE_CMD = 0xAA
FAKE_RESP = 0xBB


class _FakeJob(BaseJob):
	"""A minimal BaseJob used to exercise JobManager mechanics (dispatch,
	matching, retries, timeouts) without depending on the real protocol
	commands in jobs.py."""

	def __init__(self, target_id=1, timeout=0.05, retries=3, response_command=FAKE_RESP):
		super().__init__(target_id, timeout, retries)
		self.response_command = response_command
		self.build_payload_calls = 0

	@property
	def command_id(self) -> int:
		return FAKE_CMD

	def build_payload(self) -> bytes:
		self.build_payload_calls += 1
		return b"\x01\x02"

	def handle_response(self, response_frame: ResponseFrame):
		if response_frame.command != self.response_command:
			raise ValueError(f"unexpected response command 0x{response_frame.command:02X}")
		return response_frame.payload


class _FakeWorker:
	"""Stands in for a SerialWorker: JobManager only ever touches rx_queue
	and send_command, so that's all this fake needs to provide."""

	def __init__(self):
		self.rx_queue: "queue.Queue" = queue.Queue()
		self.sent: list[tuple[int, int, bytes]] = []

	def send_command(self, target_id: int, cmd: int, payload: bytes = b""):
		self.sent.append((target_id, cmd, payload))


def wait_until(predicate, timeout=1.0, interval=0.01) -> bool:
	"""Poll predicate() until it's truthy or timeout elapses."""
	deadline = time.time() + timeout
	while time.time() < deadline:
		if predicate():
			return True
		time.sleep(interval)
	return predicate()


class TestJobManager(unittest.TestCase):
	def setUp(self):
		self.worker = _FakeWorker()
		self.manager = JobManager(self.worker, poll_interval=0.01)
		self.manager.start()
		self.addCleanup(self._safe_stop)

	def _safe_stop(self):
		if self.manager.is_running:
			self.manager.stop()

	def test_submit_sends_the_job_frame(self):
		job = _FakeJob(target_id=1)
		self.manager.submit(job)

		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
		self.assertEqual(self.worker.sent[0], (1, FAKE_CMD, b"\x01\x02"))

	def test_matching_response_reports_success_and_frees_the_manager(self):
		job = _FakeJob(target_id=1)
		results = []
		self.manager.submit(job, on_success=results.append)

		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
		self.worker.rx_queue.put(ResponseFrame(source_id=1, command=FAKE_RESP, length=2, payload=b"hi"))

		self.assertTrue(wait_until(lambda: len(results) == 1))
		self.assertEqual(results[0], b"hi")
		self.assertTrue(wait_until(lambda: self.manager.active_job is None))

	def test_response_from_a_different_target_is_ignored(self):
		job = _FakeJob(target_id=1)
		results = []
		self.manager.submit(job, on_success=results.append)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))

		self.worker.rx_queue.put(ResponseFrame(source_id=2, command=FAKE_RESP, length=0, payload=b""))
		time.sleep(0.1)
		self.assertEqual(results, [])
		self.assertIsNotNone(self.manager.active_job)

		self.worker.rx_queue.put(ResponseFrame(source_id=1, command=FAKE_RESP, length=0, payload=b""))
		self.assertTrue(wait_until(lambda: len(results) == 1))

	def test_error_raised_by_handle_response_is_reported_as_failure(self):
		job = _FakeJob(target_id=1, response_command=FAKE_RESP)
		errors = []
		self.manager.submit(job, on_error=errors.append)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))

		self.worker.rx_queue.put(ResponseFrame(source_id=1, command=0x99, length=0, payload=b""))

		self.assertTrue(wait_until(lambda: len(errors) == 1))
		self.assertIsInstance(errors[0], ValueError)

	def test_timeout_retries_up_to_the_job_budget_then_gives_up(self):
		job = _FakeJob(target_id=1, timeout=0.03, retries=1)
		errors = []
		self.manager.submit(job, on_error=errors.append)

		# retries=1 -> the initial send plus exactly one retry, then give up.
		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 2, timeout=2.0))
		self.assertTrue(wait_until(lambda: len(errors) == 1, timeout=2.0))
		self.assertIsInstance(errors[0], JobTimeoutError)
		self.assertEqual(len(self.worker.sent), 2)

	def test_zero_retries_gives_up_after_a_single_attempt(self):
		job = _FakeJob(target_id=1, timeout=0.03, retries=0)
		errors = []
		self.manager.submit(job, on_error=errors.append)

		self.assertTrue(wait_until(lambda: len(errors) == 1, timeout=2.0))
		self.assertIsInstance(errors[0], JobTimeoutError)
		self.assertEqual(len(self.worker.sent), 1)

	def test_jobs_are_processed_one_at_a_time_in_submission_order(self):
		# timeout is set generously longer than the sleep() below on purpose:
		# _FakeJob's default timeout (0.05s) is the same order of magnitude
		# as that sleep, so scheduling jitter could let job1's own retry
		# timer fire before the assertion runs, which looks identical to
		# "job2 was dispatched early" (both add a second entry to
		# worker.sent) - this was an observed source of test flakiness.
		job1 = _FakeJob(target_id=1, timeout=1.0)
		job2 = _FakeJob(target_id=2, timeout=1.0)
		self.manager.submit(job1)
		self.manager.submit(job2)

		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))
		time.sleep(0.05)
		self.assertEqual(len(self.worker.sent), 1, "second job must wait for the first to finish")

		self.worker.rx_queue.put(ResponseFrame(source_id=1, command=FAKE_RESP, length=0, payload=b""))

		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 2))
		self.assertEqual(self.worker.sent[1][0], 2)

	def test_link_lost_fails_the_active_and_every_pending_job(self):
		job1 = _FakeJob(target_id=1)
		job2 = _FakeJob(target_id=2)
		errors1, errors2 = [], []
		self.manager.submit(job1, on_error=errors1.append)
		self.manager.submit(job2, on_error=errors2.append)
		self.assertTrue(wait_until(lambda: len(self.worker.sent) == 1))

		self.worker.rx_queue.put(SerialWorkerError("device unplugged"))

		self.assertTrue(wait_until(lambda: len(errors1) == 1 and len(errors2) == 1))
		self.assertIsInstance(errors1[0], LinkLostError)
		self.assertIsInstance(errors2[0], LinkLostError)
		self.assertEqual(self.manager.pending_count, 0)


if __name__ == "__main__":
	unittest.main()

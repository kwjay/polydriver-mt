import threading
import time
import unittest
from unittest.mock import patch

import serial

from comms.serial_worker import SerialWorker, SerialWorkerError
from comms.link_layer import ResponseFrame, build_frame


class FakeSerial:
  def __init__(self, *args, **kwargs):
    self.is_open = True
    self.write_calls: list[bytes] = []
    self._rx_buffer = bytearray()
    self._lock = threading.Lock()

    self.write_exception: Exception | None = None
    self.read_exception: Exception | None = None

    self.write_event = threading.Event()

  def write(self, data: bytes):
    if self.write_exception is not None:
      raise self.write_exception
    with self._lock:
      self.write_calls.append(bytes(data))
    self.write_event.set()

  @property
  def in_waiting(self) -> int:
    with self._lock:
      return len(self._rx_buffer)

  def read(self, size: int = 1) -> bytes:
    if self.read_exception is not None:
      raise self.read_exception
    with self._lock:
      data = bytes(self._rx_buffer[:size])
      del self._rx_buffer[:size]
    if not data:
      time.sleep(0.01)
    return data

  def close(self):
    self.is_open = False

  def feed_rx(self, data: bytes):
    with self._lock:
      self._rx_buffer.extend(data)


def wait_until(predicate, timeout=1.0, interval=0.01) -> bool:
  """Poll predicate() until it's truthy or timeout elapses.
  Returns True if predicate became true in time, False if it timed out.
  Avoids both flaky fixed-sleep tests and a real hang if something breaks.
  """
  deadline = time.time() + timeout
  while time.time() < deadline:
    if predicate():
      return True
    time.sleep(interval)
  return predicate()


class TestSerialWorker(unittest.TestCase):
  def setUp(self):
    self.fake_serial = FakeSerial()
    patcher = patch(
      "comms.serial_worker.serial.Serial", return_value=self.fake_serial
    )
    self.addCleanup(patcher.stop)
    patcher.start()

    self.worker = SerialWorker("COM_FAKE", 115200, read_timeout=0.05)
    self.addCleanup(self._safe_stop)

  def _safe_stop(self):
    if self.worker.is_running:
      self.worker.stop()

  def test_double_start_raises(self):
    self.worker.start()
    with self.assertRaises(RuntimeError):
      self.worker.start()

  def test_send_command_before_start_raises(self):
    with self.assertRaises(RuntimeError):
      self.worker.send_command(target_id=1, cmd=2)

  def test_send_command_writes_frame_to_serial(self):
    self.worker.start()
    self.worker.send_command(target_id=1, cmd=2, payload=b"\x01\x02")
    expected_frame = build_frame(1, 2, b"\x01\x02")

    self.assertTrue(
      wait_until(lambda: len(self.fake_serial.write_calls) > 0),
      "worker never wrote the queued frame to the serial port",
    )
    self.assertEqual(self.fake_serial.write_calls[0], expected_frame)

  def test_incoming_bytes_are_parsed_and_queued(self):
    self.worker.start()
    frame_bytes = build_frame(target_id=5, cmd=9, payload=b"hi")
    self.fake_serial.feed_rx(frame_bytes)

    try:
      parsed = self.worker.rx_queue.get(timeout=1.0)
    except Exception:
      self.fail("no frame appeared on rx_queue in time")

    self.assertIsInstance(parsed, ResponseFrame)
    self.assertEqual(parsed.source_id, 5)
    self.assertEqual(parsed.command, 9)
    self.assertEqual(parsed.payload, b"hi")

  def test_stop_closes_connection_and_joins_thread(self):
    self.worker.start()
    thread = self.worker._thread
    self.worker.stop()

    self.assertFalse(self.worker.is_running)
    assert thread is not None
    self.assertFalse(thread.is_alive())
    self.assertFalse(self.fake_serial.is_open)

  def test_write_exception_reported_on_rx_queue_and_stops_worker(self):
    self.fake_serial.write_exception = serial.SerialException("device unplugged")

    self.worker.start()
    self.worker.send_command(target_id=1, cmd=2)

    try:
      item = self.worker.rx_queue.get(timeout=1.0)
    except Exception:
      self.fail("worker did not report the serial error on rx_queue")

    self.assertIsInstance(item, SerialWorkerError)
    self.assertTrue(
      wait_until(lambda: self.worker.is_running is False),
      "worker did not stop itself after a SerialException",
    )

  def test_read_exception_reported_on_rx_queue_and_stops_worker(self):
    self.fake_serial.read_exception = serial.SerialException("read failed")
    self.worker.start()

    try:
      item = self.worker.rx_queue.get(timeout=1.0)
    except Exception:
      self.fail("worker did not report the serial error on rx_queue")

    self.assertIsInstance(item, SerialWorkerError)
    self.assertTrue(
      wait_until(lambda: self.worker.is_running is False),
      "worker did not stop itself after a SerialException",
    )

  def test_context_manager_starts_and_stops(self):
    with SerialWorker("COM_FAKE", 115200, read_timeout=0.05) as w:
      self.assertTrue(w.is_running)
      self.assertIsNotNone(w._thread)
      thread = w._thread
    self.assertFalse(w.is_running)
    assert thread is not None
    self.assertFalse(thread.is_alive())

  def test_context_manager_stops_on_exception(self):
    with self.assertRaises(ValueError):
      with SerialWorker("COM_FAKE", 115200, read_timeout=0.05) as w:
        self.assertTrue(w.is_running)
        thread = w._thread
        raise ValueError("boom")
    self.assertFalse(w.is_running)
    assert thread is not None
    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
  unittest.main()
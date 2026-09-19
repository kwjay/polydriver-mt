import logging
import queue
import threading
import serial
from .link_layer import ProtocolParser, build_frame

logger = logging.getLogger(__name__)

class SerialWorkerError(Exception):
	pass

class SerialWorker:
	def __init__(self, port: str, baudrate: int = 115200, read_timeout: float = 0.05):
		self.port = port
		self.baudrate = baudrate
		self.read_timeout = read_timeout
		self.serial_conn: serial.Serial | None = None
		self.parser = ProtocolParser()

		self.rx_queue: queue.Queue = queue.Queue()
		self.tx_queue: queue.Queue = queue.Queue()

		self.is_running = False
		self._thread: threading.Thread | None = None
		self._lock = threading.Lock()

	def __enter__(self):
		self.start()
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.stop()

	def start(self):
		with self._lock:
			if self.is_running:
				raise RuntimeError("SerialWorker is already running")

			self.serial_conn = serial.Serial(
				self.port, self.baudrate, timeout=self.read_timeout
			)
			self.is_running = True
			self._thread = threading.Thread(target=self._run_loop, daemon=True)
			self._thread.start()

	def stop(self):
		with self._lock:
			self.is_running = False
			if self._thread is not None:
				self._thread.join(timeout=self.read_timeout * 4 + 1.0)
				if self._thread.is_alive():
					logger.warning("Serial worker thread did not exit cleanly")
				self._thread = None

			if self.serial_conn is not None and self.serial_conn.is_open:
				self.serial_conn.close()
			self.serial_conn = None

	def send_command(self, target_id: int, cmd: int, payload: bytes = b""):
		if not self.is_running:
			raise RuntimeError("SerialWorker is not running")
		frame = build_frame(target_id, cmd, payload)
		self.tx_queue.put(frame)

	def _run_loop(self):
		while self.is_running:
			try:
				self._drain_tx()
				self._poll_rx()
			except serial.SerialException:
				logger.exception("Serial I/O error, stopping worker thread")
				self.rx_queue.put(SerialWorkerError("Serial connection lost"))
				self.is_running = False
			except Exception:
				logger.exception("Unexpected error in serial worker loop")
				self.rx_queue.put(SerialWorkerError("Unexpected worker failure"))
				self.is_running = False

	def _drain_tx(self):
		while True:
			try:
				frame = self.tx_queue.get_nowait()
			except queue.Empty:
				break
			assert self.serial_conn is not None
			self.serial_conn.write(frame)

	def _poll_rx(self):
		assert self.serial_conn is not None
		waiting = self.serial_conn.in_waiting
		byte_data = self.serial_conn.read(max(1, waiting))
		for b in byte_data:
				parsed_frame = self.parser.process_byte(b)
				if parsed_frame:
					self.rx_queue.put(parsed_frame)
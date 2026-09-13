import time
from abc import ABC, abstractmethod
from typing import Any
from comms.link_layer import ResponseFrame

class BaseJob(ABC):
	def __init__(self, target_id: int, timeout: float = 0.5, retries: int = 3):
		if timeout <= 0:
			raise ValueError("timeout must be positive")
		if retries < 0:
			raise ValueError("retries must be >= 0")

		self.target_id = target_id
		self.timeout = timeout
		self.max_retries = retries
		self.attempts = 0
		self.creation_time = time.time()

	@property
	@abstractmethod
	def command_id(self) -> int:
		"""The command byte (e.g., CMD_SET_SPEED)."""
		pass

	@abstractmethod
	def build_payload(self) -> bytes:
		"""Packs the data into bytes."""
		pass

	@abstractmethod
	def handle_response(self, response_frame: ResponseFrame) -> Any:
		"""Unpacks the device's reply and returns the formatted data."""
		pass

	def is_expired(self) -> bool:
		"""True once this job has been waiting longer than its timeout."""
		return (time.time() - self.creation_time) > self.timeout

	def record_attempt(self) -> bool:
		"""Call each time a send attempt is made.
		Returns True if another retry is still allowed, False if the job
		has exhausted its retry budget and should be given up on.
		"""
		self.attempts += 1
		return self.attempts <= self.max_retries

	def __repr__(self) -> str:
		return (
			f"{self.__class__.__name__}(target_id={self.target_id}, "
			f"attempts={self.attempts}/{self.max_retries})"
		)
"""One COM port with one board behind it: SerialWorker + RequestQueue, opened and checked in one
step (C6.1). If any step of opening fails, everything already started is stopped again (C2.14).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

from ..protocol.constants import PROTOCOL_VERSION, NackReason
from ..protocol.serial_worker import SerialPort, SerialWorker, open_serial_port
from ..requests import GetVersion, Request, VersionReport
from ..requests.request_queue import (
	REPLY_TIMEOUT_S, RETRIES, Exchange, RequestQueue, RequestTimeoutError,
)

# Opening the port of an Uno resets it. Optiboot waits up to about 1 s for an upload, and any
# other byte makes it start the firmware early, so the board answers well within this.
BOOT_WAIT_S = 4.0

Result = TypeVar("Result")


class DeviceError(Exception):
	pass


class BoardNotRespondingError(DeviceError):
	pass


class ProtocolVersionError(DeviceError):
	pass


@dataclass(frozen=True)
class LinkCounters:
	frames_sent: int
	retries: int
	reply_timeouts: int
	late_replies: int
	nacks: dict[NackReason | int, int] = field(default_factory=dict)
	host_bad_crc_frames: int = 0
	host_abandoned_frames: int = 0


class Link:
	def __init__(self, port: str, worker: SerialWorker, requests: RequestQueue, version: VersionReport):
		self._port = port
		self._worker = worker
		self._requests = requests
		self._version = version

	@classmethod
	def open(
		cls,
		port: str,
		*,
		opener: Callable[[str], SerialPort] = open_serial_port,
		boot_wait_s: float = BOOT_WAIT_S,
		reply_timeout_s: float = REPLY_TIMEOUT_S,
		retries: int = RETRIES,
		observer: Callable[[Exchange], None] | None = None,
	) -> "Link":
		worker = SerialWorker(port, opener=opener)
		requests = RequestQueue(worker, reply_timeout_s=reply_timeout_s, retries=retries, observer=observer)
		try:
			worker.start()
			requests.start()
			version = _wait_for_board(port, requests, boot_wait_s)
			if version.protocol_version != PROTOCOL_VERSION:
				raise ProtocolVersionError(
					f"{port}: the board speaks protocol {version.protocol_version}, this host speaks "
					f"{PROTOCOL_VERSION}; flash the matching firmware"
				)
		except BaseException:
			requests.stop()
			worker.stop()
			raise
		return cls(port, worker, requests, version)

	def __enter__(self) -> "Link":
		return self

	def __exit__(self, exc_type, exc_value, traceback) -> None:
		self.close()

	@property
	def port(self) -> str:
		return self._port

	@property
	def version(self) -> VersionReport:
		return self._version

	@property
	def is_open(self) -> bool:
		return self._requests.is_open and self._worker.is_open

	@property
	def stats(self) -> LinkCounters:
		queue = self._requests.stats
		return LinkCounters(
			frames_sent=queue.frames_sent,
			retries=queue.retries,
			reply_timeouts=queue.reply_timeouts,
			late_replies=queue.late_replies,
			nacks=queue.nacks,
			host_bad_crc_frames=self._worker.bad_crc_frames,
			host_abandoned_frames=self._worker.abandoned_frames,
		)

	def call(self, request: Request[Result], timeout_s: float | None = None) -> Result:
		return self._requests.call(request, timeout_s)

	def submit(self, request: Request[Result]):
		return self._requests.submit(request)

	def close(self) -> None:
		self._requests.stop()
		self._worker.stop()


def _wait_for_board(port: str, requests: RequestQueue, boot_wait_s: float) -> VersionReport:
	deadline_s = time.monotonic() + boot_wait_s
	while True:
		try:
			return requests.call(GetVersion())
		except RequestTimeoutError:
			if time.monotonic() >= deadline_s:
				raise BoardNotRespondingError(
					f"{port}: no answer to GET_VERSION within {boot_wait_s:g} s; check the port, the cable "
					"and that the firmware is flashed"
				) from None

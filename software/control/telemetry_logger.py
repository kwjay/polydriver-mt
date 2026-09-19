import csv
import os
import threading
import time
from typing import Any, Dict, Optional, TextIO

from jobs.jobs import SettingsReport, StatusReport


class TelemetryLogger:
	FIELDNAMES = [
		"timestamp",
		"target_id",
		"kind",
		"target_speed",
		"frequency",
		"pwm",
		"is_stalled",
		"kp",
		"ki",
		"kd",
		"firmware_speed",
	]

	def __init__(self, path: str):
		self.path = path
		self._lock = threading.Lock()

		parent = os.path.dirname(path)
		if parent:
			os.makedirs(parent, exist_ok=True)

		needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
		self._file: TextIO = open(path, "a", newline="")
		self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDNAMES)
		if needs_header:
			self._writer.writeheader()
			self._file.flush()

	def record_status(
		self,
		target_id: int,
		report: StatusReport,
		target_speed: Optional[float] = None,
		timestamp: Optional[float] = None,
	) -> None:
		self._write_row(
			target_id=target_id,
			kind="status",
			timestamp=timestamp,
			target_speed=target_speed,
			frequency=report.frequency,
			pwm=report.pwm,
			is_stalled=int(report.is_stalled),
		)

	def record_settings(
		self,
		target_id: int,
		report: SettingsReport,
		timestamp: Optional[float] = None,
	) -> None:
		self._write_row(
			target_id=target_id,
			kind="settings",
			timestamp=timestamp,
			kp=report.kp,
			ki=report.ki,
			kd=report.kd,
			firmware_speed=report.speed,
		)

	def _write_row(self, target_id: int, kind: str, timestamp: Optional[float] = None, **fields: Any) -> None:
		row: Dict[str, Any] = {name: "" for name in self.FIELDNAMES}
		row["timestamp"] = time.time() if timestamp is None else timestamp
		row["target_id"] = target_id
		row["kind"] = kind
		for key, value in fields.items():
			row[key] = "" if value is None else value
		with self._lock:
			self._writer.writerow(row)
			self._file.flush()

	def close(self) -> None:
		with self._lock:
			if not self._file.closed:
				self._file.close()

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

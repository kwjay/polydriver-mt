"""Run log (C8.2): a CSV with both boards side by side and a JSON sidecar describing the run.

Each board is polled by its own thread, so polls never land at the same moment. A row holds one
poll of each board: the first poll of the other board after this one's. Every poll lands in
exactly one row; a missed poll leaves its side empty rather than repeating the last value. Each
side keeps its own host and device time, so the pairing can be checked in the file. All times
are seconds since the run started, on the host's perf_counter (the Session's clock).
"""

import csv
import dataclasses
import enum
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config
from ..devices import BoardStatus, Event, FleetCommand, Segment, SessionLike
from ..protocol.constants import Role, StatusFlag
from ..requests import SettingsReport

SCHEMA = 1
DISPENSERS = (Role.POLYMER, Role.COLOUR)
BOARD_COLUMNS = (
	"host_s", "device_us", "raw_hz", "filtered_hz", "duty", "flags", "job_state", "segment", "job_elapsed_ms",
	"edges", "control_resyncs", "longest_loop_us", "dropped_frames", "bad_crc_frames", "restarted",
)
COLUMNS = ["t_s"] + [f"{role.name.lower()}_{name}" for role in DISPENSERS for name in BOARD_COLUMNS]


class RunLogError(Exception):
	pass


def _name(role: Role) -> str:
	return role.name.lower()


def _flags_text(flags: StatusFlag) -> str:
	return "|".join(flag.name for flag in StatusFlag if flag in flags)


def _board_cells(status: BoardStatus, host_s: float) -> list:
	r = status.report
	return [
		f"{host_s:.4f}", status.device_time_us, r.raw_speed_hz, r.filtered_speed_hz, r.duty, _flags_text(r.flags),
		r.job_state.name, r.segment_index, r.job_elapsed_ms, r.edge_count,
		r.control_resyncs, r.longest_loop_interval_us, r.dropped_frames, r.bad_crc_frames, int(status.device_restarted),
	]


def _plain(value: Any) -> Any:
	"""A value JSON can hold: enums by name, dataclasses as dicts."""
	if isinstance(value, enum.Enum):
		return value.name
	if dataclasses.is_dataclass(value) and not isinstance(value, type):
		return {key: _plain(item) for key, item in dataclasses.asdict(value).items()}
	if isinstance(value, dict):
		return {(_plain(key) if isinstance(key, enum.Enum) else key): _plain(item) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_plain(item) for item in value]
	return value


def settings_dict(report: SettingsReport) -> dict:
	return _plain(report)


def segments_list(segments: Sequence[Segment]) -> list[dict]:
	return [{"value": s.value, "duration_ms": s.duration_ms, "ramp": s.ramp, "duty": s.duty} for s in segments]


class RunLog:
	"""A Session sink. Create it with RunLog.create() or start_run_log(); close() ends the CSV, and the
	sidecar can still be updated afterwards (note, weighed mass)."""

	def __init__(self, csv_path: Path, roles: Sequence[Role], clock: Callable[[], float], sidecar: dict):
		self.csv_path = csv_path
		self.sidecar_path = csv_path.with_suffix(".json")
		self._roles = tuple(role for role in DISPENSERS if role in roles)
		self._clock = clock
		self._started_s = clock()
		self._lock = threading.Lock()
		self._pending: dict[Role, tuple[BoardStatus, float]] = {}
		self._rows = 0
		self._closed = False
		self._detach: Callable[[], None] | None = None
		self._sidecar = sidecar
		self._file = csv_path.open("x", newline="", encoding="utf-8")
		self._writer = csv.writer(self._file)
		self._writer.writerow(COLUMNS)
		self._file.flush()
		self._write_sidecar()

	@classmethod
	def create(
		cls,
		folder: Path,
		roles: Sequence[Role],
		*,
		name: str | None = None,
		note: str = "",
		settings: dict | None = None,
		clock: Callable[[], float] = time.perf_counter,
	) -> "RunLog":
		folder = Path(folder)
		try:
			folder.mkdir(parents=True, exist_ok=True)
		except OSError as error:
			raise RunLogError(f"cannot make the log folder {folder}: {error}") from error
		started = datetime.now().astimezone()
		base = name or started.strftime("run-%Y-%m-%d_%H%M%S")
		sidecar = {
			"schema": SCHEMA,
			"csv": None,
			"started": started.isoformat(timespec="seconds"),
			"ended": None,
			"duration_s": None,
			"result": None,
			"note": note,
			"roles": [_name(role) for role in DISPENSERS if role in roles],
			"settings": settings or {},
			"boards": {},
			"job": None,
			"fleet": [],
			"events": [],
			"rows": 0,
			"weighed_mass_g": {_name(role): None for role in DISPENSERS if role in roles},
			"clock": (
				"t_s and *_host_s: seconds since the run started (host perf_counter); "
				"*_device_us: board micros(), unwrapped"
			),
		}
		for attempt in range(1, 1000):
			stem = base if attempt == 1 else f"{base}-{attempt}"
			path = folder / f"{stem}.csv"
			if path.exists() or path.with_suffix(".json").exists():
				continue
			sidecar["csv"] = path.name
			try:
				return cls(path, roles, clock, sidecar)
			except FileExistsError:
				continue
			except OSError as error:
				raise RunLogError(f"cannot write the run log {path}: {error}") from error
		raise RunLogError(f"no free name for {base} in {folder}")

	@property
	def rows(self) -> int:
		return self._rows

	# --- Sink ---------------------------------------------------------------------

	def record_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		if role not in self._roles:
			return
		with self._lock:
			if self._closed:
				return
			if role in self._pending:
				self._write_row()
			self._pending[role] = (status, host_time_s - self._started_s)
			if len(self._pending) == len(self._roles):
				self._write_row()

	def record_event(self, event: Event) -> None:
		with self._lock:
			if self._closed:
				return
			self._sidecar["events"].append({
				"t_s": round(event.host_time_s - self._started_s, 4), "role": _name(event.role), "kind": str(event.kind),
				"message": event.message,
			})
			self._write_sidecar()

	def _write_row(self) -> None:
		row: list = [f"{min(t for _, t in self._pending.values()):.4f}"]
		for role in DISPENSERS:
			if role in self._pending:
				status, host_s = self._pending[role]
				row += _board_cells(status, host_s)
			else:
				row += [""] * len(BOARD_COLUMNS)
		self._writer.writerow(row)
		self._file.flush()
		self._pending.clear()
		self._rows += 1

	# --- What the run used --------------------------------------------------------------

	def set_board(self, role: Role, *, port: str, firmware: str, protocol: int, settings: dict) -> None:
		with self._lock:
			board = self._sidecar["boards"].setdefault(_name(role), {"segments": None, "calibration_hash": None})
			board.update(port=port, firmware=firmware, protocol=protocol, settings=settings)
			self._write_sidecar()

	def set_segments(self, role: Role, segments: Sequence[Segment], calibration_hash: str | None = None) -> None:
		"""The job uploaded to that board, as the board runs it."""
		with self._lock:
			board = self._sidecar["boards"].setdefault(_name(role), {})
			board.update(segments=segments_list(segments), calibration_hash=calibration_hash)
			self._write_sidecar()

	def set_job(self, job: Any) -> None:
		with self._lock:
			self._sidecar["job"] = _plain(job)
			self._write_sidecar()

	def add_fleet_command(self, command: FleetCommand) -> None:
		def relative(value: float | None) -> float | None:
			return None if value is None else round(value - self._started_s, 6)

		bounds = command.gap_bounds_s()
		entry = {
			"command": command.command.name,
			"gap_s": command.gap_s(),
			"gap_bounds_s": None if bounds is None else list(bounds),
			"boards": {
				_name(role): {
					"first_sent_s": relative(t.first_sent_s), "sent_s": relative(t.sent_s), "replied_s": relative(t.replied_s),
					"attempts": t.attempts, "error": t.error,
				}
				for role, t in command.timings.items()
			},
		}
		with self._lock:
			self._sidecar["fleet"].append(entry)
			self._write_sidecar()

	def set_note(self, note: str) -> None:
		with self._lock:
			self._sidecar["note"] = note
			self._write_sidecar()

	def set_weighed_mass(self, role: Role, grams: float) -> None:
		if not grams >= 0:
			raise ValueError(f"the weighed mass must be >= 0 g, not {grams}")
		if role not in self._roles:
			raise ValueError(f"the {_name(role)} board was not in this run")
		with self._lock:
			self._sidecar["weighed_mass_g"][_name(role)] = grams
			self._write_sidecar()

	# --- Ending -------------------------------------------------------------------------

	def close(self, result: str | None = None) -> None:
		detach = None
		with self._lock:
			if self._closed:
				return
			if self._pending:
				self._write_row()
			self._closed = True
			self._file.close()
			ended = datetime.now().astimezone()
			self._sidecar.update(
				ended=ended.isoformat(timespec="seconds"), duration_s=round(self._clock() - self._started_s, 3),
				result=result, rows=self._rows,
			)
			self._write_sidecar()
			detach, self._detach = self._detach, None
		if detach is not None:
			detach()

	def follow(self, session: SessionLike) -> None:
		"""Takes the session's polls and events until close()."""
		session.add_sink(self)
		with self._lock:
			self._detach = lambda: session.remove_sink(self)

	def __enter__(self) -> "RunLog":
		return self

	def __exit__(self, exc_type, exc_value, traceback) -> None:
		self.close()

	def _write_sidecar(self) -> None:
		"""Written whole to a temporary file and moved over the old one, so it is never half written."""
		temporary = self.sidecar_path.with_suffix(".json.tmp")
		temporary.write_text(json.dumps(self._sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
		os.replace(temporary, self.sidecar_path)


def start_run_log(
	session: SessionLike,
	config: Config,
	*,
	roles: Sequence[Role] | None = None,
	name: str | None = None,
	note: str = "",
	clock: Callable[[], float] = time.perf_counter,
) -> RunLog:
	"""A RunLog in the config's log folder, filled with what the connected boards are set to and
	attached to the session; close() detaches it."""
	roles = list(roles) if roles is not None else session.roles()
	if not roles:
		raise RunLogError("no board connected, so there is nothing to log")
	log = RunLog.create(
		config.resolve(config.logging.folder), roles, name=name, note=note, clock=clock,
		settings={"comms_timeout_s": config.comms_timeout_s, "poll_hz": config.logging.poll_hz},
	)
	for role in roles:
		state = session.state(role)
		v = state.version
		log.set_board(role, port=state.port, firmware=f"{v.firmware_major}.{v.firmware_minor}.{v.firmware_patch}",
					protocol=v.protocol_version, settings=settings_dict(state.settings))
	log.follow(session)
	return log

"""Checks a run log (C8.6): the CSV and its sidecar agree with each other and with how a run goes.

Rows: times go forward; a board's device time and edge count go forward unless it flagged a
restart; once a board runs its job it stays RUNNING, with its job time going forward and a segment
of its job, until it ends DONE or ABORTED, and it does not run again. Against the run (when the
runner wrote one): the states in order, the result its last state, a reason only for ABORTED, the
JOB_RUN that reached both boards, a DONE run's boards DONE at the end of their job, an ABORTED run
leaving no board RUNNING unless that board's link was lost.
"""

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .run_log import COLUMNS, DISPENSERS, SCHEMA

RUN_ORDER = ("IDLE", "UPLOADED", "READY", "RUNNING")
ENDS = ("DONE", "ABORTED")
LOST_EVENTS = ("link lost", "disconnected")
BOARD_KEYS = ("port", "firmware")
UPLOAD_KEYS = ("segments", "calibration_hash", "expected_g")


@dataclass(frozen=True)
class BoardSummary:
	name: str
	rows: int
	last_state: str | None
	last_elapsed_ms: int | None
	total_ms: int | None
	expected_g: float | None
	estimated_g: float | None

	def line(self) -> str:
		state = self.last_state or "never polled"
		if self.last_elapsed_ms is not None and self.total_ms is not None:
			state += f" at {self.last_elapsed_ms} of {self.total_ms} ms"
		text = f"{self.name}: {state}, {self.rows} polls"
		if self.estimated_g is not None:
			text += f", {self.estimated_g:.4g} g from the edges"
			if self.expected_g:
				text += f" ({self.expected_g:.4g} g expected, {(self.estimated_g / self.expected_g - 1) * 100:+.1f} %)"
		return text


@dataclass(frozen=True)
class LogCheck:
	problems: tuple[str, ...]
	result: str | None
	rows: int
	duration_s: float | None
	boards: dict[str, BoardSummary]

	@property
	def ok(self) -> bool:
		return not self.problems

	def report(self) -> list[str]:
		count = len(self.problems)
		found = "no problems" if not count else f"{count} problem{'s' if count > 1 else ''}"
		duration = "" if self.duration_s is None else f" over {self.duration_s:g} s"
		lines = [f"run log: {self.result or 'no run result'}, {self.rows} rows{duration}; {found}"]
		lines += [f"problem: {problem}" for problem in self.problems]
		return lines + [board.line() for board in self.boards.values()]


def check_run_log(path: Path) -> LogCheck:
	"""path: the CSV or its sidecar."""
	path = Path(path)
	csv_path, sidecar_path = path.with_suffix(".csv"), path.with_suffix(".json")
	if not sidecar_path.exists():
		return _failed(f"no sidecar at {sidecar_path}")
	try:
		sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
	except (OSError, ValueError) as error:
		return _failed(f"the sidecar is not JSON: {error}")
	if not csv_path.exists():
		return _failed(f"no CSV at {csv_path}")
	if sidecar.get("schema") != SCHEMA:
		return _failed(f"sidecar schema {sidecar.get('schema')}; this check reads schema {SCHEMA}")
	with csv_path.open(encoding="utf-8", newline="") as file:
		table = list(csv.reader(file))
	if not table or table[0] != COLUMNS:
		return _failed(f"the CSV columns are not the run log's (schema {SCHEMA})")
	return _Checker(sidecar, csv_path, table[1:]).check()


def _failed(problem: str) -> LogCheck:
	return LogCheck((problem,), None, 0, None, {})


def _number(text: str) -> float | None:
	return None if text == "" else float(text)


class _Board:
	def __init__(self, name: str, entry: dict):
		self.name = name
		self.entry = entry
		segments = entry.get("segments")
		self.segments = len(segments) if segments else None
		self.total_ms = sum(s["duration_ms"] for s in segments) if segments else None
		self.rows = 0
		self.host_s = self.device_us = self.edges = self.elapsed_ms = None
		self.state = None
		self.phase = "before"  # then "running", then the end state
		self.last_elapsed_ms = None


class _Checker:
	def __init__(self, sidecar: dict, csv_path: Path, rows: list[list[str]]):
		self.sidecar = sidecar
		self.csv_path = csv_path
		self.rows = rows
		self.run = sidecar.get("run")
		self.problems: list[str] = []
		names = [role.name.lower() for role in DISPENSERS]
		self.boards = {name: _Board(name, sidecar.get("boards", {}).get(name, {}))
					for name in names if name in sidecar.get("roles", names)}

	def check(self) -> LogCheck:
		self._files()
		self._board_entries()
		self._rows()
		self._board_ends()
		if self.run is not None:
			self._run()
		summaries = {
			b.name: BoardSummary(b.name, b.rows, b.state, b.last_elapsed_ms, b.total_ms, b.entry.get("expected_g"),
								b.entry.get("estimated_g"))
			for b in self.boards.values()
		}
		return LogCheck(tuple(self.problems), self.sidecar.get("result"), len(self.rows), self.sidecar.get("duration_s"),
						summaries)

	def _files(self) -> None:
		s, add = self.sidecar, self.problems.append
		if s.get("csv") != self.csv_path.name:
			add(f"the sidecar names {s.get('csv')}, not {self.csv_path.name}")
		if s.get("ended") is None:
			add("the log was never closed (no end time in the sidecar)")
			return
		if s.get("rows") != len(self.rows):
			add(f"the sidecar counts {s.get('rows')} rows, the CSV has {len(self.rows)}")
		if self.rows and s.get("duration_s") is not None:
			last_s = float(self.rows[-1][0])
			if s["duration_s"] < last_s:
				add(f"the sidecar's duration {s['duration_s']:g} s ends before the last row ({last_s:g} s)")

	def _board_entries(self) -> None:
		keys = BOARD_KEYS + (UPLOAD_KEYS if self.run is not None else ())
		ran = self.run is not None and "RUNNING" in self._states()
		for board in self.boards.values():
			for key in keys + (("estimated_g",) if ran else ()):
				if board.entry.get(key) is None:
					self.problems.append(f"{board.name}: no {key} in the sidecar")

	def _rows(self) -> None:
		previous_t = None
		for number, row in enumerate(self.rows, start=1):
			t = float(row[0])
			if previous_t is not None and t < previous_t:
				self.problems.append(f"row {number}: t_s {t:g} is before the row above ({previous_t:g})")
			previous_t = t
			for board in self.boards.values():
				self._board_row(number, board, {name: row[COLUMNS.index(f"{board.name}_{name}")] for name in (
					"host_s", "device_us", "edges", "job_state", "job_elapsed_ms", "segment", "restarted")})

	def _board_row(self, number: int, b: _Board, cell: dict[str, str]) -> None:
		if cell["host_s"] == "":
			return
		add = self.problems.append
		where = f"row {number}: {b.name}"
		b.rows += 1
		host_s, device_us, edges = float(cell["host_s"]), int(cell["device_us"]), int(cell["edges"])
		state, elapsed_ms = cell["job_state"], int(cell["job_elapsed_ms"])
		segment = None if cell["segment"] == "" else int(cell["segment"])
		restarted = cell["restarted"] == "1"

		if b.host_s is not None and host_s <= b.host_s:
			add(f"{where} host_s {host_s:g} does not come after {b.host_s:g}")
		if b.device_us is not None and device_us <= b.device_us and not restarted:
			add(f"{where} device_us went back ({b.device_us} to {device_us}) with no restart flagged")
		if b.edges is not None and edges < b.edges and not restarted:
			add(f"{where} edges went back ({b.edges} to {edges}) with no restart flagged")

		if b.phase == "running":
			if state in ENDS:
				b.phase = state
			elif state != "RUNNING":
				if restarted:
					b.phase = state
				else:
					add(f"{where} went from RUNNING to {state} with no restart flagged")
		elif b.phase in ENDS:
			if state == "RUNNING":
				add(f"{where} went from {b.phase} to RUNNING")
		elif state == "RUNNING":
			b.phase = "running"

		if b.state in ("RUNNING",) + ENDS and state in ("RUNNING",) + ENDS and b.elapsed_ms is not None \
				and elapsed_ms < b.elapsed_ms:
			add(f"{where} job time went back ({b.elapsed_ms} to {elapsed_ms} ms)")
		if state == "RUNNING":
			if segment is None:
				add(f"{where} is RUNNING with no segment")
			elif b.segments is not None and segment >= b.segments:
				add(f"{where} reports segment {segment} of a {b.segments}-segment job")
		elif state in ENDS and segment is not None:
			add(f"{where} is {state} but reports segment {segment}")

		b.host_s, b.device_us, b.edges, b.state, b.elapsed_ms = host_s, device_us, edges, state, elapsed_ms
		b.last_elapsed_ms = elapsed_ms

	def _board_ends(self) -> None:
		for b in self.boards.values():
			if b.state == "DONE" and b.total_ms is not None and b.last_elapsed_ms != b.total_ms:
				self.problems.append(f"{b.name} ended DONE at {b.last_elapsed_ms} ms of its {b.total_ms} ms job")

	def _states(self) -> list[str]:
		return [entry["state"] for entry in self.run.get("states", [])]

	def _run(self) -> None:
		add, result, run = self.problems.append, self.sidecar.get("result"), self.run
		states = self._states()
		end = states[-1] if states and states[-1] in ENDS else None
		middle = states[:-1] if end else states
		if middle != list(RUN_ORDER[:len(middle)]) or (end is None and result is not None) or \
				(end == "DONE" and middle != list(RUN_ORDER)):
			add(f"the run's states go {', '.join(states)}")
		if result is not None and end is not None and result != end:
			add(f"the result is {result} but the run's last state is {end}")
		if result == "ABORTED" and not run.get("reason"):
			add("an ABORTED run must have a reason")
		if result == "DONE" and run.get("reason"):
			add(f"the run ended DONE but has a reason: {run['reason']}")

		if "RUNNING" in states:
			runs = [entry for entry in self.sidecar.get("fleet", []) if entry.get("command") == "JOB_RUN"]
			if not runs:
				add("the run was RUNNING but no JOB_RUN is in the fleet commands")
			else:
				for name, timing in runs[0].get("boards", {}).items():
					if timing.get("error"):
						add(f"JOB_RUN to {name} failed: {timing['error']}")

		lost = {event.get("role") for event in self.sidecar.get("events", []) if event.get("kind") in LOST_EVENTS}
		for b in self.boards.values():
			if result == "DONE" and b.state != "DONE":
				add(f"the run ended DONE but {b.name}'s last state is {b.state}")
			if result == "ABORTED" and b.state == "RUNNING" and b.name not in lost:
				add(f"the run ended ABORTED but {b.name}'s last logged state is RUNNING")

"""Calibration data (C7.1): weighed measurements per (dispenser, material), saved as JSON.

A dispenser is named after the physical screw and motor (e.g. "A", "B"), not after the board it
is plugged into, so a curve stays valid when a dispenser moves to the other board. Each curve has
a hash of its raw measurements; jobs and run sidecars store it, so a later recalibration is seen.
"""

import hashlib
import json
import math
import os
import statistics
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = 2
DEFAULT_METHOD = "steady-state"
OLD_METHOD = "unknown"

HASHED_FIELDS = ("speed_hz", "grams", "duration_s", "taken_at", "method")


class CalibrationError(Exception):
	pass


class UnknownCurveError(CalibrationError):
	pass


class CalibrationFileError(CalibrationError):
	pass


@dataclass(frozen=True)
class Measurement:
	"""One weighing: the screw held at speed_hz for duration_s gave grams. taken_at is ISO 8601 with
	the UTC offset, None for measurements imported from an old file."""

	speed_hz: float
	grams: float
	duration_s: float
	taken_at: str | None = None
	method: str = DEFAULT_METHOD
	note: str = ""

	@property
	def rate_g_s(self) -> float:
		return self.grams / self.duration_s

	def problems(self) -> list[str]:
		found = []
		if not (_is_number(self.speed_hz) and math.isfinite(self.speed_hz) and self.speed_hz >= 0):
			found.append(f"speed {self.speed_hz!r} Hz must be a number >= 0")
		if not (_is_number(self.grams) and math.isfinite(self.grams) and self.grams >= 0):
			found.append(f"grams {self.grams!r} must be a number >= 0")
		if not (_is_number(self.duration_s) and math.isfinite(self.duration_s) and self.duration_s > 0):
			found.append(f"duration {self.duration_s!r} s must be a number > 0")
		if self.taken_at is not None and not isinstance(self.taken_at, str):
			found.append(f"taken_at {self.taken_at!r} must be text or null")
		if not isinstance(self.method, str) or not self.method.strip():
			found.append(f"method {self.method!r} must be text")
		if not isinstance(self.note, str):
			found.append(f"note {self.note!r} must be text")
		return found


@dataclass(frozen=True)
class Point:
	"""All measurements at one speed: the mean rate, how many, and max - min of their rates."""

	speed_hz: float
	rate_g_s: float
	repeats: int
	spread_g_s: float


@dataclass(frozen=True)
class Curve:
	dispenser: str
	material: str
	measurements: tuple[Measurement, ...]

	@property
	def key(self) -> tuple[str, str]:
		return self.dispenser, self.material

	@property
	def points(self) -> list[Point]:
		by_speed: dict[float, list[float]] = {}
		for m in self.measurements:
			by_speed.setdefault(m.speed_hz, []).append(m.rate_g_s)
		return [
			Point(speed, statistics.fmean(rates), len(rates), max(rates) - min(rates))
			for speed, rates in sorted(by_speed.items())
		]

	@property
	def hash(self) -> str:
		"""SHA-256 of the dispenser, the material and every raw value, in any order; notes are left
		out, so fixing a note does not make a job's reference stale."""
		rows = sorted(json.dumps([getattr(m, name) for name in HASHED_FIELDS]) for m in self.measurements)
		text = json.dumps({"dispenser": self.dispenser, "material": self.material, "measurements": rows})
		return hashlib.sha256(text.encode("utf-8")).hexdigest()

	@property
	def short_hash(self) -> str:
		return self.hash[:12]


class CalibrationStore:
	def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now().astimezone()):
		self._clock = clock
		self._lock = threading.Lock()
		self._curves: dict[tuple[str, str], tuple[Measurement, ...]] = {}

	def add_measurement(self, dispenser: str, material: str, speed_hz: float, grams: float, duration_s: float, *,
						method: str = DEFAULT_METHOD, note: str = "", taken_at: str | None = None) -> Curve:
		taken_at = taken_at if taken_at is not None else self._clock().isoformat(timespec="seconds")
		measurement = Measurement(speed_hz, grams, duration_s, taken_at, method, note)
		problems = measurement.problems()
		if problems:
			raise CalibrationError("; ".join(problems))
		return self._add(dispenser, material, measurement)

	def _add(self, dispenser: str, material: str, measurement: Measurement) -> Curve:
		dispenser, material = _name(dispenser, "dispenser"), _name(material, "material")
		with self._lock:
			self._refuse_case_twins(dispenser, material)
			key = (dispenser, material)
			self._curves[key] = self._curves.get(key, ()) + (measurement,)
			return Curve(dispenser, material, self._curves[key])

	def remove_measurement(self, dispenser: str, material: str, index: int) -> Curve | None:
		"""Returns the curve without it, or None when that was its last measurement."""
		with self._lock:
			key = self._key(dispenser, material)
			measurements = self._curves[key]
			if not 0 <= index < len(measurements):
				count = f"{len(measurements)} measurement" + ("" if len(measurements) == 1 else "s")
				raise CalibrationError(f"there is no measurement {index} in {key[0]} / {key[1]}, it has {count}")
			left = measurements[:index] + measurements[index + 1:]
			if left:
				self._curves[key] = left
				return Curve(*key, left)
			del self._curves[key]
			return None

	def curve(self, dispenser: str, material: str) -> Curve:
		with self._lock:
			key = self._key(dispenser, material)
			return Curve(*key, self._curves[key])

	def curves(self) -> list[Curve]:
		with self._lock:
			return [Curve(*key, measurements) for key, measurements in sorted(self._curves.items())]

	def _key(self, dispenser: str, material: str) -> tuple[str, str]:
		key = (dispenser.strip(), material.strip())
		if key not in self._curves:
			if not self._curves:
				raise UnknownCurveError(f"no calibration for dispenser {key[0]}, material {key[1]}: no calibrations yet")
			known = ", ".join(f"{d} / {m}" for d, m in sorted(self._curves))
			raise UnknownCurveError(f"no calibration for dispenser {key[0]}, material {key[1]}; there are: {known}")
		return key

	def _refuse_case_twins(self, dispenser: str, material: str) -> None:
		for known_dispenser, known_material in self._curves:
			if dispenser != known_dispenser and dispenser.casefold() == known_dispenser.casefold():
				raise CalibrationError(f"dispenser {dispenser!r} differs from {known_dispenser!r} only in case")
			if material != known_material and material.casefold() == known_material.casefold():
				raise CalibrationError(f"material {material!r} differs from {known_material!r} only in case")


# --- Files ---------------------------------------------------------------------------------

def save_calibration(store: CalibrationStore, path: Path) -> None:
	"""Written whole to a temporary file and moved over the old one, so it is never half written."""
	path = Path(path)
	data = {
		"schema": SCHEMA,
		"curves": [
			{
				"dispenser": curve.dispenser,
				"material": curve.material,
				"hash": curve.hash,
				"measurements": [_measurement_dict(m) for m in curve.measurements],
			}
			for curve in store.curves()
		],
	}
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(path.name + ".tmp")
	temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	os.replace(temporary, path)


def load_calibration(path: Path, *, old_material: str | None = None,
					old_dispensers: Mapping[int, str] | None = None) -> CalibrationStore:
	"""A missing file gives an empty store. An old file (one board, keyed by target id, no materials)
	needs old_material; old_dispensers names its targets, otherwise they become "target N"."""
	path = Path(path)
	store = CalibrationStore()
	if not path.exists():
		return store
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as error:
		raise CalibrationFileError(f"{path}: not valid JSON ({error})") from error
	if not isinstance(data, dict):
		raise CalibrationFileError(f"{path}: the file must hold a JSON object")
	if "schema" not in data and "targets" in data:
		_load_old(path, data, store, old_material, old_dispensers or {})
		return store
	if data.get("schema") != SCHEMA:
		raise CalibrationFileError(f"{path}: schema {data.get('schema')!r}, this program reads schema {SCHEMA}")
	curves = data.get("curves")
	if not isinstance(curves, list):
		raise CalibrationFileError(f"{path}: 'curves' must be a list")
	seen: dict[tuple[str, str], int] = {}
	for index, entry in enumerate(curves):
		where = f"{path}: curve {index}"
		try:
			dispenser, material, rows = entry["dispenser"], entry["material"], entry["measurements"]
			dispenser, material = _name(dispenser, "dispenser"), _name(material, "material")
			if (dispenser, material) in seen:
				raise CalibrationFileError(
					f"{where}: {dispenser} / {material} is already curve {seen[dispenser, material]}; keep one of them"
				)
			seen[dispenser, material] = index
			if not rows:
				raise CalibrationFileError(f"{where} ({dispenser} / {material}): no measurements; remove the curve")
			for row_index, row in enumerate(rows):
				measurement = _measurement_from(row, f"{where}, measurement {row_index}")
				store._add(dispenser, material, measurement)
		except (KeyError, TypeError, AttributeError) as error:
			raise CalibrationFileError(f"{where}: missing or wrong field {error}") from error
		except CalibrationFileError:
			raise
		except CalibrationError as error:
			raise CalibrationFileError(f"{where}: {error}") from error
		saved_hash = entry.get("hash")
		if saved_hash is not None and saved_hash != store.curve(dispenser, material).hash:
			raise CalibrationFileError(
				f"{where} ({dispenser} / {material}): the measurements do not match the saved hash, so the file "
				"was changed by hand; jobs made with the old values would point at different data. Remove that "
				"curve's \"hash\" line to accept the change"
			)
	return store


def _load_old(path: Path, data: dict, store: CalibrationStore, material: str | None,
			dispensers: Mapping[int, str]) -> None:
	if material is None:
		raise CalibrationFileError(
			f"{path}: an old calibration file (one board, no materials); load it again with the material it was "
			"measured with"
		)
	targets = data["targets"]
	if not isinstance(targets, dict):
		raise CalibrationFileError(f"{path}: 'targets' must be an object in an old calibration file")
	for target, entry in targets.items():
		try:
			number = int(target)
		except ValueError:
			raise CalibrationFileError(f"{path}: target {target!r} must be a number") from None
		if not isinstance(entry, dict):
			raise CalibrationFileError(f"{path}: target {target}: must be an object")
		rows = entry.get("measurements", [])
		if not isinstance(rows, list):
			raise CalibrationFileError(f"{path}: target {target}: 'measurements' must be a list")
		dispenser = dispensers.get(number, f"target {target}")
		for index, row in enumerate(rows):
			where = f"{path}: target {target}, measurement {index}"
			try:
				measurement = Measurement(row["speed"], row["grams"], row["duration_s"], None, OLD_METHOD,
										f"imported from the old file {path.name}")
			except (KeyError, TypeError) as error:
				raise CalibrationFileError(f"{where}: missing or wrong field {error}") from error
			problems = measurement.problems()
			if problems:
				raise CalibrationFileError(f"{where}: {'; '.join(problems)}")
			store._add(dispenser, material, measurement)


def _measurement_dict(m: Measurement) -> dict[str, Any]:
	return {"speed_hz": m.speed_hz, "grams": m.grams, "duration_s": m.duration_s, "taken_at": m.taken_at,
			"method": m.method, "note": m.note}


def _measurement_from(row: dict, where: str) -> Measurement:
	try:
		measurement = Measurement(row["speed_hz"], row["grams"], row["duration_s"], row.get("taken_at"),
								row.get("method", DEFAULT_METHOD), row.get("note", ""))
	except KeyError as error:
		raise CalibrationFileError(f"{where}: missing {error.args[0]}") from error
	problems = measurement.problems()
	if problems:
		raise CalibrationFileError(f"{where}: {'; '.join(problems)}")
	return measurement


def _name(value: str, what: str) -> str:
	if not isinstance(value, str) or not value.strip() or any(ch in value.strip() for ch in "\r\n\t"):
		raise CalibrationError(f"the {what} name {value!r} must be non-empty text on one line")
	return value.strip()


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool)

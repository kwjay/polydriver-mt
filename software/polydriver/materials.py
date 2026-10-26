"""Materials registry (C7.4): what the program knows about each granulate, saved as JSON.

A material is a base polymer (polymer board) or a masterbatch (colour board). Its PID gains and
feedforward are optional and, when given, replace the board's from the config for runs with that
material; alpha stays a board setting. The flow window is the g/s this material may be dispensed
at on its own channel, a process limit on top of the calibrated range. The default dispenser picks
the calibration curve (dispenser, material).
"""

import json
import math
import os
import threading
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from .calibration import CalibrationStore, Curve
from .config import MAX_DUTY, BoardSettings
from .protocol.constants import Role

SCHEMA = 1


class MaterialError(Exception):
	pass


class InvalidMaterialError(MaterialError):
	def __init__(self, label: str, problems: list[str]):
		self.problems = tuple(problems)
		super().__init__(f"{label}: " + "; ".join(problems))


class UnknownMaterialError(MaterialError):
	pass


class MaterialFileError(MaterialError):
	pass


class Kind(StrEnum):
	POLYMER = "polymer"
	MASTERBATCH = "masterbatch"


@dataclass(frozen=True)
class Material:
	name: str
	kind: Kind
	dispenser: str | None = None
	flow_min_g_s: float | None = None
	flow_max_g_s: float | None = None
	kp: float | None = None
	ki: float | None = None
	kd: float | None = None
	ff_offset_duty: float | None = None
	ff_gain_duty_per_hz: float | None = None
	note: str = ""

	def __post_init__(self) -> None:
		problems = []
		name = _clean_name(self.name, "material", problems)
		object.__setattr__(self, "name", name)
		if self.dispenser is not None:
			object.__setattr__(self, "dispenser", _clean_name(self.dispenser, "dispenser", problems))
		try:
			object.__setattr__(self, "kind", Kind(self.kind))
		except (ValueError, TypeError):
			problems.append(f"kind {str(self.kind)!r} must be polymer or masterbatch")
		problems += _flow_problems(self.flow_min_g_s, self.flow_max_g_s)
		problems += _group_problems(("kp", "ki", "kd"), (self.kp, self.ki, self.kd))
		problems += _feedforward_problems(self.ff_offset_duty, self.ff_gain_duty_per_hz)
		if not isinstance(self.note, str):
			problems.append(f"note {self.note!r} must be text")
		if problems:
			raise InvalidMaterialError(name if isinstance(name, str) and name else repr(self.name), problems)

	@property
	def role(self) -> Role:
		return Role.POLYMER if self.kind is Kind.POLYMER else Role.COLOUR

	@property
	def has_gains(self) -> bool:
		return self.kp is not None

	@property
	def has_feedforward(self) -> bool:
		return self.ff_offset_duty is not None

	@property
	def has_flow_window(self) -> bool:
		return self.flow_min_g_s is not None

	def allows_flow(self, rate_g_s: float) -> bool:
		return not self.has_flow_window or self.flow_min_g_s <= rate_g_s <= self.flow_max_g_s

	def board_settings(self, base: BoardSettings) -> BoardSettings:
		"""The board's settings for a run with this material: its gains and feedforward, where given."""
		settings = base
		if self.has_gains:
			settings = replace(settings, kp=self.kp, ki=self.ki, kd=self.kd)
		if self.has_feedforward:
			settings = replace(settings, ff_offset_duty=self.ff_offset_duty, ff_gain_duty_per_hz=self.ff_gain_duty_per_hz)
		return settings

	def curve(self, store: CalibrationStore, dispenser: str | None = None) -> Curve:
		dispenser = dispenser if dispenser is not None else self.dispenser
		if dispenser is None:
			raise MaterialError(f"{self.name} has no default dispenser; name the dispenser whose calibration to use")
		return store.curve(dispenser, self.name)


FIELDS = tuple(f.name for f in fields(Material))


class MaterialRegistry:
	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._materials: dict[str, Material] = {}

	def put(self, material: Material) -> Material:
		"""Adds the material, or replaces the one with the same name."""
		with self._lock:
			for known in self._materials:
				if known != material.name and known.casefold() == material.name.casefold():
					raise MaterialError(f"material {material.name!r} differs from {known!r} only in case")
			self._materials[material.name] = material
			return material

	def remove(self, name: str) -> None:
		with self._lock:
			del self._materials[self._known(name)]

	def get(self, name: str) -> Material:
		with self._lock:
			return self._materials[self._known(name)]

	def materials(self) -> list[Material]:
		with self._lock:
			return [self._materials[name] for name in sorted(self._materials)]

	def of_kind(self, kind: Kind | str) -> list[Material]:
		kind = Kind(kind)
		return [m for m in self.materials() if m.kind is kind]

	def _known(self, name: str) -> str:
		name = name.strip()
		if name not in self._materials:
			if not self._materials:
				raise UnknownMaterialError(f"no material {name!r}: no materials yet")
			raise UnknownMaterialError(f"no material {name!r}; there are: {', '.join(sorted(self._materials))}")
		return name


# --- Files ---------------------------------------------------------------------------------

def save_materials(registry: MaterialRegistry, path: Path) -> None:
	"""Every key is written, null where not set, so the file shows what can be filled in."""
	path = Path(path)
	data = {"schema": SCHEMA, "materials": [_material_dict(m) for m in registry.materials()]}
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(path.name + ".tmp")
	temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	os.replace(temporary, path)


def load_materials(path: Path) -> MaterialRegistry:
	"""A missing file gives an empty registry."""
	path = Path(path)
	registry = MaterialRegistry()
	if not path.exists():
		return registry
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as error:
		raise MaterialFileError(f"{path}: not valid JSON ({error})") from error
	if not isinstance(data, dict):
		raise MaterialFileError(f"{path}: the file must hold a JSON object")
	if data.get("schema") != SCHEMA:
		raise MaterialFileError(f"{path}: schema {data.get('schema')!r}, this program reads schema {SCHEMA}")
	entries = data.get("materials")
	if not isinstance(entries, list):
		raise MaterialFileError(f"{path}: 'materials' must be a list")
	seen: dict[str, int] = {}
	for index, entry in enumerate(entries):
		where = f"{path}: material {index}"
		if not isinstance(entry, dict):
			raise MaterialFileError(f"{where}: must be an object")
		if "name" not in entry:
			raise MaterialFileError(f"{where}: missing 'name'")
		named = f"{where} ({entry['name']})"
		if "kind" not in entry:
			raise MaterialFileError(f"{named}: missing 'kind'")
		unknown = [key for key in entry if key not in FIELDS]
		if unknown:
			raise MaterialFileError(f"{named}: unknown key {unknown[0]!r}; the keys are {', '.join(FIELDS)}")
		try:
			material = Material(**{key: value for key, value in entry.items() if not (key == "note" and value is None)})
		except InvalidMaterialError as error:
			raise MaterialFileError(f"{named}: {'; '.join(error.problems)}") from error
		if material.name in seen:
			raise MaterialFileError(f"{where}: {material.name} is already material {seen[material.name]}")
		seen[material.name] = index
		try:
			registry.put(material)
		except MaterialError as error:
			raise MaterialFileError(f"{named}: {error}") from error
	return registry


def _material_dict(material: Material) -> dict[str, Any]:
	return {name: getattr(material, name) for name in FIELDS} | {"kind": material.kind.value}


# --- Checks --------------------------------------------------------------------------------

def _clean_name(value: Any, what: str, problems: list[str]) -> Any:
	if not isinstance(value, str) or not value.strip() or any(ch in value.strip() for ch in "\r\n\t"):
		problems.append(f"the {what} name {value!r} must be non-empty text on one line")
		return value
	return value.strip()


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _group_problems(names: tuple[str, ...], values: tuple[Any, ...]) -> list[str]:
	"""All of a group or none of it, each a finite number >= 0."""
	given = [value is not None for value in values]
	if not any(given):
		return []
	if not all(given):
		return [f"give {_and(names)} together, or none to use the board's"]
	return [f"{name} = {value!r}: must be a finite number >= 0"
			for name, value in zip(names, values) if not (_is_number(value) and value >= 0)]


def _flow_problems(low: Any, high: Any) -> list[str]:
	problems = _group_problems(("flow_min_g_s", "flow_max_g_s"), (low, high))
	if not problems and low is not None and low >= high:
		problems.append(f"flow_min_g_s = {low!r}: must be below flow_max_g_s = {high!r}")
	return problems


def _feedforward_problems(offset: Any, gain: Any) -> list[str]:
	problems = _group_problems(("ff_offset_duty", "ff_gain_duty_per_hz"), (offset, gain))
	if not problems and offset is not None and offset > MAX_DUTY:
		problems.append(f"ff_offset_duty = {offset!r}: must be 0 to {MAX_DUTY}")
	return problems


def _and(names: tuple[str, ...]) -> str:
	return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

"""Gradient job file (C7.7): one JSON file per job, laid out as in GUI_gradient_job_flow.md §3.

A job names a material and a calibration (dispenser, material, hash) per board, the two or three
amounts the user gave, and the profile. The hash records which calibration the job was made with,
so a later recalibration is seen (check.py). pid_override stays null: gains come from the
materials (C7.4).
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..calibration import Curve
from .curves import CurveError, ProgressCurve, make_curve
from .profile import GradientProfile, ProfileError, resolve_amount

JOB_SCHEMA = 1
ROLES = ("polymer", "colour")
AMOUNT_KEYS = ("duration_s", "total_mass_g", "total_flow_g_s")
PROFILE_KEYS = ("phi_start", "phi_end", "curve", "curve_params", "hold_start_s", "hold_end_s", "table")
REQUIRED_KEYS = ("schema", "name", "materials", "calibration", "amount", "profile")
KEYS = (*REQUIRED_KEYS, "created", "modified", "pid_override", "run_defaults")


class JobError(Exception):
	pass


class JobFileError(JobError):
	pass


@dataclass(frozen=True)
class CalibrationRef:
	dispenser: str
	material: str
	hash: str

	@classmethod
	def of(cls, curve: Curve) -> "CalibrationRef":
		return cls(curve.dispenser, curve.material, curve.hash)

	def problems(self) -> list[str]:
		found = [p for p in (_name_problem(self.dispenser, "dispenser"), _name_problem(self.material, "material")) if p]
		if not isinstance(self.hash, str) or not self.hash.strip():
			found.append(f"hash {self.hash!r} must be non-empty text")
		return found


@dataclass(frozen=True)
class GradientJob:
	name: str
	polymer: CalibrationRef
	colour: CalibrationRef
	amount_given: dict[str, float]
	phi_start: float
	phi_end: float
	curve: ProgressCurve
	hold_start_s: float = 0.0
	hold_end_s: float = 0.0
	created: str | None = None
	modified: str | None = None
	log: bool | None = None
	live_plot: bool | None = None
	profile: GradientProfile = field(init=False, compare=False, repr=False)

	def __post_init__(self) -> None:
		if isinstance(self.amount_given, dict):
			object.__setattr__(self, "amount_given", {k: v for k, v in self.amount_given.items() if v is not None})
		problems = []
		name_problem = _name_problem(self.name, "job")
		if name_problem:
			problems.append(name_problem)
		for role in ROLES:
			ref = getattr(self, role)
			if not isinstance(ref, CalibrationRef):
				problems.append(f"{role} calibration {ref!r} must be a CalibrationRef")
				continue
			problems += [f"{role} calibration ({ref.dispenser} / {ref.material}): {p}" for p in ref.problems()]
		problems += [
			f"unknown amount {key!r}; the amounts are duration_s, total_mass_g and total_flow_g_s"
			for key in self.amount_given if key not in AMOUNT_KEYS
		]
		for name in ("created", "modified"):
			value = getattr(self, name)
			if value is not None and not _is_iso_time(value):
				problems.append(
					f"{name} {value!r} must be an ISO 8601 time with its UTC offset, e.g. 2026-10-26T17:00:00+01:00"
				)
		for name in ("log", "live_plot"):
			value = getattr(self, name)
			if value is not None and not isinstance(value, bool):
				problems.append(f"{name} {value!r} must be true, false or null")
		if problems:
			raise JobError("; ".join(problems))
		try:
			amount = resolve_amount(**self.amount_given)
		except ProfileError as error:
			raise JobError(f"amount: {error}") from error
		try:
			profile = GradientProfile(self.phi_start, self.phi_end, self.curve, amount, self.hold_start_s, self.hold_end_s)
		except ProfileError as error:
			raise JobError(f"profile: {error}") from error
		object.__setattr__(self, "profile", profile)


# --- Files ---------------------------------------------------------------------------------

def save_job(job: GradientJob, path: Path) -> None:
	"""Written whole to a temporary file and moved over the old one, so it is never half written."""
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_name(path.name + ".tmp")
	temporary.write_text(json.dumps(job_dict(job), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	os.replace(temporary, path)


def job_dict(job: GradientJob) -> dict[str, Any]:
	"""The job as its file holds it."""
	curve, curve_params, table = job.curve.spec()
	return {
		"schema": JOB_SCHEMA,
		"name": job.name,
		"created": job.created,
		"modified": job.modified,
		"materials": {role: getattr(job, role).material for role in ROLES},
		"calibration": {role: _ref_dict(getattr(job, role)) for role in ROLES},
		"amount": {key: job.amount_given.get(key) for key in AMOUNT_KEYS},
		"profile": {
			"phi_start": job.phi_start, "phi_end": job.phi_end, "curve": curve, "curve_params": curve_params,
			"hold_start_s": job.hold_start_s, "hold_end_s": job.hold_end_s, "table": table,
		},
		"pid_override": None,
		"run_defaults": {"log": job.log, "live_plot": job.live_plot},
	}


def load_job(path: Path) -> GradientJob:
	path = Path(path)
	if not path.exists():
		raise JobFileError(f"{path}: no job file there")
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except json.JSONDecodeError as error:
		raise JobFileError(f"{path}: not valid JSON ({error})") from error
	try:
		return _job_from(data)
	except JobFileError as error:
		raise JobFileError(f"{path}: {error}") from error
	except JobError as error:
		raise JobFileError(f"{path}: {error}") from error


def _job_from(data: Any) -> GradientJob:
	if not isinstance(data, dict):
		raise JobFileError("the file must hold a JSON object")
	if data.get("schema") != JOB_SCHEMA:
		raise JobFileError(f"schema {data.get('schema')!r}, this program reads schema {JOB_SCHEMA}")
	_object(data, "", REQUIRED_KEYS, KEYS)
	materials = _object(data["materials"], "materials", ROLES, ROLES)
	calibration = _object(data["calibration"], "calibration", ROLES, ROLES)
	refs = {}
	for role in ROLES:
		where = f"calibration.{role}"
		entry = _object(calibration[role], where, ("dispenser", "material", "hash"), ("dispenser", "material", "hash"))
		if entry["material"] != materials[role]:
			raise JobFileError(f"{where} is for {entry['material']} but materials.{role} is {materials[role]}")
		refs[role] = CalibrationRef(entry["dispenser"], entry["material"], entry["hash"])
	amount = _object(data["amount"], "amount", (), AMOUNT_KEYS)
	profile = _object(data["profile"], "profile", ("phi_start", "phi_end", "curve"), PROFILE_KEYS)
	try:
		curve = make_curve(profile["curve"], profile.get("curve_params"), profile.get("table"))
	except CurveError as error:
		raise JobFileError(f"profile: {error}") from error
	if data.get("pid_override") is not None:
		raise JobFileError("pid_override must be null: the gains come from the materials; set them there")
	run_defaults = _object(data.get("run_defaults") or {}, "run_defaults", (), ("log", "live_plot"))
	for key, value in run_defaults.items():
		if value is not None and not isinstance(value, bool):
			raise JobFileError(f"run_defaults: {key} {value!r} must be true, false or null")
	return GradientJob(
		name=data["name"], polymer=refs["polymer"], colour=refs["colour"],
		amount_given=dict(amount),
		phi_start=profile["phi_start"], phi_end=profile["phi_end"], curve=curve,
		hold_start_s=profile.get("hold_start_s", 0.0), hold_end_s=profile.get("hold_end_s", 0.0),
		created=data.get("created"), modified=data.get("modified"),
		log=run_defaults.get("log"), live_plot=run_defaults.get("live_plot"),
	)


def _object(value: Any, where: str, required: tuple[str, ...], allowed: tuple[str, ...]) -> dict:
	prefix = f"{where}: " if where else ""
	if not isinstance(value, dict):
		raise JobFileError(f"{prefix}must be an object, not {value!r}")
	for key in required:
		if key not in value:
			raise JobFileError(f"{prefix}missing {key!r}")
	for key in value:
		if key not in allowed:
			raise JobFileError(f"{prefix}unknown key {key!r}; the keys are {', '.join(allowed)}")
	return value


def _ref_dict(ref: CalibrationRef) -> dict[str, str]:
	return {"dispenser": ref.dispenser, "material": ref.material, "hash": ref.hash}


def _name_problem(value: Any, what: str) -> str | None:
	if not isinstance(value, str) or not value.strip() or any(ch in value for ch in "\r\n\t"):
		return f"the {what} name {value!r} must be non-empty text on one line"
	return None


def _is_iso_time(value: Any) -> bool:
	if not isinstance(value, str):
		return False
	try:
		return datetime.fromisoformat(value).tzinfo is not None
	except ValueError:
		return False

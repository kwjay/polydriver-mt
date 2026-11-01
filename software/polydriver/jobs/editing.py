"""What the window's job editor needs besides the job itself (C10.5): the profile from what was
typed (for the preview, before any calibration is chosen), the job with its calibration hashes, the
jobs folder as a list, and file names. Pure, so the editor holds no logic."""

import math
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from ..calibration import CalibrationError, CalibrationStore
from .curves import CurveError, make_curve
from .profile import GradientProfile, ProfileError, resolve_amount
from .recipe import AMOUNT_KEYS, CalibrationRef, GradientJob, JobError, JobFileError, load_job, save_job

CURVE_PARAM = {"exponential": ("k", 3.0), "s-curve": ("k", 10.0), "steps": ("levels", 5)}
"""The one parameter each curve takes, and its value for a new job."""


@dataclass(frozen=True)
class JobForm:
	"""The editor's fields, numbers already read (None: left blank)."""

	name: str
	polymer_material: str
	polymer_dispenser: str
	colour_material: str
	colour_dispenser: str
	duration_s: float | None
	total_mass_g: float | None
	total_flow_g_s: float | None
	phi_start: float
	phi_end: float
	curve: str
	curve_param: float | None = None
	table: tuple[tuple[float, float], ...] | None = None
	hold_start_s: float = 0.0
	hold_end_s: float = 0.0
	log: bool | None = None
	live_plot: bool | None = None

	@property
	def amounts(self) -> dict[str, float]:
		values = (self.duration_s, self.total_mass_g, self.total_flow_g_s)
		return {key: value for key, value in zip(AMOUNT_KEYS, values) if value is not None}


def form_curve(form: JobForm):
	param = CURVE_PARAM.get(form.curve)
	params = {} if param is None or form.curve_param is None else {param[0]: _param(param[0], form.curve_param)}
	table = [list(point) for point in form.table] if form.curve == "table" and form.table is not None else None
	return make_curve(form.curve, params, table)


def form_profile(form: JobForm) -> GradientProfile:
	"""The gradient as typed; raises JobError with every reason it is not one."""
	try:
		amount = resolve_amount(**form.amounts)
		return GradientProfile(form.phi_start, form.phi_end, form_curve(form), amount, form.hold_start_s,
							form.hold_end_s)
	except (ProfileError, CurveError) as error:
		raise JobError(str(error)) from None


def form_job(form: JobForm, store: CalibrationStore, *, created: str | None = None,
			now: datetime | None = None) -> GradientJob:
	"""The job, each board's calibration named with its hash as it is now. created stays as it was
	(now for a new job); modified is now."""
	refs = {}
	for role in ("polymer", "colour"):
		material, dispenser = getattr(form, f"{role}_material").strip(), getattr(form, f"{role}_dispenser").strip()
		if not material:
			raise JobError(f"{role}: choose a material")
		if not dispenser:
			raise JobError(f"{role}: name the dispenser whose calibration of {material} to use")
		try:
			refs[role] = CalibrationRef.of(store.curve(dispenser, material))
		except CalibrationError:
			raise JobError(f"{role}: there is no calibration {dispenser} / {material}; make one on Calibration") from None
	stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
	form_profile(form)
	return GradientJob(form.name.strip(), refs["polymer"], refs["colour"], form.amounts, form.phi_start, form.phi_end,
					form_curve(form), form.hold_start_s, form.hold_end_s, created=created or stamp, modified=stamp,
					log=form.log, live_plot=form.live_plot)


def job_form(job: GradientJob) -> JobForm:
	curve, params, table = job.curve.spec()
	param = CURVE_PARAM.get(curve)
	return JobForm(
		job.name, job.polymer.material, job.polymer.dispenser, job.colour.material, job.colour.dispenser,
		job.amount_given.get("duration_s"), job.amount_given.get("total_mass_g"), job.amount_given.get("total_flow_g_s"),
		job.phi_start, job.phi_end, curve, params.get(param[0]) if param else None,
		tuple(tuple(point) for point in table) if table else None, job.hold_start_s, job.hold_end_s, job.log,
		job.live_plot,
	)


def parse_table(text: str) -> tuple[tuple[float, float], ...]:
	"""Points "x p" separated by ";" or new lines, e.g. "0 0; 0.5 0.8; 1 1"; a comma may be the
	decimal mark."""
	points = []
	for number, part in enumerate((p for p in re.split(r"[;\n]", text) if p.strip()), start=1):
		words = part.split()
		if len(words) != 2:
			raise JobError(f"table point {number} {part.strip()!r}: give x and progress, e.g. 0.5 0.8")
		try:
			points.append((float(words[0].replace(",", ".")), float(words[1].replace(",", "."))))
		except ValueError:
			raise JobError(f"table point {number} {part.strip()!r}: x and progress must be numbers") from None
	return tuple(points)


def table_text(table: tuple[tuple[float, float], ...] | None) -> str:
	return "; ".join(f"{x:g} {p:g}" for x, p in table or ())


# --- The jobs folder --------------------------------------------------------------------------

@dataclass(frozen=True)
class JobEntry:
	path: Path
	job: GradientJob | None
	problem: str | None = None


def list_jobs(folder: Path) -> list[JobEntry]:
	"""Every .json file in the folder, by name; one that does not load is listed with why."""
	folder = Path(folder)
	if not folder.is_dir():
		return []
	entries = []
	for path in sorted(folder.glob("*.json"), key=lambda p: p.stem.casefold()):
		try:
			entries.append(JobEntry(path, load_job(path)))
		except JobFileError as error:
			entries.append(JobEntry(path, None, str(error)))
	return entries


def job_file_name(name: str) -> str:
	"""A file name for a job name: letters, digits, '-', '_', '.' and spaces kept, the rest '_'."""
	stem = re.sub(r"[^\w\-. ]", "_", name.strip()).strip(" .") or "job"
	return f"{stem}.json"


def free_job_path(folder: Path, name: str, *, keep: Path | None = None) -> Path:
	"""Where to save a job of this name: its own file, or with -2, -3… when another file has it.
	keep: the job's current file, which it may keep."""
	path = Path(folder) / job_file_name(name)
	number = 2
	while path.exists() and path != keep:
		path = path.with_name(f"{Path(job_file_name(name)).stem}-{number}.json")
		number += 1
	return path


def duplicate_job(path: Path, *, now: datetime | None = None) -> Path:
	"""A copy named "NAME copy" in the same folder, created and modified now; returns its file."""
	job = load_job(path)
	stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
	copy = replace(job, name=f"{job.name} copy", created=stamp, modified=stamp)
	target = free_job_path(Path(path).parent, copy.name)
	save_job(copy, target)
	return target


def _param(name: str, value: float):
	if name == "levels" and math.isfinite(value) and float(value).is_integer():
		return int(value)
	return value

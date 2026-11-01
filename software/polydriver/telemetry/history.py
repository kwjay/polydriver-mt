"""Past run logs (C10.7): the log folder as a list, newest first, and the sidecar changed afterwards
(the weighed masses and the note), written whole so it is never half written. Only CSVs with a
sidecar are run logs; a sweep's CSV beside them is not."""

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from .run_log import SCHEMA


class SidecarError(Exception):
	pass


@dataclass(frozen=True)
class RunLogEntry:
	csv_path: Path
	sidecar: dict | None
	problem: str | None = None

	@property
	def sidecar_path(self) -> Path:
		return self.csv_path.with_suffix(".json")

	@property
	def started(self) -> str:
		return (self.sidecar or {}).get("started") or ""

	@property
	def job_name(self) -> str | None:
		job = (self.sidecar or {}).get("job")
		return job.get("name") if isinstance(job, dict) else None


def list_run_logs(folder: Path) -> list[RunLogEntry]:
	folder = Path(folder)
	if not folder.is_dir():
		return []
	entries = []
	for csv_path in folder.glob("*.csv"):
		sidecar_path = csv_path.with_suffix(".json")
		if not sidecar_path.exists():
			continue
		try:
			sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
			if not isinstance(sidecar, dict):
				raise ValueError("it does not hold a JSON object")
			entries.append(RunLogEntry(csv_path, sidecar))
		except (OSError, ValueError) as error:
			entries.append(RunLogEntry(csv_path, None, f"the sidecar does not read: {error}"))
	return sorted(entries, key=lambda e: (e.started, e.csv_path.name), reverse=True)


def update_sidecar(path: Path, *, weighed_mass_g: dict[str, float | None] | None = None,
				note: str | None = None) -> dict:
	"""path: the CSV or its sidecar. weighed_mass_g by board name ("polymer", "colour"); None clears
	one. Returns the sidecar as written."""
	sidecar_path = Path(path).with_suffix(".json")
	try:
		sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
	except (OSError, ValueError) as error:
		raise SidecarError(f"{sidecar_path}: cannot read it: {error}") from error
	if not isinstance(sidecar, dict) or sidecar.get("schema") != SCHEMA:
		raise SidecarError(f"{sidecar_path}: not a run log sidecar of schema {SCHEMA}")
	masses = sidecar.setdefault("weighed_mass_g", {})
	for name, grams in (weighed_mass_g or {}).items():
		if name not in masses:
			raise SidecarError(f"the {name} board was not in this run (its boards: {', '.join(masses) or 'none'})")
		if grams is not None and not (isinstance(grams, (int, float)) and math.isfinite(grams) and grams >= 0):
			raise SidecarError(f"the weighed mass must be >= 0 g, not {grams!r}")
		masses[name] = grams
	if note is not None:
		sidecar["note"] = note
	temporary = sidecar_path.with_suffix(".json.tmp")
	temporary.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
	os.replace(temporary, sidecar_path)
	return sidecar

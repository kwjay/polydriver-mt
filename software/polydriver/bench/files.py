"""Where a procedure's file goes: <folder>/<kind>-<board>-YYYY-MM-DD_HHMMSS.csv, never over another."""

from datetime import datetime
from pathlib import Path

from ..protocol.constants import Role


def measurement_path(folder: Path, kind: str, role: Role, *, now: datetime | None = None) -> Path:
	folder = Path(folder)
	folder.mkdir(parents=True, exist_ok=True)
	stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")
	base = f"{kind}-{role.name.lower()}-{stamp}"
	path = folder / f"{base}.csv"
	number = 2
	while path.exists():
		path = folder / f"{base}-{number}.csv"
		number += 1
	return path

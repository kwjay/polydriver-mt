import json
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class CalibrationError(Exception):
 ...


@dataclass(frozen=True)
class Measurement:
	speed: float
	grams: float
	duration_s: float

	@property
	def rate_g_s(self) -> float:
		return self.grams / self.duration_s


@dataclass(frozen=True)
class CalibrationPoint:
	speed: float
	rate_g_s: float
	repeats: int
	spread_g_s: float


@dataclass
class TargetCalibration:
	target_id: int
	measurements: List[Measurement] = field(default_factory=list)

	def add_measurement(self, speed: float, grams: float, duration_s: float) -> CalibrationPoint:
		if duration_s <= 0:
			raise ValueError("duration_s must be positive")
		if grams < 0:
			raise ValueError("grams can't be negative")
		self.measurements.append(Measurement(speed=speed, grams=grams, duration_s=duration_s))
		return self._point_at(speed)

	def _point_at(self, speed: float) -> CalibrationPoint:
		rates = [m.rate_g_s for m in self.measurements if m.speed == speed]
		return CalibrationPoint(
			speed=speed,
			rate_g_s=sum(rates) / len(rates),
			repeats=len(rates),
			spread_g_s=(max(rates) - min(rates)) if len(rates) > 1 else 0.0,
		)

	@property
	def points(self) -> List[CalibrationPoint]:
		speeds = sorted({m.speed for m in self.measurements})
		return [self._point_at(speed) for speed in speeds]

	@property
	def deadband_speed(self) -> Optional[float]:
		producing = [p for p in self.points if p.rate_g_s > 0]
		if not producing:
			return None
		return min(p.speed for p in producing)

	def rate_for_speed(self, speed: float) -> float:
		points = sorted(self.points, key=lambda p: p.speed)
		return self._interpolate(points, speed, x_attr="speed", y_attr="rate_g_s")

	def speed_for_rate(self, rate_g_s: float) -> float:
		points = sorted(self.points, key=lambda p: p.rate_g_s)
		return self._interpolate(points, rate_g_s, x_attr="rate_g_s", y_attr="speed")

	def _interpolate(self, points: List[CalibrationPoint], x: float, x_attr: str, y_attr: str) -> float:
		if len(points) < 2:
			raise CalibrationError(
				f"target {self.target_id} needs at least 2 calibrated speeds, has {len(points)}"
			)
		xs = [getattr(p, x_attr) for p in points]
		if x < xs[0] or x > xs[-1]:
			raise CalibrationError(
				f"target {self.target_id}: {x!r} is outside the calibrated range "
				f"[{xs[0]!r}, {xs[-1]!r}] - refusing to extrapolate"
			)
		for lo, hi in zip(points, points[1:]):
			lo_x, hi_x = getattr(lo, x_attr), getattr(hi, x_attr)
			if lo_x <= x <= hi_x:
				if hi_x == lo_x:
					return getattr(lo, y_attr)
				frac = (x - lo_x) / (hi_x - lo_x)
				return getattr(lo, y_attr) + frac * (getattr(hi, y_attr) - getattr(lo, y_attr))
		raise CalibrationError(f"target {self.target_id}: could not interpolate {x!r}")


class CalibrationStore:
	def __init__(self):
		self._targets: Dict[int, TargetCalibration] = {}
		self._lock = threading.Lock()

	def add_measurement(self, target_id: int, speed: float, grams: float, duration_s: float) -> CalibrationPoint:
		with self._lock:
			cal = self._targets.setdefault(target_id, TargetCalibration(target_id=target_id))
			return cal.add_measurement(speed, grams, duration_s)

	def get(self, target_id: int) -> TargetCalibration:
		with self._lock:
			cal = self._targets.get(target_id)
		if cal is None:
			raise CalibrationError(f"no calibration recorded yet for target {target_id}")
		return cal

	def points(self, target_id: int) -> List[CalibrationPoint]:
		return self.get(target_id).points

	def rate_for_speed(self, target_id: int, speed: float) -> float:
		return self.get(target_id).rate_for_speed(speed)

	def speed_for_rate(self, target_id: int, rate_g_s: float) -> float:
		return self.get(target_id).speed_for_rate(rate_g_s)

	def deadband_speed(self, target_id: int) -> Optional[float]:
		return self.get(target_id).deadband_speed

	def save(self, path: str) -> None:
		with self._lock:
			data = {
				"targets": {
					str(target_id): {
						"measurements": [
							{"speed": m.speed, "grams": m.grams, "duration_s": m.duration_s}
							for m in cal.measurements
						]
					}
					for target_id, cal in self._targets.items()
				}
			}
		parent = os.path.dirname(path)
		if parent:
			os.makedirs(parent, exist_ok=True)
		tmp_path = f"{path}.tmp"
		with open(tmp_path, "w") as f:
			json.dump(data, f, indent=2, sort_keys=True)
		os.replace(tmp_path, path)

	@classmethod
	def load(cls, path: str) -> "CalibrationStore":
		store = cls()
		if not os.path.exists(path):
			return store
		with open(path, "r") as f:
			data = json.load(f)
		for target_id_str, entry in data.get("targets", {}).items():
			target_id = int(target_id_str)
			for measurement in entry.get("measurements", []):
				store.add_measurement(target_id, measurement["speed"], measurement["grams"], measurement["duration_s"])
		return store

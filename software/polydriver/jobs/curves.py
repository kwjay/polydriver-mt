"""Gradient curves (C7.6): each maps the gradient's time 0…1 to its progress 0…1, exactly 0 and 1
at the ends and never falling, so the colour share always moves from its start value towards its
end value. mean() is the exact area under the curve, from which the profile gets the colour mass.
"""

import math
from collections.abc import Sequence
from typing import Any, ClassVar

# Beyond this the exponential and S-curves are a step: e**50 is 5e21.
MAX_K = 50.0


class CurveError(Exception):
	pass


class ProgressCurve:
	NAME: ClassVar[str]

	def progress(self, x: float) -> float:
		if not 0.0 <= x <= 1.0:
			raise CurveError(f"{self.NAME}: x = {x!r} is outside 0 to 1")
		return self._progress(x)

	def mean(self) -> float:
		raise NotImplementedError

	def params(self) -> dict[str, Any]:
		return {}

	def spec(self) -> tuple[str, dict[str, Any], list[list[float]] | None]:
		"""(curve, curve_params, table) as the job file stores them."""
		return self.NAME, self.params(), None

	def _progress(self, x: float) -> float:
		raise NotImplementedError

	def __repr__(self) -> str:
		params = ", ".join(f"{k}={v!r}" for k, v in self.params().items())
		return f"{type(self).__name__}({params})"


class LinearCurve(ProgressCurve):
	NAME = "linear"

	def _progress(self, x: float) -> float:
		return x

	def mean(self) -> float:
		return 0.5


class ExponentialCurve(ProgressCurve):
	"""(e**(k x) - 1) / (e**k - 1): k > 0 starts slow and ends fast, k < 0 the other way round."""

	NAME = "exponential"

	def __init__(self, k: float = 3.0):
		if not (_is_number(k) and abs(k) <= MAX_K):
			raise CurveError(f"{self.NAME}: k = {k!r} must be a number -{MAX_K:g} to {MAX_K:g}, not 0")
		if k == 0:
			raise CurveError(f"{self.NAME}: k = 0 is the linear curve; use linear")
		self.k = float(k)

	def _progress(self, x: float) -> float:
		return math.expm1(self.k * x) / math.expm1(self.k)

	def mean(self) -> float:
		return 1.0 / self.k - 1.0 / math.expm1(self.k)

	def params(self) -> dict[str, Any]:
		return {"k": self.k}


class SCurve(ProgressCurve):
	"""A logistic curve, stretched to run exactly from 0 to 1; k is its steepness in the middle."""

	NAME = "s-curve"

	def __init__(self, k: float = 10.0):
		if not (_is_number(k) and 0 < k <= MAX_K):
			raise CurveError(f"{self.NAME}: k = {k!r} must be above 0 and at most {MAX_K:g}")
		self.k = float(k)
		self._low = _logistic(-self.k / 2)
		self._span = _logistic(self.k / 2) - self._low

	def _progress(self, x: float) -> float:
		return (_logistic(self.k * (x - 0.5)) - self._low) / self._span

	def mean(self) -> float:
		return 0.5

	def params(self) -> dict[str, Any]:
		return {"k": self.k}


class StepsCurve(ProgressCurve):
	"""levels equal steps, each held for the same time: 0, 1/(levels-1), …, 1."""

	NAME = "steps"

	def __init__(self, levels: int = 5):
		if isinstance(levels, bool) or not isinstance(levels, (int, float)) or not float(levels).is_integer():
			raise CurveError(f"{self.NAME}: levels = {levels!r} must be a whole number, at least 2")
		if levels < 2:
			raise CurveError(f"{self.NAME}: levels = {levels!r} must be at least 2")
		self.levels = int(levels)

	def _progress(self, x: float) -> float:
		return min(math.floor(x * self.levels), self.levels - 1) / (self.levels - 1)

	def mean(self) -> float:
		return 0.5

	def params(self) -> dict[str, Any]:
		return {"levels": self.levels}


class TableCurve(ProgressCurve):
	"""Straight lines between (x, progress) points, from (0, 0) to (1, 1); flat parts are allowed."""

	NAME = "table"

	def __init__(self, points: Sequence[Sequence[float]]):
		if not isinstance(points, (list, tuple)):
			raise CurveError(f"{self.NAME}: the table must be a list of points [x, progress], not {points!r}")
		if len(points) < 2:
			raise CurveError(f"{self.NAME}: the table needs at least 2 points, from [0, 0] to [1, 1]")
		clean: list[tuple[float, float]] = []
		for number, point in enumerate(points, start=1):
			where = f"{self.NAME}: point {number}"
			if not (isinstance(point, (list, tuple)) and len(point) == 2):
				raise CurveError(f"{where}: {point!r} must be a pair [x, progress]")
			x, p = point
			if not (_is_number(x) and 0 <= x <= 1):
				raise CurveError(f"{where}: x = {x!r} must be a number 0 to 1")
			if not (_is_number(p) and 0 <= p <= 1):
				raise CurveError(f"{where}: progress = {p!r} must be a number 0 to 1")
			if clean and x <= clean[-1][0]:
				raise CurveError(f"{where}: x = {x!r} must come after the previous point's x = {clean[-1][0]!r}")
			if clean and p < clean[-1][1]:
				raise CurveError(f"{where}: progress = {p!r} falls below the previous point's {clean[-1][1]!r}")
			clean.append((float(x), float(p)))
		if clean[0] != (0.0, 0.0):
			raise CurveError(f"{self.NAME}: the table must start at [0, 0], not {list(clean[0])}")
		if clean[-1] != (1.0, 1.0):
			raise CurveError(f"{self.NAME}: the table must end at [1, 1], not {list(clean[-1])}")
		self.points = tuple(clean)

	def _progress(self, x: float) -> float:
		for (x0, p0), (x1, p1) in zip(self.points, self.points[1:]):
			if x <= x1:
				return p0 + (p1 - p0) * (x - x0) / (x1 - x0)
		raise AssertionError("x in 0 to 1 lies on the table")

	def mean(self) -> float:
		return sum((x1 - x0) * (p0 + p1) / 2 for (x0, p0), (x1, p1) in zip(self.points, self.points[1:]))

	def spec(self) -> tuple[str, dict[str, Any], list[list[float]] | None]:
		return self.NAME, {}, [list(point) for point in self.points]

	def __repr__(self) -> str:
		return f"TableCurve({list(self.points)})"


CURVES: dict[str, type[ProgressCurve]] = {c.NAME: c for c in (LinearCurve, ExponentialCurve, SCurve, StepsCurve, TableCurve)}
CURVE_NAMES = tuple(CURVES)
_PARAMS = {"linear": (), "exponential": ("k",), "s-curve": ("k",), "steps": ("levels",), "table": ()}


def make_curve(name: str, params: dict[str, Any] | None = None, table: Sequence[Sequence[float]] | None = None) -> ProgressCurve:
	"""The curve a job file names: "curve", "curve_params" and "table"."""
	if name not in CURVES:
		raise CurveError(f"unknown curve {name!r}; the curves are {', '.join(CURVE_NAMES)}")
	params = {} if params is None else params
	if not isinstance(params, dict):
		raise CurveError(f"{name}: the parameters must be an object, not {params!r}")
	allowed = _PARAMS[name]
	for key in params:
		if key not in allowed:
			raise CurveError(f"{name}: unknown parameter {key!r}; it takes {', '.join(allowed) or 'none'}")
	if name == "table":
		if table is None:
			raise CurveError("table: needs a table of points [x, progress]")
		return TableCurve(table)
	if table is not None:
		raise CurveError(f"{name}: a table is only used by the table curve")
	return CURVES[name](**params)


def _logistic(z: float) -> float:
	return 1.0 / (1.0 + math.exp(-z))


def _is_number(value: Any) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

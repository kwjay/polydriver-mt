"""Envelope check of a gradient job (C7.7, BACKLOG B6), point by point.

Errors (the job cannot run as asked): a flow outside a channel's calibrated range or its
material's flow window; job-wide, a missing or wrong-kind material and a missing or falling
calibration. Warnings: a speed below what the encoder measures, the colour channel below its
lowest reliable flow, and a calibration that changed since the job was saved. 0 g/s is a stopped
screw and is not checked. phi only moves one way, so the two ends of the run decide whether
every point fits; the points in between are for the report and the preview.
"""

import math
from dataclasses import dataclass

from ..calibration import CalibrationError, CalibrationStore, Curve, speed_for_rate
from ..calibration.fit import SPREAD_LIMIT
from ..materials import Kind, Material, MaterialRegistry, UnknownMaterialError
from .recipe import ROLES, GradientJob

# As in the firmware's CaptureMath.h (a test reads it): a period longer than two timer overflows
# is a stalled screw, so this is the slowest speed the board can measure.
CLOCK_HZ = 16_000_000.0
PRESCALER = 64
TIMER_PERIOD_TICKS = 65_536
EDGE_TIMEOUT_OVERFLOWS = 2
MIN_MEASURABLE_SPEED_HZ = CLOCK_HZ / (PRESCALER * TIMER_PERIOD_TICKS * EDGE_TIMEOUT_OVERFLOWS)

KIND_OF_ROLE = {"polymer": Kind.POLYMER, "colour": Kind.MASTERBATCH}


@dataclass(frozen=True)
class Issue:
	code: str
	error: bool
	limit: str
	value: float
	unit: str

	@property
	def text(self) -> str:
		return f"{self.value:.4g} {self.unit} is {self.limit}"


@dataclass(frozen=True)
class ChannelPoint:
	rate_g_s: float
	speed_hz: float | None
	issues: tuple[Issue, ...] = ()


@dataclass(frozen=True)
class JobPoint:
	t_s: float
	phi: float
	polymer: ChannelPoint
	colour: ChannelPoint


@dataclass(frozen=True)
class JobCheck:
	problems: tuple[str, ...]
	warnings: tuple[str, ...]
	points: tuple[JobPoint, ...]

	@property
	def ok(self) -> bool:
		return not self.problems and not any(
			issue.error for point in self.points for role in ROLES for issue in getattr(point, role).issues
		)

	def report(self) -> list[str]:
		"""Job-wide errors, then the points' errors in runs, then the warnings the same way."""
		runs = _runs(self.points)
		lines = [f"error: {p}" for p in self.problems] + [f"error: {line}" for error, line in runs if error]
		lines += [f"warning: {w}" for w in self.warnings] + [f"warning: {line}" for error, line in runs if not error]
		return lines or ["no problems: every point is inside both calibrations"]


@dataclass
class _Channel:
	role: str
	material: Material | None
	curve: Curve | None
	reliable_g_s: float | None


def check_job(job: GradientJob, materials: MaterialRegistry, store: CalibrationStore, *,
			step_s: float = 1.0) -> JobCheck:
	problems: list[str] = []
	warnings: list[str] = []
	channels = {role: _channel(role, job, materials, store, problems, warnings) for role in ROLES}
	profile = job.profile
	times = {i * step_s for i in range(math.ceil(profile.duration_s / step_s))}
	times |= {profile.hold_start_s, profile.duration_s - profile.hold_end_s, profile.duration_s}
	points = tuple(
		JobPoint(t, profile.phi(t), _point(channels["polymer"], profile.polymer_g_s(t)),
				_point(channels["colour"], profile.colour_g_s(t)))
		for t in sorted(times)
	)
	return JobCheck(tuple(problems), tuple(warnings), points)


def _channel(role: str, job: GradientJob, materials: MaterialRegistry, store: CalibrationStore,
			problems: list[str], warnings: list[str]) -> _Channel:
	ref = getattr(job, role)
	material = None
	try:
		material = materials.get(ref.material)
	except UnknownMaterialError as error:
		problems.append(f"{role}: {error}")
	if material is not None and material.kind is not KIND_OF_ROLE[role]:
		problems.append(f"{role}: {material.name} is a {material.kind.value}; the {role} board takes a "
						f"{KIND_OF_ROLE[role].value}")
	try:
		curve = store.curve(ref.dispenser, ref.material)
		speed_for_rate(curve, curve.points[0].rate_g_s)
	except CalibrationError as error:
		problems.append(f"{role}: {error}")
		return _Channel(role, material, None, None)
	if curve.hash != ref.hash:
		warnings.append(f"{role}: the calibration {curve.dispenser} / {curve.material} has changed since the job was saved")
	reliable = None
	if role == "colour":
		agreeing = [p for p in curve.points if p.rate_g_s > 0 and p.repeats > 1 and p.spread_g_s <= SPREAD_LIMIT * p.rate_g_s]
		if agreeing:
			reliable = agreeing[0].rate_g_s
		else:
			warnings.append(
				f"{role}: no point of the calibration {curve.dispenser} / {curve.material} has repeats that agree within "
				f"{SPREAD_LIMIT * 100:g} %, so its lowest reliable flow is unknown; repeat the low-speed measurements"
			)
	return _Channel(role, material, curve, reliable)


def _point(channel: _Channel, rate_g_s: float) -> ChannelPoint:
	if rate_g_s == 0:
		return ChannelPoint(0.0, 0.0)
	curve = channel.curve
	if curve is None:
		return ChannelPoint(rate_g_s, None)
	issues = []
	name = f"{curve.dispenser} / {curve.material}"
	low, high = curve.points[0].rate_g_s, curve.points[-1].rate_g_s
	speed = None
	if rate_g_s < low:
		issues.append(Issue("below_calibration", True, f"below the calibrated {low:g} to {high:g} g/s of {name}", rate_g_s, "g/s"))
	elif rate_g_s > high:
		issues.append(Issue("above_calibration", True, f"above the calibrated {low:g} to {high:g} g/s of {name}", rate_g_s, "g/s"))
	else:
		speed = speed_for_rate(curve, rate_g_s)
	material = channel.material
	if material is not None and not material.allows_flow(rate_g_s):
		side = "below" if rate_g_s < material.flow_min_g_s else "above"
		window = f"{material.name}'s flow window {material.flow_min_g_s:g} to {material.flow_max_g_s:g} g/s"
		issues.append(Issue(f"{side}_flow_window", True, f"{side} {window}", rate_g_s, "g/s"))
	if speed is not None and speed < MIN_MEASURABLE_SPEED_HZ:
		issues.append(Issue(
			"below_measurable_speed", False,
			f"below {MIN_MEASURABLE_SPEED_HZ:.4g} Hz, the lowest speed the encoder measures; the board would read the "
			"screw as stalled", speed, "Hz",
		))
	if speed is not None and channel.reliable_g_s is not None and rate_g_s < channel.reliable_g_s:
		issues.append(Issue(
			"below_reliable_flow", False,
			f"below the lowest reliable flow {channel.reliable_g_s:g} g/s of {name} (its lowest point whose repeats "
			f"agree within {SPREAD_LIMIT * 100:g} %)", rate_g_s, "g/s",
		))
	return ChannelPoint(rate_g_s, speed, tuple(issues))


def _runs(points: tuple[JobPoint, ...]) -> list[tuple[bool, str]]:
	"""Consecutive points with the same issue, as (error, line), in time order."""
	found: list[tuple[float, int, bool, str]] = []
	for order, role in enumerate(ROLES):
		codes = {issue.code for point in points for issue in getattr(point, role).issues}
		for code in codes:
			run: list[tuple[float, Issue]] = []
			for point in points + (None,):
				issue = None if point is None else next((i for i in getattr(point, role).issues if i.code == code), None)
				if issue is not None:
					run.append((point.t_s, issue))
					continue
				if run:
					found.append((run[0][0], order, run[0][1].error, _run_line(role, run)))
					run = []
	return [(error, line) for _, _, error, line in sorted(found)]


def _run_line(role: str, run: list[tuple[float, Issue]]) -> str:
	first_t, first = run[0]
	if len(run) == 1:
		return f"{role}, at {first_t:g} s: {first.text}"
	pick = min if first.code.startswith("below") else max
	worst_t, worst = pick(run, key=lambda item: item[1].value)
	word = "lowest" if pick is min else "highest"
	return (f"{role}, {first_t:g} to {run[-1][0]:g} s ({len(run)} points): {first.limit}; {word} {worst.value:.4g} "
			f"{worst.unit} at {worst_t:g} s")

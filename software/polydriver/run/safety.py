"""Safety rules for a running gradient job (C8.5). Pure: fed every STATUS and Session event of the
run and a timer tick, they answer with an abort reason and warnings; the runner stops both boards.

Abort when a board aborts its job or no longer runs it, when its link is lost or it is
disconnected or restarts, when it sends no STATUS for longer than silence_s, when its speed stays
off the commanded one for tracking_s of its job time, or when the job runs overtime_s past its
total time. Below the speed the encoder measures (1.907 Hz) the speed is not judged. An encoder
stall is a warning, once per stall, but only once the encoder has turned in this job (or since a
stretch too slow to measure): a screw spinning up reads as stalled for a moment on every run. A stall
that lasts is a tracking error and is aborted as one, with or without the warning.
"""

import math
from dataclasses import dataclass, fields

from ..devices.session import Event, EventKind
from ..jobs import MIN_MEASURABLE_SPEED_HZ, GradientProgram, speed_at
from ..protocol.constants import JobState, Role, StatusFlag
from ..requests import StatusReport

ROLES = (Role.POLYMER, Role.COLOUR)


@dataclass(frozen=True)
class SafetyLimits:
	"""tracking: the measured speed may be off the commanded one by more than both tracking_fraction
	of it and tracking_floor_hz for at most tracking_s of the board's job time."""

	tracking_fraction: float = 0.25
	tracking_floor_hz: float = 1.0
	tracking_s: float = 3.0
	overtime_s: float = 2.0
	silence_s: float = 2.0

	def __post_init__(self) -> None:
		for f in fields(self):
			value = getattr(self, f.name)
			if isinstance(value, bool) or not isinstance(value, (int, float)) or not (math.isfinite(value) and value > 0):
				raise ValueError(f"{f.name} = {value!r} must be a finite number > 0")


@dataclass(frozen=True)
class Verdict:
	abort: str | None = None
	warnings: tuple[str, ...] = ()


EVENT_ABORTS = {
	EventKind.LINK_LOST: "the {name} board's link was lost: {message}",
	EventKind.DISCONNECTED: "the {name} board was disconnected",
	EventKind.RESTARTED: "the {name} board restarted and lost its job",
}

ABORT_CAUSES = (
	(StatusFlag.COMMS_LOST, ": it heard nothing from the host for its comms timeout"),
	(StatusFlag.ESTOP, ": ESTOP"),
)


class SafetyWatch:
	def __init__(self, program: GradientProgram, limits: SafetyLimits, started_s: float):
		self._limits = limits
		self._segments = {role: getattr(program, _name(role)).segments for role in ROLES}
		self._total_s = program.total_ms / 1000
		self._deadline_s = started_s + self._total_s + limits.overtime_s
		self._last_status_s = {role: started_s for role in ROLES}
		self._off_since_ms: dict[Role, int | None] = {role: None for role in ROLES}
		# True until the encoder is seen turning: the screws stand when the job starts.
		self._stalled = {role: True for role in ROLES}
		self._done: set[Role] = set()

	def on_status(self, role: Role, report: StatusReport, host_time_s: float) -> Verdict:
		self._last_status_s[role] = host_time_s
		name, state, elapsed_ms = _name(role), report.job_state, report.job_elapsed_ms
		if state is JobState.ABORTED:
			cause = "".join(text for flag, text in ABORT_CAUSES if flag in report.flags)
			return Verdict(f"the {name} board aborted its job at {elapsed_ms / 1000:g} s{cause}")
		if state is JobState.DONE:
			self._done.add(role)
			return Verdict()
		if state is not JobState.RUNNING:
			return Verdict(f"the {name} board no longer runs its job (it reports {state.name}; did it restart?)")

		commanded = speed_at(self._segments[role], elapsed_ms)
		if commanded < MIN_MEASURABLE_SPEED_HZ:
			self._off_since_ms[role], self._stalled[role] = None, True
			return Verdict()
		warnings = ()
		stalled = StatusFlag.ENCODER_STALLED in report.flags
		if stalled and not self._stalled[role]:
			warnings = (f"the {name} encoder stalled at {elapsed_ms / 1000:g} s with {commanded:.3g} Hz commanded",)
		self._stalled[role] = stalled

		measured = report.filtered_speed_hz
		limits = self._limits
		if abs(measured - commanded) <= max(limits.tracking_fraction * commanded, limits.tracking_floor_hz):
			self._off_since_ms[role] = None
			return Verdict(None, warnings)
		since_ms = self._off_since_ms[role]
		if since_ms is None:
			self._off_since_ms[role] = since_ms = elapsed_ms
		if elapsed_ms - since_ms < limits.tracking_s * 1000:
			return Verdict(None, warnings)
		return Verdict(
			f"the {name} speed was off by more than {limits.tracking_fraction * 100:g} % (and "
			f"{limits.tracking_floor_hz:g} Hz) for {limits.tracking_s:g} s: {measured:.3g} Hz measured, "
			f"{commanded:.3g} Hz commanded at {elapsed_ms / 1000:g} s" + (" (encoder stalled)" if stalled else ""),
			warnings,
		)

	def on_event(self, event: Event) -> Verdict:
		text = EVENT_ABORTS.get(event.kind)
		if text is None:
			return Verdict()
		return Verdict(text.format(name=_name(event.role), message=event.message))

	def on_tick(self, now_s: float) -> Verdict:
		running = [role for role in ROLES if role not in self._done]
		if not running:
			return Verdict()
		if now_s > self._deadline_s:
			return Verdict(f"the job has run more than {self._limits.overtime_s:g} s over its {self._total_s:g} s")
		for role in running:
			silent_s = now_s - self._last_status_s[role]
			if silent_s > self._limits.silence_s:
				return Verdict(f"no STATUS from the {_name(role)} board for {silent_s:.1f} s")
		return Verdict()


def _name(role: Role) -> str:
	return role.name.lower()

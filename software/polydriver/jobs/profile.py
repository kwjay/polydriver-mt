"""Gradient profile (C7.6): the colour share over a run at constant total flow.

The colour channel gives phi(t) x the total flow and the polymer channel the rest, so the two
always add up to the total. phi holds at phi_start for hold_start_s, follows the curve from
phi_start to phi_end, and holds at phi_end for the last hold_end_s. duration_s is the whole run,
holds included, so total mass = total flow x duration.
"""

import math
from dataclasses import dataclass

from .curves import ProgressCurve

AGREE_REL = 1e-6


class ProfileError(Exception):
	pass


@dataclass(frozen=True)
class Amount:
	duration_s: float
	total_mass_g: float
	total_flow_g_s: float


def resolve_amount(*, duration_s: float | None = None, total_mass_g: float | None = None,
				total_flow_g_s: float | None = None) -> Amount:
	"""Any two give the third; three are accepted when they agree."""
	given = {"duration_s": duration_s, "total_mass_g": total_mass_g, "total_flow_g_s": total_flow_g_s}
	for name, value in given.items():
		if value is not None and not (_is_number(value) and value > 0):
			raise ProfileError(f"{name} = {value!r} must be a finite number > 0")
	if sum(value is not None for value in given.values()) < 2:
		raise ProfileError("give two of duration_s, total_mass_g and total_flow_g_s; the third is worked out")
	if total_mass_g is None:
		return Amount(duration_s, duration_s * total_flow_g_s, total_flow_g_s)
	if duration_s is None:
		return Amount(total_mass_g / total_flow_g_s, total_mass_g, total_flow_g_s)
	if total_flow_g_s is None:
		return Amount(duration_s, total_mass_g, total_mass_g / duration_s)
	product = duration_s * total_flow_g_s
	if not math.isclose(total_mass_g, product, rel_tol=AGREE_REL):
		raise ProfileError(
			f"the three amounts disagree: {total_mass_g:g} g is not {duration_s:g} s x {total_flow_g_s:g} g/s = "
			f"{product:g} g; give only two"
		)
	return Amount(duration_s, total_mass_g, total_flow_g_s)


@dataclass(frozen=True)
class Sample:
	t_s: float
	phi: float
	colour_g_s: float
	polymer_g_s: float


@dataclass(frozen=True)
class GradientProfile:
	phi_start: float
	phi_end: float
	curve: ProgressCurve
	amount: Amount
	hold_start_s: float = 0.0
	hold_end_s: float = 0.0

	def __post_init__(self) -> None:
		problems = []
		for name in ("phi_start", "phi_end"):
			value = getattr(self, name)
			if not (_is_number(value) and 0 <= value <= 1):
				problems.append(f"{name} = {value!r} must be a colour share 0 to 1")
		for name in ("hold_start_s", "hold_end_s"):
			value = getattr(self, name)
			if not (_is_number(value) and value >= 0):
				problems.append(f"{name} = {value!r} must be a finite number >= 0")
		if not isinstance(self.curve, ProgressCurve):
			problems.append(f"curve {self.curve!r} must be a gradient curve")
		if not isinstance(self.amount, Amount):
			problems.append(f"amount {self.amount!r} must be an Amount (see resolve_amount)")
		elif not problems and self.hold_start_s + self.hold_end_s >= self.amount.duration_s:
			problems.append(
				f"the holds ({self.hold_start_s:g} s + {self.hold_end_s:g} s) take the whole "
				f"{self.amount.duration_s:g} s run and leave no time for the gradient"
			)
		if problems:
			raise ProfileError("; ".join(problems))

	@property
	def duration_s(self) -> float:
		return self.amount.duration_s

	@property
	def gradient_s(self) -> float:
		return self.amount.duration_s - self.hold_start_s - self.hold_end_s

	def phi(self, t_s: float) -> float:
		if not 0 <= t_s <= self.duration_s:
			raise ProfileError(f"t = {t_s!r} s is outside the run, 0 to {self.duration_s:g} s")
		if t_s <= self.hold_start_s:
			return self.phi_start
		if t_s >= self.duration_s - self.hold_end_s:
			return self.phi_end
		x = min(1.0, max(0.0, (t_s - self.hold_start_s) / self.gradient_s))
		progress = self.curve.progress(x)
		return self.phi_start * (1 - progress) + self.phi_end * progress

	def colour_g_s(self, t_s: float) -> float:
		return self.phi(t_s) * self.amount.total_flow_g_s

	def polymer_g_s(self, t_s: float) -> float:
		return (1 - self.phi(t_s)) * self.amount.total_flow_g_s

	@property
	def colour_mass_g(self) -> float:
		mean = self.curve.mean()
		gradient_phi = self.phi_start * (1 - mean) + self.phi_end * mean
		share_s = self.phi_start * self.hold_start_s + gradient_phi * self.gradient_s + self.phi_end * self.hold_end_s
		return share_s * self.amount.total_flow_g_s

	@property
	def polymer_mass_g(self) -> float:
		return self.amount.total_flow_g_s * self.duration_s - self.colour_mass_g

	def samples(self, step_s: float) -> tuple[Sample, ...]:
		"""Every step_s from 0, and the run's end, for a preview."""
		if not (_is_number(step_s) and step_s > 0):
			raise ProfileError(f"the step {step_s!r} s must be a finite number > 0")
		times = [i * step_s for i in range(math.ceil(self.duration_s / step_s))] + [self.duration_s]
		return tuple(Sample(t, self.phi(t), self.colour_g_s(t), self.polymer_g_s(t)) for t in times)


def _is_number(value: object) -> bool:
	return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

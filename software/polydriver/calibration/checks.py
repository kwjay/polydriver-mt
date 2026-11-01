"""What to tell the person entering a calibration about it: the same warnings the shell's
`calibrate add` prints, as text, for the window to show."""

from collections.abc import Iterable

from .data import Curve
from .fit import SPREAD_LIMIT
from .lookup import NonMonotonicError, speed_for_rate


def curve_warnings(curve: Curve, material_names: Iterable[str] | None = None) -> list[str]:
	"""Repeats that disagree by more than SPREAD_LIMIT, a rate that falls as the speed rises, and a
	material not in the materials file (material_names; None = not checked)."""
	warnings = []
	for point in curve.points:
		if point.repeats > 1 and point.rate_g_s > 0 and point.spread_g_s > SPREAD_LIMIT * point.rate_g_s:
			share = point.spread_g_s / point.rate_g_s
			warnings.append(f"the {point.repeats} weighings at {point.speed_hz:g} Hz disagree by {share * 100:.0f} % "
							f"(more than {SPREAD_LIMIT * 100:.0f} %): weigh that speed again")
	if len(curve.points) >= 2:
		try:
			speed_for_rate(curve, curve.points[0].rate_g_s)
		except NonMonotonicError as error:
			warnings.append(str(error))
	if material_names is not None:
		names = sorted(material_names)
		if curve.material not in names:
			known = ", ".join(names) if names else "none yet"
			warnings.append(f"{curve.material} is not in the materials file (there: {known}); a job can only use a "
							"material that is")
	return warnings

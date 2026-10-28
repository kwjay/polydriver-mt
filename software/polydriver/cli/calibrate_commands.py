"""Calibrate commands (C9.3): weighed measurements per dispenser and material, the fit report, the file.

Every change is saved to the calibration file at once, so a weighing is never lost to a closed
shell. The file is the config's [paths] calibration until `calibrate load` or `calibrate save PATH`
picks another; `calibrate show` says which. No board is needed.
"""

from pathlib import Path

from ..calibration import (
	SPREAD_LIMIT, CalibrationError, CalibrationStore, Curve, NonMonotonicError, NotEnoughPointsError,
	OldCalibrationFileError, fit_report, format_fit_report, load_calibration, lowest_producing_speed,
	save_calibration, speed_for_rate,
)
from ..materials import MaterialError, load_materials
from .parsing import UsageError, parse_number, split_args

ADD_USAGE = "calibrate add DISPENSER MATERIAL HZ SECONDS GRAMS [NOTE]"
REMOVE_USAGE = "calibrate remove DISPENSER MATERIAL N"
SHOW_USAGE = "calibrate show [DISPENSER MATERIAL]"
SAVE_USAGE = "calibrate save [PATH]"
LOAD_USAGE = "calibrate load [PATH]"
IMPORT_USAGE = "calibrate import OLDFILE MATERIAL [TARGET=DISPENSER ...]"


class CalibrateCommands:
	calibration: CalibrationStore | None = None
	calibration_path: Path | None = None

	def do_calibrate(self, arg: str) -> None:
		"""calibrate [add | remove | show | save | load | import]  - weighed measurements per dispenser and material

		calibrate add DISPENSER MATERIAL HZ SECONDS GRAMS [NOTE]  - the screw held at HZ for SECONDS gave GRAMS
		calibrate remove DISPENSER MATERIAL N  - drop measurement N (`calibrate show` numbers them)
		calibrate show [DISPENSER MATERIAL]  - every curve, or one curve's measurements and fit report
		calibrate save [PATH]  - write the file again, or to PATH, where changes then go
		calibrate load [PATH]  - read the config's calibration file again, or PATH, where changes then go
		calibrate import OLDFILE MATERIAL [TARGET=DISPENSER ...]  - add an old one-board file's measurements
		Every add, remove and import is saved to the calibration file at once.
		"""
		words = split_args(arg)
		action, rest = (words[0], words[1:]) if words else ("show", [])
		actions = {
			"add": self._calibrate_add, "remove": self._calibrate_remove, "show": self._calibrate_show,
			"save": self._calibrate_save, "load": self._calibrate_load, "import": self._calibrate_import,
		}
		if action not in actions:
			raise UsageError(f"{action!r} is not a calibrate action")
		actions[action](rest)

	# --- Changes ------------------------------------------------------------------------------

	def _calibrate_add(self, words: list[str]) -> None:
		if len(words) < 5:
			raise UsageError("a dispenser, a material, the speed, the time and the grams", usage=ADD_USAGE)
		dispenser, material = words[0], words[1]
		try:
			speed_hz = parse_number(words[2], "HZ")
			seconds = parse_number(words[3], "SECONDS")
			grams = parse_number(words[4], "GRAMS")
		except UsageError as error:
			raise UsageError(str(error), usage=ADD_USAGE) from None
		store = self._calibration_store()
		curve = store.add_measurement(dispenser, material, speed_hz, grams, seconds, note=" ".join(words[5:]))
		name = _name(curve)
		self.say(f"  {name}: {speed_hz:g} Hz gave {grams / seconds:.4g} g/s ({grams:g} g in {seconds:g} s), "
				f"measurement #{len(curve.measurements)}")
		point = next(p for p in curve.points if p.speed_hz == speed_hz)
		if point.repeats > 1:
			share = point.spread_g_s / point.rate_g_s if point.rate_g_s else 0.0
			self.say(f"  {speed_hz:g} Hz now has {point.repeats} repeats: mean {point.rate_g_s:.4g} g/s, "
					f"spread {point.spread_g_s:.3g} g/s ({share * 100:.0f} %)")
			if point.spread_g_s > SPREAD_LIMIT * point.rate_g_s:
				self.say(f"  ! the repeats at {speed_hz:g} Hz disagree by {share * 100:.0f} % (more than "
						f"{SPREAD_LIMIT * 100:.0f} %): weigh that speed again")
		self._say_if_falling(curve)
		self._say_if_unknown_material(curve.material)
		self._save_calibration()

	def _calibrate_remove(self, words: list[str]) -> None:
		if len(words) != 3:
			raise UsageError("a dispenser, a material and the measurement's number", usage=REMOVE_USAGE)
		try:
			number = int(words[2])
		except ValueError:
			raise UsageError(f"N must be a whole number, not {words[2]!r}", usage=REMOVE_USAGE) from None
		store = self._calibration_store()
		curve = store.curve(words[0], words[1])
		name, count = _name(curve), len(curve.measurements)
		if not 1 <= number <= count:
			self.say(f"  there is no measurement #{number} in {name}; it has {count} "
					f"(`calibrate show {_quoted(curve.dispenser)} {_quoted(curve.material)}` numbers them)")
			return
		removed = curve.measurements[number - 1]
		left = store.remove_measurement(curve.dispenser, curve.material, number - 1)
		what = f"removed #{number} of {name} ({removed.speed_hz:g} Hz, {removed.grams:g} g in {removed.duration_s:g} s)"
		if left is None:
			self.say(f"  {what}; {name} has no measurements left, so the curve is gone")
		else:
			self.say(f"  {what}; {len(left.measurements)} left")
		self._save_calibration()

	def _calibrate_import(self, words: list[str]) -> None:
		if len(words) < 2:
			raise UsageError("an old calibration file and the material it was measured with", usage=IMPORT_USAGE)
		path, material = Path(words[0]), words[1]
		dispensers = {}
		for pair in words[2:]:
			target, _, dispenser = pair.partition("=")
			if not (target.isdigit() and dispenser):
				raise UsageError(f"TARGET=DISPENSER pairs, like 1=B, not {pair!r}", usage=IMPORT_USAGE)
			dispensers[int(target)] = dispenser
		if not path.exists():
			self.say(f"  no file at {path}")
			return
		try:
			load_calibration(path)
		except OldCalibrationFileError:
			pass
		else:
			self.say(f"  {path} is not an old calibration file; `calibrate load {_quoted(str(path))}` reads it")
			return
		old = load_calibration(path, old_material=material, old_dispensers=dispensers)
		store = self._calibration_store()
		known = {curve.key: set(curve.measurements) for curve in store.curves()}
		added: dict[str, int] = {}
		skipped = 0
		for curve in old.curves():
			for measurement in curve.measurements:
				if measurement in known.get(curve.key, set()):
					skipped += 1
					continue
				store._add(curve.dispenser, curve.material, measurement)
				added[_name(curve)] = added.get(_name(curve), 0) + 1
		total = sum(added.values())
		text = f"  imported {total} measurement{'' if total == 1 else 's'}"
		if added:
			text += ": " + ", ".join(f"{name} ({count})" for name, count in added.items())
		if skipped:
			text += f"; {skipped} {'was' if skipped == 1 else 'were'} already there"
		self.say(text)
		if added:
			self._say_if_unknown_material(material)
			self._save_calibration()

	# --- Looking ------------------------------------------------------------------------------

	def _calibrate_show(self, words: list[str]) -> None:
		if len(words) == 2:
			self._show_curve(self._calibration_store().curve(words[0], words[1]))
			return
		if words:
			raise UsageError("no words, or a dispenser and a material", usage=SHOW_USAGE)
		store = self._calibration_store()
		path = self.calibration_path
		self.say(f"  calibration file {path}" + ("" if path.exists() else " (not written yet)"))
		curves = store.curves()
		if not curves:
			self.say("  no calibrations yet (`calibrate add DISPENSER MATERIAL HZ SECONDS GRAMS`)")
		for curve in curves:
			points, count = curve.points, len(curve.measurements)
			speeds = f"{points[0].speed_hz:g} Hz" if len(points) == 1 else \
				f"{points[0].speed_hz:g} to {points[-1].speed_hz:g} Hz"
			self.say(f"  {_name(curve)}: {count} measurement{'' if count == 1 else 's'} at {len(points)} "
					f"speed{'' if len(points) == 1 else 's'}, {speeds}, curve {curve.short_hash}")

	def _show_curve(self, curve: Curve) -> None:
		count = len(curve.measurements)
		self.say(f"  {_name(curve)}: {count} measurement{'' if count == 1 else 's'}, curve {curve.short_hash}")
		self.say("    #   speed Hz     grams   seconds   rate g/s   taken                       note")
		for number, m in enumerate(curve.measurements, start=1):
			self.say(f"  {number:3d}   {m.speed_hz:8g}   {m.grams:7g}   {m.duration_s:7g}   {m.rate_g_s:8.4g}   "
					f"{m.taken_at or '-':25}   {m.note}".rstrip())
		lowest = lowest_producing_speed(curve)
		self.say("  no speed gave material yet" if lowest is None else f"  lowest speed that gave material: {lowest:g} Hz")
		try:
			report = fit_report(curve)
		except NotEnoughPointsError as error:
			self.say(f"  no fit yet: {error}")
			for p in curve.points:
				self.say(f"  {p.speed_hz:g} Hz: mean {p.rate_g_s:.4g} g/s, {p.repeats} repeat{'' if p.repeats == 1 else 's'}, "
						f"spread {p.spread_g_s:.3g} g/s")
		else:
			for line in format_fit_report(report):
				self.say(f"  {line}")
		self._say_if_falling(curve)

	# --- The file -------------------------------------------------------------------------------

	def _calibrate_save(self, words: list[str]) -> None:
		if len(words) > 1:
			raise UsageError("at most one path", usage=SAVE_USAGE)
		store = self._calibration_store()
		path = Path(words[0]) if words else self.calibration_path
		save_calibration(store, path)
		count = len(store.curves())
		moved = path != self.calibration_path
		self.calibration_path = path
		self.say(f"  saved {count} curve{'' if count == 1 else 's'} to {path}" + ("; changes now go there" if moved else ""))

	def _calibrate_load(self, words: list[str]) -> None:
		if len(words) > 1:
			raise UsageError("at most one path", usage=LOAD_USAGE)
		path = Path(words[0]) if words else self.config.resolve(self.config.paths.calibration)
		still = f"; still using {self.calibration_path}" if self.calibration_path is not None else ""
		if not path.exists():
			self.say(f"  no calibration file at {path}{still}")
			return
		try:
			store = load_calibration(path)
		except OldCalibrationFileError:
			target = self.calibration_path or self.config.resolve(self.config.paths.calibration)
			self.say(f"  {path}: an old calibration file (one board, no materials): `calibrate import "
					f"{_quoted(str(path))} MATERIAL` adds its measurements to {target}")
			return
		except CalibrationError as error:
			self.say(f"  {error}{still}")
			return
		moved = path != self.calibration_path
		self.calibration, self.calibration_path = store, path
		count = len(store.curves())
		self.say(f"  loaded {count} curve{'' if count == 1 else 's'} from {path}" + ("; changes now go there" if moved else ""))

	def _calibration_store(self) -> CalibrationStore:
		"""Read from the config's file on first use; a broken file is reported and nothing is written."""
		if self.calibration is None:
			path = self.config.resolve(self.config.paths.calibration)
			self.calibration = load_calibration(path)
			self.calibration_path = path
		return self.calibration

	def _save_calibration(self) -> None:
		try:
			save_calibration(self.calibration, self.calibration_path)
		except OSError as error:
			self.say(f"  ! not saved: {error}; the change is kept here, and the next change or `calibrate save` "
					"tries again")
			return
		self.say(f"  saved to {self.calibration_path}")

	# --- Warnings -------------------------------------------------------------------------------

	def _say_if_falling(self, curve: Curve) -> None:
		if len(curve.points) < 2:
			return
		try:
			speed_for_rate(curve, curve.points[0].rate_g_s)
		except NonMonotonicError as error:
			self.say(f"  ! {error}")

	def _say_if_unknown_material(self, material: str) -> None:
		path = self.config.resolve(self.config.paths.materials)
		try:
			names = sorted(m.name for m in load_materials(path).materials())
		except MaterialError:
			return
		if names and material not in names:
			self.say(f"  ! {material} is not in {path.name} ({', '.join(names)}); a job can only use a material that is")


def _name(curve: Curve) -> str:
	return f"{curve.dispenser} / {curve.material}"


def _quoted(text: str) -> str:
	return f'"{text}"' if " " in text else text

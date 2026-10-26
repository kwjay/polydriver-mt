import json
from datetime import datetime, timedelta, timezone

import pytest

from polydriver.calibration import (
	SCHEMA, CalibrationError, CalibrationFileError, CalibrationStore, Curve, Measurement, UnknownCurveError,
	load_calibration, save_calibration,
)

T0 = datetime(2026, 10, 26, 14, 0, tzinfo=timezone(timedelta(hours=1)))


class Clock:
	def __init__(self):
		self.now = T0

	def __call__(self) -> datetime:
		self.now += timedelta(minutes=1)
		return self.now


@pytest.fixture
def store():
	return CalibrationStore(clock=Clock())


def measured(store: CalibrationStore, *rows, dispenser="B", material="PLA"):
	for speed_hz, grams, seconds in rows:
		store.add_measurement(dispenser, material, speed_hz, grams, seconds)
	return store.curve(dispenser, material)


# --- Measurements ------------------------------------------------------------------------

def test_a_measurement_keeps_everything_it_was_taken_with(store):
	store.add_measurement("B", "PLA", 20.0, 3.0, 30.0, method="steady-state", note="hopper full")
	[m] = store.curve("B", "PLA").measurements
	assert (m.speed_hz, m.grams, m.duration_s) == (20.0, 3.0, 30.0)
	assert m.rate_g_s == pytest.approx(0.1)
	assert m.method == "steady-state"
	assert m.note == "hopper full"
	assert m.taken_at == "2026-10-26T14:01:00+01:00"


def test_the_default_method_is_steady_state(store):
	store.add_measurement("B", "PLA", 20.0, 3.0, 30.0)
	assert store.curve("B", "PLA").measurements[0].method == "steady-state"


@pytest.mark.parametrize("speed_hz, grams, seconds, message", [
	(20.0, 3.0, 0.0, "duration"),
	(20.0, 3.0, -1.0, "duration"),
	(20.0, -0.1, 30.0, "grams"),
	(-1.0, 3.0, 30.0, "speed"),
	(float("nan"), 3.0, 30.0, "speed"),
	(20.0, float("inf"), 30.0, "grams"),
	(float("inf"), 3.0, 30.0, "speed"),
	(True, 3.0, 30.0, "speed"),
])
def test_impossible_measurements_are_refused(store, speed_hz, grams, seconds, message):
	with pytest.raises(CalibrationError, match=message):
		store.add_measurement("B", "PLA", speed_hz, grams, seconds)
	assert store.curves() == []


@pytest.mark.parametrize("dispenser, material", [("", "PLA"), ("B", "  "), ("B", "PL\nA"), ("A\tB", "PLA")])
def test_names_must_be_plain_text(store, dispenser, material):
	with pytest.raises(CalibrationError, match="name"):
		store.add_measurement(dispenser, material, 20.0, 3.0, 30.0)


def test_names_are_trimmed(store):
	store.add_measurement(" B ", " PLA ", 20.0, 3.0, 30.0)
	assert store.curve("B", "PLA").key == ("B", "PLA")


def test_a_name_that_differs_only_in_case_is_refused(store):
	store.add_measurement("B", "PLA", 20.0, 3.0, 30.0)
	with pytest.raises(CalibrationError, match="'pla'.*'PLA'"):
		store.add_measurement("B", "pla", 20.0, 3.0, 30.0)
	with pytest.raises(CalibrationError, match="'b'.*'B'"):
		store.add_measurement("b", "TPU", 20.0, 3.0, 30.0)
	with pytest.raises(CalibrationError, match="'pla'.*'PLA'"):
		store.add_measurement("A", "pla", 20.0, 3.0, 30.0)


# --- Curves keyed by (dispenser, material) --------------------------------------------------

def test_curves_are_kept_apart_by_dispenser_and_material(store):
	store.add_measurement("B", "PLA", 20.0, 3.0, 30.0)
	store.add_measurement("B", "TPU", 20.0, 2.0, 30.0)
	store.add_measurement("A", "PLA", 20.0, 1.0, 30.0)
	assert [c.key for c in store.curves()] == [("A", "PLA"), ("B", "PLA"), ("B", "TPU")]
	assert store.curve("B", "TPU").measurements[0].grams == 2.0


def test_an_unknown_curve_names_the_ones_there_are(store):
	store.add_measurement("B", "PLA", 20.0, 3.0, 30.0)
	with pytest.raises(UnknownCurveError, match="no calibration for dispenser A, material PLA.*B / PLA"):
		store.curve("A", "PLA")


def test_an_empty_store_says_so(store):
	with pytest.raises(UnknownCurveError, match="no calibrations yet"):
		store.curve("A", "PLA")


def test_points_average_repeats_at_one_speed(store):
	curve = measured(store, (20.0, 3.0, 30.0), (20.0, 3.6, 30.0), (10.0, 1.2, 30.0))
	assert [(p.speed_hz, p.repeats) for p in curve.points] == [(10.0, 1), (20.0, 2)]
	point = curve.points[1]
	assert point.rate_g_s == pytest.approx(0.11)
	assert point.spread_g_s == pytest.approx(0.02)
	assert curve.points[0].spread_g_s == 0.0


def test_a_measurement_can_be_dropped(store):
	measured(store, (20.0, 3.0, 30.0), (20.0, 9.9, 30.0))
	store.remove_measurement("B", "PLA", 1)
	assert [m.grams for m in store.curve("B", "PLA").measurements] == [3.0]


def test_dropping_the_last_measurement_drops_the_curve(store):
	measured(store, (20.0, 3.0, 30.0))
	store.remove_measurement("B", "PLA", 0)
	assert store.curves() == []


def test_dropping_a_measurement_that_is_not_there_is_named(store):
	measured(store, (20.0, 3.0, 30.0))
	with pytest.raises(CalibrationError, match="measurement 3.*1 measurement"):
		store.remove_measurement("B", "PLA", 3)
	with pytest.raises(CalibrationError, match="measurement -1"):
		store.remove_measurement("B", "PLA", -1)
	assert len(store.curve("B", "PLA").measurements) == 1


def test_a_curve_is_a_snapshot(store):
	curve = measured(store, (20.0, 3.0, 30.0))
	store.add_measurement("B", "PLA", 30.0, 4.0, 30.0)
	assert len(curve.measurements) == 1


# --- Hash -------------------------------------------------------------------------------------

def test_the_hash_is_stable_and_hex(store):
	curve = measured(store, (20.0, 3.0, 30.0))
	assert curve.hash == curve.hash
	assert len(curve.hash) == 64 and int(curve.hash, 16) >= 0
	assert curve.short_hash == curve.hash[:12]


def test_the_hash_changes_with_the_raw_data(store):
	before = measured(store, (20.0, 3.0, 30.0)).hash
	after = measured(store, (20.0, 3.0, 30.0)).hash
	assert before != after


def test_the_hash_ignores_notes_and_order():
	a = Measurement(20.0, 3.0, 30.0, "2026-10-26T14:01:00+01:00", "steady-state", "first")
	b = Measurement(30.0, 4.5, 30.0, "2026-10-26T14:02:00+01:00", "steady-state", "")
	b_noted = Measurement(30.0, 4.5, 30.0, "2026-10-26T14:02:00+01:00", "steady-state", "typo fixed")
	assert Curve("B", "PLA", (a, b)).hash == Curve("B", "PLA", (b_noted, a)).hash


@pytest.mark.parametrize("change", [
	{"material": "TPU"}, {"dispenser": "A"}, {"speed_hz": 21.0}, {"grams": 3.01}, {"duration_s": 31.0},
	{"taken_at": "2026-10-26T15:01:00+01:00"}, {"method": "start-stop"},
])
def test_every_raw_value_is_in_the_hash(change):
	base = dict(speed_hz=20.0, grams=3.0, duration_s=30.0, taken_at="2026-10-26T14:01:00+01:00", method="steady-state")
	key = dict(dispenser="B", material="PLA")
	measurement_changes = {k: v for k, v in change.items() if k in base}
	key_changes = {k: v for k, v in change.items() if k in key}
	original = Curve(key["dispenser"], key["material"], (Measurement(**base),))
	changed = Curve(**(key | key_changes), measurements=(Measurement(**(base | measurement_changes)),))
	assert original.hash != changed.hash


def test_the_hash_survives_a_save_and_load(store, tmp_path):
	curve = measured(store, (20.0, 3.0, 30.0), (30.0, 4.5, 30.0))
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	assert load_calibration(path).curve("B", "PLA").hash == curve.hash


# --- Saving and loading ---------------------------------------------------------------------

def test_save_and_load_round_trip(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0), (20.0, 3.3, 30.0))
	store.add_measurement("A", "MB-red", 4.0, 0.15, 60.0, method="steady-state", note="low end")
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	loaded = load_calibration(path)
	assert loaded.curves() == store.curves()


def test_the_file_is_versioned_and_readable(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0))
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	data = json.loads(path.read_text(encoding="utf-8"))
	assert data["schema"] == SCHEMA
	[curve] = data["curves"]
	assert curve["dispenser"] == "B" and curve["material"] == "PLA"
	assert curve["hash"] == store.curve("B", "PLA").hash
	assert curve["measurements"][0]["speed_hz"] == 20.0


def test_save_makes_the_folder_and_leaves_no_temporary_file(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0))
	path = tmp_path / "cal" / "calibration.json"
	save_calibration(store, path)
	assert sorted(p.name for p in path.parent.iterdir()) == ["calibration.json"]


def test_a_missing_file_is_an_empty_store(tmp_path):
	assert load_calibration(tmp_path / "calibration.json").curves() == []


@pytest.mark.parametrize("text, message", [
	("{not json", "not valid JSON"),
	('{"schema": 99, "curves": []}', "schema 99"),
	('{"schema": 2}', "curves"),
	('{"schema": 2, "curves": [{"dispenser": "B", "material": "PLA", "measurements": [{"speed_hz": 20}]}]}',
	"curve 0.*measurement 0.*grams"),
	('{"schema": 2, "curves": [{"dispenser": "B", "material": "PLA", "measurements": [{"speed_hz": 20, "grams": -1,'
	' "duration_s": 30, "taken_at": null, "method": "steady-state", "note": ""}]}]}', "curve 0.*grams"),
	('[]', "object"),
])
def test_a_broken_file_names_the_problem_and_the_file(tmp_path, text, message):
	path = tmp_path / "calibration.json"
	path.write_text(text, encoding="utf-8")
	with pytest.raises(CalibrationFileError, match=message) as raised:
		load_calibration(path)
	assert str(path) in str(raised.value)


def test_a_hash_that_does_not_match_the_data_is_reported(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0))
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	data = json.loads(path.read_text(encoding="utf-8"))
	data["curves"][0]["measurements"][0]["grams"] = 3.5
	path.write_text(json.dumps(data), encoding="utf-8")
	with pytest.raises(CalibrationFileError, match="changed by hand"):
		load_calibration(path)


def test_a_curve_without_a_saved_hash_takes_the_data_as_it_is(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0))
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	data = json.loads(path.read_text(encoding="utf-8"))
	data["curves"][0]["measurements"][0]["grams"] = 3.5
	del data["curves"][0]["hash"]
	path.write_text(json.dumps(data), encoding="utf-8")
	assert load_calibration(path).curve("B", "PLA").measurements[0].grams == 3.5


def test_a_curve_with_no_measurements_is_reported(tmp_path):
	path = tmp_path / "calibration.json"
	path.write_text(json.dumps({"schema": 2, "curves": [
		{"dispenser": "B", "material": "PLA", "hash": "0" * 64, "measurements": []},
	]}), encoding="utf-8")
	with pytest.raises(CalibrationFileError, match="curve 0 .B / PLA.: no measurements"):
		load_calibration(path)


def test_a_curve_listed_twice_is_reported_as_such_not_as_changed_by_hand(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0))
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	data = json.loads(path.read_text(encoding="utf-8"))
	data["curves"].append(dict(data["curves"][0], dispenser=" B "))
	path.write_text(json.dumps(data), encoding="utf-8")
	with pytest.raises(CalibrationFileError, match="curve 1: B / PLA is already curve 0"):
		load_calibration(path)


def test_a_bool_is_not_a_number_in_the_file(store, tmp_path):
	measured(store, (20.0, 3.0, 30.0))
	path = tmp_path / "calibration.json"
	save_calibration(store, path)
	data = json.loads(path.read_text(encoding="utf-8"))
	data["curves"][0]["measurements"][0]["duration_s"] = True
	del data["curves"][0]["hash"]
	path.write_text(json.dumps(data), encoding="utf-8")
	with pytest.raises(CalibrationFileError, match="duration"):
		load_calibration(path)


# --- Old files (one board, no materials) ---------------------------------------------------------

OLD_FILE = {"targets": {"1": {"measurements": [
	{"speed": 20.0, "grams": 3.0, "duration_s": 30.0},
	{"speed": 30.0, "grams": 4.6, "duration_s": 30.0},
]}}}


def test_an_old_file_needs_its_material_named(tmp_path):
	path = tmp_path / "calibration.json"
	path.write_text(json.dumps(OLD_FILE), encoding="utf-8")
	with pytest.raises(CalibrationFileError, match="old calibration file.*material"):
		load_calibration(path)


def test_an_old_file_loads_once_its_material_is_named(tmp_path):
	path = tmp_path / "calibration.json"
	path.write_text(json.dumps(OLD_FILE), encoding="utf-8")
	store = load_calibration(path, old_material="PLA", old_dispensers={1: "B"})
	curve = store.curve("B", "PLA")
	assert [(m.speed_hz, m.grams) for m in curve.measurements] == [(20.0, 3.0), (30.0, 4.6)]
	assert all(m.taken_at is None and m.method == "unknown" for m in curve.measurements)
	assert "old file" in curve.measurements[0].note


def test_an_old_target_without_a_dispenser_name_keeps_its_number(tmp_path):
	path = tmp_path / "calibration.json"
	path.write_text(json.dumps(OLD_FILE), encoding="utf-8")
	assert load_calibration(path, old_material="PLA").curve("target 1", "PLA").measurements


def test_an_imported_old_file_saves_in_the_new_format(tmp_path):
	path = tmp_path / "calibration.json"
	path.write_text(json.dumps(OLD_FILE), encoding="utf-8")
	store = load_calibration(path, old_material="PLA", old_dispensers={1: "B"})
	new_path = tmp_path / "new.json"
	save_calibration(store, new_path)
	assert load_calibration(new_path).curves() == store.curves()


@pytest.mark.parametrize("data, message", [
	({"targets": [1, 2]}, "'targets' must be an object"),
	({"targets": {"left": {"measurements": []}}}, "target 'left' must be a number"),
	({"targets": {"1": [1]}}, "target 1: must be an object"),
	({"targets": {"1": {"measurements": 5}}}, "target 1: 'measurements' must be a list"),
	({"targets": {"1": {"measurements": [5]}}}, "target 1, measurement 0: missing or wrong field"),
])
def test_a_broken_old_file_names_the_problem_and_the_file(tmp_path, data, message):
	path = tmp_path / "calibration.json"
	path.write_text(json.dumps(data), encoding="utf-8")
	with pytest.raises(CalibrationFileError, match=message) as raised:
		load_calibration(path, old_material="PLA")
	assert str(path) in str(raised.value)

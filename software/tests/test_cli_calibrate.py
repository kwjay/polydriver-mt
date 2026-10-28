"""C9.3: calibrate commands. A measurement is saved to the calibration file as it is added."""

import json

import pytest

from polydriver.calibration import CalibrationStore, load_calibration, save_calibration
from polydriver.protocol.constants import Role

from cli_helpers import CONFIG_TEXT, make_shell, run
from fakes import Boards


@pytest.fixture
def config_path(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text(CONFIG_TEXT, encoding="utf-8")
	return path


@pytest.fixture
def shell(config_path):
	shell = make_shell(Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR}), config_path)
	yield shell
	shell.session.close()


@pytest.fixture
def cal_file(tmp_path):
	return tmp_path / "calibration.json"


def write_materials(tmp_path, *names):
	materials = [{"name": name, "kind": "polymer", "dispenser": "B"} for name in names]
	(tmp_path / "materials.json").write_text(json.dumps({"schema": 1, "materials": materials}), encoding="utf-8")


def saved(cal_file):
	return load_calibration(cal_file)


# --- add ---------------------------------------------------------------------------------------

def test_add_records_a_measurement_and_saves_it_at_once(shell, cal_file):
	text = run(shell, "calibrate add B PP 20 10 5")
	assert "B / PP: 20 Hz gave 0.5 g/s (5 g in 10 s), measurement #1" in text
	assert f"saved to {cal_file}" in text
	(m,) = saved(cal_file).curve("B", "PP").measurements
	assert (m.speed_hz, m.duration_s, m.grams, m.method) == (20.0, 10.0, 5.0, "steady-state")
	assert m.taken_at is not None


def test_add_keeps_a_note(shell, cal_file):
	run(shell, 'calibrate add B PP 20 10 5 "hopper half full" after cleaning')
	assert saved(cal_file).curve("B", "PP").measurements[0].note == "hopper half full after cleaning"


@pytest.mark.parametrize("line, message", [
	("calibrate add B PP 20 10", "DISPENSER MATERIAL HZ SECONDS GRAMS"),
	("calibrate add B PP fast 10 5", "HZ must be a number, not 'fast'"),
	("calibrate add B PP 20 ten 5", "SECONDS must be a number, not 'ten'"),
	("calibrate add B PP 20 10 nan", "GRAMS must be a finite number"),
])
def test_add_with_wrong_words_prints_the_usage_and_records_nothing(shell, cal_file, line, message):
	text = run(shell, line)
	assert "usage: calibrate add DISPENSER MATERIAL HZ SECONDS GRAMS [NOTE]" in text and message in text
	assert not cal_file.exists()


@pytest.mark.parametrize("line, message", [
	("calibrate add B PP -1 10 5", "speed -1.0 Hz must be a number >= 0"),
	("calibrate add B PP 20 0 5", "duration 0.0 s must be a number > 0"),
	("calibrate add B PP 20 10 -2", "grams -2.0 must be a number >= 0"),
])
def test_add_with_an_impossible_value_says_why(shell, cal_file, line, message):
	assert message in run(shell, line)
	assert not cal_file.exists()


def test_a_repeat_reports_the_mean_and_spread(shell):
	run(shell, "calibrate add B PP 20 10 20")
	text = run(shell, "calibrate add B PP 20 10 21")
	assert "measurement #2" in text
	assert "20 Hz now has 2 repeats: mean 2.05 g/s, spread 0.1 g/s (5 %)" in text
	assert "disagree" not in text


def test_a_single_measurement_mentions_no_mean(shell):
	assert "repeats" not in run(shell, "calibrate add B PP 20 10 5")


@pytest.mark.parametrize("second, share", [("11", "20"), ("10.7", "14")])
def test_repeats_that_disagree_by_more_than_10_percent_are_flagged(shell, second, share):
	run(shell, "calibrate add B PP 20 10 9.3" if share == "14" else "calibrate add B PP 20 10 9")
	text = run(shell, f"calibrate add B PP 20 10 {second}")
	assert f"! the repeats at 20 Hz disagree by {share} % (more than 10 %): weigh that speed again" in text


def test_a_rate_that_falls_as_the_speed_rises_is_flagged(shell):
	run(shell, "calibrate add B PP 10 10 5", "calibrate add B PP 20 10 15")
	text = run(shell, "calibrate add B PP 30 10 12")
	assert "! B / PP: the rate falls from 1.5 g/s at 20 Hz to 1.2 g/s at 30 Hz" in text


def test_a_material_missing_from_the_materials_file_is_flagged_but_recorded(shell, tmp_path, cal_file):
	write_materials(tmp_path, "PLA", "PP")
	text = run(shell, "calibrate add B PLa 20 10 5")
	assert "! PLa is not in materials.json (PLA, PP); a job can only use a material that is" in text
	assert saved(cal_file).curve("B", "PLa")


def test_a_known_material_is_not_flagged(shell, tmp_path):
	write_materials(tmp_path, "PLA", "PP")
	assert "!" not in run(shell, "calibrate add B PP 20 10 5")


def test_without_a_materials_file_nothing_is_flagged(shell):
	assert "!" not in run(shell, "calibrate add B PP 20 10 5")


def test_a_broken_materials_file_does_not_stop_a_measurement(shell, tmp_path, cal_file):
	(tmp_path / "materials.json").write_text("{", encoding="utf-8")
	text = run(shell, "calibrate add B PP 20 10 5")
	assert "measurement #1" in text and "!" not in text
	assert saved(cal_file).curve("B", "PP")


def test_a_name_differing_only_in_case_is_refused(shell, cal_file):
	run(shell, "calibrate add B PP 20 10 5")
	text = run(shell, "calibrate add B pp 30 10 7")
	assert "material 'pp' differs from 'PP' only in case" in text and "internal error" not in text
	assert len(saved(cal_file).curve("B", "PP").measurements) == 1


def test_a_save_that_fails_keeps_the_measurement_and_the_next_change_saves_it(shell, cal_file, monkeypatch):
	import polydriver.cli.calibrate_commands as commands

	def full_disk(store, path):
		raise OSError("No space left on device")

	monkeypatch.setattr(commands, "save_calibration", full_disk)
	text = run(shell, "calibrate add B PP 20 10 5")
	assert "! not saved: No space left on device; the change is kept here" in text
	assert "B / PP: 1 measurement" in run(shell, "calibrate show")
	monkeypatch.undo()
	run(shell, "calibrate add B PP 30 10 7")
	assert len(saved(cal_file).curve("B", "PP").measurements) == 2


# --- show --------------------------------------------------------------------------------------

def test_show_alone_lists_every_curve_and_the_file(shell, cal_file):
	run(shell, "calibrate add B PP 10 10 4", "calibrate add B PP 20 10 11", "calibrate add B PP 20 10 11",
		"calibrate add A MB-red 5 100 3")
	text = run(shell, "calibrate show")
	assert f"calibration file {cal_file}" in text
	curve = saved(cal_file).curve("B", "PP")
	assert f"B / PP: 3 measurements at 2 speeds, 10 to 20 Hz, curve {curve.short_hash}" in text
	assert "A / MB-red: 1 measurement at 1 speed, 5 Hz" in text
	assert text.index("A / MB-red") < text.index("B / PP")


def test_calibrate_alone_is_show(shell):
	run(shell, "calibrate add B PP 10 10 4")
	assert run(shell, "calibrate") == run(shell, "calibrate show")


def test_show_with_no_calibrations_says_how_to_add_one(shell, cal_file):
	text = run(shell, "calibrate show")
	assert f"calibration file {cal_file} (not written yet)" in text
	assert "no calibrations yet (`calibrate add DISPENSER MATERIAL HZ SECONDS GRAMS`)" in text


def test_show_once_written_does_not_say_not_written(shell):
	run(shell, "calibrate add B PP 10 10 4")
	assert "not written yet" not in run(shell, "calibrate show")


def test_show_one_curve_lists_its_measurements_and_the_fit(shell):
	run(shell, "calibrate add B PP 10 10 4 first", "calibrate add B PP 20 10 11", "calibrate add B PP 20 10 11",
		"calibrate add B PP 30 10 14.5")
	text = run(shell, "calibrate show B PP")
	assert "B / PP: 4 measurements" in text
	lines = text.splitlines()
	first = next(line for line in lines if line.strip().startswith("1 "))
	assert "10" in first and "0.4" in first and "first" in first
	assert "lowest speed that gave material: 10 Hz" in text
	assert "rate = " in text and "R² = " in text


def test_show_one_curve_with_one_speed_explains_there_is_no_fit_yet(shell):
	run(shell, "calibrate add B PP 20 10 9", "calibrate add B PP 20 10 11")
	text = run(shell, "calibrate show B PP")
	assert "no fit yet: B / PP: a line needs at least 2 different speeds" in text
	assert "20 Hz: mean 1 g/s, 2 repeats, spread 0.2 g/s" in text


def test_show_a_curve_that_gave_no_material_says_so(shell):
	run(shell, "calibrate add B PP 5 10 0", "calibrate add B PP 8 10 0")
	text = run(shell, "calibrate show B PP")
	assert "no speed gave material yet" in text
	assert "no fit yet: B / PP: no speed gave any material" in text


def test_show_flags_a_falling_curve(shell):
	run(shell, "calibrate add B PP 10 10 5", "calibrate add B PP 20 10 15", "calibrate add B PP 30 10 12")
	assert "! B / PP: the rate falls from 1.5 g/s at 20 Hz" in run(shell, "calibrate show B PP")


def test_show_an_unknown_curve_names_the_known_ones(shell):
	run(shell, "calibrate add B PP 10 10 5")
	assert "no calibration for dispenser A, material PP; there are: B / PP" in run(shell, "calibrate show A PP")


@pytest.mark.parametrize("words", ["B", "B PP x"])
def test_show_takes_no_or_two_words(shell, words):
	run(shell, "calibrate add B PP 10 10 4")
	assert "usage: calibrate show [DISPENSER MATERIAL]" in run(shell, f"calibrate show {words}")


# --- remove ------------------------------------------------------------------------------------

def test_remove_drops_one_measurement_by_its_number_and_saves(shell, cal_file):
	run(shell, "calibrate add B PP 10 10 4", "calibrate add B PP 20 10 99", "calibrate add B PP 30 10 14")
	text = run(shell, "calibrate remove B PP 2")
	assert "removed #2 of B / PP (20 Hz, 99 g in 10 s); 2 left" in text and f"saved to {cal_file}" in text
	assert [m.speed_hz for m in saved(cal_file).curve("B", "PP").measurements] == [10.0, 30.0]


def test_removing_the_last_measurement_removes_the_curve(shell, cal_file):
	run(shell, "calibrate add B PP 10 10 4")
	assert "B / PP has no measurements left, so the curve is gone" in run(shell, "calibrate remove B PP 1")
	assert saved(cal_file).curves() == []


@pytest.mark.parametrize("number, message", [
	("0", "there is no measurement #0 in B / PP; it has 1 (`calibrate show B PP` numbers them)"),
	("2", "there is no measurement #2 in B / PP; it has 1"),
	("two", "N must be a whole number, not 'two'"),
])
def test_remove_a_measurement_that_is_not_there(shell, number, message):
	run(shell, "calibrate add B PP 10 10 4")
	assert message in run(shell, f"calibrate remove B PP {number}")


def test_a_hint_quotes_a_name_with_a_space(shell):
	run(shell, 'calibrate add B "PP black" 10 10 4')
	assert '(`calibrate show B "PP black"` numbers them)' in run(shell, 'calibrate remove B "PP black" 5')


def test_remove_takes_exactly_three_words(shell, cal_file):
	run(shell, "calibrate add B PP 10 10 4", "calibrate add B PP 20 10 9")
	assert "usage: calibrate remove DISPENSER MATERIAL N" in run(shell, "calibrate remove B PP 1 2")
	assert len(saved(cal_file).curve("B", "PP").measurements) == 2


# --- save and load -----------------------------------------------------------------------------

def test_save_writes_the_file_again(shell, cal_file):
	run(shell, "calibrate add B PP 10 10 4")
	cal_file.unlink()
	text = run(shell, "calibrate save")
	assert f"saved 1 curve to {cal_file}" in text and "changes now go there" not in text
	assert saved(cal_file).curve("B", "PP")


def test_save_to_another_file_makes_it_the_one_used(shell, tmp_path, cal_file):
	run(shell, "calibrate add B PP 10 10 4")
	other = tmp_path / "bench" / "cal-28-10.json"
	text = run(shell, f'calibrate save "{other}"')
	assert f"saved 1 curve to {other}; changes now go there" in text
	run(shell, "calibrate add B PP 20 10 11")
	assert len(saved(other).curve("B", "PP").measurements) == 2
	assert len(saved(cal_file).curve("B", "PP").measurements) == 1


def test_load_reads_another_file_and_changes_go_there(shell, tmp_path):
	other = tmp_path / "other.json"
	store = CalibrationStore()
	store.add_measurement("A", "MB-red", 5.0, 3.0, 100.0)
	save_calibration(store, other)
	text = run(shell, f"calibrate load {other}")
	assert f"loaded 1 curve from {other}; changes now go there" in text
	run(shell, "calibrate add A MB-red 8 100 5")
	assert len(saved(other).curve("A", "MB-red").measurements) == 2


def test_load_alone_reads_the_config_file_again(shell, cal_file):
	run(shell, "calibrate add B PP 10 10 4")
	store = load_calibration(cal_file)
	store.add_measurement("B", "PP", 20.0, 11.0, 10.0)
	save_calibration(store, cal_file)
	text = run(shell, "calibrate load")
	assert f"loaded 1 curve from {cal_file}" in text and "changes now go there" not in text
	assert "B / PP: 2 measurements" in run(shell, "calibrate show")


def test_load_alone_goes_back_to_the_config_file_after_a_save_elsewhere(shell, tmp_path, cal_file):
	run(shell, "calibrate add B PP 10 10 4", f"calibrate save {tmp_path / 'other.json'}")
	assert f"loaded 1 curve from {cal_file}; changes now go there" in run(shell, "calibrate load")
	run(shell, "calibrate add B PP 20 10 9")
	assert len(saved(cal_file).curve("B", "PP").measurements) == 2


@pytest.mark.parametrize("action", ["save", "load"])
def test_save_and_load_take_at_most_one_path(shell, action):
	assert f"usage: calibrate {action} [PATH]" in run(shell, f"calibrate {action} a.json b.json")


def test_load_a_missing_file_changes_nothing(shell, tmp_path, cal_file):
	run(shell, "calibrate add B PP 10 10 4")
	text = run(shell, f"calibrate load {tmp_path / 'nothing.json'}")
	assert "no calibration file at" in text
	assert f"calibration file {cal_file}" in run(shell, "calibrate show")


def test_load_a_broken_file_says_why_and_keeps_the_current_one(shell, tmp_path, cal_file):
	run(shell, "calibrate add B PP 10 10 4")
	broken = tmp_path / "broken.json"
	broken.write_text("{", encoding="utf-8")
	assert "not valid JSON" in run(shell, f"calibrate load {broken}")
	assert "B / PP: 1 measurement" in run(shell, "calibrate show")


def test_a_broken_config_file_is_reported_on_first_use_and_nothing_is_overwritten(shell, cal_file):
	cal_file.write_text("{", encoding="utf-8")
	assert "not valid JSON" in run(shell, "calibrate add B PP 10 10 4")
	assert cal_file.read_text(encoding="utf-8") == "{"


def test_an_old_file_cannot_be_loaded_only_imported(shell, tmp_path):
	old = tmp_path / "old.json"
	old.write_text(json.dumps({"targets": {"1": {"measurements": [{"speed": 20.0, "grams": 5.0, "duration_s": 10.0}]}}}),
				encoding="utf-8")
	assert "an old calibration file (one board, no materials): `calibrate import" in run(shell, f"calibrate load {old}")


# --- import ------------------------------------------------------------------------------------

def old_file(tmp_path):
	path = tmp_path / "old.json"
	rows = [{"speed": 20.0, "grams": 5.0, "duration_s": 10.0}, {"speed": 30.0, "grams": 8.0, "duration_s": 10.0}]
	path.write_text(json.dumps({"targets": {"1": {"measurements": rows}, "2": {"measurements": rows[:1]}}}),
					encoding="utf-8")
	return path


def test_import_adds_an_old_files_measurements_under_a_material_and_saves(shell, tmp_path, cal_file):
	text = run(shell, f"calibrate import {old_file(tmp_path)} PLA")
	assert "imported 3 measurements: target 1 / PLA (2), target 2 / PLA (1)" in text and f"saved to {cal_file}" in text
	curve = saved(cal_file).curve("target 1", "PLA")
	assert [m.method for m in curve.measurements] == ["unknown", "unknown"]


def test_import_can_name_the_old_targets_after_their_dispensers(shell, tmp_path, cal_file):
	text = run(shell, f"calibrate import {old_file(tmp_path)} PLA 1=B 2=A")
	assert "imported 3 measurements: A / PLA (1), B / PLA (2)" in text
	assert len(saved(cal_file).curve("B", "PLA").measurements) == 2


@pytest.mark.parametrize("pair", ["1", "x=B", "1="])
def test_a_target_name_must_be_number_equals_dispenser(shell, tmp_path, pair):
	assert "TARGET=DISPENSER" in run(shell, f"calibrate import {old_file(tmp_path)} PLA {pair}")


def test_import_twice_skips_what_is_already_there(shell, tmp_path, cal_file):
	path = old_file(tmp_path)
	run(shell, f"calibrate import {path} PLA")
	assert "imported 0 measurements; 3 were already there" in run(shell, f"calibrate import {path} PLA")
	assert len(saved(cal_file).curve("target 1", "PLA").measurements) == 2


def test_import_with_nothing_new_saves_nothing(shell, tmp_path):
	path = old_file(tmp_path)
	run(shell, f"calibrate import {path} PLA")
	assert "saved to" not in run(shell, f"calibrate import {path} PLA")


def test_import_a_file_that_is_not_there(shell, tmp_path):
	assert f"no file at {tmp_path / 'nothing.json'}" in run(shell, f"calibrate import {tmp_path / 'nothing.json'} PLA")


def test_import_needs_a_file_and_a_material(shell):
	assert "usage: calibrate import OLDFILE MATERIAL [TARGET=DISPENSER ...]" in run(shell, "calibrate import old.json")


def test_import_of_a_new_layout_file_says_to_load_it(shell, tmp_path):
	store = CalibrationStore()
	store.add_measurement("A", "MB-red", 5.0, 3.0, 100.0)
	path = tmp_path / "new.json"
	save_calibration(store, path)
	assert "is not an old calibration file; `calibrate load" in run(shell, f"calibrate import {path} PLA")


# --- the rest ----------------------------------------------------------------------------------

def test_an_unknown_action_prints_the_usage(shell):
	text = run(shell, "calibrate frobnicate 1")
	assert "usage: calibrate" in text and "'frobnicate' is not a calibrate action" in text


def test_help_explains_every_action(shell):
	text = run(shell, "help calibrate")
	for action in ("add", "remove", "show", "save", "load", "import"):
		assert f"calibrate {action}" in text


def test_the_calibration_needs_no_board(shell):
	assert "measurement #1" in run(shell, "calibrate add B PP 20 10 5")
	assert shell.session.roles() == []

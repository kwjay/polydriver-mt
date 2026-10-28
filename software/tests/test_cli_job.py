"""C9.4, single-board jobs: g/s rows through the calibration, or duty rows for the bench, uploaded
to one board and run there. On simulated boards, so the board's own rules apply."""

import json

import pytest

from polydriver.calibration import CalibrationStore, save_calibration
from polydriver.protocol.constants import JobState, Mode, Role
from polydriver.sim import SimBench, SimBoard

from cli_helpers import CONFIG_TEXT, make_shell, run
from fakes import wait_until

PP = ((10.0, 0.5), (20.0, 1.0), (30.0, 1.5), (40.0, 2.0))
# Bent, as real screws give, so a ramp across its points needs a segment per piece.
TPU = ((10.0, 0.4), (20.0, 1.1), (30.0, 1.45), (40.0, 2.0))


def write_setup(folder):
	(folder / "polydriver.toml").write_text(CONFIG_TEXT + "\n[dev]\nenabled = true\n", encoding="utf-8")
	materials = [
		{"name": "PP", "kind": "polymer", "dispenser": "B"},
		{"name": "TPU", "kind": "polymer", "dispenser": "B", "flow_min_g_s": 0.8, "flow_max_g_s": 1.6},
		{"name": "MB-red", "kind": "masterbatch", "dispenser": "A"},
	]
	(folder / "materials.json").write_text(json.dumps({"schema": 1, "materials": materials}), encoding="utf-8")
	store = CalibrationStore()
	for speed_hz, rate_g_s in PP:
		store.add_measurement("B", "PP", speed_hz, rate_g_s * 10, 10.0)
	for speed_hz, rate_g_s in TPU:
		store.add_measurement("B", "TPU", speed_hz, rate_g_s * 10, 10.0)
	save_calibration(store, folder / "calibration.json")
	return folder / "polydriver.toml"


@pytest.fixture
def bench():
	return SimBench({"COM6": SimBoard(Role.POLYMER), "COM5": SimBoard(Role.COLOUR)})


@pytest.fixture
def shell(bench, tmp_path):
	shell = make_shell(bench.link_factory, write_setup(tmp_path))
	yield shell
	shell.session.close()


def polymer(bench):
	return bench["COM6"]


# --- Building -------------------------------------------------------------------------------------

def test_a_new_job_names_its_material_dispenser_and_calibration(shell):
	text = run(shell, "job new PP")
	assert "new g/s job for PP on dispenser B (calibration B / PP" in text
	assert "`job add G/S SECONDS [ramp]`" in text


def test_rows_are_shown_with_their_speeds_grams_and_total(shell):
	run(shell, "job new PP")
	run(shell, "job add 1.2 10")
	text = run(shell, "job add 1.6 10 ramp")
	assert "1  step to 1.2 g/s for 10 s" in text and "24 Hz" in text and "12 g" in text
	assert "2  ramp to 1.6 g/s for 10 s" in text and "32 Hz" in text and "14 g" in text
	assert "20 s, 2 segments, 26 g expected" in text


def test_a_ramp_across_calibration_points_is_split_and_a_straight_one_is_not(shell):
	run(shell, "job new TPU", "job add 0.9 5")
	assert "4 segments" in run(shell, "job add 1.5 5 ramp")
	run(shell, "job new PP", "job add 0.6 5")
	assert "2 segments" in run(shell, "job add 1.9 5 ramp")


def test_a_row_the_calibration_cannot_give_is_shown_with_why(shell):
	run(shell, "job new PP")
	text = run(shell, "job add 5 10")
	assert "1  step to 5 g/s for 10 s" in text
	assert "! row 1:" in text and "outside the calibration B / PP" in text and "can't be uploaded" in text


def test_a_rate_outside_the_materials_flow_window_is_refused(shell):
	run(shell, "job new TPU")
	assert "outside TPU's flow window 0.8 to 1.6 g/s" in run(shell, "job add 1.8 10")


def test_another_dispenser_can_be_named(shell, tmp_path):
	store = CalibrationStore()
	for speed_hz, rate_g_s in PP:
		store.add_measurement("C", "PP", speed_hz, rate_g_s * 20, 10.0)
	save_calibration(store, tmp_path / "calibration.json")
	assert "new g/s job for PP on dispenser C (calibration C / PP" in run(shell, "job new PP C")
	assert "12 Hz" in run(shell, "job add 1.2 10")


def test_a_material_without_a_calibration_is_refused_at_once(shell):
	text = run(shell, "job new MB-red")
	assert "no calibration for dispenser A, material MB-red" in text
	assert "no job yet" in run(shell, "job show")


def test_an_unknown_material_names_the_known_ones(shell):
	text = run(shell, "job new PLA")
	assert "PLA" in text and "MB-red, PP, TPU" in text and "internal error" not in text


def test_a_duty_job_needs_no_material(shell):
	text = run(shell, "job new")
	assert "new duty job" in text and "`dev mode ROLE open`" in text
	run(shell, "job add duty 64 5")
	text = run(shell, "job add duty 200 10 ramp")
	assert "1  step to duty 64 for 5 s" in text and "2  ramp to duty 200 for 10 s" in text
	assert "15 s, 2 segments" in text


@pytest.mark.parametrize("first, line, message", [
	("job new PP", "job add duty 64 5", "this is a g/s job for PP; `job new` starts a duty job"),
	("job new", "job add 1.2 10", "this is a duty job; `job new MATERIAL` starts a g/s job"),
	("job new", "job add duty 300 5", "DUTY must be a whole number from 0 to 255, not '300'"),
	("job new PP", "job add 1.2", "usage: job add [duty] VALUE SECONDS [ramp]"),
	("job new PP", "job add 1.2 10 slowly", "'slowly' is not ramp"),
	("job new PP", "job add fast 10", "G/S must be a number, not 'fast'"),
	("job new PP", "job add 1.2 -1", "SECONDS must be more than 0"),
	("job new PP", "job add 1.2 0", "SECONDS must be more than 0"),
	("job new PP", "job add 1.2 10 duty", "'duty' is not ramp"),
	("job new PP", "job add 1.2 10 ramp now", "usage: job add [duty] VALUE SECONDS [ramp]"),
])
def test_rows_that_do_not_fit_the_job_are_refused(shell, first, line, message):
	run(shell, first)
	assert message in run(shell, line)
	assert "  1  " not in run(shell, "job show")


@pytest.mark.parametrize("line", ["job add fast 10", "job add 1.2 ten", "job add duty high 5"])
def test_a_value_that_is_not_a_number_shows_the_add_usage(shell, line):
	run(shell, "job new" if "duty" in line else "job new PP")
	assert "usage: job add [duty] VALUE SECONDS [ramp]" in run(shell, line)


def test_job_alone_is_show(shell):
	run(shell, "job new PP", "job add 1.2 10")
	assert run(shell, "job") == run(shell, "job show")


def test_new_takes_at_most_a_material_and_a_dispenser(shell):
	assert "usage: job new [MATERIAL [DISPENSER]]" in run(shell, "job new PP B extra")


def test_a_bug_while_compiling_is_an_internal_error_not_a_job_problem(shell, monkeypatch):
	import polydriver.cli.job_commands as commands
	run(shell, "job new PP", "job add 1.2 10")

	def broken(self, store, max_segments):
		raise RuntimeError("bug in the code")

	monkeypatch.setattr(commands.BenchJob, "compile", broken)
	text = run(shell, "job show")
	assert "internal error" in text and "can't be uploaded" not in text


def test_adding_before_a_new_job_says_how_to_start(shell):
	assert "no job yet (`job new MATERIAL` for g/s, `job new` for duty)" in run(shell, "job add 1.2 10")


def test_remove_drops_a_row(shell):
	run(shell, "job new PP", "job add 1.2 10", "job add 1.4 10", "job add 1.6 10")
	text = run(shell, "job remove 2")
	assert "removed row 2" in text and "2  step to 1.6 g/s" in text and "1.4" not in text


def test_remove_before_a_job_says_how_to_start(shell):
	assert "no job yet" in run(shell, "job remove 1")


@pytest.mark.parametrize("number, message", [
	("4", "there is no row 4; the job has 1"), ("0", "there is no row 0; the job has 1"), ("x", "N must be a whole number"),
])
def test_remove_a_row_that_is_not_there(shell, number, message):
	run(shell, "job new PP", "job add 1.2 10")
	assert message in run(shell, f"job remove {number}")


def test_show_an_empty_job(shell):
	run(shell, "job new PP")
	assert "no rows yet" in run(shell, "job show")


def test_calibration_added_in_the_shell_is_used_at_once(shell):
	run(shell, "job new PP", "job add 2.2 10")
	run(shell, "calibrate add B PP 50 10 25")
	assert "outside" not in run(shell, "job show")


# --- On a board -----------------------------------------------------------------------------------

def test_a_job_is_uploaded_to_one_board_and_run(shell, bench):
	run(shell, "connect polymer", "job new PP", "job add 1.2 1", "job add 1.6 1 ramp")
	text = run(shell, "job upload polymer")
	assert "polymer: READY, 2 segments, 2000 ms (`job run` starts it)" in text
	text = run(shell, "job run")
	assert "polymer: running" in text
	assert wait_until(lambda: polymer(bench).state().job_state is JobState.DONE, 5.0)


def test_a_duty_job_runs_in_open_loop(shell, bench):
	run(shell, "connect polymer", "dev mode polymer open", "job new", "job add duty 180 0.5")
	run(shell, "job upload polymer", "job run polymer")
	assert wait_until(lambda: polymer(bench).state().duty == 180, 2.0)


def test_a_duty_job_in_closed_loop_is_refused_by_the_board_with_a_hint(shell):
	run(shell, "connect polymer", "job new", "job add duty 180 0.5", "job upload polymer")
	text = run(shell, "job run polymer")
	assert "refused" in text and "WRONG_MODE" in text and "hint:" in text


def test_job_stop_aborts_the_job(shell, bench):
	run(shell, "connect polymer", "job new PP", "job add 1.2 10", "job upload polymer", "job run")
	assert wait_until(lambda: polymer(bench).state().job_state is JobState.RUNNING)
	assert "STOP sent to polymer" in run(shell, "job stop")
	assert polymer(bench).state().job_state is JobState.ABORTED


def test_a_job_that_cannot_compile_is_not_uploaded(shell, bench):
	run(shell, "connect polymer", "job new PP", "job add 5 10")
	assert "outside the calibration B / PP" in run(shell, "job upload polymer")
	assert polymer(bench).state().job_state is JobState.EMPTY


def test_an_empty_job_is_not_uploaded(shell):
	run(shell, "connect polymer", "job new PP")
	assert "the job has no rows" in run(shell, "job upload polymer")


def test_uploading_to_a_board_that_is_not_connected_says_so(shell):
	run(shell, "job new PP", "job add 1.2 10")
	assert "not connected" in run(shell, "job upload polymer")


def test_a_polymer_job_on_the_colour_board_is_flagged_and_uploaded(shell, bench):
	run(shell, "connect colour", "job new PP", "job add 1.2 1")
	text = run(shell, "job upload colour")
	assert "! PP is a polymer, and this is the colour board" in text
	assert bench["COM5"].state().job_state is JobState.READY


def test_run_and_stop_need_a_board_when_nothing_was_uploaded(shell):
	assert "usage: job run [ROLE]" in run(shell, "job run")
	assert "usage: job stop [ROLE]" in run(shell, "job stop")


def test_run_takes_at_most_one_board(shell):
	assert "usage: job run [ROLE]" in run(shell, "job run polymer colour")


def test_upload_needs_a_board(shell):
	run(shell, "job new PP", "job add 1.2 10")
	assert "usage: job upload ROLE" in run(shell, "job upload")


def test_the_board_must_have_the_whole_job(shell):
	run(shell, "connect polymer", "job new PP")
	for _ in range(33):
		run(shell, "job add 1.2 1")
	assert "the job compiles to 33 segments, the board holds 32" in run(shell, "job upload polymer")


def test_an_unknown_job_action_prints_the_usage(shell):
	assert "'frobnicate' is not a job action" in run(shell, "job frobnicate")


def test_help_explains_every_job_action(shell):
	text = run(shell, "help job")
	for action in ("new", "add", "remove", "show", "upload", "run", "stop", "load", "validate"):
		assert f"job {action}" in text


def test_the_mode_is_open_loop_after_dev_mode_open(shell, bench):
	run(shell, "connect polymer", "dev mode polymer open")
	assert polymer(bench).state().mode is Mode.OPEN_LOOP

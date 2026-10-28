"""C8.6: a job file runs on two simulated boards to a CSV and a sidecar that check out, clean and
with a forced fault."""

import io
import json
import math
from dataclasses import replace

import pytest

from polydriver.calibration import load_calibration
from polydriver.config import load_config
from polydriver.jobs import StepsCurve, check_job, compile_gradient_job, job_dict, load_job, save_job
from polydriver.materials import load_materials
from polydriver.protocol.constants import Role
from polydriver.run import RunState
from polydriver.sim import SimBoard
from polydriver.telemetry import LogCheck
from polydriver.sim.end_to_end import EXAMPLE_CONFIG, FAULT_KINDS, EndToEndError, Fault, main, run_end_to_end

EXAMPLE = EXAMPLE_CONFIG.parent
EXAMPLE_JOB = EXAMPLE / "jobs" / "two-minute-gradient.json"


def example_config(tmp_path):
	config = load_config(EXAMPLE_CONFIG)
	return replace(config, logging=replace(config.logging, folder=tmp_path / "logs"))


def job_file(tmp_path, seconds=8.0, **changes):
	"""The example job, shorter, as a file of its own."""
	job = replace(load_job(EXAMPLE_JOB), amount_given={"duration_s": seconds, "total_flow_g_s": 1.5},
				hold_start_s=1.0, hold_end_s=1.0, **changes)
	path = tmp_path / "jobs" / "short.json"
	save_job(job, path)
	return path


def sidecar(run):
	return json.loads(run.sidecar_path.read_text(encoding="utf-8"))


# --- The example setup ---------------------------------------------------------------------------

def test_the_example_job_is_two_minutes_and_fits_its_calibrations():
	config = load_config(EXAMPLE_CONFIG)
	materials = load_materials(config.resolve(config.paths.materials))
	store = load_calibration(config.resolve(config.paths.calibration))
	job = load_job(EXAMPLE_JOB)
	assert check_job(job, materials, store).report() == ["no problems: every point is inside both calibrations"]
	program = compile_gradient_job(job, materials, store)
	assert program.total_ms == 120_000
	assert max(len(program.polymer.segments), len(program.colour.segments)) <= 32


def test_the_example_config_logs_at_10_hz_with_a_comms_timeout():
	config = load_config(EXAMPLE_CONFIG)
	assert (config.logging.enabled, config.logging.poll_hz, config.comms_timeout_s) == (True, 10.0, 2.0)
	assert all(config.boards[role].has_gains for role in (Role.POLYMER, Role.COLOUR))


# --- Faults ---------------------------------------------------------------------------------------

def test_a_fault_is_read_as_board_kind_and_time():
	assert Fault.parse("colour:jam@40") == Fault(Role.COLOUR, "jam", 40.0)
	assert Fault.parse(" polymer:link-lost@1.5 ") == Fault(Role.POLYMER, "link-lost", 1.5)
	assert str(Fault(Role.COLOUR, "jam", 40.0)) == "colour jam at 40 s"


@pytest.mark.parametrize("text, message", [
	("colour:jam", "BOARD:KIND@SECONDS"),
	("pink:jam@1", "board 'pink': polymer or colour"),
	("colour:melt@1", "kind 'melt': " + ", ".join(FAULT_KINDS)),
	("colour:jam@soon", "time 'soon': seconds after the start"),
	("colour:jam@-1", "time -1.0: seconds after the start"),
	("colour:jam@nan", "time nan: seconds after the start"),
	("colour:jam@inf", "time inf: seconds after the start"),
])
def test_a_fault_that_cannot_be_read_says_what_is_wrong(text, message):
	with pytest.raises(ValueError, match=message.replace("(", r"\(")):
		Fault.parse(text)


def test_a_fault_is_validated_when_made():
	with pytest.raises(ValueError):
		Fault(Role.NONE, "jam", 1.0)
	with pytest.raises(ValueError):
		Fault(Role.COLOUR, "jam", math.inf)
	with pytest.raises(ValueError, match="kind 'melt'"):
		Fault(Role.COLOUR, "melt", 1.0)


@pytest.mark.parametrize("kind, effect", [
	("jam", lambda board: board.jammed),
	("link-lost", lambda board: board.link_lost),
	("fast-clock", lambda board: board.clock_error == 0.005),
	("noise", lambda board: (board.corrupt_replies, board.corrupt_requests, board.drop_replies) == (1, 1, 1)),
	("restart", lambda board: board.state().time_us < 1_000_000),
])
def test_each_fault_does_what_it_says(kind, effect):
	now_s = [0.0]
	board = SimBoard(Role.COLOUR, clock=lambda: now_s[0])
	now_s[0] = 10.0
	Fault(Role.COLOUR, kind, 0.0).apply(board)
	assert effect(board)


# --- Runs -----------------------------------------------------------------------------------------

def test_a_clean_run_goes_from_the_job_file_to_logs_that_check_out(tmp_path):
	path = job_file(tmp_path)
	run = run_end_to_end(example_config(tmp_path), path)
	assert (run.state, run.reason, run.warnings) == (RunState.DONE, None, ())
	assert run.check.problems == ()
	assert {name: b.last_state for name, b in run.check.boards.items()} == {"polymer": "DONE", "colour": "DONE"}
	assert 70 <= run.check.rows <= 100
	assert run.csv_path.parent == tmp_path / "logs"
	side = sidecar(run)
	assert side["job"] == job_dict(load_job(path))
	assert side["note"] == "simulated run of short.json"
	assert side["boards"]["colour"]["calibration_hash"] == load_job(path).colour.hash
	for name in ("polymer", "colour"):
		board = run.check.boards[name]
		assert board.estimated_g == pytest.approx(board.expected_g, rel=0.15)


def test_a_jammed_colour_screw_aborts_the_run_and_the_log_shows_both_boards_stopped(tmp_path):
	run = run_end_to_end(example_config(tmp_path), job_file(tmp_path), faults=[Fault(Role.COLOUR, "jam", 2.0)])
	assert run.state is RunState.ABORTED
	assert run.reason.startswith("the colour speed was off") and run.reason.endswith("(encoder stalled)")
	assert [w for w in run.warnings if w.startswith("the colour encoder stalled at")]
	assert run.check.problems == ()
	assert {name: b.last_state for name, b in run.check.boards.items()} == {"polymer": "ABORTED", "colour": "ABORTED"}
	side = sidecar(run)
	assert [entry["command"] for entry in side["fleet"]] == ["JOB_RUN", "STOP"]
	assert side["note"] == "simulated run of short.json; fault: colour jam at 2 s"
	assert side["run"]["reason"] == run.reason


def test_a_lost_colour_link_aborts_the_run_and_the_log_shows_the_loss(tmp_path):
	run = run_end_to_end(example_config(tmp_path), job_file(tmp_path), faults=[Fault(Role.COLOUR, "link-lost", 2.0)])
	assert run.state is RunState.ABORTED
	assert run.reason.startswith("the colour board's link was lost")
	assert run.check.problems == ()
	assert run.check.boards["polymer"].last_state == "ABORTED"
	assert [e["kind"] for e in sidecar(run)["events"] if e["role"] == "colour"][-1] == "link lost"
	assert run.bench[run.bench.COLOUR_PORT].link_lost


def test_a_job_that_does_not_fit_its_calibrations_is_refused_before_a_board_moves(tmp_path):
	path = job_file(tmp_path, phi_end=0.5)
	with pytest.raises(EndToEndError, match="does not fit its calibrations") as error:
		run_end_to_end(example_config(tmp_path), path)
	assert "error:" in str(error.value)
	assert not (tmp_path / "logs").exists()


def test_a_job_that_needs_more_segments_than_the_boards_hold_is_refused_and_its_log_closed(tmp_path):
	path = job_file(tmp_path, curve=StepsCurve(40))
	with pytest.raises(EndToEndError, match="could not be uploaded") as error:
		run_end_to_end(example_config(tmp_path), path)
	assert "segments" in str(error.value)
	(sidecar_path,) = (tmp_path / "logs").glob("*.json")
	assert json.loads(sidecar_path.read_text(encoding="utf-8"))["ended"] is not None


def test_a_missing_job_file_is_an_end_to_end_error(tmp_path):
	with pytest.raises(EndToEndError, match="no job file"):
		run_end_to_end(example_config(tmp_path), tmp_path / "nothing.json")


# --- The command --------------------------------------------------------------------------------

def config_file(tmp_path):
	text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
	text = text.replace('folder = "logs"', f'folder = "{(tmp_path / "logs").as_posix()}"')
	for name in ("calibration.json", "materials.json"):
		text = text.replace(f'"{name}"', f'"{(EXAMPLE / name).as_posix()}"')
	path = tmp_path / "polydriver.toml"
	path.write_text(text, encoding="utf-8")
	return path


def test_the_command_runs_the_job_and_reports_the_run_and_the_log(tmp_path):
	out = io.StringIO()
	code = main([str(job_file(tmp_path, seconds=3.0)), "--config", str(config_file(tmp_path))], stdout=out)
	text = out.getvalue()
	assert code == 0, text
	assert "short.json: 3 s on two simulated boards, no fault" in text
	assert "run: DONE" in text
	assert "run log: DONE" in text and "; no problems" in text
	assert "CSV: " in text and "sidecar: " in text


def test_the_command_forces_the_faults_it_is_given(tmp_path):
	out = io.StringIO()
	code = main([str(job_file(tmp_path, seconds=3.0)), "--config", str(config_file(tmp_path)),
				"--fault", "polymer:link-lost@1"], stdout=out)
	text = out.getvalue()
	assert code == 0, text
	assert "fault: polymer link-lost at 1 s" in text
	assert "run: ABORTED: the polymer board's link was lost" in text


def test_the_command_exits_1_when_the_log_does_not_check_out(tmp_path, monkeypatch):
	import polydriver.sim.end_to_end as e2e
	monkeypatch.setattr(e2e, "check_run_log", lambda path: LogCheck(("row 3: colour edges went back",), "DONE", 30, 3.0, {}))
	out = io.StringIO()
	assert main([str(job_file(tmp_path, seconds=3.0)), "--config", str(config_file(tmp_path))], stdout=out) == 1
	assert "problem: row 3: colour edges went back" in out.getvalue()


@pytest.mark.parametrize("args, message", [
	(["--fault", "pink:jam@1"], "board 'pink'"),
	(["--config", "nowhere.toml"], "no config file at nowhere.toml"),
])
def test_the_command_stops_at_a_setup_problem_with_code_2(tmp_path, args, message):
	out = io.StringIO()
	job = job_file(tmp_path, seconds=3.0)
	if "--config" not in args:
		args = args + ["--config", str(config_file(tmp_path))]
	assert main([str(job), *args], stdout=out) == 2
	assert message in out.getvalue()


def test_the_command_refuses_a_job_that_does_not_fit(tmp_path):
	out = io.StringIO()
	code = main([str(job_file(tmp_path, phi_end=0.5)), "--config", str(config_file(tmp_path))], stdout=out)
	assert code == 2
	assert "does not fit its calibrations" in out.getvalue()

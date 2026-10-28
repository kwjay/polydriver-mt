"""C9.4, gradient runs: a job file loaded and checked, uploaded to both boards, started, followed and
stopped from the shell, on two simulated boards, with the run log."""

import io
import json
import time
from dataclasses import replace

import pytest

from polydriver.cli import main
from polydriver.jobs import load_job, save_job
from polydriver.protocol.constants import JobState, Role
from polydriver.run import RunState
from polydriver.sim import SimBench, SimBoard
from polydriver.sim.end_to_end import EXAMPLE_CONFIG
from polydriver.telemetry import check_run_log

from cli_helpers import make_shell, run
from fakes import wait_until

EXAMPLE = EXAMPLE_CONFIG.parent


def config_text(tmp_path, logging=True):
	return f"""
[ports]
polymer = "COM6"
colour = "COM5"

[safety]
comms_timeout_s = 1.0

[board.polymer]
kp = 0.5
ki = 2.0
kd = 0.0
alpha = 0.5
ff_offset_duty = 128.0
ff_gain_duty_per_hz = 1.67

[board.colour]
kp = 0.5
ki = 2.0
kd = 0.0
alpha = 0.5
ff_offset_duty = 128.0
ff_gain_duty_per_hz = 1.67

[logging]
enabled = {"true" if logging else "false"}
folder = "logs"
poll_hz = 10.0

[paths]
jobs = "jobs"
calibration = "{(EXAMPLE / "calibration.json").as_posix()}"
materials = "{(EXAMPLE / "materials.json").as_posix()}"
"""


def write_job(tmp_path, name="short", seconds=4.0, **changes):
	job = replace(load_job(EXAMPLE / "jobs" / "two-minute-gradient.json"), name=f"{name} test job",
				amount_given={"duration_s": seconds, "total_flow_g_s": 1.5}, hold_start_s=0.5, hold_end_s=0.5, **changes)
	path = tmp_path / "jobs" / f"{name}.json"
	save_job(job, path)
	return path


@pytest.fixture
def bench():
	return SimBench({"COM6": SimBoard(Role.POLYMER), "COM5": SimBoard(Role.COLOUR)})


def new_shell(bench, tmp_path, logging=True):
	path = tmp_path / "polydriver.toml"
	path.write_text(config_text(tmp_path, logging), encoding="utf-8")
	return make_shell(bench.link_factory, path)


@pytest.fixture
def shell(bench, tmp_path):
	write_job(tmp_path)
	shell = new_shell(bench, tmp_path)
	yield shell
	shell.before_quit()
	shell.session.close()
	shell.on_quit()


def everything(shell):
	with shell._output_lock:
		return shell.stdout.getvalue()


def ready(shell):
	run(shell, "connect", "job load short")
	assert "READY" in run(shell, "run upload")


def started(shell):
	ready(shell)
	assert "started" in run(shell, "run start")
	assert wait_until(lambda: shell.runner.progress().elapsed_s > 0.3, 3.0)


def sidecar(shell):
	return json.loads(shell.runner_log.sidecar_path.read_text(encoding="utf-8"))


# --- Loading ---------------------------------------------------------------------------------------

def test_validate_reports_the_check_and_the_program_and_loads_nothing(shell, tmp_path):
	text = run(shell, f"job validate {tmp_path / 'jobs' / 'short.json'}")
	assert "no problems: every point is inside both calibrations" in text
	assert "polymer:" in text and "segments" in text and "g expected" in text and "4 s" in text
	assert "`job load FILE` first" in run(shell, "connect", "run upload")


def test_load_takes_a_name_from_the_jobs_folder(shell, tmp_path):
	text = run(shell, "job load short")
	assert f"loaded {tmp_path / 'jobs' / 'short.json'}: short test job" in text
	assert "no problems" in text and "`run upload` sends it to both boards" in text


def test_load_takes_a_path_with_the_json_ending(shell, tmp_path):
	assert "loaded" in run(shell, f"job load {tmp_path / 'jobs' / 'short.json'}")


def test_a_name_with_the_json_ending_is_looked_for_in_the_jobs_folder_too(shell, tmp_path):
	assert f"loaded {tmp_path / 'jobs' / 'short.json'}" in run(shell, "job load short.json")


def test_a_file_in_the_current_folder_comes_before_the_jobs_folder(shell, tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	write_job(tmp_path, "local")
	(tmp_path / "jobs" / "local.json").rename(tmp_path / "local.json")
	assert "loaded local.json: local test job" in run(shell, "job load local.json")


def test_a_path_with_folders_is_taken_as_it_is(shell, tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	text = run(shell, "job load sub/short.json")
	assert "no job file" in text and str(tmp_path / "jobs" / "sub") not in text


def test_load_takes_one_file(shell):
	assert "usage: job load FILE | job validate FILE" in run(shell, "job load a b")


def test_a_job_that_does_not_fit_is_reported_and_not_loaded(shell, tmp_path):
	write_job(tmp_path, "steep", phi_end=0.5)
	run(shell, "job load short")
	text = run(shell, "job load steep")
	assert "error:" in text and "not loaded; still short test job" in text
	assert shell.gradient_job.name == "short test job"
	# Each error once: from the check, not again from compiling a job the check refused.
	from polydriver.jobs import check_job
	check = check_job(load_job(tmp_path / "jobs" / "steep.json"), shell._materials(), shell._calibration_store())
	assert text.count("error:") == sum(line.startswith("error:") for line in check.report())


def test_validate_a_job_that_does_not_fit_shows_the_errors(shell, tmp_path):
	write_job(tmp_path, "steep", phi_end=0.5)
	text = run(shell, "job validate steep")
	assert "error:" in text and "it cannot run" in text


def test_a_missing_job_file_is_named(shell, tmp_path):
	text = run(shell, "job load nothing")
	assert "no job file" in text and str(tmp_path / "jobs" / "nothing.json") in text and "internal error" not in text


def test_a_broken_job_file_is_named(shell, tmp_path):
	(tmp_path / "jobs" / "broken.json").write_text("{", encoding="utf-8")
	text = run(shell, "job load broken")
	assert "broken.json" in text and "internal error" not in text


# --- A whole run -----------------------------------------------------------------------------------

def test_a_whole_gradient_run_from_the_shell_with_its_log(shell, bench):
	run(shell, "connect", "job load short")
	text = run(shell, "run upload")
	assert "uploaded to both boards: polymer" in text and "READY" in text and "log " in text
	text = run(shell, "run start")
	assert "started both boards" in text and "ms apart" in text
	assert wait_until(lambda: "! run DONE" in everything(shell), 10.0)
	assert f"! run DONE; log {shell.runner_log.csv_path}" in everything(shell)
	text = run(shell, "run status")
	assert "run DONE: 4.0 of 4 s (100 %)" in text and "polymer: DONE" in text and "colour: DONE" in text
	assert "g expected" in text and "why:" not in text
	check = check_run_log(shell.runner_log.csv_path)
	assert check.problems == () and check.result == "DONE"
	assert all(bench[port].state().job_state is JobState.DONE for port in ("COM6", "COM5"))


def test_run_status_while_running_shows_the_time_and_grams(shell):
	started(shell)
	text = run(shell, "run status")
	assert "run RUNNING:" in text and "of 4 s" in text and "polymer: RUNNING segment" in text


def test_run_status_shows_the_warnings(shell, bench):
	started(shell)
	# Once the encoder has measured a speed; before that a stall is the slow start, not a warning.
	assert wait_until(lambda: shell.runner.progress().elapsed_s > 1.5, 3.0)
	bench["COM5"].jammed = True
	assert wait_until(lambda: shell.runner.progress().warnings, 3.0)
	assert "! the colour encoder stalled at" in run(shell, "run status")


def test_run_stop_aborts_both_boards_and_closes_the_log(shell, bench):
	started(shell)
	text = run(shell, "run stop")
	assert "STOP sent to both boards" in text
	assert wait_until(lambda: "! run ABORTED: stopped by the operator" in everything(shell), 3.0)
	assert sidecar(shell)["result"] == "ABORTED"
	assert all(bench[port].state().job_state is JobState.ABORTED for port in ("COM6", "COM5"))
	text = run(shell, "run status")
	assert "run ABORTED:" in text and "why: stopped by the operator" in text
	assert "polymer: ABORTED" in text and "colour: ABORTED" in text


def test_a_stop_that_cannot_reach_a_board_says_so(shell, bench):
	started(shell)
	bench["COM5"].lose_link()
	assert wait_until(lambda: "! run ABORTED" in everything(shell), 3.0)
	text = run(shell, "run stop")
	assert "STOP FAILED on colour" in text and "both boards" not in text


def test_ctrl_c_stops_the_run_as_the_operator(shell, bench):
	started(shell)
	shell.interrupt()
	assert wait_until(lambda: "! run ABORTED: stopped by the operator" in everything(shell), 3.0)
	assert "STOP sent to polymer, colour" in everything(shell)


def test_quit_stops_the_run_and_closes_its_log(bench, tmp_path):
	write_job(tmp_path)
	shell = new_shell(bench, tmp_path)
	started(shell)
	log, close = shell.runner_log, shell.runner_log.close

	def slow_close(result=None):
		time.sleep(1.0)
		close(result)

	# The wind-up ends with the log's close; quit waits for it, however long it takes.
	log.close = slow_close
	run(shell, "quit")
	assert sidecar(shell)["ended"] is not None
	assert sidecar(shell)["result"] == "ABORTED"
	assert sidecar(shell)["run"]["reason"] == "stopped by the operator"


def test_quit_after_a_finished_run_stops_nothing(bench, tmp_path):
	write_job(tmp_path, seconds=2.0)
	shell = new_shell(bench, tmp_path)
	started(shell)
	assert shell.runner.wait(5.0) is RunState.DONE
	text = run(shell, "quit")
	assert "run stopped" not in text and sidecar(shell)["result"] == "DONE"


def test_a_safety_abort_is_announced(shell, bench):
	started(shell)
	bench["COM5"].lose_link()
	assert wait_until(lambda: "! run ABORTED: the colour board's link was lost" in everything(shell), 3.0)


def test_nolog_runs_without_a_log(shell):
	run(shell, "connect", "job load short")
	assert "no log" in run(shell, "run upload nolog")
	assert shell.runner_log is None


def test_with_logging_off_in_the_config_log_still_logs_one_run(bench, tmp_path):
	write_job(tmp_path, log=None)
	shell = new_shell(bench, tmp_path, logging=False)
	run(shell, "connect", "job load short")
	assert "no log" in run(shell, "run upload")
	shell.before_quit()
	shell.session.close()
	shell = new_shell(SimBench({"COM6": SimBoard(Role.POLYMER), "COM5": SimBoard(Role.COLOUR)}), tmp_path, logging=False)
	run(shell, "connect", "job load short")
	assert "log " in run(shell, "run upload log") and shell.runner_log is not None
	shell.before_quit()
	shell.session.close()


def test_the_jobs_own_log_setting_wins_over_the_config(bench, tmp_path):
	write_job(tmp_path, "quiet", log=False)
	shell = new_shell(bench, tmp_path)
	run(shell, "connect", "job load quiet")
	assert "no log" in run(shell, "run upload")
	shell.before_quit()
	shell.session.close()


def test_upload_needs_both_boards_and_makes_no_log_without_them(shell, tmp_path):
	run(shell, "connect polymer", "job load short")
	assert "colour board is not connected" in run(shell, "run upload")
	assert not list((tmp_path / "logs").glob("*.csv"))


def test_a_board_not_yet_ready_after_the_upload_is_waited_for(shell, monkeypatch):
	run(shell, "connect", "job load short")

	def no_poll(role):
		raise TimeoutError("no reply")

	monkeypatch.setattr(shell.session, "poll_now", no_poll)
	assert "run UPLOADED: waiting for both boards to report READY (`run status`)" in run(shell, "run upload")


def test_upload_needs_a_loaded_job(shell):
	run(shell, "connect")
	assert "no gradient job loaded (`job load FILE` first)" in run(shell, "run upload")


def test_an_upload_a_board_refuses_closes_its_log(shell, tmp_path):
	run(shell, "connect", "job new PP", "job add 1.2 10", "job upload polymer", "job run")
	run(shell, "job load short")
	text = run(shell, "run upload")
	assert "refused" in text and "not uploaded" in text and "hint:" in text
	(sidecar_path,) = (tmp_path / "logs").glob("*.json")
	assert json.loads(sidecar_path.read_text(encoding="utf-8"))["ended"] is not None
	assert shell.runner is None


def test_upload_while_a_hand_log_runs_is_refused(shell):
	run(shell, "connect", "job load short", "log on")
	assert "a run log is open (`log off` first; the run writes its own)" in run(shell, "run upload")


def test_a_second_upload_while_running_is_refused(shell):
	started(shell)
	assert "a run is RUNNING (`run stop` first)" in run(shell, "run upload")


def test_after_a_run_another_can_be_uploaded(shell):
	started(shell)
	run(shell, "run stop")
	assert wait_until(lambda: "! run ABORTED" in everything(shell), 3.0)
	assert "READY" in run(shell, "run upload")


def test_start_before_upload_says_what_to_do(shell):
	assert "no run uploaded (`run upload` first)" in run(shell, "run start")


def test_start_twice_is_refused_by_the_runner(shell):
	started(shell)
	assert "cannot become RUNNING" in run(shell, "run start")


def test_status_and_stop_without_a_run(shell):
	assert "no run yet" in run(shell, "run status")
	assert "no run yet" in run(shell, "run stop")


def test_run_alone_is_status(shell):
	assert run(shell, "run") == run(shell, "run status")


@pytest.mark.parametrize("line, message", [
	("run upload often", "log or nolog, not 'often'"),
	("run frobnicate", "'frobnicate' is not a run action"),
	("run start now", "usage: run start"),
])
def test_wrong_words_print_the_usage(shell, line, message):
	assert message in run(shell, line)


def test_help_explains_every_run_action(shell):
	text = run(shell, "help run")
	for action in ("upload", "start", "stop", "status"):
		assert f"run {action}" in text


# --- python -m polydriver --sim ------------------------------------------------------------------

def test_sim_runs_the_shell_on_two_simulated_boards(tmp_path):
	write_job(tmp_path)
	path = tmp_path / "polydriver.toml"
	path.write_text(config_text(tmp_path), encoding="utf-8")
	out = io.StringIO()
	lines = "connect\njob load short\nrun upload nolog\nquit\n"
	assert main(["--sim", "--config", str(path)], stdin=io.StringIO(lines), stdout=out) == 0
	text = out.getvalue()
	assert "simulated boards on COM6 (polymer) and COM5 (colour)" in text
	assert "polymer: connected" in text and "READY" in text


def test_sim_without_ports_in_the_config_names_its_own(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text("", encoding="utf-8")
	out = io.StringIO()
	assert main(["--sim", "--config", str(path)], stdin=io.StringIO("connect\nquit\n"), stdout=out) == 0
	assert "simulated boards on SIM-POLYMER (polymer) and SIM-COLOUR (colour)" in out.getvalue()

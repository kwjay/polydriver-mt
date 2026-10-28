"""C8.6: the run-log check finds what makes a log wrong, and nothing in a right one."""

import copy
import csv
import json

import pytest

from polydriver.telemetry import COLUMNS, check_run_log

SEGMENTS = [{"value": 10.0, "duration_ms": 500, "ramp": False, "duty": False},
			{"value": 20.0, "duration_ms": 500, "ramp": True, "duty": False}]


def cells(host_s, state, elapsed_ms=0, segment="", edges=0, device_us=None, restarted=0):
	device_us = round(host_s * 1e6) + 5_000_000 if device_us is None else device_us
	return [f"{host_s:.4f}", device_us, 10.0, 10.0, 160, "REGULATING", state, segment, elapsed_ms, edges, 0, 10000, 0, 0,
			restarted]


class Log:
	"""A finished 1 s run on two boards, polled at 10 Hz: two READY rows, RUNNING, then DONE."""

	def __init__(self):
		self.rows = [[0.0] + cells(0.0, "READY") + cells(0.001, "READY")]
		for tick in range(1, 10):
			t = tick / 10
			segment = 0 if t < 0.5 else 1
			self.rows.append([t] + cells(t, "RUNNING", tick * 100, segment, tick * 2)
							+ cells(t + 0.001, "RUNNING", tick * 100, segment, tick))
		self.rows.append([1.0] + cells(1.0, "DONE", 1000, "", 20) + cells(1.001, "DONE", 1000, "", 10))
		board = {"port": "SIM", "firmware": "3.0.0", "protocol": 3, "settings": {}, "segments": copy.deepcopy(SEGMENTS),
				"calibration_hash": "abc", "expected_g": 1.0, "estimated_g": 1.01}
		self.sidecar = {
			"schema": 1, "csv": "run.csv", "started": "2026-10-28T09:00:00+01:00", "ended": "2026-10-28T09:00:01+01:00",
			"duration_s": 1.05, "result": "DONE", "note": "", "roles": ["polymer", "colour"], "settings": {},
			"boards": {"polymer": board, "colour": copy.deepcopy(board)},
			"job": {"name": "test"},
			"run": {"error_limit": 0.005, "limits": {}, "reason": None, "warnings": [], "states": [
				{"state": "IDLE", "t_s": 0.0}, {"state": "UPLOADED", "t_s": 0.0}, {"state": "READY", "t_s": 0.0},
				{"state": "RUNNING", "t_s": 0.01}, {"state": "DONE", "t_s": 1.0}]},
			"fleet": [{"command": "JOB_RUN", "gap_s": 0.0, "gap_bounds_s": None, "boards": {
				name: {"first_sent_s": 0.01, "sent_s": 0.01, "replied_s": 0.011, "attempts": 1, "error": None}
				for name in ("polymer", "colour")}}],
			"events": [], "rows": len(self.rows), "weighed_mass_g": {"polymer": None, "colour": None}, "clock": "",
		}

	def cell(self, row: int, column: str, value) -> None:
		self.rows[row][COLUMNS.index(column)] = value

	def abort(self, at_row: int, reason: str = "the colour speed was off") -> None:
		"""The run ends ABORTED at that row: both boards stopped, the rows after it gone."""
		del self.rows[at_row + 1:]
		self.cell(at_row, "polymer_job_state", "ABORTED")
		self.cell(at_row, "colour_job_state", "ABORTED")
		self.cell(at_row, "polymer_segment", "")
		self.cell(at_row, "colour_segment", "")
		self.sidecar["rows"] = len(self.rows)
		self.sidecar["result"] = "ABORTED"
		self.sidecar["run"]["reason"] = reason
		self.sidecar["run"]["states"][-1]["state"] = "ABORTED"
		self.sidecar["fleet"].append({"command": "STOP", "gap_s": 0.0, "gap_bounds_s": None, "boards": {}})

	def write(self, folder):
		path = folder / "run.csv"
		with path.open("w", newline="", encoding="utf-8") as file:
			writer = csv.writer(file)
			writer.writerow(COLUMNS)
			for row in self.rows:
				writer.writerow([f"{row[0]:.4f}", *row[1:]])
		path.with_suffix(".json").write_text(json.dumps(self.sidecar), encoding="utf-8")
		return path


@pytest.fixture
def log():
	return Log()


def problems(log, tmp_path):
	return check_run_log(log.write(tmp_path)).problems


def test_a_right_log_has_no_problems_and_a_summary(log, tmp_path):
	check = check_run_log(log.write(tmp_path))
	assert check.problems == ()
	assert check.ok
	assert (check.result, check.rows) == ("DONE", 11)
	polymer = check.boards["polymer"]
	assert (polymer.last_state, polymer.last_elapsed_ms, polymer.total_ms, polymer.rows) == ("DONE", 1000, 1000, 11)
	assert (polymer.expected_g, polymer.estimated_g) == (1.0, 1.01)


def test_the_check_takes_the_csv_or_the_sidecar(log, tmp_path):
	path = log.write(tmp_path)
	assert check_run_log(path.with_suffix(".json")) == check_run_log(path)


def test_the_report_names_the_result_and_each_board(log, tmp_path):
	report = check_run_log(log.write(tmp_path)).report()
	assert report[0] == "run log: DONE, 11 rows over 1.05 s; no problems"
	assert report[1].startswith("polymer: DONE at 1000 of 1000 ms, 11 polls, 1.01 g from the edges (1 g expected")


def test_a_wrong_log_reports_its_problems_first(log, tmp_path):
	log.cell(3, "polymer_edges", 0)
	report = check_run_log(log.write(tmp_path)).report()
	assert report[0].startswith("run log: DONE, 11 rows over 1.05 s; 1 problem")
	assert report[1].startswith("problem: row 4: polymer edges went back")


# --- The files -------------------------------------------------------------------------------

def test_a_missing_sidecar_is_a_problem(log, tmp_path):
	path = log.write(tmp_path)
	path.with_suffix(".json").unlink()
	assert check_run_log(path).problems == (f"no sidecar at {path.with_suffix('.json')}",)


def test_a_broken_sidecar_is_a_problem(log, tmp_path):
	path = log.write(tmp_path)
	path.with_suffix(".json").write_text("{", encoding="utf-8")
	(problem,) = check_run_log(path).problems
	assert problem.startswith("the sidecar is not JSON:")


def test_a_missing_csv_is_a_problem(log, tmp_path):
	path = log.write(tmp_path)
	path.unlink()
	assert check_run_log(path.with_suffix(".json")).problems == (f"no CSV at {path}",)


def test_an_unknown_schema_is_a_problem(log, tmp_path):
	log.sidecar["schema"] = 2
	assert problems(log, tmp_path) == ("sidecar schema 2; this check reads schema 1",)


def test_the_sidecar_must_name_its_csv(log, tmp_path):
	log.sidecar["csv"] = "other.csv"
	assert problems(log, tmp_path) == ("the sidecar names other.csv, not run.csv",)


def test_the_columns_must_be_the_run_logs(log, tmp_path):
	path = log.write(tmp_path)
	text = path.read_text(encoding="utf-8").replace("polymer_duty", "polymer_pwm", 1)
	path.write_text(text, encoding="utf-8")
	assert check_run_log(path).problems == ("the CSV columns are not the run log's (schema 1)",)


def test_a_log_that_was_never_closed_says_so(log, tmp_path):
	log.sidecar["ended"] = None
	log.sidecar["result"] = None
	assert problems(log, tmp_path) == ("the log was never closed (no end time in the sidecar)",)


def test_the_row_count_must_match(log, tmp_path):
	log.sidecar["rows"] = 12
	assert problems(log, tmp_path) == ("the sidecar counts 12 rows, the CSV has 11",)


def test_the_duration_must_cover_the_rows(log, tmp_path):
	log.sidecar["duration_s"] = 0.5
	assert problems(log, tmp_path) == ("the sidecar's duration 0.5 s ends before the last row (1 s)",)


# --- Times and counts --------------------------------------------------------------------------

def test_row_times_may_not_go_back(log, tmp_path):
	log.rows[4][0] = 0.25
	assert problems(log, tmp_path) == ("row 5: t_s 0.25 is before the row above (0.3)",)


def test_a_boards_host_times_must_go_forward(log, tmp_path):
	log.cell(4, "colour_host_s", "0.3010")
	assert problems(log, tmp_path) == ("row 5: colour host_s 0.301 does not come after 0.301",)


def test_a_boards_device_time_must_go_forward_unless_it_restarted(log, tmp_path):
	log.cell(4, "polymer_device_us", 10)
	assert problems(log, tmp_path) == ("row 5: polymer device_us went back (5300000 to 10) with no restart flagged",)


def test_edges_may_not_go_back_unless_the_board_restarted(log, tmp_path):
	log.cell(3, "polymer_edges", 0)
	assert problems(log, tmp_path) == ("row 4: polymer edges went back (4 to 0) with no restart flagged",)


def test_a_restart_may_start_the_counts_over_but_ends_the_run(log, tmp_path):
	for column, value in (("colour_device_us", 100_000), ("colour_edges", 0), ("colour_restarted", 1),
						("colour_job_state", "EMPTY"), ("colour_job_elapsed_ms", 0), ("colour_segment", "")):
		log.cell(5, column, value)
	log.abort(5, "the colour board restarted and lost its job")
	log.cell(5, "colour_job_state", "EMPTY")
	assert problems(log, tmp_path) == ()


def test_empty_sides_are_skipped(log, tmp_path):
	for name in ("host_s", "device_us", "edges", "job_state", "job_elapsed_ms", "segment"):
		log.cell(4, f"colour_{name}", "")
	check = check_run_log(log.write(tmp_path))
	assert check.problems == ()
	assert check.boards["colour"].rows == 10


# --- Job states ---------------------------------------------------------------------------------

def test_job_time_may_not_go_back_while_running(log, tmp_path):
	log.cell(5, "polymer_job_elapsed_ms", 350)
	assert problems(log, tmp_path) == ("row 6: polymer job time went back (400 to 350 ms)",)


def test_a_running_board_may_not_go_back_to_ready(log, tmp_path):
	log.cell(5, "colour_job_state", "READY")
	log.cell(5, "colour_segment", "")
	(problem,) = problems(log, tmp_path)
	assert problem == "row 6: colour went from RUNNING to READY with no restart flagged"


def test_a_finished_board_may_not_run_again(log, tmp_path):
	log.cell(8, "colour_job_state", "DONE")
	log.cell(8, "colour_segment", "")
	assert problems(log, tmp_path) == ("row 10: colour went from DONE to RUNNING",)


def test_before_the_run_any_state_may_follow_any(log, tmp_path):
	log.cell(0, "polymer_job_state", "DONE")
	log.cell(0, "polymer_job_elapsed_ms", 5000)
	log.rows.insert(1, [0.05] + cells(0.05, "EMPTY") + cells(0.051, "LOADING"))
	log.sidecar["rows"] = len(log.rows)
	assert problems(log, tmp_path) == ()


def test_a_running_board_reports_a_segment_of_its_job(log, tmp_path):
	log.cell(3, "polymer_segment", 2)
	assert problems(log, tmp_path) == ("row 4: polymer reports segment 2 of a 2-segment job",)


def test_a_running_board_reports_a_segment(log, tmp_path):
	log.cell(3, "polymer_segment", "")
	assert problems(log, tmp_path) == ("row 4: polymer is RUNNING with no segment",)


def test_a_finished_board_reports_no_segment(log, tmp_path):
	log.cell(10, "colour_segment", 1)
	assert problems(log, tmp_path) == ("row 11: colour is DONE but reports segment 1",)


def test_a_done_board_ran_its_whole_job(log, tmp_path):
	log.cell(10, "colour_job_elapsed_ms", 990)
	assert problems(log, tmp_path) == ("colour ended DONE at 990 ms of its 1000 ms job",)


# --- Against the run ------------------------------------------------------------------------------

def test_a_done_run_has_both_boards_done(log, tmp_path):
	log.cell(10, "colour_job_state", "ABORTED")
	assert problems(log, tmp_path) == ("the run ended DONE but colour's last state is ABORTED",)


def test_an_aborted_run_leaves_no_board_running(log, tmp_path):
	log.abort(5)
	log.cell(5, "polymer_job_state", "RUNNING")
	log.cell(5, "polymer_segment", 0)
	assert problems(log, tmp_path) == ("the run ended ABORTED but polymer's last logged state is RUNNING",)


def test_a_board_that_was_lost_may_end_the_log_running(log, tmp_path):
	log.abort(5, "the colour board's link was lost")
	for name in ("host_s", "device_us", "edges", "job_state", "job_elapsed_ms", "segment"):
		log.cell(5, f"colour_{name}", "")
	log.sidecar["events"].append({"t_s": 0.5, "role": "colour", "kind": "link lost", "message": "COM5 lost"})
	assert problems(log, tmp_path) == ()


def test_the_run_states_must_go_in_order(log, tmp_path):
	states = log.sidecar["run"]["states"]
	states[1], states[2] = states[2], states[1]
	assert problems(log, tmp_path) == ("the run's states go IDLE, READY, UPLOADED, RUNNING, DONE",)


def test_an_aborted_runs_states_go_in_order_up_to_the_abort(log, tmp_path):
	log.abort(0, "the colour board refused to start")
	log.sidecar["run"]["states"] = [{"state": s, "t_s": 0.0} for s in ("IDLE", "READY", "ABORTED")]
	log.sidecar["fleet"] = []
	log.cell(0, "polymer_job_state", "READY")
	log.cell(0, "colour_job_state", "READY")
	assert problems(log, tmp_path) == ("the run's states go IDLE, READY, ABORTED",)


def test_a_done_run_went_through_every_state(log, tmp_path):
	del log.sidecar["run"]["states"][3]
	log.sidecar["fleet"] = []
	assert problems(log, tmp_path) == ("the run's states go IDLE, UPLOADED, READY, DONE",)


def test_a_closed_run_ends_in_done_or_aborted(log, tmp_path):
	del log.sidecar["run"]["states"][4]
	assert problems(log, tmp_path) == ("the run's states go IDLE, UPLOADED, READY, RUNNING",)


def test_an_aborted_run_may_end_at_any_state(log, tmp_path):
	log.abort(0, "the colour board refused to start")
	del log.sidecar["run"]["states"][3]
	log.sidecar["fleet"] = []
	log.cell(0, "polymer_job_state", "READY")
	log.cell(0, "colour_job_state", "READY")
	assert problems(log, tmp_path) == ()


def test_the_last_run_state_is_the_result(log, tmp_path):
	log.sidecar["result"] = "ABORTED"
	assert problems(log, tmp_path) == (
		"the result is ABORTED but the run's last state is DONE", "an ABORTED run must have a reason")


def test_a_done_run_has_no_reason(log, tmp_path):
	log.sidecar["run"]["reason"] = "stopped"
	assert problems(log, tmp_path) == ("the run ended DONE but has a reason: stopped",)


def test_a_run_that_ran_has_its_run_command(log, tmp_path):
	log.sidecar["fleet"] = []
	assert problems(log, tmp_path) == ("the run was RUNNING but no JOB_RUN is in the fleet commands",)


def test_the_run_command_reached_both_boards(log, tmp_path):
	log.sidecar["fleet"][0]["boards"]["colour"]["error"] = "timed out"
	assert problems(log, tmp_path) == ("JOB_RUN to colour failed: timed out",)


def test_each_board_has_its_upload_and_grams(log, tmp_path):
	del log.sidecar["boards"]["colour"]["segments"]
	log.sidecar["boards"]["polymer"]["estimated_g"] = None
	assert problems(log, tmp_path) == ("polymer: no estimated_g in the sidecar", "colour: no segments in the sidecar")


def test_each_board_has_its_port_and_firmware(log, tmp_path):
	del log.sidecar["boards"]["polymer"]["firmware"]
	assert problems(log, tmp_path) == ("polymer: no firmware in the sidecar",)


def test_a_log_with_no_run_is_checked_as_polls_only(log, tmp_path):
	log.sidecar["run"] = None
	log.sidecar["result"] = None
	for board in log.sidecar["boards"].values():
		for key in ("segments", "calibration_hash", "expected_g", "estimated_g"):
			del board[key]
	check = check_run_log(log.write(tmp_path))
	assert check.problems == ()
	assert check.boards["colour"].total_ms is None

import csv
import json
from dataclasses import replace

import pytest

from polydriver.calibration import CalibrationStore, rate_for_speed
from polydriver.config import BoardSettings
from polydriver.devices import BoardStatus, Session
from polydriver.devices.fleet import FleetRunError, FleetUploadError
from polydriver.jobs import JobCompileError, StepsCurve
from polydriver.materials import Kind, Material, MaterialRegistry
from polydriver.protocol.constants import JobState, Role, StatusFlag
from polydriver.protocol.constants import Command, NackReason
from polydriver.requests import JobClear, JobRun, JobSegment, RequestNackError, Stop
from polydriver.requests.reports import StatusReport
from polydriver.protocol.serial_worker import LinkLostError
from polydriver.run import DeliveredGrams, Runner, RunState, RunStateError, RunStates, SafetyLimits
from polydriver.telemetry import start_run_log

from fakes import POLYMER_SETTINGS, Boards, make_config, wait_until
from test_job_program import calibrations, short

COLOUR_CONFIG = BoardSettings(kp=0.2, ki=0.1, kd=0.0)
MB_GAINS = dict(kp=1.5, ki=0.8, kd=0.0)


# --- The states ------------------------------------------------------------------------------

class Clock:
	def __init__(self):
		self.now_s = 100.0

	def __call__(self):
		return self.now_s


def test_a_run_goes_idle_uploaded_ready_running_done():
	clock = Clock()
	states = RunStates(clock)
	for state in (RunState.UPLOADED, RunState.READY, RunState.RUNNING, RunState.DONE):
		clock.now_s += 1
		states.move(state)
	assert states.state is RunState.DONE and states.finished
	assert states.history == [(RunState.IDLE, 100.0), (RunState.UPLOADED, 101.0), (RunState.READY, 102.0),
							(RunState.RUNNING, 103.0), (RunState.DONE, 104.0)]


@pytest.mark.parametrize("path, wrong", [
	((), RunState.RUNNING),
	((), RunState.READY),
	((RunState.UPLOADED,), RunState.UPLOADED),
	((RunState.UPLOADED,), RunState.DONE),
	((RunState.UPLOADED, RunState.READY), RunState.DONE),
	((RunState.UPLOADED, RunState.READY, RunState.RUNNING), RunState.READY),
	((RunState.UPLOADED, RunState.READY, RunState.RUNNING, RunState.DONE), RunState.ABORTED),
])
def test_a_state_only_moves_forward(path, wrong):
	states = RunStates(Clock())
	for state in path:
		states.move(state)
	with pytest.raises(RunStateError, match=f"a run that is {states.state} cannot become {wrong}"):
		states.move(wrong)


@pytest.mark.parametrize("path", [(), (RunState.UPLOADED,), (RunState.UPLOADED, RunState.READY),
								(RunState.UPLOADED, RunState.READY, RunState.RUNNING)])
def test_any_unfinished_run_can_be_aborted(path):
	states = RunStates(Clock())
	for state in path:
		states.move(state)
	states.move(RunState.ABORTED)
	assert states.finished


# --- Grams from the edge count -----------------------------------------------------------------

def curve(points=((10.0, 0.5), (20.0, 1.0), (40.0, 3.0))):
	store = CalibrationStore()
	for speed_hz, rate_g_s in points:
		store.add_measurement("B", "PP", speed_hz, rate_g_s * 10, 10.0)
	return store.curve("B", "PP")


def report(job_state, elapsed_ms=0, edges=0, speed_hz=0.0):
	return StatusReport(speed_hz, speed_hz, 0, StatusFlag(0), 0, edges % 2**32, 0, 0, 0, 0, job_state, None, elapsed_ms)


def per_edge(speed_hz):
	return rate_for_speed(curve(), speed_hz) / speed_hz


def test_grams_are_the_edges_times_the_grams_per_edge_at_the_boards_speed():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 5000, speed_hz=0.0))
	grams.add(report(JobState.RUNNING, 1000, 5000 + 20, speed_hz=20.0))
	grams.add(report(JobState.RUNNING, 3000, 5000 + 20 + 60, speed_hz=30.0))
	assert grams.grams == pytest.approx(20 * per_edge(10.0) + 60 * per_edge(25.0))
	assert (grams.counted_from_ms, grams.counted_to_ms) == (0, 3000)


def test_the_edge_count_may_wrap():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 2**32 - 10, speed_hz=20.0))
	grams.add(report(JobState.RUNNING, 1000, 2**32 + 10, speed_hz=20.0))
	assert grams.grams == pytest.approx(20 * per_edge(20.0))


def test_outside_the_calibration_the_nearest_ends_grams_per_edge_are_used():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 0, speed_hz=5.0))
	grams.add(report(JobState.RUNNING, 1000, 5, speed_hz=5.0))
	grams.add(report(JobState.RUNNING, 2000, 5 + 80, speed_hz=80.0))
	assert grams.grams == pytest.approx(0.5 / 10 * 5 + 3.0 / 40 * 80)


def test_the_poll_that_ends_the_job_uses_the_speed_before_it():
	# 30 Hz, where the calibration bends: grams per edge differ between 15 and 30 Hz.
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 0, speed_hz=30.0))
	grams.add(report(JobState.DONE, 1000, 30, speed_hz=0.0))
	assert grams.grams == pytest.approx(30 * per_edge(30.0))
	assert per_edge(30.0) != pytest.approx(per_edge(15.0))


def test_edges_after_the_job_ended_are_not_counted():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 0, speed_hz=20.0))
	grams.add(report(JobState.DONE, 1000, 20, speed_hz=3.0))
	grams.add(report(JobState.DONE, 1000, 26, speed_hz=1.0))
	assert grams.grams == pytest.approx(20 * per_edge(20.0))


def test_with_no_speed_measured_the_edges_over_the_time_stand_in():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 0, speed_hz=0.0))
	grams.add(report(JobState.RUNNING, 1000, 30, speed_hz=0.0))
	assert grams.grams == pytest.approx(30 * per_edge(30.0))
	grams.add(report(JobState.RUNNING, 2000, 30, speed_hz=0.0))
	assert grams.grams == pytest.approx(30 * per_edge(30.0)), "a standing screw adds nothing"


def test_without_the_ready_count_the_grams_start_at_the_first_poll():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.RUNNING, 400, 1000, speed_hz=20.0))
	grams.add(report(JobState.RUNNING, 1400, 1020, speed_hz=20.0))
	assert grams.grams == pytest.approx(20 * per_edge(20.0))
	assert grams.counted_from_ms == 400


def test_a_new_ready_count_starts_over():
	grams = DeliveredGrams(curve())
	grams.add(report(JobState.READY, 0, 0, speed_hz=20.0))
	grams.add(report(JobState.RUNNING, 1000, 20, speed_hz=20.0))
	grams.add(report(JobState.READY, 0, 500))
	assert grams.grams == 0.0


# --- A run on two boards ----------------------------------------------------------------------

def materials(mb_gains=MB_GAINS):
	registry = MaterialRegistry()
	registry.put(Material("PP", Kind.POLYMER, dispenser="B"))
	registry.put(Material("MB-red", Kind.MASTERBATCH, dispenser="A", **mb_gains))
	return registry


class Rig:
	def __init__(self, tmp_path, scale=100.0):
		self.boards = Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR})
		self.config = make_config(boards={Role.POLYMER: POLYMER_SETTINGS, Role.COLOUR: COLOUR_CONFIG}, base_dir=tmp_path)
		self.session = Session(self.config, link_factory=self.boards)
		self.session.connect(Role.POLYMER)
		self.session.connect(Role.COLOUR)
		for link in self.boards.links.values():
			link.job_time_scale = scale
		self.store = calibrations()
		self.job = short(self.store)

	def runner(self, job=None, **kwargs):
		return Runner(self.session, job or self.job, materials(), self.store, **kwargs)

	def link(self, role):
		return self.boards.links["COM6" if role is Role.POLYMER else "COM5"]


@pytest.fixture
def rig(tmp_path):
	rig = Rig(tmp_path)
	yield rig
	rig.session.close()


def test_a_60_s_profile_runs_on_two_boards_and_ends_in_done(rig):
	runner = rig.runner()
	program = runner.upload()
	assert runner.state is RunState.READY
	runner.start()
	assert runner.state is RunState.RUNNING
	assert runner.wait(5.0) is RunState.DONE
	assert all(rig.link(role).job_state is JobState.DONE for role in (Role.POLYMER, Role.COLOUR))
	progress = runner.progress()
	assert progress.elapsed_s == pytest.approx(60.0) and progress.fraction == 1.0
	for role, channel in ((Role.POLYMER, program.polymer), (Role.COLOUR, program.colour)):
		assert getattr(progress, channel.role).delivered_g == pytest.approx(channel.expected_grams, rel=0.01)


def test_the_run_is_done_when_both_boards_are(rig):
	rig.link(Role.POLYMER).job_time_scale, rig.link(Role.COLOUR).job_time_scale = 100.0, 40.0
	runner = rig.runner()
	runner.upload()
	runner.start()
	assert wait_until(lambda: runner.progress().polymer.job_state is JobState.DONE)
	assert runner.state is RunState.RUNNING
	assert runner.wait(5.0) is RunState.DONE
	assert rig.link(Role.COLOUR).job_state is JobState.DONE


def test_the_states_on_the_way_are_kept_with_their_times(rig):
	runner = rig.runner()
	runner.upload()
	runner.start()
	runner.wait(5.0)
	assert [state for state, _ in runner.history] == [RunState.IDLE, RunState.UPLOADED, RunState.READY,
													RunState.RUNNING, RunState.DONE]
	times = [t for _, t in runner.history]
	assert times == sorted(times)


def test_a_job_that_does_not_compile_never_reaches_the_boards(rig):
	store = calibrations(mb=((3.0, 0.02), (5.0, 0.03), (8.0, 0.05), (15.0, 0.09)))
	runner = Runner(rig.session, short(store), materials(), store)
	with pytest.raises(JobCompileError, match="does not fit"):
		runner.upload()
	assert runner.state is RunState.IDLE
	assert rig.link(Role.POLYMER).count(JobClear) == rig.link(Role.COLOUR).count(JobClear) == 0


def test_the_material_gains_are_used_for_the_run_and_the_config_put_back(rig):
	runner = rig.runner()
	runner.upload()
	assert rig.link(Role.COLOUR).gains == (1.5, pytest.approx(0.8), 0.0)
	assert rig.link(Role.POLYMER).gains == (pytest.approx(POLYMER_SETTINGS.kp), pytest.approx(POLYMER_SETTINGS.ki),
											pytest.approx(POLYMER_SETTINGS.kd))
	runner.start()
	runner.wait(5.0)
	assert wait_until(lambda: rig.link(Role.COLOUR).gains == (pytest.approx(0.2), pytest.approx(0.1), 0.0))


def test_the_boards_own_segment_limit_is_used(rig):
	rig.link(Role.COLOUR).version = replace(rig.link(Role.COLOUR).version, max_segments=2)
	with pytest.raises(JobCompileError, match="colour: .* the board holds 2"):
		rig.runner().upload()


def test_a_failed_upload_leaves_the_boards_as_they_were(rig):
	rig.link(Role.COLOUR).refuse[JobSegment] = RequestNackError(Command.JOB_SEGMENT, NackReason.BAD_INDEX)
	rig.link(Role.COLOUR).delays_s[JobSegment] = 0.1
	runner = rig.runner()
	with pytest.raises(FleetUploadError):
		runner.upload()
	assert runner.state is RunState.IDLE
	assert rig.link(Role.COLOUR).gains == (pytest.approx(0.2), pytest.approx(0.1), 0.0)
	assert runner.progress().polymer.job_state is None, "an IDLE run shows no board"
	rig.session.poll_now(Role.POLYMER)
	assert runner.progress().polymer.job_state is None, "still listening"


def test_both_boards_must_report_ready(rig):
	colour = rig.link(Role.COLOUR)
	real_status = colour._status
	colour._status = lambda: replace(real_status(), job_state=JobState.EMPTY)
	runner = rig.runner()
	runner.upload()
	assert runner.state is RunState.UPLOADED
	colour._status = real_status
	assert wait_until(lambda: runner.state is RunState.READY)


def test_one_ready_board_is_not_a_ready_run(rig):
	runner = rig.runner()
	runner._states.move(RunState.UPLOADED)
	runner.record_status(Role.POLYMER, BoardStatus(report(JobState.READY), 0, False), 0.0)
	assert runner.state is RunState.UPLOADED
	runner.record_status(Role.COLOUR, BoardStatus(report(JobState.LOADING), 0, False), 0.0)
	assert runner.state is RunState.UPLOADED
	runner.record_status(Role.COLOUR, BoardStatus(report(JobState.READY), 0, False), 0.0)
	assert runner.state is RunState.READY


def test_a_run_that_a_board_refuses_to_start_ends_aborted(rig):
	rig.link(Role.COLOUR).refuse[JobRun] = RequestNackError(Command.JOB_RUN, NackReason.WRONG_STATE)
	runner = rig.runner()
	runner.upload()
	with pytest.raises(FleetRunError) as raised:
		runner.start()
	assert runner.wait(2.0) is RunState.ABORTED
	assert runner.reason == str(raised.value) and "did not start" in runner.reason


def test_after_the_run_the_runner_stops_listening(rig):
	runner = rig.runner()
	runner.upload()
	runner.start()
	runner.wait(5.0)
	rig.link(Role.COLOUR).restart()
	rig.session.poll_now(Role.COLOUR)
	assert runner.progress().colour.job_state is JobState.DONE


def test_start_before_the_boards_are_ready_is_refused(rig):
	runner = rig.runner()
	with pytest.raises(RunStateError, match="a run that is IDLE cannot become RUNNING; upload the job first"):
		runner.start()


def test_stop_aborts_both_boards_and_keeps_the_grams_so_far(rig):
	rig.link(Role.POLYMER).job_time_scale = rig.link(Role.COLOUR).job_time_scale = 20.0
	runner = rig.runner()
	runner.upload()
	runner.start()
	assert wait_until(lambda: runner.progress().elapsed_s > 10)
	runner.stop()
	assert runner.wait(2.0) is RunState.ABORTED
	assert all(rig.link(role).job_state is JobState.ABORTED for role in (Role.POLYMER, Role.COLOUR))
	progress = runner.progress()
	assert 10 < progress.elapsed_s < 60
	assert 0 < progress.colour.delivered_g < runner.program.colour.expected_grams
	assert rig.link(Role.COLOUR).gains == (pytest.approx(0.2), pytest.approx(0.1), 0.0), "config gains back"


def test_stop_before_the_start_abandons_the_run(rig):
	runner = rig.runner()
	runner.upload()
	runner.stop()
	assert runner.wait(1.0) is RunState.ABORTED
	assert rig.link(Role.POLYMER).count(Stop) == rig.link(Role.COLOUR).count(Stop) == 1


def test_progress_while_running(rig):
	rig.link(Role.POLYMER).job_time_scale = rig.link(Role.COLOUR).job_time_scale = 10.0
	runner = rig.runner()
	runner.upload()
	before = runner.progress()
	assert (before.state, before.elapsed_s, before.fraction, before.total_s) == (RunState.READY, 0.0, 0.0, 60.0)
	runner.start()
	assert wait_until(lambda: runner.progress().elapsed_s > 5)
	during = runner.progress()
	assert during.state is RunState.RUNNING and 0 < during.fraction < 1
	assert during.colour.job_state is JobState.RUNNING and during.colour.segment_index is not None
	runner.stop()
	runner.wait(2.0)


def test_the_runs_elapsed_time_is_the_furthest_boards(rig):
	rig.link(Role.POLYMER).job_time_scale, rig.link(Role.COLOUR).job_time_scale = 20.0, 10.0
	runner = rig.runner()
	runner.upload()
	runner.start()
	assert wait_until(lambda: runner.progress().polymer.elapsed_s > runner.progress().colour.elapsed_s + 2)
	progress = runner.progress()
	assert progress.elapsed_s == progress.polymer.elapsed_s
	runner.stop()
	runner.wait(2.0)


# --- Safety (C8.5) ---------------------------------------------------------------------------------

def running(rig, scale=10.0, **kwargs):
	"""A runner whose 60 s job is running on both boards, on job clocks scale times real time."""
	rig.link(Role.POLYMER).job_time_scale = rig.link(Role.COLOUR).job_time_scale = scale
	runner = rig.runner(**kwargs)
	runner.upload()
	runner.start()
	assert wait_until(lambda: runner.progress().elapsed_s > 1)
	return runner


def both_stopped(rig):
	return all(rig.link(role).count(Stop) >= 1 and rig.link(role).job_state is not JobState.RUNNING
			for role in (Role.POLYMER, Role.COLOUR))


def test_a_lost_link_stops_the_other_board(rig):
	runner = running(rig)
	rig.link(Role.COLOUR).fail_with = LinkLostError("COM5 lost")
	assert runner.wait(3.0) is RunState.ABORTED
	assert runner.reason.startswith("the colour board's link was lost: COM5 lost")
	assert rig.link(Role.POLYMER).count(Stop) == 1 and rig.link(Role.POLYMER).job_state is JobState.ABORTED


def test_a_board_that_aborts_its_job_stops_the_other(rig):
	runner = running(rig)
	rig.link(Role.COLOUR).abort(StatusFlag.COMMS_LOST)
	assert runner.wait(3.0) is RunState.ABORTED
	assert "the colour board aborted its job at" in runner.reason
	assert runner.reason.endswith(": it heard nothing from the host for its comms timeout")
	assert both_stopped(rig)


def test_a_speed_that_stays_off_stops_both_boards(rig):
	runner = running(rig)
	rig.link(Role.COLOUR).speed_factor = 0.5
	assert runner.wait(3.0) is RunState.ABORTED
	assert runner.reason.startswith("the colour speed was off by more than 25 %")
	assert both_stopped(rig)


def test_an_encoder_stall_warns_at_once_and_stops_the_run_once_it_lasts(rig):
	runner = running(rig)
	rig.link(Role.COLOUR).stalled = True
	assert runner.wait(3.0) is RunState.ABORTED
	assert runner.reason.endswith("(encoder stalled)")
	assert [w for w in runner.progress().warnings if w.startswith("the colour encoder stalled at")]
	assert both_stopped(rig)


def test_a_board_that_falls_silent_stops_the_run(rig):
	runner = running(rig, limits=SafetyLimits(silence_s=0.3))
	rig.link(Role.COLOUR).status_timeouts = 10**6
	assert runner.wait(3.0) is RunState.ABORTED
	assert runner.reason.startswith("no STATUS from the colour board for")
	assert rig.link(Role.POLYMER).count(Stop) == 1 and rig.link(Role.POLYMER).job_state is JobState.ABORTED


def test_a_job_that_never_ends_is_stopped_after_its_time(rig):
	job = short(rig.store, amount_given={"duration_s": 1.0, "total_flow_g_s": 1.5}, hold_start_s=0.1, hold_end_s=0.1)
	rig.link(Role.POLYMER).job_time_scale = rig.link(Role.COLOUR).job_time_scale = 1.0
	runner = rig.runner(job=job, limits=SafetyLimits(overtime_s=0.3))
	runner.upload()
	runner.start()
	rig.link(Role.POLYMER).freeze_job()
	rig.link(Role.COLOUR).freeze_job()
	assert runner.wait(3.0) is RunState.ABORTED
	assert runner.reason == "the job has run more than 0.3 s over its 1 s"
	assert both_stopped(rig)


def test_a_board_that_restarts_mid_run_stops_the_run(rig):
	runner = running(rig)
	rig.link(Role.COLOUR).restart()
	assert runner.wait(3.0) is RunState.ABORTED
	assert runner.reason.startswith("the colour board")
	assert "restarted" in runner.reason or "no longer runs its job" in runner.reason
	assert rig.link(Role.POLYMER).count(Stop) == 1


def test_a_finished_run_has_no_reason_and_no_warnings(rig):
	runner = rig.runner()
	runner.upload()
	runner.start()
	assert runner.wait(5.0) is RunState.DONE
	assert (runner.reason, runner.progress().warnings) == (None, ())


def test_a_stop_says_who_stopped_the_run(rig):
	runner = running(rig)
	# The polymer STOP answers late, so the colour board's ABORTED is polled while stop() waits.
	rig.link(Role.POLYMER).delays_s[Stop] = 0.2
	runner.stop()
	assert runner.wait(2.0) is RunState.ABORTED
	assert runner.reason == "stopped by the operator"
	assert rig.link(Role.POLYMER).count(Stop) == rig.link(Role.COLOUR).count(Stop) == 1


def test_the_silence_limit_follows_the_comms_timeout(rig):
	rig.session.reload_config(replace(rig.config, comms_timeout_s=1.5))
	assert rig.runner().limits.silence_s == 1.5
	rig.session.reload_config(replace(rig.config, comms_timeout_s=0.0))
	assert rig.runner().limits.silence_s == SafetyLimits().silence_s
	assert rig.runner(limits=SafetyLimits(silence_s=0.7)).limits.silence_s == 0.7


def test_the_run_log_gets_the_reason_the_warnings_and_the_limits(rig):
	log = start_run_log(rig.session, rig.config)
	runner = running(rig, run_log=log)
	rig.link(Role.COLOUR).stalled = True
	runner.wait(3.0)
	run = json.loads(log.sidecar_path.read_text(encoding="utf-8"))["run"]
	assert run["reason"] == runner.reason and run["reason"].endswith("(encoder stalled)")
	assert run["warnings"] and run["warnings"][0].startswith("the colour encoder stalled at")
	assert run["limits"] == {"tracking_fraction": 0.25, "tracking_floor_hz": 1.0, "tracking_s": 3.0,
							"overtime_s": 2.0, "silence_s": 2.0}


# --- The run log -----------------------------------------------------------------------------------

def test_the_run_log_gets_the_job_the_segments_the_grams_and_the_result(rig):
	log = start_run_log(rig.session, rig.config, note="test run")
	runner = rig.runner(run_log=log)
	program = runner.upload()
	runner.start()
	runner.wait(5.0)
	side = json.loads(log.sidecar_path.read_text(encoding="utf-8"))
	assert side["result"] == "DONE" and side["ended"] is not None
	assert side["job"]["name"] == rig.job.name and side["job"]["profile"]["curve"] == "linear"
	colour = side["boards"]["colour"]
	assert len(colour["segments"]) == len(program.colour.segments)
	assert colour["calibration_hash"] == program.colour.curve_hash
	assert colour["settings"]["kp"] == 1.5
	assert colour["expected_g"] == pytest.approx(program.colour.expected_grams)
	assert colour["estimated_g"] == pytest.approx(program.colour.expected_grams, rel=0.01)
	assert [entry["command"] for entry in side["fleet"]] == ["JOB_RUN"]
	assert side["run"]["error_limit"] == 0.005
	assert [entry["state"] for entry in side["run"]["states"]] == ["IDLE", "UPLOADED", "READY", "RUNNING", "DONE"]


def test_a_stopped_run_logs_the_stop_and_aborted(rig):
	log = start_run_log(rig.session, rig.config)
	runner = rig.runner(run_log=log)
	runner.upload()
	runner.start()
	runner.stop()
	runner.wait(2.0)
	side = json.loads(log.sidecar_path.read_text(encoding="utf-8"))
	assert side["result"] == "ABORTED"
	assert [entry["command"] for entry in side["fleet"]] == ["JOB_RUN", "STOP"]


def last_logged_states(log):
	with log.csv_path.open(encoding="utf-8", newline="") as file:
		rows = list(csv.DictReader(file))
	return {name: next(row[f"{name}_job_state"] for row in reversed(rows) if row[f"{name}_job_state"])
			for name in ("polymer", "colour")}


def slow_polls(rig):
	"""2 Hz, so no ordinary poll is likely to land between the end of the run and the log's close."""
	rig.session.reload_config(replace(rig.config, logging=replace(rig.config.logging, poll_hz=2.0)))


def test_after_a_safety_abort_the_log_ends_with_both_boards_stopped(rig):
	slow_polls(rig)
	log = start_run_log(rig.session, rig.config)
	runner = running(rig, run_log=log)
	rig.link(Role.COLOUR).speed_factor = 0.5
	assert runner.wait(5.0) is RunState.ABORTED
	assert last_logged_states(log) == {"polymer": "ABORTED", "colour": "ABORTED"}


def test_after_an_operator_stop_the_log_ends_with_both_boards_stopped(rig):
	slow_polls(rig)
	log = start_run_log(rig.session, rig.config)
	runner = running(rig, run_log=log)
	runner.stop()
	assert runner.wait(2.0) is RunState.ABORTED
	assert last_logged_states(log) == {"polymer": "ABORTED", "colour": "ABORTED"}


def test_after_a_stop_the_progress_shows_the_boards_final_state(rig):
	slow_polls(rig)
	runner = running(rig)
	runner.stop()
	assert runner.wait(2.0) is RunState.ABORTED
	progress = runner.progress()
	assert (progress.polymer.job_state, progress.colour.job_state) == (JobState.ABORTED, JobState.ABORTED)


def test_a_board_lost_at_the_end_does_not_keep_the_other_out_of_the_log(rig, caplog):
	slow_polls(rig)
	log = start_run_log(rig.session, rig.config)
	runner = running(rig, run_log=log)
	rig.link(Role.COLOUR).fail_with = LinkLostError("COM5 lost")
	assert runner.wait(5.0) is RunState.ABORTED
	assert last_logged_states(log)["polymer"] == "ABORTED"
	assert json.loads(log.sidecar_path.read_text(encoding="utf-8"))["result"] == "ABORTED"
	# The lost board is no longer connected, so the poll after the run leaves it out.
	assert not [r for r in caplog.records if "the poll after the run failed" in r.getMessage()]


def test_a_steps_job_runs_too(rig):
	runner = rig.runner(job=replace(rig.job, curve=StepsCurve(4)))
	runner.upload()
	runner.start()
	assert runner.wait(5.0) is RunState.DONE

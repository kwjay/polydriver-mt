"""The bench tool (tools/bench.py) against a small in-memory board."""

import csv
import importlib.util
import io
from pathlib import Path

import pytest

from polydriver.devices import BoardNotRespondingError, LinkCounters
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Role, StatusFlag
from polydriver.requests import (
	GetId, JobClear, JobCommit, JobInfo, JobRun, JobSegment, RequestNackError, RequestSettings, RequestStatus,
	SetCommsTimeout, SetId, SetMode, SetPid, SetPwm, SetSpeed, SettingsReport, StatusReport, Stop, VersionReport,
)

BENCH_PATH = Path(__file__).resolve().parents[1] / "tools" / "bench.py"
spec = importlib.util.spec_from_file_location("bench", BENCH_PATH)
bench_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench_module)


class SimLink:
	"""Answers like the firmware for the requests the bench tool sends."""

	def __init__(self):
		self.version = VersionReport(3, 3, 0, 0, 32)
		self.sent = []
		self.mode = Mode.CLOSED_LOOP
		self.duty = 0
		self.target_hz = 0.0
		self.configured = False
		self.timeout_ms = 0
		self.role = Role.NONE
		self.device_time_us = 0
		self.restart_next_status = False
		self.closed = False

	def speed_hz(self) -> float:
		if self.mode is Mode.OPEN_LOOP:
			return max(0.0, (self.duty - 120) * 0.5)
		return self.target_hz

	def call(self, request, timeout_s=None):
		self.sent.append(request)
		match request:
			case SetMode(mode=mode):
				self.mode, self.duty, self.target_hz = Mode(mode), 0, 0.0
			case SetPwm(duty=duty):
				if self.mode is not Mode.OPEN_LOOP:
					raise RequestNackError(Command.SET_PWM, NackReason.WRONG_MODE)
				self.duty = duty
			case SetSpeed(target_speed_hz=target):
				if self.mode is not Mode.CLOSED_LOOP:
					raise RequestNackError(Command.SET_SPEED, NackReason.WRONG_MODE)
				if target > 0 and not self.configured:
					raise RequestNackError(Command.SET_SPEED, NackReason.NOT_CONFIGURED)
				self.target_hz = target
			case SetPid():
				self.configured = True
			case SetCommsTimeout(timeout_ms=timeout_ms):
				self.timeout_ms = timeout_ms
			case SetId(role=role):
				self.role = Role(role)
			case GetId():
				return self.role
			case Stop():
				self.duty, self.target_hz = 0, 0.0
			case JobCommit(segment_count=count, total_ms=total):
				return JobInfo(JobState.READY, count, total)
			case RequestSettings():
				return SettingsReport(1.0, 0.0, 0.0, self.target_hz, self.timeout_ms, self.mode, self.duty, 1.0, 0.0, 0.0)
			case RequestStatus():
				if self.restart_next_status:
					self.device_time_us = 1000
					self.restart_next_status = False
				else:
					self.device_time_us += 100_000
				flags = StatusFlag(0) if self.configured else StatusFlag.UNCONFIGURED
				if self.mode is Mode.OPEN_LOOP:
					flags |= StatusFlag.OPEN_LOOP
				return StatusReport(self.speed_hz(), self.speed_hz(), self.duty, flags, self.device_time_us, 0, 0, 10010,
								0, 0, JobState.EMPTY, None, 0)
		return None

	@property
	def stats(self):
		return LinkCounters(frames_sent=len(self.sent), retries=0, reply_timeouts=0, late_replies=0)

	def close(self):
		self.closed = True


@pytest.fixture
def sim():
	return SimLink()


@pytest.fixture
def run(sim):
	out = io.StringIO()
	opened = []

	def factory(port):
		opened.append(port)
		return sim

	shell = bench_module.Bench(link_factory=factory, stdout=out, stdin=io.StringIO(), keepalive_s=None)

	def run_commands(*lines):
		out.seek(0)
		out.truncate()
		results = [shell.onecmd(line) for line in lines]
		return out.getvalue(), results

	run_commands.shell = shell
	run_commands.opened = opened
	return run_commands


def read_csv(path: Path) -> list[dict]:
	with path.open(newline="") as file:
		return list(csv.DictReader(file))


def connected(run):
	run("connect COM5")
	return run


def test_connect_shows_the_board(run):
	text, _ = run("connect COM5")
	assert run.opened == ["COM5"]
	assert "protocol 3, firmware 3.0.0" in text
	assert "role NONE" in text
	assert "UNCONFIGURED" in text


def test_a_board_that_does_not_answer_is_reported(run):
	def factory(port):
		raise BoardNotRespondingError(f"{port}: no answer")

	run.shell._link_factory = factory
	text, _ = run("connect COM9")
	assert "COM9: no answer" in text
	assert run.shell.board is None


def test_commands_need_a_connection_first(run):
	text, _ = run("pwm 10")
	assert "not connected" in text


def test_pwm_in_closed_loop_gets_a_hint(run, sim):
	text, _ = connected(run)("pwm 100")
	assert "WRONG_MODE" in text
	assert "mode open" in text


def test_speed_before_gains_gets_a_hint(run):
	text, _ = connected(run)("speed 20")
	assert "NOT_CONFIGURED" in text
	assert "pid KP KI KD" in text


def test_open_loop_pwm(run, sim):
	text, _ = connected(run)("mode open", "pwm 128")
	assert sim.duty == 128
	assert "50.2 %" in text


def test_closed_loop_speed_after_pid(run, sim):
	connected(run)("pid 1.5 0.2 0", "speed 25")
	assert SetPid(1.5, 0.2, 0.0) in sim.sent
	assert sim.target_hz == 25.0


def test_bad_arguments_print_the_usage_and_keep_the_shell(run):
	text, _ = connected(run)("mode sideways", "pwm lots", "pid 1 2")
	assert text.count("usage:") == 3


def test_timeout_in_seconds_and_off(run, sim):
	connected(run)("timeout 1.5")
	assert sim.timeout_ms == 1500
	run("timeout off")
	assert sim.timeout_ms == 0


def test_role_is_read_and_stored(run, sim):
	text, _ = connected(run)("id colour")
	assert sim.role is Role.COLOUR
	assert "role COLOUR" in text


def test_sweep_steps_the_duty_and_ends_at_zero(run, sim, tmp_path):
	path = tmp_path / "sweep.csv"
	text, _ = connected(run)("mode open", f"sweep 120 160 20 0.15 {path}")
	duties = [r.duty for r in sim.sent if isinstance(r, SetPwm)]
	assert duties == [120, 140, 160, 0]
	rows = read_csv(path)
	assert [int(r["duty"]) for r in rows] == [120, 140, 160]
	assert float(rows[2]["mean_raw_hz"]) == pytest.approx(20.0)
	assert "duty back to 0" in text


def test_a_downward_sweep(run, sim):
	connected(run)("mode open", "sweep 160 120 -20 0.05")
	assert [r.duty for r in sim.sent if isinstance(r, SetPwm)] == [160, 140, 120, 0]


def test_sweep_needs_open_loop(run, sim):
	text, _ = connected(run)("sweep 100 200 10 1")
	assert "mode open" in text
	assert not any(isinstance(r, SetPwm) for r in sim.sent)


def test_sweep_refuses_a_step_pointing_away(run):
	text, _ = connected(run)("mode open", "sweep 100 200 -10 1")
	assert "usage:" in text


def test_watch_for_a_time_writes_csv(run, tmp_path):
	path = tmp_path / "watch.csv"
	text, _ = connected(run)(f"watch 50 0.1 {path}")
	rows = read_csv(path)
	assert len(rows) >= 3
	assert rows[0]["flags"] == str(int(StatusFlag.UNCONFIGURED))
	assert "raw" in text and "Hz" in text


def test_a_board_restart_is_announced(run, sim):
	connected(run)("status")
	sim.restart_next_status = True
	text, _ = run("status")
	assert "the board restarted" in text


def test_job_is_built_shown_uploaded_and_run(run, sim):
	text, _ = connected(run)("job new", "job add duty 64 5", "job add duty 200 10 ramp", "job upload", "job run")
	assert JobClear() in sim.sent
	assert JobSegment(0, 64.0, 5000, duty=True) in sim.sent
	assert JobSegment(1, 200.0, 10000, ramp=True, duty=True) in sim.sent
	assert JobCommit(2, 15000) in sim.sent
	assert JobRun() in sim.sent
	assert "ramp to 200 duty for 10 s" in text
	assert "READY, 2 segments, 15000 ms" in text


def test_an_empty_job_is_not_uploaded(run, sim):
	text, _ = connected(run)("job new", "job upload")
	assert "empty" in text
	assert not any(isinstance(r, JobCommit) for r in sim.sent)


def test_quit_sends_stop_and_closes(run, sim):
	_, results = connected(run)("quit")
	assert results == [True]
	assert sim.sent[-1] == Stop()
	assert sim.closed


def test_ctrl_c_sends_stop(run, sim):
	connected(run)
	run.shell.emergency_stop()
	assert sim.sent[-1] == Stop()


def test_stats(run):
	text, _ = connected(run)("stats")
	assert "frames sent" in text
	assert "board: dropped frames 0, bad CRC 0" in text


@pytest.mark.parametrize("line, expected", [
	(r"10 2 C:\data\run.csv", ["10", "2", r"C:\data\run.csv"]),
	(r'10 2 "C:\my data\run 1.csv"', ["10", "2", r"C:\my data\run 1.csv"]),
	("COM5", ["COM5"]),
	("", []),
])
def test_arguments_keep_windows_backslashes_and_quoted_spaces(line, expected):
	assert bench_module.split_args(line) == expected

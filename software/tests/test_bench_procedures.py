"""The sweep and step test on two simulated boards (polydriver.sim), in real time. The simulated
motor turns from duty 128 at 0.6 Hz per duty step (MotorModel), so its feedforward is offset 128,
gain 1/0.6 duty/Hz."""

import csv
import threading
import time

import pytest

from polydriver.analysis import fit_feedforward, load_sweep
from polydriver.bench import Cancel, Cancelled, run_step_test, run_sweep
from polydriver.config import BoardSettings, Config, LoggingSettings
from polydriver.devices import NotConnectedError, Session
from polydriver.protocol.constants import Mode, Role
from polydriver.sim import SimBench
from polydriver.telemetry import start_run_log

from fakes import wait_until

GAINS = BoardSettings(kp=0.5, ki=2.0, kd=0.0, alpha=0.5, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.67)


@pytest.fixture
def bench():
	return SimBench.pair()


@pytest.fixture
def session(bench, tmp_path):
	config = Config(ports=bench.ports, comms_timeout_s=1.0, boards={Role.POLYMER: GAINS, Role.COLOUR: GAINS},
					logging=LoggingSettings(poll_hz=50.0), base_dir=tmp_path)
	with Session(config, link_factory=bench.link_factory) as session:
		session.connect(Role.POLYMER)
		session.connect(Role.COLOUR)
		yield session


def sim_state(bench, role):
	return bench[bench.ports[role]].state()


# --- Cancel ---------------------------------------------------------------------------------------

def test_a_command_after_cancel_is_refused():
	cancel = Cancel()
	cancel.cancel()
	with pytest.raises(Cancelled):
		with cancel.command():
			pytest.fail("ran after cancel")


def test_cancel_waits_for_a_command_in_flight():
	cancel = Cancel()
	inside = threading.Event()
	order = []

	def command():
		with cancel.command():
			inside.set()
			time.sleep(0.1)
			order.append("command done")

	thread = threading.Thread(target=command)
	thread.start()
	assert inside.wait(1.0)
	cancel.cancel()
	order.append("cancel returned")
	thread.join(1.0)
	assert order == ["command done", "cancel returned"]


def test_wait_returns_as_soon_as_cancelled():
	cancel = Cancel()
	threading.Timer(0.05, cancel.cancel).start()
	started = time.monotonic()
	assert cancel.wait(5.0)
	assert time.monotonic() - started < 1.0
	assert cancel.is_set


# --- Sweep ----------------------------------------------------------------------------------------

def test_a_sweep_of_the_simulated_motor_gives_its_feedforward(session, bench, tmp_path):
	path = tmp_path / "sweep.csv"
	steps = []
	result = run_sweep(session, Role.POLYMER, [125, 140, 160, 180, 200], 1.2, path=path,
						on_step=lambda i, n, row: steps.append((i, n, row.duty)))
	assert not result.ended_early and result.cleanup_problem is None
	assert steps == [(0, 5, 125), (1, 5, 140), (2, 5, 160), (3, 5, 180), (4, 5, 200)]
	assert not result.rows[0].turning
	fit = fit_feedforward(result.rows)
	assert fit.start_duty == 140
	assert fit.ff_offset_duty == pytest.approx(128.0, abs=4.0)
	assert fit.ff_gain_duty_per_hz == pytest.approx(1 / 0.6, rel=0.08)
	assert load_sweep(path) == list(result.rows)


def test_a_sweep_puts_duty_0_and_closed_loop_back(session, bench):
	run_sweep(session, Role.POLYMER, [150], 0.3)
	assert wait_until(lambda: sim_state(bench, Role.POLYMER).duty == 0, 1.0)
	assert sim_state(bench, Role.POLYMER).mode is Mode.CLOSED_LOOP


def test_a_sweep_started_in_open_loop_stays_in_open_loop(session, bench):
	session.board(Role.POLYMER).set_mode(Mode.OPEN_LOOP)
	run_sweep(session, Role.POLYMER, [150], 0.3)
	assert wait_until(lambda: sim_state(bench, Role.POLYMER).duty == 0, 1.0)
	assert sim_state(bench, Role.POLYMER).mode is Mode.OPEN_LOOP


def test_a_cancelled_sweep_ends_at_once_and_stops_the_motor(session, bench):
	cancel = Cancel()
	threading.Timer(0.5, cancel.cancel).start()
	started = time.monotonic()
	result = run_sweep(session, Role.POLYMER, [150, 160, 170, 180], 2.0, cancel=cancel)
	assert time.monotonic() - started < 1.5
	assert result.ended_early
	assert result.rows == ()
	assert wait_until(lambda: sim_state(bench, Role.POLYMER).duty == 0, 1.0)


def test_a_sweep_leaves_the_other_board_alone(session, bench):
	run_sweep(session, Role.POLYMER, [160], 0.3)
	assert sim_state(bench, Role.COLOUR).mode is Mode.CLOSED_LOOP
	assert sim_state(bench, Role.COLOUR).duty == 0


def test_a_sweep_of_a_board_not_connected_is_refused(session):
	session.disconnect(Role.COLOUR)
	with pytest.raises(NotConnectedError):
		run_sweep(session, Role.COLOUR, [150], 0.3)


def test_a_sweep_takes_its_sink_off_the_session(session):
	before = len(session._sinks)
	run_sweep(session, Role.POLYMER, [150], 0.2)
	assert len(session._sinks) == before


def test_a_sweep_needs_duties_and_a_hold(session):
	with pytest.raises(ValueError, match="no duties"):
		run_sweep(session, Role.POLYMER, [], 1.0)
	with pytest.raises(ValueError, match="hold"):
		run_sweep(session, Role.POLYMER, [150], 0.0)


def test_a_failure_putting_things_back_is_reported_not_raised(session, bench, monkeypatch):
	board = session.board(Role.POLYMER)
	real_set_pwm = board.set_pwm

	def set_pwm(duty):
		if duty == 0:
			raise OSError("COM6 lost")
		real_set_pwm(duty)

	monkeypatch.setattr(board, "set_pwm", set_pwm)
	result = run_sweep(session, Role.POLYMER, [150], 0.2)
	assert "could not put duty 0" in result.cleanup_problem and "COM6 lost" in result.cleanup_problem


# --- Step test ------------------------------------------------------------------------------------

def test_a_step_test_measures_each_step(session, bench):
	seen = []
	result = run_step_test(session, Role.POLYMER, [0.0, 10.0, 5.0], 3.5,
							on_step=lambda i, n, step: seen.append((i, n, step.from_hz, step.to_hz)))
	assert not result.ended_early and result.cleanup_problem is None
	assert seen == [(0, 2, 0.0, 10.0), (1, 2, 10.0, 5.0)]
	up, down = (step.metrics for step in result.steps)
	assert up.rise_time_s is not None and 0 < up.rise_time_s < 2.0
	assert abs(up.steady_error_hz) < 0.5
	assert abs(down.steady_error_hz) < 0.5
	assert sim_state(bench, Role.POLYMER).target_speed_hz == 0.0


def test_a_step_test_switches_to_closed_loop(session, bench):
	session.board(Role.POLYMER).set_mode(Mode.OPEN_LOOP)
	run_step_test(session, Role.POLYMER, [0.0, 5.0], 0.5)
	assert sim_state(bench, Role.POLYMER).mode is Mode.CLOSED_LOOP


def test_a_step_test_writes_a_run_log_and_its_metrics(session, tmp_path):
	result = run_step_test(session, Role.POLYMER, [0.0, 8.0], 2.0,
							start_log=lambda: start_run_log(session, session.config, note="step test"),
							note="kp 0.5 ki 2")
	assert result.log_path.exists()
	assert result.metrics_path == result.log_path.with_name(f"{result.log_path.stem}-steps.csv")
	with result.metrics_path.open(encoding="utf-8") as file:
		rows = list(csv.reader(file))
	assert rows[0] == ["# kp 0.5 ki 2"]
	assert rows[2][:2] == ["0.0", "8.0"]


def test_a_cancelled_step_test_stops_and_still_closes_its_log(session, bench, tmp_path):
	closed = []

	class Log:
		csv_path = tmp_path / "run-x.csv"

		def close(self):
			closed.append(True)

	cancel = Cancel()
	threading.Timer(0.4, cancel.cancel).start()
	started = time.monotonic()
	result = run_step_test(session, Role.POLYMER, [0.0, 10.0, 5.0], 3.0, cancel=cancel, start_log=Log)
	assert time.monotonic() - started < 1.5
	assert result.ended_early and closed == [True]
	assert sim_state(bench, Role.POLYMER).target_speed_hz == 0.0
	assert result.metrics_path.exists()


def test_a_failed_step_test_still_closes_its_log_and_stops(session, bench, tmp_path, monkeypatch):
	closed = []

	class Log:
		csv_path = tmp_path / "run-y.csv"

		def close(self):
			closed.append(True)

	board = session.board(Role.POLYMER)
	real_set_speed = board.set_speed

	def set_speed(hz):
		if hz == 5.0:
			raise OSError("COM6 lost")
		real_set_speed(hz)

	monkeypatch.setattr(board, "set_speed", set_speed)
	with pytest.raises(OSError, match="COM6 lost"):
		run_step_test(session, Role.POLYMER, [0.0, 5.0], 0.3, start_log=Log)
	assert closed == [True]
	assert sim_state(bench, Role.POLYMER).target_speed_hz == 0.0


def test_a_step_too_short_to_measure_says_why(session):
	result = run_step_test(session, Role.POLYMER, [0.0, 5.0], 0.03)
	assert result.steps[0].metrics is None
	assert "polls" in result.steps[0].problem


def test_a_step_test_on_an_unconfigured_board_is_refused_and_stops(bench, tmp_path):
	config = Config(ports=bench.ports, logging=LoggingSettings(poll_hz=50.0), base_dir=tmp_path)
	with Session(config, link_factory=bench.link_factory) as session:
		session.connect(Role.POLYMER)
		with pytest.raises(Exception, match="NOT_CONFIGURED"):
			run_step_test(session, Role.POLYMER, [0.0, 5.0], 0.5)
		assert sim_state(bench, Role.POLYMER).target_speed_hz == 0.0


# --- File names -----------------------------------------------------------------------------------

def test_a_measurement_file_is_named_after_its_kind_board_and_time(tmp_path):
	from datetime import datetime
	from polydriver.bench import measurement_path
	now = datetime(2026, 10, 30, 9, 5, 7)
	path = measurement_path(tmp_path / "site", "sweep", Role.COLOUR, now=now)
	assert path == tmp_path / "site" / "sweep-colour-2026-10-30_090507.csv"
	assert path.parent.is_dir()
	path.write_text("x", encoding="utf-8")
	assert measurement_path(tmp_path / "site", "sweep", Role.COLOUR, now=now).name == "sweep-colour-2026-10-30_090507-2.csv"


def test_a_step_test_cancelled_during_a_step_ends_early(session, bench):
	cancel = Cancel()
	threading.Timer(1.2, cancel.cancel).start()
	result = run_step_test(session, Role.POLYMER, [0.0, 5.0, 8.0], 0.8, cancel=cancel)
	assert result.ended_early
	assert result.steps == ()


def test_step_metrics_use_the_filtered_speed():
	from polydriver.bench.step_test import _measure
	from gui_fakes import make_status
	samples = [(make_status(raw_hz=0.0, filtered_hz=float(v)), t) for t, v in
				[(1.0, 0), (1.1, 5), (1.2, 9), (1.3, 10), (2.5, 10), (3.0, 10), (3.4, 10)]]
	result = _measure(0.0, 10.0, 1.0, samples, 0.05)
	assert result.metrics.steady_error_hz == pytest.approx(0.0)

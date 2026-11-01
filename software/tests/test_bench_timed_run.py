"""A job the board times itself, on two simulated boards in real time."""

import threading
import time

import pytest

from polydriver.bench import Cancel, TimedRunError, run_segments, speed_segments
from polydriver.config import BoardSettings, Config, LoggingSettings
from polydriver.devices import Segment, Session
from polydriver.protocol.constants import JobState, Mode, Role
from polydriver.sim import SimBench

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


def sim(bench, role):
	return bench[bench.ports[role]].state()


def test_one_speed_is_one_step_segment():
	assert speed_segments(12.5, 1.5) == [Segment(value=12.5, duration_ms=1500)]


def test_a_timed_run_needs_a_speed_and_a_time():
	with pytest.raises(TimedRunError, match="above 0"):
		speed_segments(0, 1.0)
	with pytest.raises(TimedRunError, match="10 ms"):
		speed_segments(5, 0.001)


def test_the_board_runs_the_speed_for_the_time_and_stops(session, bench):
	progress = []
	started = time.monotonic()
	result = run_segments(session, Role.POLYMER, speed_segments(10.0, 1.0), on_progress=lambda e, t: progress.append((e, t)))
	took = time.monotonic() - started
	assert result.done and result.state is JobState.DONE
	assert result.total_ms == 1000 and result.elapsed_ms == 1000
	assert 1.0 <= took < 2.5
	assert result.edges > 0
	assert progress and progress[-1][1] == 1000
	assert sim(bench, Role.POLYMER).target_speed_hz == 0.0
	assert sim(bench, Role.COLOUR).job_state is JobState.EMPTY


def test_a_board_in_open_loop_is_switched_to_closed_loop(session, bench):
	session.board(Role.POLYMER).set_mode(Mode.OPEN_LOOP)
	assert run_segments(session, Role.POLYMER, speed_segments(5.0, 0.3)).done
	assert sim(bench, Role.POLYMER).mode is Mode.CLOSED_LOOP


def test_a_cancelled_run_stops_the_board_at_once(session, bench):
	cancel = Cancel()
	threading.Timer(0.4, cancel.cancel).start()
	started = time.monotonic()
	result = run_segments(session, Role.POLYMER, speed_segments(10.0, 5.0), cancel=cancel)
	assert time.monotonic() - started < 1.5
	assert result.ended_early and not result.done
	assert result.state is JobState.ABORTED
	assert sim(bench, Role.POLYMER).target_speed_hz == 0.0


def test_an_unconfigured_board_refuses_and_the_error_comes_through(bench, tmp_path):
	config = Config(ports=bench.ports, logging=LoggingSettings(poll_hz=50.0), base_dir=tmp_path)
	with Session(config, link_factory=bench.link_factory) as session:
		session.connect(Role.POLYMER)
		with pytest.raises(Exception, match="NOT_CONFIGURED"):
			run_segments(session, Role.POLYMER, speed_segments(5.0, 0.5))
		assert sim(bench, Role.POLYMER).target_speed_hz == 0.0


def test_a_board_that_never_reports_the_end_is_stopped(session, monkeypatch):
	import polydriver.bench.timed_run as timed_run
	monkeypatch.setattr(timed_run, "SLACK_S", 0.0)
	monkeypatch.setattr(timed_run, "ENDED", ())
	with pytest.raises(TimedRunError, match="did not report the end"):
		run_segments(session, Role.POLYMER, speed_segments(5.0, 0.2))


def test_the_run_takes_its_sink_off(session):
	before = len(session._sinks)
	run_segments(session, Role.POLYMER, speed_segments(5.0, 0.2))
	assert len(session._sinks) == before

import csv
import threading

import pytest

from polydriver.cli.dev_commands import StatusFeed, sweep_row
from polydriver.devices import BoardStatus
from polydriver.protocol.constants import JobState, Mode, Role, StatusFlag
from polydriver.protocol.serial_worker import LinkLostError
from polydriver.requests import SetCommsTimeout, SetFeedforward, SetFilter, SetMode, SetPwm, StatusReport

from cli_helpers import CONFIG_TEXT, FakeKeys, make_shell, run
from fakes import Boards, wait_until


@pytest.fixture
def boards():
	return Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR})


@pytest.fixture
def config_path(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text(CONFIG_TEXT, encoding="utf-8")
	return path


@pytest.fixture
def shell(boards, config_path):
	shell = make_shell(boards, config_path)
	run(shell, "connect")
	yield shell
	shell.session.close()


@pytest.fixture
def polymer(boards):
	return boards.links["COM6"]


@pytest.fixture
def colour(boards):
	return boards.links["COM5"]


def status(speed_hz: float) -> BoardStatus:
	report = StatusReport(speed_hz, speed_hz, 50, StatusFlag.OPEN_LOOP, 0, 0, 0, 0, 0, 0, JobState.EMPTY, None, 0)
	return BoardStatus(report, 0, False)


def read_csv(path):
	with open(path, newline="", encoding="utf-8") as file:
		return list(csv.reader(file))


# --- dev on its own ---------------------------------------------------------------------

def test_dev_alone_lists_what_it_has(shell):
	text = run(shell, "dev")
	assert "usage: dev mode|pwm|timeout|filter|ff|watch|sweep|stats" in text


def test_an_unknown_dev_option_is_named(shell):
	assert "'spin' is not a dev option" in run(shell, "dev spin polymer")


def test_help_dev_explains_every_option(shell):
	text = run(shell, "help dev")
	for line in ("dev mode ROLE open|closed", "dev pwm ROLE DUTY", "dev timeout [ROLE] SECONDS|off",
				"dev filter ROLE ALPHA", "dev ff ROLE OFFSET GAIN", "dev watch [ROLE] [SECONDS] [FILE.csv]",
				"dev sweep ROLE FROM TO STEP HOLD_S [FILE.csv]", "dev stats [ROLE]"):
		assert line in text


def test_a_mistake_prints_that_options_usage(shell, polymer):
	assert "usage: dev pwm ROLE DUTY" in run(shell, "dev pwm polymer")
	assert polymer.count(SetPwm) == 0


# --- mode and pwm -------------------------------------------------------------------------

def test_mode_switches_the_board(shell, polymer):
	assert "polymer: mode OPEN_LOOP" in run(shell, "dev mode p open")
	assert polymer.mode is Mode.OPEN_LOOP
	run(shell, "dev mode p closed")
	assert SetMode(Mode.CLOSED_LOOP) in polymer.sent


def test_mode_needs_open_or_closed(shell, polymer):
	assert "usage: dev mode ROLE open|closed" in run(shell, "dev mode p half")
	assert polymer.count(SetMode) == 0


def test_pwm_sets_the_duty_in_open_loop(shell, polymer):
	run(shell, "dev mode p open")
	assert "polymer: duty 128 (50.2 %)" in run(shell, "dev pwm p 128")
	assert polymer.pwm_duty == 128


@pytest.mark.parametrize("duty", ["256", "-1", "12.5", "lots"])
def test_pwm_needs_a_whole_duty_from_0_to_255(shell, polymer, duty):
	run(shell, "dev mode p open")
	assert "DUTY must be a whole number from 0 to 255" in run(shell, f"dev pwm p {duty}")
	assert polymer.count(SetPwm) == 0


def test_pwm_in_closed_loop_is_refused_with_the_way_out(shell):
	text = run(shell, "dev pwm p 50")
	assert "WRONG_MODE" in text


# --- timeout, filter, feedforward ------------------------------------------------------------

def test_timeout_sets_both_boards(shell, polymer, colour):
	text = run(shell, "dev timeout 5")
	assert SetCommsTimeout(5000) in polymer.sent and SetCommsTimeout(5000) in colour.sent
	assert "comms timeout 5 s on polymer, colour" in text
	assert "`config reload` or a restart sends the config's 2 s again" in text


def test_timeout_off(shell, polymer):
	assert "comms timeout off on polymer" in run(shell, "dev timeout p off")
	assert polymer.timeout_ms == 0


@pytest.mark.parametrize("value, reason", [("0.2", "at least 0.5"), ("70", "65.535"), ("soon", "a number")])
def test_timeout_follows_the_config_rule(shell, polymer, value, reason):
	text = run(shell, f"dev timeout p {value}")
	assert reason in text
	assert polymer.count(SetCommsTimeout) == 1


def test_filter_sets_alpha(shell, polymer):
	assert "polymer: filter alpha 0.25" in run(shell, "dev filter p 0.25")
	assert SetFilter(0.25) in polymer.sent


def test_ff_sets_offset_and_gain(shell, colour):
	assert "colour: feedforward 120 duty + 1.5 duty/Hz" in run(shell, "dev ff c 120 1.5")
	assert SetFeedforward(120.0, 1.5) in colour.sent


# --- watch ---------------------------------------------------------------------------------

def test_watch_prints_the_polls_for_a_time(shell):
	text = run(shell, "dev watch polymer 0.3")
	lines = [line for line in text.splitlines() if " raw " in line]
	assert len(lines) >= 5
	assert all("polymer" in line and "filt" in line and "duty" in line for line in lines)
	assert "colour" not in text


def test_watch_without_a_role_shows_both(shell):
	text = run(shell, "dev watch 0.2")
	assert "polymer" in text and "colour" in text


def test_watch_ends_on_enter(shell):
	shell.keys = FakeKeys(press_on_call=3)
	text = run(shell, "dev watch")
	assert "ended" in text


def test_watch_saves_a_csv(shell, tmp_path):
	path = tmp_path / "watch.csv"
	run(shell, f"dev watch c 0.3 {path}")
	rows = read_csv(path)
	assert rows[0][:3] == ["host_time_s", "role", "device_time_us_unwrapped"]
	assert "raw_speed_hz" in rows[0] and "job_state" in rows[0]
	assert len(rows) >= 6
	assert {row[1] for row in rows[1:]} == {"colour"}


def test_watch_ends_when_its_boards_are_gone(shell, colour):
	shell.keys = FakeKeys()
	colour.fail_with = LinkLostError("COM5 lost")
	assert wait_until(lambda: Role.COLOUR not in shell.session.roles())
	assert "no board to watch" in run(shell, "dev watch c")


def test_watch_ends_when_its_board_goes_during_the_watch(shell, colour):
	shell.keys = FakeKeys()
	threading.Timer(0.15, lambda: setattr(colour, "fail_with", LinkLostError("COM5 lost"))).start()
	assert "no board left to watch" in run(shell, "dev watch c")


def test_watch_leaves_no_sink_behind(shell):
	sinks = len(shell.session._sinks)
	run(shell, "dev watch 0.1")
	assert len(shell.session._sinks) == sinks


# --- sweep --------------------------------------------------------------------------------------

def test_sweep_steps_the_duty_and_ends_at_zero(shell, polymer, tmp_path):
	run(shell, "dev mode p open")
	path = tmp_path / "sweep.csv"
	text = run(shell, f"dev sweep p 0 100 50 0.2 {path}")
	assert [r.duty for r in polymer.sent if isinstance(r, SetPwm)] == [0, 50, 100, 0]
	rows = read_csv(path)
	assert rows[0][:2] == ["duty", "mean_raw_hz"]
	assert [row[0] for row in rows[1:]] == ["0", "50", "100"]
	assert float(rows[3][1]) == pytest.approx(20.0)
	assert "duty back to 0" in text


def test_a_short_hold_is_pointed_out(shell):
	run(shell, "dev mode p open")
	assert "polls per step" in run(shell, "dev sweep p 0 50 50 0.2")
	assert "polls per step" not in run(shell, "dev sweep p 0 50 50 0.4")


def test_a_step_is_judged_on_the_second_half_of_its_hold():
	samples = [status(0.0), status(0.0), status(10.0), status(10.0)]
	row = sweep_row(50, samples)
	assert row[1] == 10.0 and row[6] == 2


def test_a_hold_counts_only_polls_that_arrive_during_it(shell):
	feed = StatusFeed([Role.POLYMER])
	feed.record_status(Role.POLYMER, status(99.0), 0.0)
	shell.keys = FakeKeys()
	samples = shell._hold(feed, 0.05)
	assert all(s.report.raw_speed_hz != 99.0 for s in samples)


def test_sweep_needs_open_loop(shell, polymer):
	assert "`dev mode polymer open` first" in run(shell, "dev sweep p 0 100 50 0.2")
	assert polymer.count(SetPwm) == 0


@pytest.mark.parametrize("line", ["dev sweep p 0 100 0 1", "dev sweep p 100 0 50 1", "dev sweep p 0 100 50 0",
								"dev sweep p 0 300 50 1"])
def test_sweep_refuses_steps_that_go_nowhere(shell, polymer, line):
	run(shell, "dev mode p open")
	assert "usage: dev sweep" in run(shell, line)
	assert polymer.count(SetPwm) == 0


def test_sweep_ends_early_on_enter_and_still_stops(shell, polymer):
	run(shell, "dev mode p open")
	shell.keys = FakeKeys(press_on_call=2)
	text = run(shell, "dev sweep p 0 200 50 0.3")
	assert "ended early" in text
	assert [r.duty for r in polymer.sent if isinstance(r, SetPwm)][-1] == 0


def test_sweep_says_when_the_duty_could_not_be_set_back(shell, polymer):
	run(shell, "dev mode p open")
	original = polymer.call

	def call(request, timeout_s=None):
		if isinstance(request, SetPwm) and request.duty == 50:
			polymer.fail_with = LinkLostError("COM6 lost")
		return original(request, timeout_s)

	polymer.call = call
	text = run(shell, "dev sweep p 0 100 50 0.2")
	assert "link lost" in text
	assert "duty NOT set back to 0" in text


# --- stats -----------------------------------------------------------------------------------------

def test_stats_shows_both_ends_of_the_link(shell):
	text = run(shell, "dev stats polymer")
	assert "polymer:" in text
	assert "frames sent" in text and "retries 1" in text and "WRONG_MODE 2" in text
	assert "board: dropped 0, bad CRC 0" in text
	assert "polls" in text

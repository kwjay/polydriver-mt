import io

import pytest

from polydriver.cli import Shell, UsageError, main, parse_number, parse_role, parse_roles, split_args
from polydriver.config import default_config_text, load_config
from polydriver.devices import Session
from polydriver.protocol.constants import Command, JobState, NackReason, Role
from polydriver.protocol.serial_worker import LinkLostError
from polydriver.requests import (
	ClearEstop, Estop, RequestNackError, RequestStatus, SetId, SetPid, SetSpeed, Stop,
)

from cli_helpers import CONFIG_TEXT, make_shell, run
from fakes import Boards, wait_until

@pytest.fixture
def boards():
	return Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR, "COM7": Role.POLYMER, "COM9": Role.NONE})


@pytest.fixture
def config_path(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text(CONFIG_TEXT, encoding="utf-8")
	return path


@pytest.fixture
def shell(boards, config_path):
	shell = make_shell(boards, config_path)
	yield shell
	shell.session.close()


# --- Parsing ------------------------------------------------------------------------

@pytest.mark.parametrize("token, role", [
	("polymer", Role.POLYMER), ("p", Role.POLYMER), ("POLY", Role.POLYMER),
	("colour", Role.COLOUR), ("color", Role.COLOUR), ("c", Role.COLOUR), ("col", Role.COLOUR),
])
def test_a_role_can_be_shortened(token, role):
	assert parse_role(token) is role


def test_an_unknown_role_names_the_choices():
	with pytest.raises(UsageError, match="polymer or colour"):
		parse_role("dye")


def test_no_role_means_both():
	assert parse_roles([]) == [Role.POLYMER, Role.COLOUR]
	assert parse_roles(["c"]) == [Role.COLOUR]


def test_roles_come_once_each_in_order():
	assert parse_roles(["c", "p", "polymer"]) == [Role.POLYMER, Role.COLOUR]


@pytest.mark.parametrize("token", ["fast", "nan", "inf", "-inf", ""])
def test_a_number_must_be_a_finite_number(token):
	with pytest.raises(UsageError):
		parse_number(token, "HZ")


# --- The shell ------------------------------------------------------------------------

def test_an_unknown_command_is_named(shell):
	assert "unknown command: spin" in run(shell, "spin polymer")


def test_an_empty_line_does_not_repeat_the_last_command(shell, boards):
	run(shell, "connect polymer", "speed polymer 10")
	run(shell, "")
	assert boards.links["COM6"].count(SetSpeed) == 1


def test_help_lists_the_board_commands(shell):
	text = run(shell, "help")
	for name in ("connect", "list", "status", "settings", "speed", "pid", "stop", "estop", "config", "ports"):
		assert name in text
	assert "EOF" not in text


def test_help_on_one_command_has_no_source_indentation(shell):
	lines = run(shell, "help log").splitlines()
	assert lines[0].startswith("log [on [NOTE]")
	assert any(line.startswith("log on [NOTE]  - start a run log") for line in lines)
	assert not [line for line in lines if line.startswith(("\t", " "))]


def test_help_on_an_unknown_command_says_so(shell):
	assert "No help on frobnicate" in run(shell, "help frobnicate")


def test_help_on_a_command_without_help_text_says_so(shell):
	shell.do_nodoc = lambda arg: None
	assert "No help on nodoc" in run(shell, "help nodoc")


def test_a_bug_in_a_command_is_reported_and_the_shell_goes_on(shell, boards):
	run(shell, "connect polymer")
	boards.links["COM6"].fail_with = RuntimeError("bug in the code")
	text = run(shell, "speed polymer 10")
	assert "internal error" in text and "bug in the code" in text
	boards.links["COM6"].fail_with = None
	assert "target 10 Hz" in run(shell, "speed polymer 10")


def test_a_usage_mistake_found_inside_a_board_action_is_still_a_usage_mistake(shell):
	def action(role):
		raise UsageError("found late")

	with pytest.raises(UsageError, match="found late"):
		shell.each([Role.POLYMER], action)


def test_quit_stops_and_closes_every_board(shell, boards):
	run(shell, "connect")
	assert shell.onecmd("quit") is True
	for port in ("COM6", "COM5"):
		assert boards.links[port].count(Stop) == 1
		assert boards.links[port].closed


def test_ctrl_c_sends_stop_to_every_board(shell, boards):
	run(shell, "connect")
	shell.interrupt()
	assert "STOP sent to polymer, colour" in shell.stdout.getvalue()
	assert boards.links["COM6"].count(Stop) == 1 and boards.links["COM5"].count(Stop) == 1


def test_ctrl_c_says_when_stop_did_not_arrive(shell, boards):
	run(shell, "connect")
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost")
	shell.interrupt()
	assert "STOP FAILED on colour - the board stops by itself 2 s after" in shell.stdout.getvalue()


def test_a_failed_stop_without_a_comms_timeout_says_to_cut_the_power(boards, config_path):
	config_path.write_text(CONFIG_TEXT + "\n[safety]\ncomms_timeout_s = 0\n", encoding="utf-8")
	config = load_config(config_path)
	with Session(config, link_factory=boards) as session:
		shell = Shell(session, config, config_path, stdin=io.StringIO(), stdout=io.StringIO())
		run(shell, "connect colour")
		boards.links["COM5"].fail_with = LinkLostError("COM5 lost")
		assert "cut the motor power" in run(shell, "stop")


# --- Connecting -------------------------------------------------------------------------

def test_connect_takes_the_port_from_the_config(shell, boards):
	text = run(shell, "connect polymer")
	assert boards.opened == ["COM6"]
	assert "polymer: connected on COM6" in text
	assert "firmware 3.0.0" in text
	assert "pid 0.5 1 0" in text and "alpha 0.4" in text and "timeout 2 s" in text


@pytest.mark.parametrize("line", ["connect polymer COM7", "connect COM7 polymer", "connect p COM7"])
def test_connect_takes_a_port_in_either_order(shell, boards, line):
	run(shell, line)
	assert boards.opened == ["COM7"]


@pytest.mark.parametrize("line, reason", [
	("connect polymer colour", "one board"), ("connect polymer COM6 COM7", "one port"), ("connect COM7", "say which board"),
])
def test_connect_with_unclear_words_prints_the_usage(shell, boards, line, reason):
	text = run(shell, line)
	assert "usage: connect [ROLE] [PORT]" in text and reason in text
	assert boards.opened == []


def test_connect_without_a_role_connects_both(shell, boards):
	text = run(shell, "connect")
	assert sorted(boards.opened) == ["COM5", "COM6"]
	assert "polymer: connected" in text and "colour: connected" in text


def test_connect_says_when_the_board_stays_unconfigured(shell):
	assert "no gains in polydriver.toml, closed loop is refused" in run(shell, "connect colour")


def test_one_board_failing_to_connect_does_not_stop_the_other(shell, boards):
	boards.roles["COM5"] = Role.POLYMER
	text = run(shell, "connect")
	assert "polymer: connected" in text
	assert "colour:" in text and "swapped" in text
	assert shell.session.roles() == [Role.POLYMER]


def test_connect_without_any_port_explains_where_to_set_it(boards, tmp_path):
	path = tmp_path / "polydriver.toml"
	config = load_config(path)
	with Session(config, link_factory=boards) as session:
		shell = Shell(session, config, path, stdin=io.StringIO(), stdout=io.StringIO())
		assert "set [ports] polymer" in run(shell, "connect polymer")


def test_a_board_without_a_stored_role_is_flagged_with_the_fix(shell):
	text = run(shell, "connect polymer COM9")
	assert "no role stored" in text and "`id polymer`" in text


def test_id_stores_the_role_on_the_board(shell, boards):
	run(shell, "connect polymer COM9")
	assert "stored polymer" in run(shell, "id polymer")
	assert boards.links["COM9"].stored_role is Role.POLYMER
	assert SetId(Role.POLYMER) in boards.links["COM9"].sent


def test_connect_twice_is_reported(shell):
	run(shell, "connect polymer")
	assert "already connected" in run(shell, "connect polymer")


def test_disconnect_closes_one_board(shell, boards):
	run(shell, "connect")
	assert "colour: disconnected" in run(shell, "disconnect colour")
	assert boards.links["COM5"].closed and not boards.links["COM6"].closed


def test_disconnect_of_a_board_that_is_not_connected_says_so(shell):
	assert "the colour board is not connected" in run(shell, "disconnect colour")


def test_disconnect_without_a_role_closes_both(shell, boards):
	run(shell, "connect")
	run(shell, "disconnect")
	assert boards.links["COM5"].closed and boards.links["COM6"].closed


def test_a_lost_link_is_announced_without_a_command(shell, boards):
	run(shell, "connect")
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost: device unplugged")
	assert wait_until(lambda: "! colour: link lost" in shell.stdout.getvalue())


# --- Reading ------------------------------------------------------------------------------

def test_list_shows_both_boards(shell):
	run(shell, "connect polymer")
	shell.session.poll_now(Role.POLYMER)
	text = run(shell, "list")
	assert "polymer  COM6" in text and "12.00 Hz" in text and "duty 100" in text
	assert "colour   not connected (COM5 in the config)" in text


def test_list_shows_the_last_error_of_a_lost_board(shell, boards):
	run(shell, "connect")
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost: device unplugged")
	assert wait_until(lambda: not shell.session.state(Role.COLOUR).connected)
	assert "link lost: COM5 lost: device unplugged" in run(shell, "list")


def test_status_polls_now_and_shows_every_field(shell, boards):
	run(shell, "connect polymer")
	before = boards.links["COM6"].count(RequestStatus)
	text = run(shell, "status polymer")
	assert boards.links["COM6"].count(RequestStatus) > before
	for part in ("raw 12.00 Hz", "filtered 12.00 Hz", "duty 100", "edges", "device time", "longest",
				"bad CRC", "job EMPTY"):
		assert part in text


def test_status_without_a_role_shows_the_connected_boards(shell):
	run(shell, "connect")
	text = run(shell, "status")
	assert "polymer:" in text and "colour:" in text


def test_status_without_a_role_skips_a_board_that_is_not_connected(shell):
	run(shell, "connect polymer")
	text = run(shell, "status")
	assert "polymer:" in text and "colour" not in text


def test_settings_reads_the_board(shell):
	run(shell, "connect polymer")
	text = run(shell, "settings p")
	assert "pid kp 0.5 ki 1 kd 0" in text and "comms timeout 2 s" in text


def test_a_command_for_a_board_that_is_not_connected_says_how(shell):
	assert "the colour board is not connected" in run(shell, "status colour")


# --- Driving -------------------------------------------------------------------------------

def test_speed_sets_the_target(shell, boards):
	run(shell, "connect polymer")
	assert "polymer: target 20 Hz" in run(shell, "speed polymer 20")
	assert SetSpeed(20.0) in boards.links["COM6"].sent


@pytest.mark.parametrize("line", ["speed polymer fast", "speed fast", "speed polymer", "speed polymer nan",
								"speed dye 10"])
def test_a_bad_speed_prints_the_usage_and_sends_nothing(shell, boards, line):
	run(shell, "connect polymer")
	text = run(shell, line)
	assert "usage: speed ROLE HZ" in text
	assert boards.links["COM6"].count(SetSpeed) == 0


def test_a_refusal_comes_with_a_hint(shell):
	run(shell, "connect colour")
	text = run(shell, "speed colour 10")
	assert "refused: SET_SPEED refused: NOT_CONFIGURED" in text
	assert "[board.colour] kp, ki, kd" in text


def test_pid_sets_the_gains(shell, boards):
	run(shell, "connect colour")
	text = run(shell, "pid colour 1 0.5 0")
	assert SetPid(1.0, 0.5, 0.0) in boards.links["COM5"].sent
	assert "until the board restarts (the config has no gains for it)" in text


def test_pid_needs_three_gains(shell, boards):
	run(shell, "connect colour")
	assert "usage: pid ROLE KP KI KD" in run(shell, "pid colour 1 2")
	assert boards.links["COM5"].count(SetPid) == 0


def test_stop_without_a_role_stops_every_board(shell, boards):
	run(shell, "connect")
	assert "STOP sent to polymer, colour" in run(shell, "stop")
	assert boards.links["COM6"].count(Stop) == 1 and boards.links["COM5"].count(Stop) == 1


def test_stop_names_a_board_it_could_not_reach(shell, boards):
	run(shell, "connect")
	boards.links["COM5"].fail_with = LinkLostError("COM5 lost")
	text = run(shell, "stop")
	assert "STOP FAILED on colour" in text
	assert boards.links["COM6"].count(Stop) == 1


def test_stop_one_board(shell, boards):
	run(shell, "connect")
	run(shell, "stop polymer")
	assert boards.links["COM6"].count(Stop) == 1 and boards.links["COM5"].count(Stop) == 0


def test_estop_latches_and_clear_releases(shell, boards):
	run(shell, "connect")
	assert "ESTOP latched on polymer, colour" in run(shell, "estop")
	assert boards.links["COM6"].count(Estop) == 1
	run(shell, "clear polymer")
	assert boards.links["COM6"].count(ClearEstop) == 1


def test_a_nack_on_one_board_of_two_still_reaches_the_other(shell, boards):
	run(shell, "connect")
	boards.links["COM6"].refuse[Estop] = RequestNackError(Command.ESTOP, NackReason.WRONG_STATE)
	text = run(shell, "estop")
	assert "polymer: refused" in text
	assert boards.links["COM5"].count(Estop) == 1


# --- Config -------------------------------------------------------------------------------

def test_config_shows_what_is_loaded(shell, config_path):
	text = run(shell, "config")
	assert str(config_path) in text
	assert "polymer: port COM6, pid 0.5 1 0, alpha 0.4" in text
	assert "colour: port COM5, no gains" in text
	assert "comms timeout 2 s, poll 50 Hz" in text


def test_config_reload_sends_the_new_settings(shell, boards, config_path):
	run(shell, "connect polymer")
	config_path.write_text(CONFIG_TEXT.replace("kp = 0.5", "kp = 2.0"), encoding="utf-8")
	assert "reloaded" in run(shell, "config reload")
	assert boards.links["COM6"].gains[0] == 2.0
	assert "polymer: port COM6, pid 2 1 0" in run(shell, "config")


def test_a_broken_config_keeps_the_old_one(shell, boards, config_path):
	run(shell, "connect polymer")
	config_path.write_text(CONFIG_TEXT.replace("kp = 0.5", "kpp = 2.0"), encoding="utf-8")
	text = run(shell, "config reload")
	assert "kpp" in text and "still using the previous config" in text
	assert shell.config.boards[Role.POLYMER].kp == 0.5


def test_config_reload_waits_for_a_running_job(shell, boards, config_path):
	run(shell, "connect polymer")
	boards.links["COM6"].job_state = JobState.RUNNING
	shell.session.poll_now(Role.POLYMER)
	config_path.write_text(CONFIG_TEXT.replace("kp = 0.5", "kp = 2.0"), encoding="utf-8")
	text = run(shell, "config reload")
	assert "a job is running on polymer" in text and "still using the previous config" in text
	assert shell.config.boards[Role.POLYMER].kp == 0.5
	assert boards.links["COM6"].gains[0] == 0.5


def test_config_new_writes_the_template_once(shell, tmp_path):
	path = tmp_path / "other.toml"
	assert "wrote" in run(shell, f"config new {path}")
	assert path.read_text(encoding="utf-8") == default_config_text()
	assert "already exists" in run(shell, f"config new {path}")


def test_arguments_keep_backslashes_for_windows_paths():
	assert split_args(r'new C:\Users\jake\x.toml') == ["new", r"C:\Users\jake\x.toml"]
	assert split_args('new "my folder/x.toml"') == ["new", "my folder/x.toml"]


# --- Starting -------------------------------------------------------------------------------

def test_main_runs_commands_from_stdin_and_stops_on_eof(config_path, boards, tmp_path):
	out = io.StringIO()
	code = main(["--config", str(config_path)], link_factory=boards, stdin=io.StringIO("connect\nspeed p 15\n"),
				stdout=out)
	assert code == 0
	assert "polymer: target 15 Hz" in out.getvalue()
	assert boards.links["COM6"].closed and boards.links["COM5"].closed
	assert (tmp_path / "logs" / "polydriver.log").exists()


def test_main_leaves_no_log_handler_and_no_open_port_behind(config_path, boards):
	import logging

	class Broken(io.StringIO):
		def readline(self, *args):
			raise RuntimeError("console gone")

	before = list(logging.getLogger().handlers)
	with pytest.raises(RuntimeError):
		main(["--config", str(config_path), "--connect"], link_factory=boards, stdin=Broken(), stdout=io.StringIO())
	assert logging.getLogger().handlers == before
	assert boards.links["COM6"].closed and boards.links["COM5"].closed


def test_main_can_connect_on_start(config_path, boards):
	out = io.StringIO()
	main(["--config", str(config_path), "--connect"], link_factory=boards, stdin=io.StringIO(""), stdout=out)
	assert "polymer: connected" in out.getvalue() and "colour: connected" in out.getvalue()


def test_main_without_a_config_file_says_so(tmp_path, boards):
	out = io.StringIO()
	main(["--config", str(tmp_path / "polydriver.toml")], link_factory=boards, stdin=io.StringIO(""), stdout=out)
	assert "no polydriver.toml" in out.getvalue() and "config new" in out.getvalue()


def test_main_with_a_broken_config_stops_with_the_reason(tmp_path, boards):
	path = tmp_path / "polydriver.toml"
	path.write_text("[motor]\nspeed = 1\n", encoding="utf-8")
	out = io.StringIO()
	assert main(["--config", str(path)], link_factory=boards, stdin=io.StringIO(""), stdout=out) == 2
	assert "unknown section" in out.getvalue()

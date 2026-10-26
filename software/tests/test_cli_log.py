import csv
import io
import json

import pytest

from polydriver.cli import main
from polydriver.protocol.constants import Role

from cli_helpers import CONFIG_TEXT, make_shell, run
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
	shell.on_quit()
	shell.session.close()


def sidecar(shell) -> dict:
	return json.loads(shell.last_run_log.sidecar_path.read_text(encoding="utf-8"))


def rows(shell) -> list[dict]:
	with open(shell.last_run_log.csv_path, newline="", encoding="utf-8") as file:
		return list(csv.DictReader(file))


# --- on and off ----------------------------------------------------------------------------

def test_log_on_writes_to_the_config_log_folder(shell, tmp_path):
	text = run(shell, "log on first dry run")
	log = shell.last_run_log
	assert log.csv_path.parent == tmp_path / "logs"
	assert str(log.csv_path) in text and "polymer, colour" in text
	assert wait_until(lambda: log.rows >= 5)
	assert sidecar(shell)["note"] == "first dry run"


def test_log_off_closes_the_run_and_says_what_it_wrote(shell):
	run(shell, "log on")
	assert wait_until(lambda: shell.last_run_log.rows >= 5)
	text = run(shell, "log off")
	assert "rows" in text and str(shell.last_run_log.sidecar_path) in text
	count = len(rows(shell))
	assert count >= 5 and sidecar(shell)["rows"] == count
	assert sidecar(shell)["ended"] is not None
	assert shell.last_run_log not in shell.session._sinks


def test_log_on_twice_is_refused(shell):
	run(shell, "log on")
	assert "already logging to" in run(shell, "log on")


def test_log_off_without_a_log_says_so(shell):
	assert "not logging" in run(shell, "log off")


def test_log_on_without_a_board(boards, config_path):
	shell = make_shell(boards, config_path)
	assert "no board connected" in run(shell, "log on")
	shell.session.close()


def test_a_log_folder_that_cannot_be_made_is_reported(boards, tmp_path):
	(tmp_path / "blocked").write_text("x", encoding="utf-8")
	path = tmp_path / "polydriver.toml"
	text = CONFIG_TEXT.replace("[logging]\npoll_hz = 50\n", '[logging]\npoll_hz = 50\nfolder = "blocked/logs"\n')
	path.write_text(text, encoding="utf-8")
	shell = make_shell(boards, path)
	run(shell, "connect")
	out = run(shell, "log on")
	assert "cannot make the log folder" in out and "internal error" not in out
	assert shell.run_log is None
	shell.session.close()


def test_log_alone_says_whether_it_is_logging(shell):
	assert "not logging" in run(shell, "log")
	run(shell, "log on")
	assert wait_until(lambda: shell.last_run_log.rows >= 1)
	text = run(shell, "log")
	assert "logging to" in text and "rows" in text
	run(shell, "log off")
	assert "last run" in run(shell, "log")


def test_quit_closes_the_log(shell):
	run(shell, "log on")
	shell.onecmd("quit")
	assert sidecar(shell)["ended"] is not None


def test_main_closes_the_log_however_it_ends(config_path, boards):
	class Broken(io.StringIO):
		calls = 0

		def readline(self, *args):
			Broken.calls += 1
			if Broken.calls == 1:
				return "log on\n"
			raise RuntimeError("console gone")

	with pytest.raises(RuntimeError):
		main(["--config", str(config_path), "--connect"], link_factory=boards, stdin=Broken(), stdout=io.StringIO())
	[side] = list((config_path.parent / "logs").glob("*.json"))
	assert json.loads(side.read_text(encoding="utf-8"))["ended"] is not None


# --- note and mass --------------------------------------------------------------------------

def test_log_note_while_logging_and_after(shell):
	run(shell, "log on")
	run(shell, "log note screw primed")
	assert sidecar(shell)["note"] == "screw primed"
	run(shell, "log off")
	run(shell, 'log note "jammed at 40 s"')
	assert sidecar(shell)["note"] == "jammed at 40 s"


def test_log_mass_fills_the_sidecar_after_the_run(shell):
	run(shell, "log on")
	run(shell, "log off")
	assert "polymer: 81.25 g" in run(shell, "log mass polymer 81.25")
	assert "colour: 4.5 g" in run(shell, "log mass c 4.5")
	assert sidecar(shell)["weighed_mass_g"] == {"polymer": 81.25, "colour": 4.5}


@pytest.mark.parametrize("line, message", [
	("log mass polymer", "usage: log mass ROLE GRAMS"),
	("log mass polymer heavy", "GRAMS must be a number"),
	("log mass dye 3", "not a board"),
	("log mass polymer -2", "must be >= 0 g"),
	("log mass polymer 3 4", "usage: log mass ROLE GRAMS"),
])
def test_a_bad_mass_is_refused(shell, line, message):
	run(shell, "log on")
	run(shell, "log off")
	text = run(shell, line)
	assert message in text and "internal error" not in text
	assert sidecar(shell)["weighed_mass_g"] == {"polymer": None, "colour": None}


def test_note_and_mass_need_a_run(shell):
	assert "no run log yet" in run(shell, "log note x")
	assert "no run log yet" in run(shell, "log mass polymer 3")


@pytest.mark.parametrize("line", ["log start", "log off now", "log note"])
def test_log_with_words_it_does_not_take(shell, line):
	run(shell, "log on")
	assert "usage: log [on [NOTE] | off | note TEXT | mass ROLE GRAMS]" in run(shell, line)
	assert shell.run_log is not None


# --- What happens during a logged run ------------------------------------------------------

def test_commands_during_a_run_go_in_the_sidecar(shell):
	run(shell, "log on")
	run(shell, "speed polymer 20", "dev mode colour open", "dev pwm colour 120", "pid polymer 1 0.5 0", "stop")
	run(shell, "log off")
	commands = [(e["role"], e["message"]) for e in sidecar(shell)["events"] if e["kind"] == "command"]
	assert commands[:4] == [("polymer", "speed 20 Hz"), ("colour", "mode OPEN_LOOP"), ("colour", "duty 120"),
						("polymer", "pid 1 0.5 0")]
	assert ("polymer", "STOP") in commands and ("colour", "STOP") in commands


def test_the_log_rows_show_the_commanded_duty(shell):
	run(shell, "dev mode colour open", "dev pwm colour 120")
	run(shell, "log on")
	assert wait_until(lambda: shell.last_run_log.rows >= 3)
	run(shell, "log off")
	assert rows(shell)[-1]["colour_duty"] == "120"

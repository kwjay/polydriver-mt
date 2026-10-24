"""Shared by the CLI tests: a config, a shell on fake boards, and running lines in it."""

import io

from polydriver.cli import Shell
from polydriver.config import load_config
from polydriver.devices import Session

CONFIG_TEXT = """
[ports]
polymer = "COM6"
colour = "COM5"

[board.polymer]
kp = 0.5
ki = 1.0
kd = 0.0
alpha = 0.4

[logging]
poll_hz = 50
"""


class FakeKeys:
	"""Enter is 'pressed' on the given call of pressed(), or never."""

	def __init__(self, press_on_call: int | None = None):
		self.press_on_call = press_on_call
		self.calls = 0

	def pressed(self) -> bool:
		self.calls += 1
		return self.press_on_call is not None and self.calls >= self.press_on_call


def make_shell(boards, config_path) -> Shell:
	config = load_config(config_path)
	session = Session(config, link_factory=boards)
	return Shell(session, config, config_path, stdin=io.StringIO(), stdout=io.StringIO())


def run(shell: Shell, *lines: str) -> str:
	"""Runs the lines and returns only what they printed."""
	out = shell.stdout
	with shell._output_lock:
		start = out.tell()
	for line in lines:
		shell.onecmd(shell.precmd(line))
	with shell._output_lock:
		out.seek(start)
		text = out.read()
		out.seek(0, io.SEEK_END)
	return text

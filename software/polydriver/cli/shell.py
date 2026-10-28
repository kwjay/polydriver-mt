"""The polydriver shell and `python -m polydriver`. Each command group is a mixin of do_ methods;
ShellBase comes last, so its onecmd() wraps every one of them."""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from ..config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from ..devices import Link, Session
from ..protocol.constants import Role
from ..startup import log_to_file, simulate
from .base import ShellBase
from .board_commands import BoardCommands
from .calibrate_commands import CalibrateCommands
from .dev_commands import DevCommands
from .job_commands import JobCommands
from .log_commands import LogCommands
from .run_commands import RunCommands

class Shell(BoardCommands, DevCommands, LogCommands, CalibrateCommands, JobCommands, RunCommands, ShellBase):
	pass


def main(
	argv: list[str] | None = None,
	*,
	link_factory: Callable[[str], Link] | None = None,
	stdin: TextIO | None = None,
	stdout: TextIO | None = None,
) -> int:
	parser = argparse.ArgumentParser(prog="polydriver", description="PolydriverMT control shell")
	parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config file (default: polydriver.toml)")
	parser.add_argument("--connect", action="store_true", help="connect both boards at start")
	parser.add_argument("--sim", action="store_true",
						help="two simulated boards on the config's ports instead of the serial ports (polydriver.sim)")
	args = parser.parse_args(argv)
	out = stdout or sys.stdout
	config_path = Path(args.config)
	try:
		config = load_config(config_path)
	except ConfigError as error:
		print(error, file=out)
		return 2

	sim_ports = None
	if args.sim:
		config, link_factory = simulate(config)
		sim_ports = config.ports
	log_handler = log_to_file(config)
	session = Session(config, link_factory=link_factory or Link.open)
	shell: Shell | None = None
	try:
		shell = Shell(session, config, config_path, stdin=stdin or sys.stdin, stdout=out)
		shell.say("PolydriverMT shell. Ctrl+C sends STOP to every board. `help` lists the commands, "
				"`help CMD` explains one.")
		if not config_path.exists():
			shell.say(f"no polydriver.toml at {config_path}, so the defaults are used (`config new` writes one)")
		shell.say(f"program log (errors, every command): {log_handler.baseFilename}")
		if sim_ports is not None:
			shell.say(f"simulated boards on {sim_ports[Role.POLYMER]} (polymer) and {sim_ports[Role.COLOUR]} (colour); "
					"no serial port is opened")
		if args.connect:
			shell.onecmd("connect")
		while True:
			try:
				shell.cmdloop(intro="")
				return 0
			except KeyboardInterrupt:
				shell.interrupt()
	finally:
		session.close()
		if shell is not None:
			shell.on_quit()
		logging.getLogger().removeHandler(log_handler)
		log_handler.close()


"""The polydriver shell and `python -m polydriver`. Each command group is a mixin of do_ methods;
ShellBase comes last, so its onecmd() wraps every one of them."""

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from ..config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from ..devices import Link, Session
from ..protocol.constants import Role
from ..sim import SimBench, SimBoard
from .base import ShellBase
from .board_commands import BoardCommands
from .calibrate_commands import CalibrateCommands
from .dev_commands import DevCommands
from .job_commands import JobCommands
from .log_commands import LogCommands
from .run_commands import RunCommands

LOG_FILE_NAME = "polydriver.log"


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
		config, link_factory = _simulated(config)
		sim_ports = config.ports
	log_handler = _log_to_file(config)
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


def _simulated(config: Config) -> tuple[Config, Callable[[str], Link]]:
	"""A SimBench on the config's ports, or on SIM-POLYMER and SIM-COLOUR where the config names none."""
	defaults = {Role.POLYMER: SimBench.POLYMER_PORT, Role.COLOUR: SimBench.COLOUR_PORT}
	ports = {role: config.ports.get(role) or port for role, port in defaults.items()}
	bench = SimBench({ports[role]: SimBoard(role) for role in defaults})
	return replace(config, ports=ports), bench.link_factory


def _log_to_file(config: Config) -> logging.FileHandler:
	folder = config.resolve(config.logging.folder)
	folder.mkdir(parents=True, exist_ok=True)
	handler = logging.FileHandler(folder / LOG_FILE_NAME, encoding="utf-8")
	handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
	root = logging.getLogger()
	root.addHandler(handler)
	root.setLevel(logging.INFO)
	return handler

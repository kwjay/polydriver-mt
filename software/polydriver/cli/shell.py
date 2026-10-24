"""The polydriver shell and `python -m polydriver`. Each command group is a mixin of do_ methods;
ShellBase comes last, so its onecmd() wraps every one of them."""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from ..config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from ..devices import Link, Session
from .base import ShellBase
from .board_commands import BoardCommands
from .dev_commands import DevCommands

LOG_FILE_NAME = "polydriver.log"


class Shell(BoardCommands, DevCommands, ShellBase):
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
	args = parser.parse_args(argv)
	out = stdout or sys.stdout
	config_path = Path(args.config)
	try:
		config = load_config(config_path)
	except ConfigError as error:
		print(error, file=out)
		return 2

	log_handler = _log_to_file(config)
	session = Session(config, link_factory=link_factory or Link.open)
	try:
		shell = Shell(session, config, config_path, stdin=stdin or sys.stdin, stdout=out)
		shell.say("PolydriverMT shell. Ctrl+C sends STOP to every board. `help` lists the commands, "
				"`help CMD` explains one.")
		if not config_path.exists():
			shell.say(f"no polydriver.toml at {config_path}, so the defaults are used (`config new` writes one)")
		shell.say(f"log: {log_handler.baseFilename}")
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
		logging.getLogger().removeHandler(log_handler)
		log_handler.close()


def _log_to_file(config: Config) -> logging.FileHandler:
	folder = config.resolve(config.logging.folder)
	folder.mkdir(parents=True, exist_ok=True)
	handler = logging.FileHandler(folder / LOG_FILE_NAME, encoding="utf-8")
	handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
	root = logging.getLogger()
	root.addHandler(handler)
	root.setLevel(logging.INFO)
	return handler

"""What the shell and the window both do when they start: simulated boards for --sim, and the
program log."""

import logging
from collections.abc import Callable
from dataclasses import replace

from .config import Config
from .devices import Link
from .protocol.constants import Role
from .sim import SimBench, SimBoard

LOG_FILE_NAME = "polydriver.log"


def simulate(config: Config) -> tuple[Config, Callable[[str], Link]]:
	"""A SimBench on the config's ports, or on SIM-POLYMER and SIM-COLOUR where the config names none."""
	defaults = {Role.POLYMER: SimBench.POLYMER_PORT, Role.COLOUR: SimBench.COLOUR_PORT}
	ports = {role: config.ports.get(role) or port for role, port in defaults.items()}
	bench = SimBench({ports[role]: SimBoard(role) for role in defaults})
	return replace(config, ports=ports), bench.link_factory


def log_to_file(config: Config) -> logging.FileHandler:
	folder = config.resolve(config.logging.folder)
	folder.mkdir(parents=True, exist_ok=True)
	handler = logging.FileHandler(folder / LOG_FILE_NAME, encoding="utf-8")
	handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
	root = logging.getLogger()
	root.addHandler(handler)
	root.setLevel(logging.INFO)
	return handler

"""The PolydriverMT window: `python -m polydriver.gui` or the `polydriver-gui` command, with the
shell's --config and --sim. Closing the window closes the Session, which sends STOP to every board."""

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from ..config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from ..devices import Link, Session
from ..protocol.constants import Role
from ..startup import log_to_file, simulate
from .app import MainWindow
from .bridge import Bridge, BridgeClosedError, Call

__all__ = ["Bridge", "BridgeClosedError", "Call", "MainWindow", "main"]

TITLE = "PolydriverMT"
CLOSE_WAIT_S = 10.0


def main(
	argv: list[str] | None = None,
	*,
	link_factory: Callable[[str], Link] | None = None,
	run: Callable[[MainWindow], int] | None = None,
	report: Callable[[str], None] | None = None,
) -> int:
	"""run shows the window until it closes (the Qt event loop) and report shows a start-up error;
	tests replace both."""
	parser = argparse.ArgumentParser(prog="polydriver-gui", description="PolydriverMT window")
	parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config file (default: polydriver.toml)")
	parser.add_argument("--sim", action="store_true",
						help="two simulated boards on the config's ports instead of the serial ports (polydriver.sim)")
	args = parser.parse_args(argv)
	app = QApplication.instance() or QApplication(sys.argv[:1])
	try:
		config = load_config(Path(args.config))
	except ConfigError as error:
		(report or _message_box)(str(error))
		return 2

	title = TITLE
	if args.sim:
		config, link_factory = simulate(config)
		title += (f" — simulated boards on {config.ports[Role.POLYMER]} (polymer) and "
				f"{config.ports[Role.COLOUR]} (colour)")
	log_handler = log_to_file(config)
	session = Session(config, link_factory=link_factory or Link.open)
	bridge = Bridge()
	window: MainWindow | None = None
	try:
		window = MainWindow(bridge, title=title)
		window.show()
		return run(window) if run is not None else app.exec()
	finally:
		if window is not None:
			window.close()
		bridge.close(CLOSE_WAIT_S)
		session.close()
		logging.getLogger().removeHandler(log_handler)
		log_handler.close()


def _message_box(text: str) -> None:
	QMessageBox.critical(None, TITLE, text)

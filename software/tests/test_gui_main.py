import logging

import pytest

import polydriver.gui
from polydriver.devices import Session
from polydriver.gui import BridgeClosedError, main


def write_config(tmp_path, text: str = ""):
	path = tmp_path / "polydriver.toml"
	path.write_text(text, encoding="utf-8")
	return path


@pytest.fixture
def sessions(monkeypatch):
	"""Every Session main() makes, so a test can see it was closed."""
	made = []

	class RecordingSession(Session):
		def __init__(self, *args, **kwargs):
			super().__init__(*args, **kwargs)
			self.closed = False
			made.append(self)

		def close(self):
			self.closed = True
			super().close()

	monkeypatch.setattr(polydriver.gui, "Session", RecordingSession)
	return made


def test_sim_opens_the_window_on_two_simulated_boards(qtbot, tmp_path):
	seen = []

	def run(window):
		seen.append((window.windowTitle(), window.page_names()[0], window.isVisible()))
		return 0

	assert main(["--sim", "--config", str(write_config(tmp_path))], run=run) == 0
	assert seen == [(
		"PolydriverMT — simulated boards on SIM-POLYMER (polymer) and SIM-COLOUR (colour)", "Devices", True,
	)]


def test_sim_uses_the_ports_the_config_names(qtbot, tmp_path):
	path = write_config(tmp_path, '[ports]\npolymer = "COM6"\ncolour = "COM5"\n')
	titles = []
	main(["--sim", "--config", str(path)], run=lambda window: titles.append(window.windowTitle()) or 0)
	assert titles == ["PolydriverMT — simulated boards on COM6 (polymer) and COM5 (colour)"]


def test_without_sim_the_title_is_plain(qtbot, tmp_path):
	titles = []
	main(["--config", str(write_config(tmp_path))], run=lambda window: titles.append(window.windowTitle()) or 0)
	assert titles == ["PolydriverMT"]


def test_main_returns_what_the_event_loop_returns(qtbot, tmp_path):
	assert main(["--sim", "--config", str(write_config(tmp_path))], run=lambda window: 3) == 3


def test_a_broken_config_is_reported_and_nothing_opens(qtbot, tmp_path, sessions):
	reports = []
	runs = []
	path = write_config(tmp_path, "[ports\n")
	assert main(["--config", str(path)], run=runs.append, report=reports.append) == 2
	assert len(reports) == 1 and str(path.name) in reports[0]
	assert runs == [] and sessions == []


def test_closing_the_window_closes_the_session_and_the_bridge(qtbot, tmp_path, sessions):
	windows = []
	main(["--sim", "--config", str(write_config(tmp_path))], run=lambda window: windows.append(window) or 0)
	assert [session.closed for session in sessions] == [True]
	assert not windows[0].isVisible()
	with pytest.raises(BridgeClosedError):
		windows[0].bridge.run(lambda: None)


def test_the_session_is_closed_even_when_the_window_fails(qtbot, tmp_path, sessions):
	def run(window):
		raise RuntimeError("a bug in a screen")

	with pytest.raises(RuntimeError, match="a bug in a screen"):
		main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	assert [session.closed for session in sessions] == [True]


def test_the_program_log_is_written_while_the_window_is_open_and_released_after(qtbot, tmp_path):
	handlers_before = list(logging.getLogger().handlers)

	def run(window):
		logging.getLogger("polydriver.gui.test").warning("written while open")
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	text = (tmp_path / "logs" / "polydriver.log").read_text(encoding="utf-8")
	assert "written while open" in text
	assert logging.getLogger().handlers == handlers_before

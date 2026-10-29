"""The Manual screen's Save to config, Sweep and Step test, with fake procedures (the real ones run in
test_bench_procedures.py and, through the window, in test_gui_sim.py)."""

import threading

import pytest
from PySide6.QtCore import Qt

from gui_fakes import FakeSession
from polydriver.analysis import SweepRow
from polydriver.analysis.step_response import StepMetrics
from polydriver.bench import Cancelled, StepResult, StepTestResult, SweepResult
from polydriver.config import BoardSettings, Config, LoggingSettings, load_config
from polydriver.gui.bridge import Bridge
from polydriver.gui.manual_page import ManualPage
from polydriver.gui.telemetry import QtSink
from polydriver.protocol.constants import Role


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def session():
	session = FakeSession()
	session.connect(Role.POLYMER)
	session.connect(Role.COLOUR)
	session.calls.clear()
	return session


def config_for(tmp_path):
	return Config(boards={Role.POLYMER: BoardSettings(kp=0.5, ki=2.0, kd=0.0, alpha=0.5), Role.COLOUR: BoardSettings()},
				logging=LoggingSettings(folder=tmp_path / "logs"), base_dir=tmp_path)


class FakeProcedure:
	"""Stands in for run_sweep / run_step_test: reports the steps it is given, waits for release (or a
	cancel), and returns the result it is given."""

	def __init__(self, steps=(), result=None, error=None):
		self.steps = list(steps)
		self.result = result
		self.error = error
		self.release = threading.Event()
		self.release.set()
		self.calls = []

	def __call__(self, session, role, values, hold_s, *, cancel, on_step, **kwargs):
		self.calls.append((role, list(values), hold_s, kwargs))
		self.cancel = cancel
		for index, step in enumerate(self.steps):
			on_step(index, len(self.steps), step)
		while not self.release.wait(0.01):
			if cancel.is_set:
				return self.result
		if self.error:
			raise self.error
		return self.result


def make_page(qtbot, session, bridge, tmp_path, *, sweep=None, step_test=None, config_path=None):
	page = ManualPage(session, config_for(tmp_path), bridge, QtSink(), config_path=config_path,
					sweep=sweep or FakeProcedure(), step_test=step_test or FakeProcedure(),
					start_log=lambda session, config, note="": None)
	qtbot.addWidget(page)
	return page


def click(qtbot, button):
	qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


LINE = [SweepRow(duty, (duty - 128) / 1.5, 10, 0) for duty in range(140, 200, 10)]


# --- Save to config -------------------------------------------------------------------------------

def test_save_to_config_is_hidden_without_a_config_file(qtbot, session, bridge, tmp_path):
	page = make_page(qtbot, session, bridge, tmp_path)
	assert page.save_button.isHidden()


def test_save_to_config_writes_the_form_for_the_chosen_board(qtbot, session, bridge, tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text('[ports]\ncolour = "COM5"\n', encoding="utf-8")
	page = make_page(qtbot, session, bridge, tmp_path, config_path=path)
	page.kp.setValue(0.8)
	page.ff_offset.setValue(130.0)
	click(qtbot, page.save_button)
	qtbot.waitUntil(lambda: page.note_label.text() != "", timeout=2000)
	saved = load_config(path)
	assert saved.boards[Role.POLYMER] == page.form_values()
	assert saved.ports[Role.COLOUR] == "COM5"
	assert page.note_label.text() == f"saved the polymer settings to {path} (comments in that file are not kept)"


def test_saved_settings_come_back_when_the_board_is_chosen_again(qtbot, session, bridge, tmp_path):
	path = tmp_path / "polydriver.toml"
	page = make_page(qtbot, session, bridge, tmp_path, config_path=path)
	page.kp.setValue(0.9)
	click(qtbot, page.save_button)
	qtbot.waitUntil(lambda: page.note_label.text() != "", timeout=2000)
	page.select_role(Role.COLOUR)
	page.select_role(Role.POLYMER)
	assert page.kp.value() == 0.9


def test_a_broken_config_file_is_reported_and_left(qtbot, session, bridge, tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text("[ports\n", encoding="utf-8")
	page = make_page(qtbot, session, bridge, tmp_path, config_path=path)
	click(qtbot, page.save_button)
	qtbot.waitUntil(lambda: page.message_label.text() != "", timeout=2000)
	assert "not valid TOML" in page.message_label.text()
	assert path.read_text(encoding="utf-8") == "[ports\n"


# --- Sweep ----------------------------------------------------------------------------------------

def test_the_sweep_runs_the_duties_on_the_chosen_board_into_the_log_folder(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult(tuple(LINE), False, tmp_path / "logs" / "sweep.csv"))
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	page.select_role(Role.COLOUR)
	panel = page.sweep_panel
	panel.from_duty.setValue(140)
	panel.to_duty.setValue(160)
	panel.step_duty.setValue(10)
	panel.hold.setValue(2.0)
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: not panel.running, timeout=2000)
	role, duties, hold_s, kwargs = sweep.calls[0]
	assert (role, duties, hold_s) == (Role.COLOUR, [140, 150, 160], 2.0)
	assert kwargs["path"].parent == tmp_path / "logs"
	assert kwargs["path"].name.startswith("sweep-colour-")


def test_a_finished_sweep_shows_the_fit_and_use_fills_the_feedforward(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult(tuple(LINE), False, tmp_path / "sweep.csv"))
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	panel = page.sweep_panel
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: panel.use_button.isEnabled(), timeout=2000)
	assert panel.result_label.text().startswith("start duty 140 · offset 128.0 · gain 1.500 duty/Hz")
	assert f"saved {tmp_path / 'sweep.csv'}" in panel.result_label.text()
	click(qtbot, panel.use_button)
	assert page.ff_offset.value() == pytest.approx(128.0)
	assert page.ff_gain.value() == pytest.approx(1.5)
	assert "Send to board" in page.note_label.text()


def test_a_sweep_too_short_to_fit_says_why(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult(tuple(LINE[:2]), False, None))
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	panel = page.sweep_panel
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: not panel.running, timeout=2000)
	assert panel.result_label.text().startswith("no fit: the screw turned in 2 sweep step(s)")
	assert not panel.use_button.isEnabled()


def test_each_sweep_step_shows_as_it_comes(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(steps=[SweepRow(120, 0.0, 10, 10), LINE[0]], result=SweepResult((), False, None))
	sweep.release.clear()
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	panel = page.sweep_panel
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: panel.progress_label.text().startswith("step 2/2"), timeout=2000)
	assert panel.progress_label.text() == "step 2/2: duty 140 → 8.00 Hz"
	sweep.release.set()
	qtbot.waitUntil(lambda: not panel.running, timeout=2000)


def test_while_a_sweep_runs_the_other_controls_are_off_and_stop_stays_on(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult((), False, None))
	sweep.release.clear()
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	click(qtbot, page.sweep_panel.run_button)
	qtbot.waitUntil(lambda: page.sweep_panel.running, timeout=2000)
	for widget in (page.role_combo, page.send_button, page.speed_button, page.duty_button, page.closed_loop_button,
					page.open_loop_button, page.step_panel.run_button):
		assert not widget.isEnabled()
	assert page.stop_button.isEnabled()
	assert page.sweep_panel.run_button.text() == "Stop sweep"
	sweep.release.set()
	qtbot.waitUntil(lambda: not page.sweep_panel.running, timeout=2000)
	assert page.speed_button.isEnabled() and page.step_panel.run_button.isEnabled()
	assert page.sweep_panel.run_button.text() == "Run sweep"


def test_stop_sweep_cancels_it(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult((), True, None))
	sweep.release.clear()
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	panel = page.sweep_panel
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: panel.running, timeout=2000)
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: not panel.running, timeout=2000)
	assert sweep.cancel.is_set
	assert panel.result_label.text().startswith("stopped early")


def test_stop_this_board_cancels_a_running_sweep_before_its_stop(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult((), True, None))
	sweep.release.clear()
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	click(qtbot, page.sweep_panel.run_button)
	qtbot.waitUntil(lambda: page.sweep_panel.running, timeout=2000)
	order = []
	board = session.boards[Role.POLYMER]
	real_stop = board.stop
	board.stop = lambda: (order.append(("stop", sweep.cancel.is_set)), real_stop())
	click(qtbot, page.stop_button)
	qtbot.waitUntil(lambda: not page.sweep_panel.running, timeout=2000)
	assert order == [("stop", True)]


def test_cancel_procedures_cancels_a_running_one(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(result=SweepResult((), True, None))
	sweep.release.clear()
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	click(qtbot, page.sweep_panel.run_button)
	qtbot.waitUntil(lambda: page.sweep_panel.running, timeout=2000)
	page.cancel_procedures()
	assert sweep.cancel.is_set
	qtbot.waitUntil(lambda: not page.sweep_panel.running, timeout=2000)


def test_a_failed_sweep_says_why_and_ends(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(error=OSError("COM6 lost"))
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	panel = page.sweep_panel
	click(qtbot, panel.run_button)
	qtbot.waitUntil(lambda: not panel.running, timeout=2000)
	assert panel.result_label.text() == "sweep failed: COM6 lost"
	assert page.speed_button.isEnabled()


def test_a_backwards_duty_range_is_refused(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure()
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	page.sweep_panel.from_duty.setValue(200)
	page.sweep_panel.to_duty.setValue(150)
	click(qtbot, page.sweep_panel.run_button)
	assert sweep.calls == []
	assert "from duty must not be above to duty" in page.sweep_panel.result_label.text()


# --- Step test ------------------------------------------------------------------------------------

UP = StepResult(0.0, 10.0, StepMetrics(0.0, 10.0, 0.42, 12.5, 1.8, -0.04, -0.4))
DOWN = StepResult(10.0, 5.0, StepMetrics(10.0, 5.0, None, 0.0, None, 0.31, 6.2))


def test_the_step_test_sends_the_form_first_then_runs_the_typed_steps(qtbot, session, bridge, tmp_path):
	step_test = FakeProcedure(result=StepTestResult((UP,), False))
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	page.kp.setValue(0.7)
	page.step_panel.steps.setText("0, 10, 5")
	page.step_panel.hold.setValue(3.0)
	page.step_panel.band.setValue(2.0)
	click(qtbot, page.step_panel.run_button)
	qtbot.waitUntil(lambda: not page.step_panel.running, timeout=2000)
	assert session.calls[-1][:2] == ("apply_settings", Role.POLYMER)
	assert session.calls[-1][2].kp == 0.7
	role, speeds, hold_s, kwargs = step_test.calls[0]
	assert (role, speeds, hold_s, kwargs["band_fraction"]) == (Role.POLYMER, [0.0, 10.0, 5.0], 3.0, 0.02)
	assert kwargs["note"].startswith("step test polymer: kp 0.7 ki 2 kd 0 alpha 0.5")


def test_each_step_adds_a_row_of_metrics(qtbot, session, bridge, tmp_path):
	step_test = FakeProcedure(steps=[UP, DOWN], result=StepTestResult((UP, DOWN), False))
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	click(qtbot, page.step_panel.run_button)
	qtbot.waitUntil(lambda: not page.step_panel.running, timeout=2000)
	assert page.step_panel.rows() == [
		["0 → 10", "0.42", "12.5", "1.80", "-0.04"],
		["10 → 5", "—", "0.0", "—", "+0.31"],
	]


def test_a_step_without_metrics_shows_why(qtbot, session, bridge, tmp_path):
	short = StepResult(0.0, 5.0, None, "2 poll(s) after the step; at least 3 polls are needed: hold longer")
	step_test = FakeProcedure(steps=[short], result=StepTestResult((short,), False))
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	click(qtbot, page.step_panel.run_button)
	qtbot.waitUntil(lambda: not page.step_panel.running, timeout=2000)
	assert page.step_panel.rows()[0][:2] == ["0 → 5", short.problem]


def test_the_step_test_says_where_it_saved(qtbot, session, bridge, tmp_path):
	log = tmp_path / "run-x.csv"
	step_test = FakeProcedure(result=StepTestResult((UP,), False, log, tmp_path / "run-x-steps.csv"))
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	click(qtbot, page.step_panel.run_button)
	qtbot.waitUntil(lambda: not page.step_panel.running, timeout=2000)
	assert page.step_panel.status_label.text() == f"saved {log} and run-x-steps.csv"


def test_badly_typed_steps_are_refused_before_anything_is_sent(qtbot, session, bridge, tmp_path):
	step_test = FakeProcedure()
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	page.step_panel.steps.setText("0, fast")
	click(qtbot, page.step_panel.run_button)
	assert step_test.calls == [] and session.calls == []
	assert page.step_panel.status_label.text().startswith("'fast' is not a speed")


def test_a_step_test_refused_by_the_board_says_why(qtbot, session, bridge, tmp_path):
	step_test = FakeProcedure(error=OSError("COM6 lost"))
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	click(qtbot, page.step_panel.run_button)
	qtbot.waitUntil(lambda: not page.step_panel.running, timeout=2000)
	assert page.step_panel.status_label.text() == "step test failed: COM6 lost"


def test_shutdown_cancels_a_running_step_test(qtbot, session, bridge, tmp_path):
	step_test = FakeProcedure(result=StepTestResult((), True))
	step_test.release.clear()
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	click(qtbot, page.step_panel.run_button)
	qtbot.waitUntil(lambda: page.step_panel.running, timeout=2000)
	page.shutdown()
	assert step_test.cancel.is_set


def test_a_cancelled_procedure_raising_cancelled_is_not_shown_as_a_failure(qtbot, session, bridge, tmp_path):
	sweep = FakeProcedure(error=Cancelled())
	page = make_page(qtbot, session, bridge, tmp_path, sweep=sweep)
	click(qtbot, page.sweep_panel.run_button)
	qtbot.waitUntil(lambda: not page.sweep_panel.running, timeout=2000)
	assert page.sweep_panel.result_label.text() == "stopped early"


def test_a_second_step_test_starts_with_an_empty_table(qtbot, session, bridge, tmp_path):
	step_test = FakeProcedure(steps=[UP], result=StepTestResult((UP,), False))
	page = make_page(qtbot, session, bridge, tmp_path, step_test=step_test)
	for _ in range(2):
		click(qtbot, page.step_panel.run_button)
		qtbot.waitUntil(lambda: not page.step_panel.running, timeout=2000)
	assert len(page.step_panel.rows()) == 1

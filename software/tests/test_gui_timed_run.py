import threading

import pytest

from gui_fakes import FakeSession
from polydriver.bench import TimedRunResult
from polydriver.calibration import CalibrationStore, save_calibration
from polydriver.config import Config
from polydriver.gui.bridge import Bridge
from polydriver.gui.manual_page import ManualPage
from polydriver.gui.procedures import Procedures
from polydriver.gui.telemetry import QtSink
from polydriver.gui.timed_run_panel import G_S, HZ
from polydriver.materials import Kind, Material, MaterialRegistry, save_materials
from polydriver.protocol.constants import JobState, Role


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
	return session


@pytest.fixture
def config(tmp_path):
	config = Config(base_dir=tmp_path)
	registry = MaterialRegistry()
	registry.put(Material("PLA", Kind.POLYMER, dispenser="A"))
	registry.put(Material("TPU", Kind.POLYMER))
	registry.put(Material("Red", Kind.MASTERBATCH, dispenser="B"))
	save_materials(registry, config.resolve(config.paths.materials))
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 30.0), (20.0, 60.0), (30.0, 90.0)):
		store.add_measurement("A", "PLA", speed_hz, grams, 60.0)
	save_calibration(store, config.resolve(config.paths.calibration))
	return config


class FakeRun:
	def __init__(self, state=JobState.DONE, hold=False):
		self.calls = []
		self.state = state
		self.release = threading.Event()
		if not hold:
			self.release.set()

	def __call__(self, session, role, segments, *, cancel=None, on_progress=None):
		self.calls.append((role, list(segments)))
		total = sum(s.duration_ms for s in segments)
		if on_progress:
			on_progress(total // 2, total)
		while not self.release.wait(0.01):
			if cancel is not None and cancel.is_set:
				return TimedRunResult(JobState.ABORTED, total, total // 2, 5, True)
		return TimedRunResult(self.state, total, total, 321, False)


def make_page(qtbot, session, config, bridge, run, procedures=None):
	page = ManualPage(session, config, bridge, QtSink(), timed_run=run, procedures=procedures)
	qtbot.addWidget(page)
	return page.timed_panel, page


def finish(qtbot, panel):
	qtbot.waitUntil(lambda: panel.run_button.text() == "Run" and "running" not in panel.progress_label.text(),
					timeout=2000)


def test_a_speed_runs_as_one_step_for_the_time(qtbot, session, config, bridge):
	run = FakeRun()
	panel, _ = make_page(qtbot, session, config, bridge, run)
	panel.unit.setCurrentText(HZ)
	panel.value.setValue(12.0)
	panel.seconds.setValue(5.0)
	panel.run_button.click()
	finish(qtbot, panel)
	(role, segments), = run.calls
	assert role is Role.POLYMER
	assert [(s.value, s.duration_ms) for s in segments] == [(12.0, 5000)]
	assert panel.progress_label.text() == "done: 12 Hz for 5 s (321 encoder edges)"


def test_a_rate_goes_through_the_calibration_curve(qtbot, session, config, bridge):
	run = FakeRun()
	panel, _ = make_page(qtbot, session, config, bridge, run)
	panel.unit.setCurrentText(G_S)
	panel.material.setCurrentText("PLA")
	panel.value.setValue(1.0)
	panel.seconds.setValue(4.0)
	panel.run_button.click()
	finish(qtbot, panel)
	(role, segments), = run.calls
	assert [s.value for s in segments] == [pytest.approx(20.0)]
	assert "done: 1 g/s of PLA for 4 s" in panel.progress_label.text()
	assert "it should have given 4 g" in panel.progress_label.text()


def test_only_the_chosen_boards_materials_are_offered(qtbot, session, config, bridge):
	panel, page = make_page(qtbot, session, config, bridge, FakeRun())
	panel.unit.setCurrentText(G_S)
	assert [panel.material.itemText(i) for i in range(panel.material.count())] == ["PLA", "TPU"]
	page.select_role(Role.COLOUR)
	assert [panel.material.itemText(i) for i in range(panel.material.count())] == ["Red"]


def test_a_material_with_no_curve_is_not_run(qtbot, session, config, bridge):
	run = FakeRun()
	panel, page = make_page(qtbot, session, config, bridge, run)
	panel.unit.setCurrentText(G_S)
	page.select_role(Role.COLOUR)
	panel.run_button.click()
	assert panel.result_label.text().startswith("not run:")
	assert run.calls == []


def test_a_material_with_no_dispenser_says_so(qtbot, session, config, bridge):
	panel, _ = make_page(qtbot, session, config, bridge, FakeRun())
	panel.unit.setCurrentText(G_S)
	panel.material.setCurrentText("TPU")
	panel.run_button.click()
	assert "no default dispenser" in panel.result_label.text()


def test_a_rate_outside_the_curve_is_not_run(qtbot, session, config, bridge):
	run = FakeRun()
	panel, _ = make_page(qtbot, session, config, bridge, run)
	panel.unit.setCurrentText(G_S)
	panel.material.setCurrentText("PLA")
	panel.value.setValue(50.0)
	panel.run_button.click()
	assert panel.result_label.text().startswith("not run:")
	assert run.calls == []


def test_stop_run_cancels_it(qtbot, session, config, bridge):
	panel, _ = make_page(qtbot, session, config, bridge, FakeRun(hold=True))
	panel.unit.setCurrentText(HZ)
	panel.run_button.click()
	qtbot.waitUntil(lambda: "running:" in panel.progress_label.text(), timeout=2000)
	panel.run_button.click()
	finish(qtbot, panel)
	assert panel.progress_label.text().startswith("stopped early")


def test_a_timed_run_is_one_of_the_windows_procedures(qtbot, session, config, bridge):
	procedures = Procedures()
	panel, page = make_page(qtbot, session, config, bridge, FakeRun(hold=True), procedures)
	panel.run_button.click()
	qtbot.waitUntil(lambda: procedures.running == "the timed run", timeout=2000)
	assert not page.sweep_panel.run_button.isEnabled() and not page.speed_button.isEnabled()
	procedures.cancel_all()
	finish(qtbot, panel)
	assert procedures.running is None and page.speed_button.isEnabled()


def test_a_board_that_ends_the_job_itself_is_reported(qtbot, session, config, bridge):
	panel, _ = make_page(qtbot, session, config, bridge, FakeRun(state=JobState.ABORTED))
	panel.unit.setCurrentText(HZ)
	panel.run_button.click()
	finish(qtbot, panel)
	assert "the board ended the job ABORTED" in panel.progress_label.text()


def test_the_material_is_asked_for_only_for_a_rate(qtbot, session, config, bridge):
	panel, page = make_page(qtbot, session, config, bridge, FakeRun())
	page.show()
	panel.unit.setCurrentText(HZ)
	assert not panel.material.isVisibleTo(panel)
	panel.unit.setCurrentText(G_S)
	assert panel.material.isVisibleTo(panel)

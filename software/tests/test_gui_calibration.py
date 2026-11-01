import json
import threading

import pytest

from gui_fakes import FakeSession
from polydriver.bench import Cancelled, TimedRunResult
from polydriver.calibration import CalibrationStore, load_calibration, save_calibration
from polydriver.config import Config
from polydriver.gui.bridge import Bridge
from polydriver.gui.calibration_page import BOARD_TIMED, CalibrationPage
from polydriver.gui.procedures import Procedures
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
	registry.put(Material("Red", Kind.MASTERBATCH, dispenser="B"))
	save_materials(registry, config.resolve(config.paths.materials))
	return config


class FakeRun:
	"""Stands in for run_segments: records the call and ends the way a test says."""

	def __init__(self, state=JobState.DONE, ended_early=False, error=None, hold=False):
		self.calls = []
		self.state, self.ended_early, self.error = state, ended_early, error
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
				return TimedRunResult(JobState.ABORTED, total, total // 2, 10, True)
		if self.error is not None:
			raise self.error
		return TimedRunResult(self.state, total, total, 1234, self.ended_early)


def make_page(qtbot, session, config, bridge, run=None, procedures=None):
	page = CalibrationPage(session, config, bridge, run=run or FakeRun(), procedures=procedures)
	qtbot.addWidget(page)
	return page


def wait_idle(qtbot, bridge):
	qtbot.waitUntil(lambda: not bridge.busy, timeout=2000)


def weigh(qtbot, page, bridge, speed_hz, seconds, grams, note=""):
	page.speed.setValue(speed_hz)
	page.seconds.setValue(seconds)
	page.grams.setValue(grams)
	page.note.setText(note)
	page.save_button.click()
	wait_idle(qtbot, bridge)


def stored(config):
	return load_calibration(config.resolve(config.paths.calibration))


def test_choosing_a_material_takes_its_dispenser_and_board(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	assert page.material_combo.currentText() == "PLA"
	assert page.dispenser.text() == "A"
	page.material_combo.setCurrentText("Red")
	assert page.dispenser.text() == "B"
	assert page.role_combo.currentData() is Role.COLOUR


def test_a_run_holds_the_speed_for_the_time_on_the_chosen_board(qtbot, session, config, bridge):
	run = FakeRun()
	page = make_page(qtbot, session, config, bridge, run)
	page.speed.setValue(12.5)
	page.seconds.setValue(30.0)
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	(role, segments), = run.calls
	assert role is Role.POLYMER
	assert [(s.value, s.duration_ms) for s in segments] == [(12.5, 30000)]
	assert "done: 12.5 Hz for 30 s" in page.progress_label.text()


def test_a_weighing_after_a_run_is_saved_as_board_timed(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	page.speed.setValue(10.0)
	page.seconds.setValue(60.0)
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0, note="first")
	(m,) = stored(config).curve("A", "PLA").measurements
	assert (m.speed_hz, m.grams, m.duration_s, m.method, m.note) == (10.0, 30.0, 60.0, BOARD_TIMED, "first")
	assert "gave 0.5 g/s" in page.message_label.text() and "saved to" in page.message_label.text()


def test_a_weighing_typed_without_a_run_is_steady_state(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	assert stored(config).curve("A", "PLA").measurements[0].method == "steady-state"


def test_a_weighing_for_another_speed_than_the_run_is_not_board_timed(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	page.speed.setValue(10.0)
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	weigh(qtbot, page, bridge, 15.0, 60.0, 30.0)
	assert stored(config).curve("A", "PLA").measurements[0].method == "steady-state"


def test_a_run_ended_early_says_not_to_use_the_weighing(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge, FakeRun(state=JobState.ABORTED))
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	assert "ABORTED" in page.progress_label.text() and "do not use this weighing" in page.progress_label.text()
	weigh(qtbot, page, bridge, page.speed.value(), page.seconds.value(), 5.0)
	assert stored(config).curve("A", "PLA").measurements[0].method == "steady-state"


def test_stop_run_cancels_it(qtbot, session, config, bridge):
	run = FakeRun(hold=True)
	page = make_page(qtbot, session, config, bridge, run)
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Stop run", timeout=2000)
	qtbot.waitUntil(lambda: "running:" in page.progress_label.text(), timeout=2000)
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	assert "stopped early" in page.progress_label.text()


def test_a_failed_run_says_why(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge, FakeRun(error=OSError("COM6 lost")))
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	assert "run failed" in page.progress_label.text() and "COM6 lost" in page.progress_label.text()


def test_a_cancelled_run_raising_says_stopped(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge, FakeRun(error=Cancelled()))
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	assert page.progress_label.text() == "run failed: stopped early"


def test_a_run_is_refused_while_another_procedure_runs(qtbot, session, config, bridge):
	procedures = Procedures()
	procedures.begin("the sweep")
	run = FakeRun()
	page = make_page(qtbot, session, config, bridge, run, procedures)
	assert not page.run_button.isEnabled()
	page.toggle_run()
	assert "the sweep is running" in page.progress_label.text()
	assert run.calls == []


def test_a_run_frees_the_registry_when_it_ends(qtbot, session, config, bridge):
	procedures = Procedures()
	page = make_page(qtbot, session, config, bridge, FakeRun(), procedures)
	page.run_button.click()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)
	assert procedures.running is None


def test_the_window_stop_cancels_a_run(qtbot, session, config, bridge):
	procedures = Procedures()
	page = make_page(qtbot, session, config, bridge, FakeRun(hold=True), procedures)
	page.run_button.click()
	qtbot.waitUntil(lambda: procedures.running == "the calibration run", timeout=2000)
	procedures.cancel_all()
	qtbot.waitUntil(lambda: page.run_button.text() == "Run", timeout=2000)


def test_weighings_are_listed_with_their_rates(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0, note="a")
	weigh(qtbot, page, bridge, 20.0, 60.0, 66.0)
	rows = page.table_rows()
	assert [row[:5] for row in rows] == [["1", "10", "30", "60", "0.5"], ["2", "20", "66", "60", "1.1"]]
	assert rows[0][7] == "a"
	assert "A / PLA: 2 weighings" in page.curve_label.text()


def test_one_speed_has_no_fit_yet_two_do(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	assert "no fit yet" in page.fit_label.text()
	assert "lowest speed that gave material: 10 Hz" in page.fit_label.text()
	weigh(qtbot, page, bridge, 20.0, 60.0, 60.0)
	assert "no fit yet" not in page.fit_label.text()
	assert "fitted 10 to 20 Hz" in page.fit_label.text()


def test_repeats_that_disagree_are_warned_about(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	weigh(qtbot, page, bridge, 10.0, 60.0, 40.0)
	assert "disagree" in page.warnings_label.text()


def test_a_material_not_in_the_file_is_warned_about(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	page.material_combo.setCurrentText("PP")
	page.dispenser.setText("A")
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	assert "PP is not in the materials file" in page.warnings_label.text()


def test_removing_a_weighing_saves_the_file_without_it(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	weigh(qtbot, page, bridge, 20.0, 60.0, 66.0)
	page.table.selectRow(0)
	page.remove_button.click()
	wait_idle(qtbot, bridge)
	assert [m.speed_hz for m in stored(config).curve("A", "PLA").measurements] == [20.0]
	assert "removed 1 weighing from A / PLA" in page.message_label.text()


def test_a_weighing_that_is_not_valid_is_refused(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	page.dispenser.setText("")
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	assert page.message_label.text().startswith("not added:")
	assert not config.resolve(config.paths.calibration).exists()


def test_the_curves_already_in_the_file_are_shown(qtbot, session, config, bridge):
	store = CalibrationStore()
	store.add_measurement("A", "PLA", 10.0, 30.0, 60.0)
	save_calibration(store, config.resolve(config.paths.calibration))
	page = make_page(qtbot, session, config, bridge)
	assert page.table.rowCount() == 1
	page.dispenser.setText("B")
	assert "B / PLA: no weighings yet" in page.curve_label.text()


def test_an_old_file_is_refused_and_left_alone(qtbot, session, config, bridge):
	path = config.resolve(config.paths.calibration)
	path.write_text(json.dumps({"points": [{"speed": 10, "rate": 0.5}]}), encoding="utf-8")
	before = path.read_bytes()
	page = make_page(qtbot, session, config, bridge)
	assert not page.save_button.isEnabled()
	assert "calibrate import" in page.message_label.text() or "fix it by hand" in page.message_label.text()
	assert path.read_bytes() == before


def test_a_save_that_fails_keeps_the_weighing_and_says_so(qtbot, session, config, bridge, monkeypatch):
	import polydriver.gui.calibration_page as module

	def refuse(store, path):
		raise OSError("disk full")

	monkeypatch.setattr(module, "save_calibration", refuse)
	page = make_page(qtbot, session, config, bridge)
	weigh(qtbot, page, bridge, 10.0, 60.0, 30.0)
	assert "NOT SAVED" in page.message_label.text() and "disk full" in page.message_label.text()
	assert page.table.rowCount() == 1


def test_new_materials_reach_the_material_list(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	registry = MaterialRegistry()
	registry.put(Material("TPU", Kind.POLYMER, dispenser="C"))
	page.set_materials(registry)
	assert [page.material_combo.itemText(i) for i in range(page.material_combo.count())] == ["TPU"]

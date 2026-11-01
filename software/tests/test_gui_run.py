import threading
from types import SimpleNamespace

import pytest

from gui_fakes import FakeSession, make_status
from polydriver.calibration import CalibrationStore, save_calibration
from polydriver.config import Config, LoggingSettings
from polydriver.devices.board import Segment
from polydriver.devices.fleet import FleetUploadError
from polydriver.gui.bridge import Bridge
from polydriver.gui.procedures import Procedures
from polydriver.gui.run_page import RunPage
from polydriver.gui.telemetry import QtSink
from polydriver.jobs import save_job
from polydriver.jobs.editing import JobForm, form_job
from polydriver.materials import Kind, Material, MaterialRegistry, save_materials
from polydriver.protocol.constants import JobState, Role
from polydriver.run import ChannelProgress, RunProgress, RunState


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


def make_store():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 30.0), (20.0, 60.0), (40.0, 120.0)):
		store.add_measurement("A", "PP", speed_hz, grams, 60.0)
	for speed_hz, grams in ((10.0, 1.2), (10.0, 1.2), (20.0, 2.4), (40.0, 4.8)):
		store.add_measurement("B", "Red", speed_hz, grams, 60.0)
	return store


@pytest.fixture
def config(tmp_path):
	config = Config(base_dir=tmp_path, logging=LoggingSettings(enabled=False))
	registry = MaterialRegistry()
	registry.put(Material("PP", Kind.POLYMER, dispenser="A"))
	registry.put(Material("Red", Kind.MASTERBATCH, dispenser="B"))
	save_materials(registry, config.resolve(config.paths.materials))
	store = make_store()
	save_calibration(store, config.resolve(config.paths.calibration))
	write_job(config, store, "PP red")
	return config


def write_job(config, store, name, **changes):
	values = dict(name=name, polymer_material="PP", polymer_dispenser="A", colour_material="Red",
				colour_dispenser="B", duration_s=60.0, total_mass_g=None, total_flow_g_s=1.0, phi_start=0.02,
				phi_end=0.05, curve="linear")
	path = config.resolve(config.paths.jobs) / f"{name}.json"
	save_job(form_job(JobForm(**(values | changes)), store), path)
	return path


class Command:
	def gap_s(self):
		return 0.0012


class FakeRunner:
	made = []

	def __init__(self, session, job, materials, store, *, run_log=None):
		self.job, self.run_log = job, run_log
		self.state = RunState.IDLE
		self.reason = None
		self.program = None
		self.finished = threading.Event()
		self.refuse = None
		self.elapsed_s = 0.0
		FakeRunner.made.append(self)

	def upload(self):
		if self.refuse is not None:
			raise self.refuse
		segments = (Segment(10.0, 30000), Segment(20.0, 30000, ramp=True))
		self.program = SimpleNamespace(polymer=SimpleNamespace(segments=segments),
									colour=SimpleNamespace(segments=segments[:1]), total_ms=60000)
		self.state = RunState.READY
		return self.program

	def start(self):
		self.state = RunState.RUNNING
		return Command()

	def stop(self):
		self.state, self.reason = RunState.ABORTED, "stopped by the operator"
		self.finished.set()
		return Command()

	def finish(self):
		self.state = RunState.DONE
		self.elapsed_s = 60.0
		self.finished.set()

	def wait(self, timeout_s=None):
		self.finished.wait(timeout_s)
		return self.state

	def progress(self):
		def channel(name, state, delivered):
			return ChannelProgress(name, state, 0, self.elapsed_s, delivered, 2.0)
		state = {RunState.READY: JobState.READY, RunState.RUNNING: JobState.RUNNING, RunState.DONE: JobState.DONE,
				RunState.ABORTED: JobState.ABORTED}.get(self.state)
		return RunProgress(self.state, self.elapsed_s, 60.0, self.elapsed_s / 60.0, channel("polymer", state, 1.5),
						channel("colour", state, 0.1), self.reason)


@pytest.fixture(autouse=True)
def fresh_runners():
	FakeRunner.made.clear()


class FakeLog:
	def __init__(self, path):
		self.csv_path = path / "run-1.csv"
		self.sidecar_path = path / "run-1.json"
		self.closed = False
		self.weighed = {}
		self.note = None

	def close(self, result=None):
		self.closed = True

	def set_weighed_mass(self, role, grams):
		self.weighed[role] = grams

	def set_note(self, note):
		self.note = note


def make_page(qtbot, session, config, bridge, procedures=None, logs=None):
	def start_log(session, config, *, note=""):
		log = FakeLog(config.base_dir)
		if logs is not None:
			logs.append((log, note))
		return log

	page = RunPage(session, config, bridge, QtSink(), runner=FakeRunner, start_log=start_log, procedures=procedures)
	qtbot.addWidget(page)
	return page


def uploaded(qtbot, page):
	page.upload_button.click()
	qtbot.waitUntil(lambda: page.runner is not None, timeout=2000)
	return page.runner


def test_the_checklist_passes_with_both_boards_and_a_job_that_fits(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	assert page.job_combo.currentText() == "PP red"
	text = page.checks_label.text()
	assert "✓ both boards connected" in text and "✓ the same firmware on both" in text
	assert "✓ the job fits the calibrations" in text
	assert page.upload_button.isEnabled() and not page.start_button.isEnabled()


def test_a_missing_board_fails_the_checklist_and_blocks_upload(qtbot, session, config, bridge):
	session.disconnect(Role.COLOUR)
	page = make_page(qtbot, session, config, bridge)
	assert "✗ not connected: colour" in page.checks_label.text()
	page.upload_button.click()
	assert page.message_label.text().startswith("not uploaded: not connected: colour")
	assert FakeRunner.made == []


def test_a_job_that_does_not_fit_fails_the_checklist(qtbot, session, config, bridge):
	write_job(config, make_store(), "too much", total_flow_g_s=5.0)
	page = make_page(qtbot, session, config, bridge)
	page.job_combo.setCurrentText("too much")
	assert "✗ the job does not fit: error: polymer" in page.checks_label.text()


def test_logging_asks_for_a_writable_folder(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	page.log_combo.setCurrentIndex(1)
	assert "✓ log folder" in page.checks_label.text()


def test_upload_then_start_once_the_operator_confirms(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	runner = uploaded(qtbot, page)
	assert runner.job.name == "PP red" and runner.run_log is None
	assert "uploaded: polymer 2 segments, colour 1 segments, 60 s; no log" in page.message_label.text()
	assert "polymer READY" in page.lights[Role.POLYMER].text()
	assert not page.start_button.isEnabled()
	page.confirm.setChecked(True)
	assert page.start_button.isEnabled()
	page.start_button.click()
	qtbot.waitUntil(lambda: runner.state is RunState.RUNNING, timeout=2000)
	qtbot.waitUntil(lambda: "1.2 ms apart" in page.message_label.text(), timeout=2000)
	assert page.stop_button.isEnabled() and not page.upload_button.isEnabled()


def test_the_end_of_a_run_shows_its_summary(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	runner = uploaded(qtbot, page)
	page.confirm.setChecked(True)
	page.start_button.click()
	qtbot.waitUntil(lambda: runner.state is RunState.RUNNING, timeout=2000)
	ended = []
	page.run_ended.connect(ended.append)
	runner.finish()
	qtbot.waitUntil(lambda: page.summary_box.isVisibleTo(page), timeout=3000)
	text = page.summary_label.text()
	assert text.startswith("DONE") and "polymer: about 1.5 g by the encoder (expected 2 g)" in text
	assert ended == [runner]
	assert page.upload_button.isEnabled()


def test_stop_run_stops_it(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	runner = uploaded(qtbot, page)
	page.stop_button.click()
	qtbot.waitUntil(lambda: page.summary_box.isVisibleTo(page), timeout=3000)
	assert page.summary_label.text().startswith("ABORTED: stopped by the operator")
	assert runner.state is RunState.ABORTED


def test_the_windows_stop_stops_the_run(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	uploaded(qtbot, page)
	page.stop_now()
	assert page.runner.state is RunState.ABORTED
	qtbot.waitUntil(lambda: page.summary_box.isVisibleTo(page), timeout=3000)


def test_a_run_is_one_of_the_windows_procedures(qtbot, session, config, bridge):
	procedures = Procedures()
	page = make_page(qtbot, session, config, bridge, procedures)
	runner = uploaded(qtbot, page)
	assert procedures.running == "the run"
	runner.stop()
	qtbot.waitUntil(lambda: procedures.running is None, timeout=3000)


def test_no_upload_while_another_procedure_runs(qtbot, session, config, bridge):
	procedures = Procedures()
	procedures.begin("the sweep")
	page = make_page(qtbot, session, config, bridge, procedures)
	assert not page.upload_button.isEnabled()
	page.upload()
	assert "the sweep is running" in page.message_label.text()


def test_a_failed_upload_closes_its_log_and_frees_the_procedures(qtbot, session, config, bridge, monkeypatch):
	procedures = Procedures()
	logs = []
	page = make_page(qtbot, session, config, bridge, procedures, logs)
	page.log_combo.setCurrentIndex(1)
	monkeypatch.setattr(FakeRunner, "upload", lambda self: (_ for _ in ()).throw(
		FleetUploadError({Role.COLOUR: OSError("COM5 lost")})))
	page.upload_button.click()
	qtbot.waitUntil(lambda: page.message_label.text().startswith("not uploaded"), timeout=2000)
	assert "COM5 lost" in page.message_label.text()
	assert logs[0][0].closed and procedures.running is None and page.runner is None


def test_a_logged_run_names_its_log_and_takes_the_weighed_masses(qtbot, session, config, bridge):
	logs = []
	page = make_page(qtbot, session, config, bridge, logs=logs)
	page.log_combo.setCurrentIndex(1)
	page.note.setText("first try")
	runner = uploaded(qtbot, page)
	log, note = logs[0]
	assert runner.run_log is log and note == "first try"
	assert "log " in page.message_label.text() and "run-1.csv" in page.message_label.text()
	runner.finish()
	qtbot.waitUntil(lambda: page.summary_box.isVisibleTo(page), timeout=3000)
	page.weighed[Role.POLYMER].setValue(55.5)
	page.weighed_note.setText("weighed after cooling")
	page.save_weighed_button.click()
	qtbot.waitUntil(lambda: page.message_label.text().startswith("saved to"), timeout=2000)
	assert log.weighed == {Role.POLYMER: 55.5} and log.note == "weighed after cooling"


def test_the_job_decides_the_log_unless_changed(qtbot, session, config, bridge):
	write_job(config, make_store(), "logged", log=True)
	page = make_page(qtbot, session, config, bridge)
	page.job_combo.setCurrentText("logged")
	assert page.log_combo.itemText(0) == "as the job says (log)" and page.logging_on()
	page.log_combo.setCurrentIndex(2)
	assert not page.logging_on()
	page.job_combo.setCurrentText("PP red")
	page.log_combo.setCurrentIndex(0)
	assert page.log_combo.itemText(0) == "as the job says (no log)" and not page.logging_on()


def test_set_job_chooses_it(qtbot, session, config, bridge):
	path = write_job(config, make_store(), "second")
	page = make_page(qtbot, session, config, bridge)
	page.set_job(path)
	assert page.job_combo.currentText() == "second"


def test_commanded_and_measured_speed_are_plotted_while_it_runs(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	runner = uploaded(qtbot, page)
	runner.state = RunState.RUNNING
	status = make_status(filtered_hz=9.0, job_state=JobState.RUNNING)
	status = SimpleNamespace(report=SimpleNamespace(**{**status.report.__dict__, "job_elapsed_ms": 45000}))
	page._on_status(Role.POLYMER, status, 1.0)
	x, y = page.curves[(Role.POLYMER, "commanded")].getData()
	assert list(x) == [45.0] and list(y) == [pytest.approx(15.0)]
	assert list(page.curves[(Role.POLYMER, "measured")].getData()[1]) == [9.0]


def test_start_needs_the_operators_confirmation(qtbot, session, config, bridge):
	page = make_page(qtbot, session, config, bridge)
	runner = uploaded(qtbot, page)
	page.start()
	qtbot.wait(100)
	assert runner.state is RunState.READY


def test_different_firmware_fails_the_checklist(qtbot, session, config, bridge):
	from dataclasses import replace
	real_state = session.state

	def state(role):
		found = real_state(role)
		return replace(found, version=replace(found.version, firmware_minor=9)) if role is Role.COLOUR else found

	session.state = state
	page = make_page(qtbot, session, config, bridge)
	assert "✗ different firmware: polymer 3.0.0, colour 3.9.0" in page.checks_label.text()
	assert not page.ready_to_upload()

"""The window on two simulated boards (polydriver.sim), driven as a person would: the PID test of
29.10. Real time, so these take a few seconds each."""

from PySide6.QtCore import Qt

from polydriver.gui import main
from polydriver.protocol.constants import Role, StatusFlag

CONFIG = """
[safety]
comms_timeout_s = 2.0

[board.polymer]
kp = 0.5
ki = 2.0
kd = 0.0
alpha = 0.5
ff_offset_duty = 128.0
ff_gain_duty_per_hz = 1.67

[board.colour]
kp = 0.5
ki = 2.0
kd = 0.0
alpha = 0.5
ff_offset_duty = 128.0
ff_gain_duty_per_hz = 1.67

[logging]
poll_hz = 20.0
"""


def write_config(tmp_path):
	path = tmp_path / "polydriver.toml"
	path.write_text(CONFIG, encoding="utf-8")
	return path


def click(qtbot, button):
	qtbot.mouseClick(button, Qt.MouseButton.LeftButton)


def last(values, default):
	return values[-1] if values else default


def connect_both(qtbot, window):
	devices = window.screen("Devices")
	click(qtbot, devices.connect_both_button)
	qtbot.waitUntil(lambda: all(panel.connected for panel in devices.panels.values()), timeout=15000)
	return devices


def test_a_simulated_screw_follows_a_speed_and_stops(qtbot, tmp_path):
	def run(window):
		devices = connect_both(qtbot, window)
		manual = window.screen("Manual")
		manual.speed.setValue(10.0)
		click(qtbot, manual.speed_button)
		qtbot.waitUntil(lambda: manual.target_hz(Role.POLYMER) == 10.0, timeout=5000)
		filtered = manual.traces[Role.POLYMER]
		qtbot.waitUntil(lambda: abs(last(filtered.values("filtered"), 0.0) - 10.0) < 1.0, timeout=15000)
		assert devices.panels[Role.POLYMER].lights[StatusFlag.REGULATING].is_on
		assert not devices.panels[Role.COLOUR].lights[StatusFlag.REGULATING].is_on
		with qtbot.waitSignal(window.stopped, timeout=5000):
			window.stop_everything()
		assert manual.target_hz(Role.POLYMER) == 0.0
		qtbot.waitUntil(lambda: not devices.panels[Role.POLYMER].lights[StatusFlag.REGULATING].is_on, timeout=5000)
		return 0

	assert main(["--sim", "--config", str(write_config(tmp_path))], run=run) == 0


def test_gains_sent_from_the_window_reach_the_simulated_board(qtbot, tmp_path):
	def run(window):
		connect_both(qtbot, window)
		manual = window.screen("Manual")
		manual.kp.setValue(0.75)
		manual.alpha.setValue(0.3)
		click(qtbot, manual.send_button)
		qtbot.waitUntil(lambda: manual.board_label.text().startswith("on the board: pid 0.75 2 0, alpha 0.3"),
						timeout=5000)
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)


def test_a_recording_writes_a_run_log(qtbot, tmp_path):
	def run(window):
		connect_both(qtbot, window)
		manual = window.screen("Manual")
		click(qtbot, manual.record_button)
		qtbot.waitUntil(lambda: manual.log is not None, timeout=5000)
		manual.speed.setValue(5.0)
		click(qtbot, manual.speed_button)
		qtbot.wait(1000)
		click(qtbot, manual.record_button)
		qtbot.waitUntil(lambda: manual.record_label.text().startswith("saved "), timeout=5000)
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	logs = sorted((tmp_path / "logs").glob("run-*.csv"))
	assert len(logs) == 1
	assert len(logs[0].read_text(encoding="utf-8").splitlines()) > 10
	assert logs[0].with_suffix(".json").exists()


def test_a_sweep_from_the_window_fits_the_simulated_motor(qtbot, tmp_path):
	fits = []

	def run(window):
		connect_both(qtbot, window)
		panel = window.screen("Manual").sweep_panel
		panel.from_duty.setValue(140)
		panel.to_duty.setValue(200)
		panel.step_duty.setValue(20)
		panel.hold.setValue(1.2)
		click(qtbot, panel.run_button)
		qtbot.waitUntil(lambda: not panel.running and panel.result_label.text() != "", timeout=20000)
		fits.append((panel.fit, panel.result_label.text()))
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	fit, text = fits[0]
	assert fit is not None, text
	assert abs(fit.ff_offset_duty - 128) < 6 and abs(fit.ff_gain_duty_per_hz - 1 / 0.6) < 0.2
	assert len(list((tmp_path / "logs").glob("sweep-polymer-*.csv"))) == 1


def test_a_step_test_from_the_window_fills_its_table_and_saves(qtbot, tmp_path):
	rows = []

	def run(window):
		connect_both(qtbot, window)
		panel = window.screen("Manual").step_panel
		panel.steps.setText("0, 8")
		panel.hold.setValue(3.0)
		click(qtbot, panel.run_button)
		qtbot.waitUntil(lambda: not panel.running and panel.status_label.text().startswith("saved"), timeout=20000)
		rows.extend(panel.rows())
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	assert rows[0][0] == "0 → 8" and rows[0][1] != "—"
	assert len(list((tmp_path / "logs").glob("run-*-steps.csv"))) == 1


def test_store_role_and_link_counters_work_on_a_simulated_board(qtbot, tmp_path):
	notes = []

	def run(window):
		devices = connect_both(qtbot, window)
		panel = devices.panels[Role.COLOUR]
		click(qtbot, panel.store_role_button)
		qtbot.waitUntil(lambda: panel.note_label.text().startswith("stored"), timeout=5000)
		notes.append(panel.note_label.text())
		click(qtbot, panel.counters_button)
		qtbot.waitUntil(lambda: panel.note_label.text().startswith("PC:"), timeout=5000)
		notes.append(panel.note_label.text())
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	assert notes[0] == "stored the colour role on SIM-COLOUR: a swapped port is caught from now on"
	assert "retries" in notes[1] and "board: dropped 0, bad CRC 0" in notes[1]


def test_a_calibration_run_on_a_simulated_board_and_its_weighing(qtbot, tmp_path):
	from polydriver.calibration import load_calibration
	seen = {}

	def run(window):
		connect_both(qtbot, window)
		page = window.screen("Calibration")
		page.material_combo.setEditText("PLA")
		page.dispenser.setText("A")
		page.speed.setValue(8.0)
		page.seconds.setValue(1.5)
		click(qtbot, page.run_button)
		qtbot.waitUntil(lambda: page.run_button.text() == "Run" and "done" in page.progress_label.text(),
						timeout=10000)
		seen["progress"] = page.progress_label.text()
		page.grams.setValue(3.0)
		click(qtbot, page.save_button)
		qtbot.waitUntil(lambda: "saved to" in page.message_label.text(), timeout=3000)
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	assert "8 Hz for 1.5 s" in seen["progress"]
	(m,) = load_calibration(tmp_path / "calibration.json").curve("A", "PLA").measurements
	assert (m.speed_hz, m.duration_s, m.grams, m.method) == (8.0, 1.5, 3.0, "board-timed")


def test_a_timed_run_on_a_simulated_board_stops_by_itself(qtbot, tmp_path):
	seen = {}

	def run(window):
		connect_both(qtbot, window)
		manual = window.screen("Manual")
		panel = manual.timed_panel
		panel.unit.setCurrentText("Hz")
		panel.value.setValue(9.0)
		panel.seconds.setValue(1.0)
		click(qtbot, panel.run_button)
		qtbot.waitUntil(lambda: panel.progress_label.text().startswith("done"), timeout=10000)
		seen["text"] = panel.progress_label.text()
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	assert seen["text"].startswith("done: 9 Hz for 1 s")


def test_settings_saved_on_simulated_boards_reach_them(qtbot, tmp_path):
	path = write_config(tmp_path)
	seen = {}

	def run(window):
		connect_both(qtbot, window)
		page = window.screen("Settings")
		page.fields[("board.colour", "alpha")].setText("0.25")
		click(qtbot, page.save_button)
		qtbot.waitUntil(lambda: "sent to the connected boards" in page.message_label.text(), timeout=5000)
		seen["alpha"] = window.screen("Manual").session.board(Role.COLOUR).settings().alpha
		return 0

	main(["--sim", "--config", str(path)], run=run)
	assert seen["alpha"] == 0.25
	assert "SIM" not in path.read_text(encoding="utf-8")


def test_a_gradient_job_made_on_jobs_runs_on_simulated_boards(qtbot, tmp_path):
	from polydriver.calibration import CalibrationStore, save_calibration
	from polydriver.materials import Kind, Material, MaterialRegistry, save_materials
	from polydriver.run import RunState
	registry = MaterialRegistry()
	registry.put(Material("PP", Kind.POLYMER, dispenser="A"))
	registry.put(Material("Red", Kind.MASTERBATCH, dispenser="B"))
	save_materials(registry, tmp_path / "materials.json")
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 30.0), (20.0, 60.0), (40.0, 120.0)):
		store.add_measurement("A", "PP", speed_hz, grams, 60.0)
	for speed_hz, grams in ((10.0, 1.2), (10.0, 1.2), (20.0, 2.4), (40.0, 4.8)):
		store.add_measurement("B", "Red", speed_hz, grams, 60.0)
	save_calibration(store, tmp_path / "calibration.json")
	seen = {}

	def run(window):
		connect_both(qtbot, window)
		jobs = window.screen("Jobs")
		editor = jobs.editor
		editor.new()
		editor.name.setText("sim gradient")
		editor.duration.setText("3")
		editor.flow.setText("1")
		editor.phi_start.setText("2")
		editor.phi_end.setText("5")
		click(qtbot, editor.save_button)
		assert editor.message_label.text() == "saved sim gradient.json", editor.message_label.text()
		jobs.table.selectRow(0)
		click(qtbot, jobs.run_button)
		assert window.current_page() == "Run"
		page = window.screen("Run")
		assert page.job_combo.currentText() == "sim gradient"
		page.log_combo.setCurrentIndex(1)
		click(qtbot, page.upload_button)
		qtbot.waitUntil(lambda: page.runner is not None and page.runner.state is RunState.READY, timeout=10000)
		page.confirm.setChecked(True)
		click(qtbot, page.start_button)
		qtbot.waitUntil(lambda: page.summary_box.isVisibleTo(page), timeout=15000)
		seen["summary"] = page.summary_label.text()
		seen["log"] = page.log.sidecar_path
		page.weighed[Role.COLOUR].setValue(0.11)
		click(qtbot, page.save_weighed_button)
		qtbot.waitUntil(lambda: page.message_label.text().startswith("saved to"), timeout=3000)
		return 0

	main(["--sim", "--config", str(write_config(tmp_path))], run=run)
	assert seen["summary"].startswith("DONE"), seen["summary"]
	import json
	sidecar = json.loads(seen["log"].read_text(encoding="utf-8"))
	assert sidecar["result"] == "DONE" and sidecar["weighed_mass_g"]["colour"] == 0.11

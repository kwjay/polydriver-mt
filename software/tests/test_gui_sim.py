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

"""The thesis figures of the window (C10.8): every screen on two simulated boards, with a demo
material, calibration and job, saved as PNG. Run it on the PC whose look the figures should have:

    python -m polydriver.gui.screenshots --out ..\\docs\\figures\\gui

The demo files go to a temporary folder (or --work), never next to the real config."""

import argparse
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from ..calibration import CalibrationStore, save_calibration
from ..jobs import save_job
from ..jobs.editing import JobForm, form_job
from ..materials import Kind, Material, MaterialRegistry, save_materials
from ..protocol.constants import Role
from ..run import RunState

CONFIG = """\
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
SIZE = (1400, 900)
JOB_S = 20.0


def write_demo(folder: Path) -> Path:
	"""polydriver.toml, a material per board, their calibrations and one job; returns the config."""
	folder.mkdir(parents=True, exist_ok=True)
	config = folder / "polydriver.toml"
	config.write_text(CONFIG, encoding="utf-8")
	registry = MaterialRegistry()
	registry.put(Material("PP", Kind.POLYMER, dispenser="A", flow_min_g_s=0.2, flow_max_g_s=2.5, note="demo"))
	registry.put(Material("MB-red", Kind.MASTERBATCH, dispenser="B", note="demo"))
	save_materials(registry, folder / "materials.json")
	store = CalibrationStore()
	for speed_hz, grams in ((5.0, 0.0), (10.0, 29.1), (10.0, 30.4), (20.0, 61.2), (20.0, 59.8), (30.0, 88.9),
							(40.0, 121.5)):
		store.add_measurement("A", "PP", speed_hz, grams, 60.0, note="demo")
	for speed_hz, grams in ((10.0, 1.18), (10.0, 1.22), (20.0, 2.41), (30.0, 3.55), (40.0, 4.83)):
		store.add_measurement("B", "MB-red", speed_hz, grams, 60.0, note="demo")
	save_calibration(store, folder / "calibration.json")
	form = JobForm("PP red 2-5 % S-curve", "PP", "A", "MB-red", "B", JOB_S, None, 1.0, 0.02, 0.05, "s-curve", 8.0,
				hold_start_s=2.0, hold_end_s=2.0, log=True)
	save_job(form_job(form, store), folder / "jobs" / "PP red 2-5 % S-curve.json")
	return config


def wait(seconds: float, until=None) -> bool:
	"""Lets the window run for up to seconds, or until until() is true."""
	end = time.monotonic() + seconds
	while time.monotonic() < end:
		if until is not None and until():
			return True
		loop = QEventLoop()
		QTimer.singleShot(50, loop.quit)
		loop.exec()
	return until is None or until()


def take(window, out: Path, name: str, saved: list[Path]) -> None:
	wait(0.3)
	path = out / f"{name}.png"
	window.grab().save(str(path))
	saved.append(path)


def shoot(window, out: Path) -> list[Path]:
	saved: list[Path] = []
	window.resize(*SIZE)
	devices = window.screen("Devices")
	window.show_page("Devices")
	devices.connect_both_button.click()
	wait(15, lambda: all(panel.connected for panel in devices.panels.values()))
	take(window, out, "devices", saved)

	window.show_page("Manual")
	manual = window.screen("Manual")
	manual.speed.setValue(12.0)
	manual.speed_button.click()
	wait(6)
	take(window, out, "manual", saved)
	manual.stop_board()
	wait(1)

	window.show_page("Calibration")
	calibration = window.screen("Calibration")
	calibration.material_combo.setCurrentText("PP")
	calibration.show_curve()
	take(window, out, "calibration", saved)

	window.show_page("Materials")
	take(window, out, "materials", saved)

	window.show_page("Jobs")
	jobs = window.screen("Jobs")
	jobs.table.selectRow(0)
	jobs.editor.update_preview()
	take(window, out, "jobs", saved)

	jobs.run_button.click()
	run = window.screen("Run")
	run.confirm.setChecked(True)
	take(window, out, "run-checklist", saved)
	run.upload_button.click()
	wait(10, lambda: run.runner is not None and run.runner.state is RunState.READY)
	run.start_button.click()
	wait(JOB_S / 2)
	take(window, out, "run", saved)
	wait(JOB_S + 5, lambda: run.summary_box.isVisible())
	run.weighed[Role.POLYMER].setValue(19.6)
	run.weighed[Role.COLOUR].setValue(0.68)
	take(window, out, "run-summary", saved)
	run.save_weighed_button.click()
	wait(1)

	window.show_page("History")
	history = window.screen("History")
	history.refresh()
	history.table.selectRow(0)
	take(window, out, "history", saved)

	window.show_page("Settings")
	take(window, out, "settings", saved)
	return saved


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(prog="python -m polydriver.gui.screenshots", description=__doc__.splitlines()[0])
	parser.add_argument("--out", required=True, help="folder for the PNG files")
	parser.add_argument("--work", help="folder for the demo config and files (default: a temporary one)")
	args = parser.parse_args(argv)
	from . import main as gui_main
	out = Path(args.out)
	out.mkdir(parents=True, exist_ok=True)
	QApplication.instance() or QApplication(sys.argv[:1])
	with tempfile.TemporaryDirectory(prefix="polydriver-figures-") as temporary:
		config = write_demo(Path(args.work) if args.work else Path(temporary))
		saved: list[Path] = []
		gui_main(["--sim", "--config", str(config)], run=lambda window: saved.extend(shoot(window, out)) or 0)
	for path in saved:
		print(path)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())

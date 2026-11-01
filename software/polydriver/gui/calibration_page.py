"""Calibration: run a screw at one speed for T seconds (the board times it and stops), weigh what came
out, type the grams; every weighing is saved to the calibration file at once, so none is lost to a
closed window. The curve of the chosen dispenser and material is shown: its weighings, the rate
against speed, the fit report and the warnings the shell gives.

A weighing typed without a run here is kept too (method steady-state, as `calibrate add`); one that
follows a completed run here with the same speed and time is marked method board-timed."""

from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
	QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
	QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..bench import Cancelled, TimedRunError, run_segments, speed_segments
from ..calibration import (
	DEFAULT_METHOD, CalibrationError, CalibrationStore, NotEnoughPointsError, OldCalibrationFileError,
	UnknownCurveError, curve_warnings, fit_report, format_fit_report, load_calibration, lowest_producing_speed,
	save_calibration,
)
from ..config import Config
from ..materials import MaterialError, MaterialRegistry, load_materials
from ..protocol.constants import Role
from .bridge import Bridge
from .messages import error_text, role_title
from .procedures import ProcedureBusyError, Procedures

ROLES = (Role.POLYMER, Role.COLOUR)
BOARD_TIMED = "board-timed"
MEASUREMENT_COLUMNS = ("#", "speed [Hz]", "grams", "seconds", "rate [g/s]", "taken", "method", "note")


class CalibrationPage(QWidget):
	_progressed = Signal(int, int)

	def __init__(self, session, config: Config, bridge: Bridge, *, run=run_segments,
				procedures: Procedures | None = None):
		super().__init__()
		self.session = session
		self.bridge = bridge
		self.registry = procedures or Procedures(self)
		self._run = run
		self.path: Path = config.resolve(config.paths.calibration)
		self.store = CalibrationStore()
		self.materials = MaterialRegistry()
		self._materials_path = config.resolve(config.paths.materials)
		self._cancel = None
		self._last_run: tuple[float, float] | None = None

		self.role_combo = QComboBox()
		for role in ROLES:
			self.role_combo.addItem(f"{role_title(role)} board", role)
		self.material_combo = QComboBox()
		self.material_combo.setEditable(True)
		self.dispenser = QLineEdit()
		self.dispenser.setPlaceholderText("e.g. A or B")
		self.speed = _spin(0.5, 200.0, 2, 10.0, " Hz")
		self.seconds = _spin(1.0, 600.0, 1, 60.0, " s")
		self.run_button = QPushButton("Run")
		self.progress_label = QLabel("")
		self.grams = _spin(0.0, 100000.0, 2, 0.0, " g")
		self.note = QLineEdit()
		self.save_button = QPushButton("Save weighing")
		self.message_label = QLabel("")
		self.message_label.setWordWrap(True)

		self.curve_label = QLabel("")
		self.table = QTableWidget(0, len(MEASUREMENT_COLUMNS))
		self.table.setHorizontalHeaderLabels(MEASUREMENT_COLUMNS)
		self.table.verticalHeader().setVisible(False)
		self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
		self.remove_button = QPushButton("Remove selected weighing")
		self.fit_label = QLabel("")
		self.fit_label.setWordWrap(True)
		self.fit_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		self.fit_label.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
		self.warnings_label = QLabel("")
		self.warnings_label.setWordWrap(True)
		self.warnings_label.setStyleSheet("color: #ef6c00;")
		self.plot = pg.PlotWidget()
		self.plot.setLabel("bottom", "speed", units="Hz")
		self.plot.setLabel("left", "rate", units="g/s")
		self.weighings = pg.ScatterPlotItem(size=8, brush=pg.mkBrush("#1565c0"))
		self.means = self.plot.plot([], [], pen=pg.mkPen("#9e9e9e", width=2))
		self.plot.addItem(self.weighings)

		self._lay_out()
		self.material_combo.currentTextChanged.connect(self._material_chosen)
		self.dispenser.textChanged.connect(lambda _: self.show_curve())
		self.run_button.clicked.connect(self.toggle_run)
		self.save_button.clicked.connect(self.save_weighing)
		self.remove_button.clicked.connect(self.remove_selected)
		self._progressed.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
		self.registry.changed.connect(self._procedure_running)
		self._procedure_running(self.registry.running is not None)
		self._load()

	def _lay_out(self) -> None:
		run = QGroupBox("1. Run the screw")
		form = QFormLayout(run)
		form.addRow("board", self.role_combo)
		form.addRow("material", self.material_combo)
		form.addRow("dispenser", self.dispenser)
		form.addRow("speed", self.speed)
		form.addRow("for", self.seconds)
		form.addRow(self.run_button)
		form.addRow(self.progress_label)
		weigh = QGroupBox("2. Weigh and save")
		weigh_form = QFormLayout(weigh)
		weigh_form.addRow("weighed", self.grams)
		weigh_form.addRow("note", self.note)
		weigh_form.addRow(self.save_button)
		left = QVBoxLayout()
		left.addWidget(run)
		left.addWidget(weigh)
		left.addWidget(self.message_label)
		left.addStretch(1)
		right = QVBoxLayout()
		right.addWidget(self.curve_label)
		right.addWidget(self.table, 2)
		right.addWidget(self.remove_button)
		right.addWidget(self.plot, 2)
		right.addWidget(self.fit_label)
		right.addWidget(self.warnings_label)
		layout = QHBoxLayout(self)
		layout.addLayout(left)
		layout.addLayout(right, 1)

	# --- Files ------------------------------------------------------------------------------------

	def _load(self) -> None:
		try:
			self.store = load_calibration(self.path)
		except OldCalibrationFileError:
			self._refuse_file(f"{self.path} is an old one-board calibration file: import it in the shell with "
							f"`calibrate import {self.path} MATERIAL`")
		except CalibrationError as error:
			self._refuse_file(f"{error}\nThe file is left as it is: fix it by hand, then restart.")
		try:
			self.set_materials(load_materials(self._materials_path))
		except MaterialError as error:
			self.message_label.setText(str(error))
		self.show_curve()

	def _refuse_file(self, text: str) -> None:
		self.store = CalibrationStore()
		self.save_button.setEnabled(False)
		self.remove_button.setEnabled(False)
		self.message_label.setText(text)

	def set_materials(self, registry: MaterialRegistry) -> None:
		self.materials = registry
		current = self.material_combo.currentText()
		self.material_combo.blockSignals(True)
		self.material_combo.clear()
		self.material_combo.addItems([m.name for m in registry.materials()])
		self.material_combo.setCurrentText(current or (self.material_combo.itemText(0) if registry.materials() else ""))
		self.material_combo.blockSignals(False)
		self._material_chosen(self.material_combo.currentText())

	def _material_chosen(self, name: str) -> None:
		try:
			material = self.materials.get(name)
		except MaterialError:
			material = None
		if material is not None:
			if material.dispenser:
				self.dispenser.setText(material.dispenser)
			self.role_combo.setCurrentIndex(ROLES.index(material.role))
		self.show_curve()

	# --- Running ----------------------------------------------------------------------------------

	def toggle_run(self) -> None:
		if self._cancel is not None:
			self.run_button.setEnabled(False)
			cancel = self._cancel
			self.bridge.run_now(cancel.cancel, name="stop the calibration run",
								on_result=lambda _: self.run_button.setEnabled(True))
			return
		role, speed_hz, seconds = self.role_combo.currentData(), self.speed.value(), self.seconds.value()
		try:
			segments = speed_segments(speed_hz, seconds)
			self._cancel = self.registry.begin("the calibration run")
		except (TimedRunError, ProcedureBusyError) as error:
			self.progress_label.setText(str(error))
			return
		self.run_button.setText("Stop run")
		self.run_button.setEnabled(True)
		self._last_run = None
		self.progress_label.setText(f"running {role.name.lower()} at {speed_hz:g} Hz for {seconds:g} s…")
		self.bridge.run_now(self._run, self.session, role, segments, cancel=self._cancel,
							on_progress=lambda elapsed, total: self._progressed.emit(elapsed, total),
							name=f"calibration run {role.name.lower()}",
							on_result=lambda result: self._ran(result, speed_hz, seconds),
							on_error=lambda error: self._run_failed(error, role))

	def cancel(self) -> None:
		"""Thread-safe."""
		cancel = self._cancel
		if cancel is not None:
			cancel.cancel()

	def _ran(self, result, speed_hz: float, seconds: float) -> None:
		self._end_run()
		if result.done:
			self._last_run = (speed_hz, seconds)
			self.progress_label.setText(f"done: {speed_hz:g} Hz for {seconds:g} s ({result.edges} encoder edges). "
										"Weigh what came out and type the grams below.")
			self.grams.setFocus()
		else:
			why = "stopped early" if result.ended_early else f"the board ended the job {result.state.name}"
			extra = f"; {result.cleanup_problem}" if result.cleanup_problem else ""
			self.progress_label.setText(f"{why} after {result.elapsed_ms / 1000:g} s{extra}: do not use this weighing")

	def _run_failed(self, error: BaseException, role: Role) -> None:
		self._end_run()
		text = "stopped early" if isinstance(error, Cancelled) else error_text("calibration run", error, role)
		self.progress_label.setText(f"run failed: {text}")

	def _end_run(self) -> None:
		cancel, self._cancel = self._cancel, None
		self.run_button.setText("Run")
		self.run_button.setEnabled(True)
		if cancel is not None:
			self.registry.end(cancel)

	def _on_progress(self, elapsed_ms: int, total_ms: int) -> None:
		if self._cancel is not None:
			self.progress_label.setText(f"running: {elapsed_ms / 1000:.1f} of {total_ms / 1000:g} s")

	def _procedure_running(self, running: bool) -> None:
		if self._cancel is None:
			self.run_button.setEnabled(not running)

	# --- Weighings --------------------------------------------------------------------------------

	def save_weighing(self) -> None:
		speed_hz, seconds, grams = self.speed.value(), self.seconds.value(), self.grams.value()
		method = BOARD_TIMED if self._last_run == (speed_hz, seconds) else DEFAULT_METHOD
		try:
			curve = self.store.add_measurement(self.dispenser.text(), self.material_combo.currentText(), speed_hz, grams,
												seconds, method=method, note=self.note.text())
		except CalibrationError as error:
			self.message_label.setText(f"not added: {error}")
			return
		self._last_run = None
		self.note.clear()
		self.grams.setValue(0.0)
		self.message_label.setText(f"{curve.dispenser} / {curve.material}: {speed_hz:g} Hz gave "
									f"{grams / seconds:.4g} g/s ({grams:g} g in {seconds:g} s), weighing "
									f"#{len(curve.measurements)}")
		self._write()
		self.show_curve()

	def remove_selected(self) -> None:
		rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
		key = self._key()
		if not rows or key is None:
			return
		for row in rows:
			self.store.remove_measurement(*key, row)
		self.message_label.setText(f"removed {len(rows)} weighing{'' if len(rows) == 1 else 's'} from {key[0]} / {key[1]}")
		self._write()
		self.show_curve()

	def _write(self) -> None:
		store, path = self.store, self.path
		self.bridge.run(save_calibration, store, path, name="save calibration",
						on_result=lambda _: self._saved(path),
						on_error=lambda error: self.message_label.setText(
							f"{self.message_label.text()}\nNOT SAVED: {error_text('save calibration', error)}; the "
							"weighing is kept here and the next save tries again"))

	def _saved(self, path: Path) -> None:
		self.message_label.setText(f"{self.message_label.text()}\nsaved to {path}")

	# --- The curve --------------------------------------------------------------------------------

	def _key(self) -> tuple[str, str] | None:
		dispenser, material = self.dispenser.text().strip(), self.material_combo.currentText().strip()
		return (dispenser, material) if dispenser and material else None

	def show_curve(self) -> None:
		key = self._key()
		curve = None
		if key is not None:
			try:
				curve = self.store.curve(*key)
			except (UnknownCurveError, CalibrationError):
				curve = None
		self.table.setRowCount(0)
		if curve is None:
			self.curve_label.setText(f"{key[0]} / {key[1]}: no weighings yet" if key else
									"choose a material and a dispenser")
			self.weighings.setData([], [])
			self.means.setData([], [])
			self.fit_label.setText("")
			self.warnings_label.setText("")
			return
		count = len(curve.measurements)
		self.curve_label.setText(f"{curve.dispenser} / {curve.material}: {count} weighing{'' if count == 1 else 's'}, "
								f"curve {curve.short_hash}")
		for number, m in enumerate(curve.measurements, start=1):
			cells = [str(number), f"{m.speed_hz:g}", f"{m.grams:g}", f"{m.duration_s:g}", f"{m.rate_g_s:.4g}",
					m.taken_at or "-", m.method, m.note]
			row = self.table.rowCount()
			self.table.insertRow(row)
			for column, text in enumerate(cells):
				self.table.setItem(row, column, QTableWidgetItem(text))
		self.weighings.setData([m.speed_hz for m in curve.measurements], [m.rate_g_s for m in curve.measurements])
		self.means.setData([p.speed_hz for p in curve.points], [p.rate_g_s for p in curve.points])
		lines = []
		lowest = lowest_producing_speed(curve)
		lines.append("no speed gave material yet" if lowest is None else f"lowest speed that gave material: {lowest:g} Hz")
		try:
			lines += format_fit_report(fit_report(curve))
		except (NotEnoughPointsError, CalibrationError) as error:
			lines.append(f"no fit yet: {error}")
		self.fit_label.setText("\n".join(lines))
		names = [m.name for m in self.materials.materials()]
		self.warnings_label.setText("\n".join(curve_warnings(curve, names)))

	def table_rows(self) -> list[list[str]]:
		return [[self.table.item(r, c).text() for c in range(self.table.columnCount())] for r in range(self.table.rowCount())]


def _spin(minimum: float, maximum: float, decimals: int, value: float, suffix: str) -> QDoubleSpinBox:
	spin = QDoubleSpinBox()
	spin.setRange(minimum, maximum)
	spin.setDecimals(decimals)
	spin.setValue(value)
	spin.setSuffix(suffix)
	return spin

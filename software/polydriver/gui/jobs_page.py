"""Jobs: the jobs folder as a list (New, Duplicate, Delete with a second click to confirm, Run) and
the job editor beside it (GUI_gradient_job_flow.md §2 screens 4 and 5).

The editor takes the two materials and their dispensers, two or three of duration, total mass and
total flow, and the colour share's start, end, curve and holds. The preview shows each channel's
g/s and the speed it needs over the run, points that do not fit in red, with the check of the job
against the calibrations as they are now (jobs.check_job) and the segments each board would get.
Save writes the job with each calibration's hash, so a later recalibration is seen; a job whose
calibration is missing is not saved. The working out is in jobs.editing; this screen only shows it."""

import math
from dataclasses import replace
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
	QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QTableWidget,
	QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..calibration import CalibrationError, CalibrationStore, load_calibration
from ..config import Config
from ..jobs import (
	CURVE_NAMES, DEFAULT_MAX_SEGMENTS, JobCompileError, JobError, check_job, compile_gradient_job, load_job, save_job,
)
from ..jobs.editing import (
	CURVE_PARAM, JobForm, duplicate_job, form_job, form_profile, free_job_path, job_form, list_jobs, parse_table,
	table_text,
)
from ..materials import Kind, MaterialError, MaterialRegistry, load_materials
from .bridge import Bridge
from .messages import error_text

LIST_COLUMNS = ("job", "polymer", "colour", "duration", "mass", "modified")
PREVIEW_POINTS = 200
LOG_CHOICES = (("as the config says", None), ("log", True), ("do not log", False))
PENS = {"polymer": pg.mkPen("#1565c0", width=2), "colour": pg.mkPen("#c62828", width=2)}


class JobsPage(QWidget):
	run_requested = Signal(object)
	"""The Path of the job to run."""

	def __init__(self, config: Config, bridge: Bridge):
		super().__init__()
		self.config = config
		self.bridge = bridge
		self.folder = config.resolve(config.paths.jobs)
		self.entries = []
		self.table = QTableWidget(0, len(LIST_COLUMNS))
		self.table.setHorizontalHeaderLabels(LIST_COLUMNS)
		self.table.verticalHeader().setVisible(False)
		self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
		self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
		self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
		self.new_button = QPushButton("New")
		self.duplicate_button = QPushButton("Duplicate")
		self.delete_button = QPushButton("Delete")
		self.cancel_delete_button = QPushButton("Keep it")
		self.cancel_delete_button.setVisible(False)
		self.run_button = QPushButton("Run…")
		self.run_button.setToolTip("Open this job on the Run screen")
		self.list_label = QLabel("")
		self.list_label.setWordWrap(True)
		self.editor = JobEditor(config)
		self._delete_armed: Path | None = None

		buttons = QHBoxLayout()
		for button in (self.new_button, self.duplicate_button, self.delete_button, self.cancel_delete_button,
					self.run_button):
			buttons.addWidget(button)
		left = QWidget()
		left_layout = QVBoxLayout(left)
		left_layout.addWidget(QLabel(f"folder: {self.folder}"))
		left_layout.addLayout(buttons)
		left_layout.addWidget(self.table, 1)
		left_layout.addWidget(self.list_label)
		splitter = QSplitter()
		splitter.addWidget(left)
		splitter.addWidget(self.editor)
		splitter.setStretchFactor(1, 2)
		layout = QVBoxLayout(self)
		layout.addWidget(splitter)

		self.table.itemSelectionChanged.connect(self._selected)
		self.new_button.clicked.connect(self.new_job)
		self.duplicate_button.clicked.connect(self.duplicate)
		self.delete_button.clicked.connect(self.delete)
		self.cancel_delete_button.clicked.connect(self._disarm)
		self.run_button.clicked.connect(self._run)
		self.editor.saved.connect(self._saved)
		self.refresh()

	def showEvent(self, event) -> None:
		self.refresh()
		super().showEvent(event)

	def refresh(self, select: Path | None = None) -> None:
		select = select or self.selected_path()
		self.entries = list_jobs(self.folder)
		self.table.blockSignals(True)
		self.table.setRowCount(0)
		for entry in self.entries:
			row = self.table.rowCount()
			self.table.insertRow(row)
			job = entry.job
			if job is None:
				cells = [entry.path.name, "-", "-", "-", "-", "does not load"]
			else:
				amount = job.profile.amount
				cells = [job.name, job.polymer.material, job.colour.material, f"{amount.duration_s:g} s",
						f"{amount.total_mass_g:.4g} g", (job.modified or "-")[:16].replace("T", " ")]
			for column, text in enumerate(cells):
				self.table.setItem(row, column, QTableWidgetItem(text))
		self.table.blockSignals(False)
		paths = [entry.path for entry in self.entries]
		if select in paths:
			self.table.selectRow(paths.index(select))
		self.editor.reload_files()
		self._buttons()

	def selected_path(self) -> Path | None:
		rows = {index.row() for index in self.table.selectedIndexes()}
		return self.entries[rows.pop()].path if len(rows) == 1 and self.entries else None

	def _selected(self) -> None:
		self._disarm()
		path = self.selected_path()
		entry = next((e for e in self.entries if e.path == path), None)
		if entry is None:
			return
		if entry.job is None:
			self.list_label.setText(entry.problem)
		else:
			self.list_label.setText("")
			self.editor.open(entry.path, entry.job)
		self._buttons()

	def _buttons(self) -> None:
		path = self.selected_path()
		loaded = path is not None and any(e.path == path and e.job is not None for e in self.entries)
		self.duplicate_button.setEnabled(loaded)
		self.run_button.setEnabled(loaded)
		self.delete_button.setEnabled(path is not None)

	def new_job(self) -> None:
		self.table.clearSelection()
		self.editor.new()

	def duplicate(self) -> None:
		path = self.selected_path()
		if path is None:
			return
		try:
			copy = duplicate_job(path)
		except (JobError, OSError) as error:
			self.list_label.setText(f"not duplicated: {error}")
			return
		self.list_label.setText(f"copied to {copy.name}")
		self.refresh(select=copy)

	def delete(self) -> None:
		path = self.selected_path()
		if path is None:
			return
		if self._delete_armed != path:
			self._delete_armed = path
			self.delete_button.setText(f"Really delete {path.name}?")
			self.cancel_delete_button.setVisible(True)
			return
		self._disarm()
		try:
			path.unlink()
		except OSError as error:
			self.list_label.setText(f"not deleted: {error}")
			return
		self.list_label.setText(f"deleted {path.name}")
		if self.editor.path == path:
			self.editor.new()
		self.refresh()

	def _disarm(self) -> None:
		self._delete_armed = None
		self.delete_button.setText("Delete")
		self.cancel_delete_button.setVisible(False)

	def _run(self) -> None:
		path = self.selected_path()
		if path is not None:
			self.run_requested.emit(path)

	def _saved(self, path: Path) -> None:
		self.refresh(select=path)


class JobEditor(QWidget):
	saved = Signal(object)
	"""The Path just written."""

	def __init__(self, config: Config):
		super().__init__()
		self.config = config
		self.folder = config.resolve(config.paths.jobs)
		self.materials = MaterialRegistry()
		self.store = CalibrationStore()
		self.path: Path | None = None
		self.created: str | None = None
		self.opened = None
		"""The job as its file holds it, so the check sees a calibration changed since it was saved."""
		self.problems: list[str] = []
		self.check = None

		self.title_label = QLabel("new job")
		self.name = QLineEdit()
		self.polymer_material = QComboBox()
		self.polymer_dispenser = QLineEdit()
		self.colour_material = QComboBox()
		self.colour_dispenser = QLineEdit()
		self.duration = QLineEdit()
		self.mass = QLineEdit()
		self.flow = QLineEdit()
		self.amount_label = QLabel("")
		self.phi_start = QLineEdit("1")
		self.phi_end = QLineEdit("5")
		self.curve = QComboBox()
		self.curve.addItems(CURVE_NAMES)
		self.curve_param = QLineEdit()
		self.table = QLineEdit("0 0; 0.5 0.5; 1 1")
		self.hold_start = QLineEdit("0")
		self.hold_end = QLineEdit("0")
		self.log = QComboBox()
		for text, value in LOG_CHOICES:
			self.log.addItem(text, value)
		self.save_button = QPushButton("Save")
		self.save_as_button = QPushButton("Save as new job")
		self.message_label = QLabel("")
		self.message_label.setWordWrap(True)
		self.check_label = QLabel("")
		self.check_label.setWordWrap(True)
		self.check_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

		self.flow_plot = pg.PlotWidget()
		self.flow_plot.setLabel("left", "flow", units="g/s")
		self.flow_plot.addLegend(offset=(10, 10))
		self.speed_plot = pg.PlotWidget()
		self.speed_plot.setLabel("left", "speed", units="Hz")
		self.speed_plot.setLabel("bottom", "time", units="s")
		self.speed_plot.setXLink(self.flow_plot)
		self.flow_curves = {role: self.flow_plot.plot([], [], pen=PENS[role], name=f"{role} g/s") for role in PENS}
		self.speed_curves = {role: self.speed_plot.plot([], [], pen=PENS[role], name=role) for role in PENS}
		self.bad_points = pg.ScatterPlotItem(size=7, brush=pg.mkBrush("#d50000"), pen=None)
		self.flow_plot.addItem(self.bad_points)
		self.ranges = {role: pg.LinearRegionItem(orientation="horizontal", movable=False,
												brush=pg.mkBrush(PENS[role].color().red(), PENS[role].color().green(),
																PENS[role].color().blue(), 30))
					for role in PENS}
		for region in self.ranges.values():
			for line in region.lines:
				line.setPen(pg.mkPen(None))
			region.setVisible(False)
			self.flow_plot.addItem(region)

		self._timer = QTimer(self)
		self._timer.setSingleShot(True)
		self._timer.setInterval(150)
		self._timer.timeout.connect(self.update_preview)
		self._lay_out()
		self._curve_chosen(self.curve.currentText())
		for line in (self.name, self.polymer_dispenser, self.colour_dispenser, self.duration, self.mass, self.flow,
					self.phi_start, self.phi_end, self.curve_param, self.table, self.hold_start, self.hold_end):
			line.textChanged.connect(self._changed)
		for combo in (self.polymer_material, self.colour_material, self.curve, self.log):
			combo.currentTextChanged.connect(self._changed)
		self.polymer_material.currentTextChanged.connect(lambda name: self._take_dispenser(name, self.polymer_dispenser))
		self.colour_material.currentTextChanged.connect(lambda name: self._take_dispenser(name, self.colour_dispenser))
		self.curve.currentTextChanged.connect(self._curve_chosen)
		self.save_button.clicked.connect(self.save)
		self.save_as_button.clicked.connect(self.save_as_new)
		self.reload_files()
		self.new()

	def _lay_out(self) -> None:
		job = QGroupBox("Job")
		job_form_layout = QFormLayout(job)
		job_form_layout.addRow("name", self.name)
		for role, combo, dispenser in (("polymer", self.polymer_material, self.polymer_dispenser),
										("colour", self.colour_material, self.colour_dispenser)):
			row = QHBoxLayout()
			row.addWidget(combo, 2)
			row.addWidget(QLabel("dispenser"))
			row.addWidget(dispenser, 1)
			job_form_layout.addRow(role, row)
		amount = QGroupBox("Amount: give two, the third is worked out")
		amount_form = QFormLayout(amount)
		amount_form.addRow("duration [s]", self.duration)
		amount_form.addRow("total mass [g]", self.mass)
		amount_form.addRow("total flow [g/s]", self.flow)
		amount_form.addRow(self.amount_label)
		profile = QGroupBox("Colour share over the run")
		self.profile_form = QFormLayout(profile)
		self.profile_form.addRow("start [%]", self.phi_start)
		self.profile_form.addRow("end [%]", self.phi_end)
		self.profile_form.addRow("curve", self.curve)
		self.profile_form.addRow("curve k", self.curve_param)
		self.profile_form.addRow("points (x progress; …)", self.table)
		self.profile_form.addRow("hold at start [s]", self.hold_start)
		self.profile_form.addRow("hold at end [s]", self.hold_end)
		self.profile_form.addRow("run log", self.log)
		buttons = QHBoxLayout()
		buttons.addWidget(self.save_button)
		buttons.addWidget(self.save_as_button)
		buttons.addStretch(1)
		left = QVBoxLayout()
		left.addWidget(self.title_label)
		left.addWidget(job)
		left.addWidget(amount)
		left.addWidget(profile)
		left.addLayout(buttons)
		left.addWidget(self.message_label)
		left.addStretch(1)
		right = QVBoxLayout()
		right.addWidget(self.flow_plot, 2)
		right.addWidget(self.speed_plot, 1)
		right.addWidget(self.check_label)
		layout = QHBoxLayout(self)
		layout.addLayout(left)
		layout.addLayout(right, 1)

	# --- Files ------------------------------------------------------------------------------------

	def reload_files(self) -> None:
		"""The materials and calibrations as they are on disk now."""
		problems = []
		try:
			self.materials = load_materials(self.config.resolve(self.config.paths.materials))
		except MaterialError as error:
			self.materials = MaterialRegistry()
			problems.append(str(error))
		try:
			self.store = load_calibration(self.config.resolve(self.config.paths.calibration))
		except CalibrationError as error:
			self.store = CalibrationStore()
			problems.append(str(error))
		self.file_problems = problems
		for combo, kind in ((self.polymer_material, Kind.POLYMER), (self.colour_material, Kind.MASTERBATCH)):
			current = combo.currentText()
			combo.blockSignals(True)
			combo.clear()
			combo.addItems([m.name for m in self.materials.of_kind(kind)])
			combo.setEditable(True)
			combo.setCurrentText(current)
			combo.blockSignals(False)
		self._changed()

	def new(self) -> None:
		self.path, self.created, self.opened = None, None, None
		self.title_label.setText("new job")
		self.name.setText("")
		for combo, dispenser in ((self.polymer_material, self.polymer_dispenser),
								(self.colour_material, self.colour_dispenser)):
			combo.setCurrentText(self._first_calibrated(combo))
			self._take_dispenser(combo.currentText(), dispenser)
		self.duration.setText("600")
		self.mass.setText("")
		self.flow.setText("1")
		self.phi_start.setText("1")
		self.phi_end.setText("5")
		self.curve.setCurrentText("linear")
		self.hold_start.setText("0")
		self.hold_end.setText("0")
		self.log.setCurrentIndex(0)
		self.message_label.setText("")
		self.update_preview()

	def _first_calibrated(self, combo: QComboBox) -> str:
		"""The first material in the list with a calibration for its own dispenser, else the first."""
		names = [combo.itemText(i) for i in range(combo.count())]
		for name in names:
			material = self.materials.get(name)
			if material.dispenser and any(c.key == (material.dispenser, name) for c in self.store.curves()):
				return name
		return names[0] if names else ""

	def open(self, path: Path, job=None) -> None:
		job = job or load_job(path)
		if Path(path) != self.path:
			self.message_label.setText("")
		self.path, self.created, self.opened = Path(path), job.created, job
		self.title_label.setText(f"editing {Path(path).name}")
		self.fill(job_form(job))
		self.update_preview()

	def fill(self, form: JobForm) -> None:
		self.name.setText(form.name)
		self.polymer_material.setCurrentText(form.polymer_material)
		self.polymer_dispenser.setText(form.polymer_dispenser)
		self.colour_material.setCurrentText(form.colour_material)
		self.colour_dispenser.setText(form.colour_dispenser)
		self.duration.setText(_shown(form.duration_s))
		self.mass.setText(_shown(form.total_mass_g))
		self.flow.setText(_shown(form.total_flow_g_s))
		self.phi_start.setText(_shown(form.phi_start * 100))
		self.phi_end.setText(_shown(form.phi_end * 100))
		self.curve.setCurrentText(form.curve)
		self.curve_param.setText(_shown(form.curve_param))
		if form.table:
			self.table.setText(table_text(form.table))
		self.hold_start.setText(_shown(form.hold_start_s))
		self.hold_end.setText(_shown(form.hold_end_s))
		self.log.setCurrentIndex([value for _, value in LOG_CHOICES].index(form.log))

	def form(self) -> JobForm:
		"""What was typed; raises JobError naming every field that is not a number."""
		problems = []
		numbers = {}
		for key, field, label, blank_ok in (
			("duration_s", self.duration, "duration", True), ("total_mass_g", self.mass, "total mass", True),
			("total_flow_g_s", self.flow, "total flow", True), ("phi_start", self.phi_start, "start", False),
			("phi_end", self.phi_end, "end", False), ("curve_param", self.curve_param, "curve parameter", True),
			("hold_start_s", self.hold_start, "hold at start", True), ("hold_end_s", self.hold_end, "hold at end", True),
		):
			text = field.text().strip()
			if not text:
				if not blank_ok:
					problems.append(f"{label}: give a value")
				numbers[key] = None
				continue
			try:
				numbers[key] = float(text.replace(",", "."))
			except ValueError:
				problems.append(f"{label} {text!r} is not a number")
				numbers[key] = None
		table = None
		if self.curve.currentText() == "table":
			try:
				table = parse_table(self.table.text())
			except JobError as error:
				problems.append(str(error))
		if problems:
			raise JobError("; ".join(problems))
		return JobForm(
			self.name.text(), self.polymer_material.currentText(), self.polymer_dispenser.text(),
			self.colour_material.currentText(), self.colour_dispenser.text(), numbers["duration_s"],
			numbers["total_mass_g"], numbers["total_flow_g_s"], numbers["phi_start"] / 100, numbers["phi_end"] / 100,
			self.curve.currentText(), numbers["curve_param"], table, numbers["hold_start_s"] or 0.0,
			numbers["hold_end_s"] or 0.0, self.log.currentData(),
		)

	def save(self) -> None:
		self._save(self.path)

	def save_as_new(self) -> None:
		self._save(None)

	def _save(self, path: Path | None) -> None:
		try:
			form = self.form()
			if not form.name.strip():
				raise JobError("give the job a name")
			job = form_job(form, self.store, created=self.created if path is not None else None)
		except JobError as error:
			self.message_label.setText(f"not saved: {error}")
			return
		target = path if path is not None else free_job_path(self.folder, job.name)
		try:
			save_job(job, target)
		except OSError as error:
			self.message_label.setText(f"not saved: {error_text('save job', error)}")
			return
		self.path, self.created, self.opened = target, job.created, job
		self.title_label.setText(f"editing {target.name}")
		self.message_label.setText(f"saved {target.name}")
		self.update_preview()
		self.saved.emit(target)

	# --- Preview ----------------------------------------------------------------------------------

	def _changed(self, *_) -> None:
		self._timer.start()

	def _take_dispenser(self, name: str, dispenser: QLineEdit) -> None:
		try:
			material = self.materials.get(name)
		except MaterialError:
			return
		if material.dispenser:
			dispenser.setText(material.dispenser)

	def _curve_chosen(self, curve: str) -> None:
		param = CURVE_PARAM.get(curve)
		self.profile_form.setRowVisible(self.curve_param, param is not None)
		self.profile_form.setRowVisible(self.table, curve == "table")
		if param is not None:
			self.profile_form.labelForField(self.curve_param).setText(f"curve {param[0]}")
			self.curve_param.setText(_shown(param[1]))

	def update_preview(self) -> None:
		self._timer.stop()
		self.check = None
		lines: list[str] = list(self.file_problems)
		try:
			form = self.form()
			profile = form_profile(form)
		except JobError as error:
			self._clear_preview()
			self.amount_label.setText("")
			self.check_label.setText("\n".join(lines + [f"not a job yet: {error}"]))
			return
		amount = profile.amount
		self.amount_label.setText(f"{amount.duration_s:g} s × {amount.total_flow_g_s:.4g} g/s = "
								f"{amount.total_mass_g:.4g} g: polymer {profile.polymer_mass_g:.4g} g, colour "
								f"{profile.colour_mass_g:.4g} g")
		samples = profile.samples(amount.duration_s / PREVIEW_POINTS)
		times = [s.t_s for s in samples]
		self.flow_curves["polymer"].setData(times, [s.polymer_g_s for s in samples])
		self.flow_curves["colour"].setData(times, [s.colour_g_s for s in samples])
		try:
			job = form_job(form if form.name.strip() else replace(form, name="preview"), self.store, created=self.created)
			job = self._with_saved_hashes(job)
		except JobError as error:
			self._clear_speeds()
			self.check_label.setText("\n".join(lines + [f"error: {error}"]))
			return
		self.check = check_job(job, self.materials, self.store, step_s=amount.duration_s / PREVIEW_POINTS)
		bad = [(p.t_s, getattr(p, role).rate_g_s) for p in self.check.points for role in ("polymer", "colour")
				if any(issue.error for issue in getattr(p, role).issues)]
		self.bad_points.setData([t for t, _ in bad], [g for _, g in bad])
		for role in ("polymer", "colour"):
			ref = getattr(job, role)
			curve = self.store.curve(ref.dispenser, ref.material)
			self.ranges[role].setRegion((curve.points[0].rate_g_s, curve.points[-1].rate_g_s))
			self.ranges[role].setVisible(True)
			points = self.check.points
			self.speed_curves[role].setData([p.t_s for p in points],
											[_hz(getattr(p, role).speed_hz) for p in points], connect="finite")
		lines += self.check.report()
		lines.append(self._segments(job))
		self.check_label.setText("\n".join(lines))

	def _with_saved_hashes(self, job):
		"""The job with the hashes its file holds, where it still names the same calibrations."""
		if self.opened is None:
			return job
		changes = {}
		for role in ("polymer", "colour"):
			saved, now = getattr(self.opened, role), getattr(job, role)
			if (saved.dispenser, saved.material) == (now.dispenser, now.material):
				changes[role] = saved
		return replace(job, **changes)

	def _segments(self, job) -> str:
		try:
			program = compile_gradient_job(job, self.materials, self.store, max_segments=DEFAULT_MAX_SEGMENTS)
		except JobCompileError as error:
			return f"not runnable: {error}"
		return (f"segments: polymer {len(program.polymer.segments)}, colour {len(program.colour.segments)} "
				f"(the board holds {DEFAULT_MAX_SEGMENTS})")

	def _clear_preview(self) -> None:
		for curve in self.flow_curves.values():
			curve.setData([], [])
		self._clear_speeds()

	def _clear_speeds(self) -> None:
		for curve in self.speed_curves.values():
			curve.setData([], [])
		self.bad_points.setData([], [])
		for region in self.ranges.values():
			region.setVisible(False)

	def flow_data(self, role: str) -> tuple[list[float], list[float]]:
		x, y = self.flow_curves[role].getData()
		return ([] if x is None else list(x)), ([] if y is None else list(y))


def _shown(value: float | None) -> str:
	return "" if value is None else f"{value:g}"


def _hz(value: float | None) -> float:
	return math.nan if value is None else value

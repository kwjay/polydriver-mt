"""The Manual screen's Timed run tab: run the chosen board at a speed (Hz) or a rate (g/s) for N
seconds as one job the board times and stops itself (bench.run_segments), so the length does not
depend on this PC. A rate goes through the material's calibration curve (jobs.compile_rate_job), the
same way a job does, and the grams it should give are shown to weigh against. The board's current
settings are used as they are (the Controller box above)."""

from pathlib import Path

from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..bench import Cancelled, TimedRunError, run_segments, speed_segments
from ..calibration import CalibrationError, load_calibration
from ..jobs import JobCompileError, RateRow, compile_rate_job
from ..materials import MaterialError, load_materials
from ..protocol.constants import Role
from .messages import error_text
from .tuning import ProcedurePanel

HZ = "Hz"
G_S = "g/s"


class TimedRunPanel(ProcedurePanel):
	RUN_TEXT = "Run"
	STOP_TEXT = "Stop run"
	WHAT = "the timed run"

	def __init__(self, page, *, run=run_segments):
		super().__init__(page)
		self._run = run
		self.unit = QComboBox()
		self.unit.addItems([HZ, G_S])
		self.value = QDoubleSpinBox()
		self.value.setRange(0.01, 200.0)
		self.value.setDecimals(3)
		self.value.setValue(10.0)
		self.seconds = QDoubleSpinBox()
		self.seconds.setRange(0.1, 3600.0)
		self.seconds.setDecimals(1)
		self.seconds.setValue(10.0)
		self.seconds.setSuffix(" s")
		self.material = QComboBox()
		self.run_button = QPushButton(self.RUN_TEXT)
		self.progress_label = QLabel("")
		self.result_label = QLabel("")
		self.result_label.setWordWrap(True)

		form = QFormLayout()
		value_row = QHBoxLayout()
		value_row.addWidget(self.value, 1)
		value_row.addWidget(self.unit)
		form.addRow("run at", value_row)
		form.addRow("for", self.seconds)
		form.addRow("material (g/s)", self.material)
		self._form = form
		layout = QVBoxLayout(self)
		layout.addWidget(QLabel("Closed loop, timed by the board; it stops by itself at the end."))
		layout.addLayout(form)
		layout.addWidget(self.run_button)
		layout.addWidget(self.progress_label)
		layout.addWidget(self.result_label)
		layout.addStretch(1)
		self.unit.currentTextChanged.connect(self._unit_chosen)
		self.run_button.clicked.connect(self.toggle)
		self._unit_chosen(self.unit.currentText())

	def _paths(self) -> tuple[Path, Path]:
		config = self.page.config
		return config.resolve(config.paths.materials), config.resolve(config.paths.calibration)

	def _unit_chosen(self, unit: str) -> None:
		self._form.setRowVisible(self.material, unit == G_S)
		if unit == G_S:
			self.refresh_materials()

	def refresh_materials(self) -> None:
		"""The materials of the chosen board, read from the materials file."""
		current = self.material.currentText()
		self.material.clear()
		try:
			registry = load_materials(self._paths()[0])
		except MaterialError as error:
			self._say(str(error))
			return
		self.material.addItems([m.name for m in registry.materials() if m.role is self.page.role])
		if current:
			self.material.setCurrentText(current)

	def toggle(self) -> None:
		if self.running:
			self._ask_to_stop()
			return
		page = self.page
		role, unit, value, seconds = page.role, self.unit.currentText(), self.value.value(), self.seconds.value()
		material = self.material.currentText()
		try:
			segments, expected_g = self.segments(role, unit, value, seconds, material)
		except (TimedRunError, MaterialError, CalibrationError, JobCompileError) as error:
			self._say(f"not run: {error}")
			return
		cancel = self._begin()
		if cancel is None:
			return
		self.result_label.setText("")
		what = f"{value:g} {unit}" + (f" of {material}" if unit == G_S else "")
		self.progress_label.setText(f"running {what} for {seconds:g} s…")
		page.bridge.run_now(self._run, page.session, role, segments, cancel=cancel,
							on_progress=lambda elapsed, total: self._report_step(elapsed, total, None),
							name=f"timed run {role.name.lower()}",
							on_result=lambda result: self._done(result, what, seconds, expected_g),
							on_error=lambda error: self._failed(error, role))

	def segments(self, role: Role, unit: str, value: float, seconds: float, material_name: str):
		"""(segments, grams expected or None). Raises with the reason it cannot run."""
		if unit == HZ:
			return speed_segments(value, seconds), None
		if not material_name:
			raise MaterialError(f"no {role.name.lower()} material in the materials file: add one on Materials")
		materials_path, calibration_path = self._paths()
		material = load_materials(materials_path).get(material_name)
		curve = material.curve(load_calibration(calibration_path))
		job = compile_rate_job(material, curve, [RateRow(value, seconds)])
		return list(job.segments), job.expected_grams

	def _done(self, result, what: str, seconds: float, expected_g: float | None) -> None:
		self._end()
		if result.done:
			text = f"done: {what} for {seconds:g} s ({result.edges} encoder edges)"
			if expected_g is not None:
				text += f"; it should have given {expected_g:.3g} g"
			self.progress_label.setText(text)
		else:
			why = "stopped early" if result.ended_early else f"the board ended the job {result.state.name}"
			extra = f"; {result.cleanup_problem}" if result.cleanup_problem else ""
			self.progress_label.setText(f"{why} after {result.elapsed_ms / 1000:g} s{extra}")

	def _failed(self, error: BaseException, role: Role) -> None:
		self._end()
		self.progress_label.setText("stopped early" if isinstance(error, Cancelled)
									else f"run failed: {error_text('timed run', error, role)}")

	def _on_step(self, elapsed_ms, total_ms, _) -> None:
		if self.running:
			self.progress_label.setText(f"running: {elapsed_ms / 1000:.1f} of {total_ms / 1000:g} s")

	def _say(self, text: str) -> None:
		self.result_label.setText(text)

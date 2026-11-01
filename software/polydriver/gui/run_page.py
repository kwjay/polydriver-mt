"""Run: a gradient job on both boards (GUI_gradient_job_flow.md §2 screen 6), through the runner
(run.Runner) with its safety rules, as the shell's `run` does.

Choose a job; the checklist shows whether both boards are connected with the same firmware, the job
fits the calibrations as they are now and the log folder can be written, and asks the operator to
confirm the hoppers are filled and the screws primed. Upload compiles the job and sends each board
its half; the lights show each board's job state until both are READY; START starts both together.
While it runs: progress, time left, commanded against measured speed per channel, grams so far
against expected. Stop run (or the window's STOP) sends STOP to both and the run ends ABORTED;
faults end it on their own (safety rules). After it: a summary, and the weighed masses and a note
saved into the run log's sidecar.

The run log is written when the job says so, else when [logging] enabled says so; the choice can be
changed for this run. One Runner per run: a new upload needs the last run to have ended."""

import os
import tempfile
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
	QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
	QPushButton, QVBoxLayout, QWidget,
)

from ..calibration import CalibrationError, load_calibration
from ..config import Config
from ..devices import BoardStatus
from ..devices.fleet import FleetUploadError
from ..jobs import JobError, check_job, load_job, speed_at
from ..jobs.editing import list_jobs
from ..materials import MaterialError, load_materials
from ..protocol.constants import JobState, Role
from ..run import Runner, RunState
from ..telemetry import start_run_log
from .bridge import Bridge
from .messages import error_text, role_title
from .procedures import ProcedureBusyError, Procedures
from .telemetry import QtSink

ROLES = (Role.POLYMER, Role.COLOUR)
ENDED = (RunState.DONE, RunState.ABORTED)
TICK_MS = 200
BUTTON_STYLE = ("QPushButton { background: %s; color: white; font-weight: bold; padding: 6px; } "
				"QPushButton:disabled { background: #bdbdbd; color: #eeeeee; }")
LOG_CHOICES = (("as the job says", None), ("log", True), ("do not log", False))
PENS = {
	(Role.POLYMER, "commanded"): pg.mkPen("#90caf9", width=2, style=Qt.PenStyle.DashLine),
	(Role.POLYMER, "measured"): pg.mkPen("#1565c0", width=2),
	(Role.COLOUR, "commanded"): pg.mkPen("#ef9a9a", width=2, style=Qt.PenStyle.DashLine),
	(Role.COLOUR, "measured"): pg.mkPen("#c62828", width=2),
}
LIGHT = {
	None: "#9e9e9e", JobState.EMPTY: "#9e9e9e", JobState.LOADING: "#fbc02d", JobState.READY: "#2e7d32",
	JobState.RUNNING: "#1565c0", JobState.DONE: "#2e7d32", JobState.ABORTED: "#c62828",
}


class RunPage(QWidget):
	run_ended = Signal(object)
	"""The Runner whose run just ended and was wound up."""
	_ended = Signal(object)

	def __init__(self, session, config: Config, bridge: Bridge, sink: QtSink, *, runner=Runner,
				start_log=start_run_log, procedures: Procedures | None = None):
		super().__init__()
		self.session = session
		self.config = config
		self.bridge = bridge
		self.registry = procedures or Procedures(self)
		self._make_runner = runner
		self._start_log = start_log
		self.runner = None
		self.log = None
		self.job = None
		self.job_path: Path | None = None
		self._cancel = None
		self._waiting = None
		self._traces = {key: ([], []) for key in PENS}

		self.job_combo = QComboBox()
		self.log_combo = QComboBox()
		for text, value in LOG_CHOICES:
			self.log_combo.addItem(text, value)
		self.note = QLineEdit()
		self.checks_label = QLabel("")
		self.checks_label.setWordWrap(True)
		self.confirm = QCheckBox("hoppers filled and screws primed")
		self.upload_button = QPushButton("Upload")
		self.start_button = QPushButton("START")
		self.start_button.setStyleSheet(BUTTON_STYLE % "#2e7d32")
		self.stop_button = QPushButton("Stop run")
		self.stop_button.setStyleSheet(BUTTON_STYLE % "#c62828")
		self.lights = {role: QLabel("") for role in ROLES}
		self.progress = QProgressBar()
		self.progress.setRange(0, 1000)
		self.progress_label = QLabel("")
		self.grams_label = QLabel("")
		self.message_label = QLabel("")
		self.message_label.setWordWrap(True)
		self.summary_label = QLabel("")
		self.summary_label.setWordWrap(True)
		self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
		self.weighed = {role: _grams_spin() for role in ROLES}
		self.weighed_note = QLineEdit()
		self.save_weighed_button = QPushButton("Save to the run log")
		self.summary_box = QGroupBox("After the run")
		self.plot = pg.PlotWidget()
		self.plot.setLabel("left", "speed", units="Hz")
		self.plot.setLabel("bottom", "job time", units="s")
		self.plot.addLegend(offset=(10, 10))
		self.curves = {key: self.plot.plot([], [], pen=pen, name=f"{key[0].name.lower()} {key[1]}")
					for key, pen in PENS.items()}
		self._timer = QTimer(self)
		self._timer.setInterval(TICK_MS)
		self._timer.timeout.connect(self.tick)

		self._lay_out()
		self.job_combo.currentIndexChanged.connect(lambda _: self._job_chosen())
		self.log_combo.currentIndexChanged.connect(lambda _: self.show_checks())
		self.confirm.toggled.connect(lambda _: self._buttons())
		self.upload_button.clicked.connect(self.upload)
		self.start_button.clicked.connect(self.start)
		self.stop_button.clicked.connect(self.stop)
		self.save_weighed_button.clicked.connect(self.save_weighed)
		self._ended.connect(self._on_ended, Qt.ConnectionType.QueuedConnection)
		sink.status.connect(self._on_status, Qt.ConnectionType.QueuedConnection)
		self.registry.changed.connect(lambda _: self._buttons())
		self.refresh()

	def _lay_out(self) -> None:
		options = QGroupBox("Job")
		form = QFormLayout(options)
		form.addRow("job", self.job_combo)
		form.addRow("run log", self.log_combo)
		form.addRow("note", self.note)
		checks = QGroupBox("Before the run")
		checks_layout = QVBoxLayout(checks)
		checks_layout.addWidget(self.checks_label)
		checks_layout.addWidget(self.confirm)
		buttons = QHBoxLayout()
		buttons.addWidget(self.upload_button)
		buttons.addWidget(self.start_button)
		buttons.addWidget(self.stop_button)
		lights = QHBoxLayout()
		for role in ROLES:
			lights.addWidget(self.lights[role])
		summary = QFormLayout(self.summary_box)
		summary.addRow(self.summary_label)
		for role in ROLES:
			summary.addRow(f"weighed {role.name.lower()}", self.weighed[role])
		summary.addRow("note", self.weighed_note)
		summary.addRow(self.save_weighed_button)
		self.summary_box.setVisible(False)
		left = QVBoxLayout()
		left.addWidget(options)
		left.addWidget(checks)
		left.addLayout(buttons)
		left.addLayout(lights)
		left.addWidget(self.message_label)
		left.addWidget(self.summary_box)
		left.addStretch(1)
		right = QVBoxLayout()
		right.addWidget(self.progress)
		right.addWidget(self.progress_label)
		right.addWidget(self.grams_label)
		right.addWidget(self.plot, 1)
		layout = QHBoxLayout(self)
		layout.addLayout(left)
		layout.addLayout(right, 1)

	# --- The job ----------------------------------------------------------------------------------

	def showEvent(self, event) -> None:
		self.refresh()
		super().showEvent(event)

	def refresh(self) -> None:
		"""The jobs folder again, keeping the chosen job."""
		keep = self.job_path
		self.job_combo.blockSignals(True)
		self.job_combo.clear()
		for entry in list_jobs(self.config.resolve(self.config.paths.jobs)):
			if entry.job is not None:
				self.job_combo.addItem(entry.job.name, entry.path)
		paths = [self.job_combo.itemData(i) for i in range(self.job_combo.count())]
		self.job_combo.setCurrentIndex(paths.index(keep) if keep in paths else (0 if paths else -1))
		self.job_combo.blockSignals(False)
		self._job_chosen()

	def set_job(self, path: Path) -> None:
		"""Choose this job (the Jobs screen's Run…)."""
		self.job_path = Path(path)
		self.refresh()

	def _job_chosen(self) -> None:
		path = self.job_combo.currentData()
		self.job_path = path
		self.job = None
		if path is not None:
			try:
				self.job = load_job(path)
			except JobError as error:
				self.message_label.setText(str(error))
		if self.job is not None:
			self.log_combo.setItemText(0, f"as the job says ({_log_word(self._job_log_default())})")
		self.show_checks()

	def _job_log_default(self) -> bool:
		return self.job.log if self.job.log is not None else self.config.logging.enabled

	def logging_on(self) -> bool:
		chosen = self.log_combo.currentData()
		return chosen if chosen is not None else (self._job_log_default() if self.job is not None else False)

	# --- Checklist --------------------------------------------------------------------------------

	def checklist(self) -> list[tuple[bool, str]]:
		items = []
		connected = set(self.session.roles())
		missing = [role.name.lower() for role in ROLES if role not in connected]
		items.append((not missing, "both boards connected" if not missing else f"not connected: {', '.join(missing)}"))
		if not missing:
			versions = {role: self.session.state(role).version for role in ROLES}
			firmware = {role: (v.firmware_major, v.firmware_minor, v.firmware_patch) for role, v in versions.items()}
			same = len(set(firmware.values())) == 1
			items.append((same, "the same firmware on both" if same else
						"different firmware: " + ", ".join(f"{role.name.lower()} {'.'.join(map(str, f))}"
															for role, f in firmware.items())))
		items.append(self._job_fits())
		if self.logging_on():
			folder = self.config.resolve(self.config.logging.folder)
			writable = _writable(folder)
			items.append((writable, f"log folder {folder} can be written" if writable else
						f"log folder {folder} cannot be written"))
		return items

	def _job_fits(self) -> tuple[bool, str]:
		if self.job is None:
			return False, "no job chosen (make one on Jobs)"
		try:
			materials = load_materials(self.config.resolve(self.config.paths.materials))
			store = load_calibration(self.config.resolve(self.config.paths.calibration))
		except (MaterialError, CalibrationError) as error:
			return False, str(error)
		check = check_job(self.job, materials, store)
		if check.ok:
			warnings = [line for line in check.report() if line.startswith("warning")]
			return True, "the job fits the calibrations" + (": " + "; ".join(warnings) if warnings else "")
		return False, "the job does not fit: " + "; ".join(line for line in check.report() if line.startswith("error"))

	def show_checks(self) -> None:
		self.checks_label.setText("\n".join(("✓ " if ok else "✗ ") + text for ok, text in self.checklist()))
		self._buttons()

	def ready_to_upload(self) -> bool:
		return all(ok for ok, _ in self.checklist())

	def _buttons(self) -> None:
		runner = self.runner
		going = runner is not None and runner.state not in ENDED
		self.upload_button.setEnabled(not going and self.job is not None and
									(self.registry.running is None or self._cancel is not None))
		self.start_button.setEnabled(going and runner.state is RunState.READY and self.confirm.isChecked())
		self.stop_button.setEnabled(going)
		self.job_combo.setEnabled(not going)
		self.log_combo.setEnabled(not going)

	# --- Running ----------------------------------------------------------------------------------

	def upload(self) -> None:
		self.show_checks()
		failed = [text for ok, text in self.checklist() if not ok]
		if failed:
			self.message_label.setText("not uploaded: " + "; ".join(failed))
			return
		if self.runner is not None and self.runner.state not in ENDED:
			return
		try:
			self._cancel = self.registry.begin("the run")
		except ProcedureBusyError as error:
			self.message_label.setText(str(error))
			return
		job, logged, note = self.job, self.logging_on(), self.note.text().strip()
		self.message_label.setText(f"uploading {job.name}…")
		self.summary_box.setVisible(False)
		for key in self._traces:
			self._traces[key] = ([], [])
		self.upload_button.setEnabled(False)
		self.bridge.run(self._upload, job, logged, note, name="upload the job", on_result=self._uploaded,
						on_error=self._upload_failed)

	def _upload(self, job, logged: bool, note: str):
		"""On the bridge's worker: the files, the log, the runner and the upload."""
		materials = load_materials(self.config.resolve(self.config.paths.materials))
		store = load_calibration(self.config.resolve(self.config.paths.calibration))
		log = self._start_log(self.session, self.config, note=note or f"gradient job {job.name}") if logged else None
		runner = self._make_runner(self.session, job, materials, store, run_log=log)
		try:
			program = runner.upload()
		except BaseException:
			if log is not None:
				log.close()
			raise
		return runner, log, program

	def _uploaded(self, result) -> None:
		self.runner, self.log, program = result
		self.message_label.setText(
			f"uploaded: polymer {len(program.polymer.segments)} segments, colour {len(program.colour.segments)} "
			f"segments, {program.total_ms / 1000:g} s" + (f"; log {self.log.csv_path}" if self.log else "; no log")
			+ ". START when both lights are READY.")
		self._timer.start()
		self.tick()

	def _upload_failed(self, error: BaseException) -> None:
		self._release()
		if isinstance(error, FleetUploadError):
			text = "; ".join(f"{role_title(role)}: {error_text('upload', failure, role)}"
							for role, failure in error.failures.items())
		else:
			text = error_text("upload", error)
		self.message_label.setText(f"not uploaded: {text}")
		self._buttons()

	def start(self) -> None:
		runner = self.runner
		if runner is None or not self.confirm.isChecked():
			return
		self.start_button.setEnabled(False)
		self.bridge.run_now(runner.start, name="start the run", on_result=lambda command: self._started(runner, command),
							on_error=lambda error: self.message_label.setText(f"not started: {error_text('start', error)}"))

	def _started(self, runner, command) -> None:
		gap_s = command.gap_s()
		self.message_label.setText("running" + (f"; the boards started {gap_s * 1000:.1f} ms apart" if gap_s is not None else ""))
		self._watch_end(runner)

	def _watch_end(self, runner) -> None:
		"""Once per run: the summary when the run has ended and been wound up (log closed)."""
		if self._waiting is not runner:
			self._waiting = runner
			self.bridge.run_now(runner.wait, name="wait for the run", on_result=lambda _: self._ended.emit(runner))

	def stop(self) -> None:
		runner = self.runner
		if runner is not None and runner.state not in ENDED:
			self.stop_button.setEnabled(False)
			self.bridge.run_now(runner.stop, name="stop the run", on_result=lambda _: self._watch_end(runner),
								on_error=lambda error: self.message_label.setText(error_text("stop the run", error)))

	def stop_now(self) -> None:
		"""Thread-safe: the window's STOP, before it sends STOP to every board."""
		runner = self.runner
		if runner is not None and runner.state not in ENDED:
			runner.stop()

	def shutdown(self, wait_s: float = 10.0) -> None:
		"""The window is closing: a run still going is stopped and wound up (its log closed)."""
		runner = self.runner
		if runner is not None and runner.state not in ENDED:
			runner.stop()
			runner.wait(wait_s)

	def tick(self) -> None:
		runner = self.runner
		if runner is None:
			return
		progress = runner.progress()
		for role, channel in ((Role.POLYMER, progress.polymer), (Role.COLOUR, progress.colour)):
			state = channel.job_state
			self.lights[role].setText(f"● {role.name.lower()} {state.name if state is not None else '-'}")
			self.lights[role].setStyleSheet(f"color: {LIGHT.get(state, '#9e9e9e')}; font-weight: bold;")
		self.progress.setValue(round(1000 * min(1.0, max(0.0, progress.fraction))))
		left_s = max(0.0, progress.total_s - progress.elapsed_s)
		self.progress_label.setText(f"{progress.state}: {progress.elapsed_s:.1f} of {progress.total_s:g} s, "
									f"{left_s:.0f} s left" + (f" ({progress.reason})" if progress.reason else ""))
		self.grams_label.setText(" · ".join(
			f"{channel.role} {channel.delivered_g:.3g} g" + (f" of {channel.expected_g:.3g} g" if channel.expected_g else "")
			for channel in (progress.polymer, progress.colour)) + "".join(f"\nwarning: {w}" for w in progress.warnings))
		self._buttons()
		if runner.state in ENDED:
			self._watch_end(runner)

	def _on_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		runner = self.runner
		program = getattr(runner, "program", None)
		if program is None or runner.state not in (RunState.RUNNING, RunState.DONE, RunState.ABORTED):
			return
		report = status.report
		if report.job_state not in (JobState.RUNNING, JobState.DONE, JobState.ABORTED):
			return
		t_s = report.job_elapsed_ms / 1000
		segments = getattr(program, role.name.lower()).segments
		for kind, value in (("commanded", speed_at(segments, report.job_elapsed_ms)),
							("measured", report.filtered_speed_hz)):
			times, values = self._traces[(role, kind)]
			times.append(t_s)
			values.append(value)
			self.curves[(role, kind)].setData(times, values)

	def _on_ended(self, runner) -> None:
		if runner is not self.runner or self.summary_box.isVisible():
			return
		self._timer.stop()
		self.tick()
		self._release()
		progress = runner.progress()
		lines = [f"{runner.state}" + (f": {runner.reason}" if runner.reason else ""),
				f"{progress.elapsed_s:.1f} of {progress.total_s:g} s"]
		for channel in (progress.polymer, progress.colour):
			expected = f" (expected {channel.expected_g:.4g} g)" if channel.expected_g is not None else ""
			lines.append(f"{channel.role}: about {channel.delivered_g:.4g} g by the encoder{expected}")
		lines.append(f"log {self.log.csv_path}" if self.log is not None else "no log")
		self.summary_label.setText("\n".join(lines))
		self.summary_box.setVisible(True)
		self.save_weighed_button.setEnabled(self.log is not None)
		self.message_label.setText(f"run {runner.state}")
		self._buttons()
		self.run_ended.emit(runner)

	def _release(self) -> None:
		cancel, self._cancel = self._cancel, None
		if cancel is not None:
			self.registry.end(cancel)

	def save_weighed(self) -> None:
		log = self.log
		if log is None:
			return
		masses = {role: spin.value() for role, spin in self.weighed.items() if spin.value() > 0}
		note = self.weighed_note.text().strip()

		def write():
			for role, grams in masses.items():
				log.set_weighed_mass(role, grams)
			if note:
				log.set_note(note)

		self.bridge.run(write, name="save the weighed masses",
						on_result=lambda _: self.message_label.setText(f"saved to {log.sidecar_path}"),
						on_error=lambda error: self.message_label.setText(f"not saved: {error_text('save', error)}"))


def _grams_spin() -> QDoubleSpinBox:
	spin = QDoubleSpinBox()
	spin.setRange(0.0, 1_000_000.0)
	spin.setDecimals(2)
	spin.setSuffix(" g")
	return spin


def _log_word(on: bool) -> str:
	return "log" if on else "no log"


def _writable(folder: Path) -> bool:
	try:
		folder.mkdir(parents=True, exist_ok=True)
		handle, name = tempfile.mkstemp(dir=folder, prefix=".write-test-")
		os.close(handle)
		os.unlink(name)
		return True
	except OSError:
		return False

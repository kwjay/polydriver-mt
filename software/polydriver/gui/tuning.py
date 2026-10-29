"""The Manual screen's Sweep and Step test tabs. Each runs a bench procedure on the chosen board on a
thread of its own (Bridge.run_now, so a minutes-long sweep never holds up other calls) and shows
its steps as they come. The window's STOP and "STOP this board" cancel a running procedure before
they send STOP, so the procedure's last command can never land after it (bench.Cancel)."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
	QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTableWidget,
	QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..analysis import FeedforwardFitError, StepError, fit_feedforward, parse_steps
from ..bench import Cancel, Cancelled, measurement_path, run_step_test, run_sweep
from .messages import error_text

STEP_COLUMNS = ("step [Hz]", "rise [s]", "overshoot [%]", "settling [s]", "steady error [Hz]")


class ProcedurePanel(QWidget):
	running_changed = Signal(bool)
	_stepped = Signal(object, object, object)

	def __init__(self, page):
		super().__init__()
		self.page = page
		self._cancel: Cancel | None = None
		self.running = False
		self._stepped.connect(self._on_step, Qt.ConnectionType.QueuedConnection)

	def cancel(self) -> None:
		"""Thread-safe; may wait for one request in flight, so call it off the GUI thread."""
		cancel = self._cancel
		if cancel is not None:
			cancel.cancel()

	def _begin(self) -> Cancel:
		self._cancel = Cancel()
		self._set_running(True)
		return self._cancel

	def _end(self) -> None:
		self._cancel = None
		self._set_running(False)

	def _set_running(self, running: bool) -> None:
		self.running = running
		self.run_button.setText(self.STOP_TEXT if running else self.RUN_TEXT)
		self.running_changed.emit(running)

	def _ask_to_stop(self) -> None:
		self.run_button.setEnabled(False)
		self.page.bridge.run_now(self.cancel, name="stop the procedure",
								on_result=lambda _: self.run_button.setEnabled(True))

	def _report_step(self, index, count, result) -> None:
		"""Called on the procedure's thread."""
		self._stepped.emit(index, count, result)

	def _on_step(self, index, count, result) -> None:
		raise NotImplementedError


class SweepPanel(ProcedurePanel):
	RUN_TEXT = "Run sweep"
	STOP_TEXT = "Stop sweep"

	def __init__(self, page, *, run=run_sweep):
		super().__init__(page)
		self._run = run
		self.fit = None
		self.from_duty = _duty(100)
		self.to_duty = _duty(255)
		self.step_duty = QSpinBox()
		self.step_duty.setRange(1, 100)
		self.step_duty.setValue(5)
		self.hold = _seconds(6.0)
		self.run_button = QPushButton(self.RUN_TEXT)
		self.progress_label = QLabel("")
		self.result_label = QLabel("")
		self.result_label.setWordWrap(True)
		self.use_button = QPushButton("Use these")
		self.use_button.setToolTip("Put the fitted offset and gain in the feedforward fields (then Send to board)")
		self.use_button.setEnabled(False)

		form = QFormLayout()
		form.addRow("from duty", self.from_duty)
		form.addRow("to duty", self.to_duty)
		form.addRow("step", self.step_duty)
		form.addRow("hold per step", self.hold)
		layout = QVBoxLayout(self)
		layout.addWidget(QLabel("Open loop: holds each duty and fits the feedforward to the speeds."))
		layout.addLayout(form)
		buttons = QHBoxLayout()
		buttons.addWidget(self.run_button)
		buttons.addWidget(self.use_button)
		layout.addLayout(buttons)
		layout.addWidget(self.progress_label)
		layout.addWidget(self.result_label)
		layout.addStretch(1)
		self.run_button.clicked.connect(self.toggle)
		self.use_button.clicked.connect(self.use_fit)

	def duties(self) -> list[int]:
		return list(range(self.from_duty.value(), self.to_duty.value() + 1, self.step_duty.value()))

	def toggle(self) -> None:
		if self.running:
			self._ask_to_stop()
			return
		duties = self.duties()
		if not duties:
			self.result_label.setText("from duty must not be above to duty")
			return
		page = self.page
		role, hold_s = page.role, self.hold.value()
		path = measurement_path(page.config.resolve(page.config.logging.folder), "sweep", role)
		self.fit = None
		self.use_button.setEnabled(False)
		self.result_label.setText("")
		self.progress_label.setText(f"sweeping {len(duties)} steps of {hold_s:g} s, about "
									f"{len(duties) * hold_s / 60:.1f} min")
		cancel = self._begin()
		page.bridge.run_now(self._run, page.session, role, duties, hold_s, path=path, cancel=cancel,
							on_step=self._report_step, name=f"sweep {role.name.lower()}",
							on_result=self._done, on_error=self._failed)

	def use_fit(self) -> None:
		if self.fit is None:
			return
		self.page.ff_offset.setValue(self.fit.ff_offset_duty)
		self.page.ff_gain.setValue(self.fit.ff_gain_duty_per_hz)
		self.page.show_note("feedforward filled in from the sweep: press Send to board to use it")

	def _on_step(self, index, count, row) -> None:
		turning = f"{row.mean_raw_hz:.2f} Hz" if row.turning else "not turning"
		self.progress_label.setText(f"step {index + 1}/{count}: duty {row.duty} → {turning}")

	def _done(self, result) -> None:
		self._end()
		lines = []
		if result.ended_early:
			lines.append("stopped early")
		if result.cleanup_problem:
			lines.append(result.cleanup_problem)
		try:
			self.fit = fit_feedforward(result.rows)
		except FeedforwardFitError as error:
			lines.append(f"no fit: {error}")
		else:
			lines.append(self.fit.report())
			self.use_button.setEnabled(True)
		if result.path is not None:
			lines.append(f"saved {result.path}")
		self.result_label.setText("\n".join(lines))

	def _failed(self, error: BaseException) -> None:
		self._end()
		self.result_label.setText(_failure("sweep", error, self.page.role))


class StepTestPanel(ProcedurePanel):
	RUN_TEXT = "Run step test"
	STOP_TEXT = "Stop step test"

	def __init__(self, page, *, run=run_step_test):
		super().__init__(page)
		self._run = run
		self.steps = QLineEdit("0, 10, 20, 10")
		self.steps.setToolTip("Speeds in Hz: the first is where the test starts, each next one is a step")
		self.hold = _seconds(5.0)
		self.band = QDoubleSpinBox()
		self.band.setRange(1.0, 50.0)
		self.band.setDecimals(1)
		self.band.setValue(5.0)
		self.band.setSuffix(" %")
		self.run_button = QPushButton(self.RUN_TEXT)
		self.table = QTableWidget(0, len(STEP_COLUMNS))
		self.table.setHorizontalHeaderLabels(STEP_COLUMNS)
		self.table.verticalHeader().setVisible(False)
		self.status_label = QLabel("")
		self.status_label.setWordWrap(True)

		form = QFormLayout()
		form.addRow("speeds [Hz]", self.steps)
		form.addRow("hold per step", self.hold)
		form.addRow("settling band", self.band)
		layout = QVBoxLayout(self)
		layout.addWidget(QLabel("Closed loop: sends the controller settings above, then steps the target."))
		layout.addLayout(form)
		layout.addWidget(self.run_button)
		layout.addWidget(self.table, 1)
		layout.addWidget(self.status_label)
		self.run_button.clicked.connect(self.toggle)

	def toggle(self) -> None:
		if self.running:
			self._ask_to_stop()
			return
		try:
			speeds = parse_steps(self.steps.text())
		except StepError as error:
			self.status_label.setText(str(error))
			return
		page = self.page
		role, settings, hold_s = page.role, page.form_values(), self.hold.value()
		band = self.band.value() / 100
		note = (f"step test {role.name.lower()}: kp {settings.kp:g} ki {settings.ki:g} kd {settings.kd:g} "
				f"alpha {settings.alpha:g} ff {settings.ff_offset_duty:g} + {settings.ff_gain_duty_per_hz:g}/Hz")
		session, config, start_log, run = page.session, page.config, page.start_log, self._run

		def work():
			session.apply_settings(role, settings)
			return run(session, role, speeds, hold_s, band_fraction=band, cancel=cancel, on_step=self._report_step,
						start_log=lambda: start_log(session, config, note=note), note=note)

		self.table.setRowCount(0)
		self.status_label.setText(f"{len(speeds) - 1} steps of {hold_s:g} s with {note[len('step test '):]}")
		cancel = self._begin()
		page.bridge.run_now(work, name=f"step test {role.name.lower()}", on_result=self._done, on_error=self._failed)

	def rows(self) -> list[list[str]]:
		return [[self.table.item(r, c).text() for c in range(self.table.columnCount())] for r in range(self.table.rowCount())]

	def _on_step(self, index, count, step) -> None:
		m = step.metrics
		if m is None:
			cells = [_step_text(step), step.problem, "", "", ""]
		else:
			cells = [_step_text(step), _maybe(m.rise_time_s, "{:.2f}"), f"{m.overshoot_percent:.1f}",
					_maybe(m.settling_time_s, "{:.2f}"), f"{m.steady_error_hz:+.2f}"]
		row = self.table.rowCount()
		self.table.insertRow(row)
		for column, text in enumerate(cells):
			self.table.setItem(row, column, QTableWidgetItem(text))

	def _done(self, result) -> None:
		self._end()
		lines = []
		if result.ended_early:
			lines.append("stopped early")
		if result.cleanup_problem:
			lines.append(result.cleanup_problem)
		if result.log_path is not None:
			lines.append(f"saved {result.log_path} and {result.metrics_path.name}")
		self.status_label.setText("\n".join(lines))

	def _failed(self, error: BaseException) -> None:
		self._end()
		self.status_label.setText(_failure("step test", error, self.page.role))


def _failure(what: str, error: BaseException, role) -> str:
	if isinstance(error, Cancelled):
		return "stopped early"
	return f"{what} failed: {error_text(what, error, role)}"


def _step_text(step) -> str:
	return f"{step.from_hz:g} → {step.to_hz:g}"


def _maybe(value, form: str) -> str:
	return "—" if value is None else form.format(value)


def _duty(value: int) -> QSpinBox:
	spin = QSpinBox()
	spin.setRange(0, 255)
	spin.setValue(value)
	return spin


def _seconds(value: float) -> QDoubleSpinBox:
	spin = QDoubleSpinBox()
	spin.setRange(0.2, 120.0)
	spin.setDecimals(1)
	spin.setValue(value)
	spin.setSuffix(" s")
	return spin

"""History: the run logs in the log folder, newest first (GUI_gradient_job_flow.md §2 screen 8).
Choosing one shows its check (telemetry.check_run_log: the CSV and sidecar agree with how a run
goes), its files, and lets the weighed masses and the note be set afterwards, written into the
sidecar (telemetry.update_sidecar)."""

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
	QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSplitter, QTableWidget,
	QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..config import Config
from ..telemetry import SidecarError, check_run_log, list_run_logs, update_sidecar
from .bridge import Bridge
from .messages import error_text

COLUMNS = ("started", "what", "result", "duration", "weighed")
BOARDS = ("polymer", "colour")


class HistoryPage(QWidget):
	def __init__(self, config: Config, bridge: Bridge, *, open_url=QDesktopServices.openUrl):
		super().__init__()
		self.bridge = bridge
		self.folder = config.resolve(config.logging.folder)
		self._open_url = open_url
		self.entries = []
		self.table = QTableWidget(0, len(COLUMNS))
		self.table.setHorizontalHeaderLabels(COLUMNS)
		self.table.verticalHeader().setVisible(False)
		self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
		self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
		self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
		self.refresh_button = QPushButton("Refresh")
		self.open_folder_button = QPushButton("Open the log folder")
		self.check_label = QLabel("choose a run")
		self.check_label.setWordWrap(True)
		self.files_label = QLabel("")
		self.files_label.setWordWrap(True)
		self.weighed = {name: _grams_spin() for name in BOARDS}
		self.note = QLineEdit()
		self.save_button = QPushButton("Save to the run log")
		self.save_button.setEnabled(False)
		self.message_label = QLabel("")
		self.message_label.setWordWrap(True)

		buttons = QHBoxLayout()
		buttons.addWidget(self.refresh_button)
		buttons.addWidget(self.open_folder_button)
		buttons.addStretch(1)
		left = QWidget()
		left_layout = QVBoxLayout(left)
		left_layout.addWidget(QLabel(f"folder: {self.folder}"))
		left_layout.addLayout(buttons)
		left_layout.addWidget(self.table, 1)
		after = QGroupBox("Weighed afterwards")
		form = QFormLayout(after)
		for name in BOARDS:
			form.addRow(f"{name} [g] (0 = not weighed)", self.weighed[name])
		form.addRow("note", self.note)
		form.addRow(self.save_button)
		right = QWidget()
		right_layout = QVBoxLayout(right)
		right_layout.addWidget(self.check_label)
		right_layout.addWidget(self.files_label)
		right_layout.addWidget(after)
		right_layout.addWidget(self.message_label)
		right_layout.addStretch(1)
		splitter = QSplitter()
		splitter.addWidget(left)
		splitter.addWidget(right)
		splitter.setStretchFactor(0, 2)
		layout = QVBoxLayout(self)
		layout.addWidget(splitter)

		self.table.itemSelectionChanged.connect(self._selected)
		self.refresh_button.clicked.connect(self.refresh)
		self.open_folder_button.clicked.connect(lambda: self._open_url(QUrl.fromLocalFile(str(self.folder))))
		self.save_button.clicked.connect(self.save)
		self.refresh()

	def showEvent(self, event) -> None:
		self.refresh()
		super().showEvent(event)

	def refresh(self, *_) -> None:
		keep = self.selected()
		keep_path = keep.csv_path if keep is not None else None
		self.entries = list_run_logs(self.folder)
		self.table.blockSignals(True)
		self.table.setRowCount(0)
		for entry in self.entries:
			row = self.table.rowCount()
			self.table.insertRow(row)
			sidecar = entry.sidecar or {}
			duration = sidecar.get("duration_s")
			weighed = {k: v for k, v in (sidecar.get("weighed_mass_g") or {}).items() if v is not None}
			cells = [
				entry.started[:19].replace("T", " ") or entry.csv_path.name,
				entry.job_name or sidecar.get("note") or entry.csv_path.stem,
				sidecar.get("result") or ("does not read" if entry.problem else "not closed"),
				"-" if duration is None else f"{duration:.1f} s",
				", ".join(f"{k} {v:g} g" for k, v in weighed.items()) or "-",
			]
			for column, text in enumerate(cells):
				self.table.setItem(row, column, QTableWidgetItem(text))
		self.table.blockSignals(False)
		paths = [entry.csv_path for entry in self.entries]
		if keep_path in paths:
			self.table.selectRow(paths.index(keep_path))
		elif not self.entries:
			self.check_label.setText("no run logs yet" if self.folder.is_dir() else f"no log folder yet ({self.folder})")

	def selected(self):
		rows = {index.row() for index in self.table.selectedIndexes()}
		return self.entries[rows.pop()] if len(rows) == 1 and self.entries else None

	def _selected(self) -> None:
		entry = self.selected()
		self.message_label.setText("")
		if entry is None:
			return
		self.files_label.setText(f"CSV {entry.csv_path}\nsidecar {entry.sidecar_path}")
		if entry.problem:
			self.check_label.setText(entry.problem)
			self.save_button.setEnabled(False)
			return
		self.check_label.setText("\n".join(check_run_log(entry.csv_path).report()))
		masses = entry.sidecar.get("weighed_mass_g") or {}
		for name, spin in self.weighed.items():
			spin.setEnabled(name in masses)
			spin.setValue(masses.get(name) or 0.0)
		self.note.setText(entry.sidecar.get("note") or "")
		self.save_button.setEnabled(True)

	def save(self) -> None:
		entry = self.selected()
		if entry is None or entry.sidecar is None:
			return
		in_run = entry.sidecar.get("weighed_mass_g") or {}
		masses = {name: (spin.value() or None) for name, spin in self.weighed.items() if name in in_run}
		note, path = self.note.text(), entry.csv_path
		self.bridge.run(update_sidecar, path, weighed_mass_g=masses, note=note, name="save to the run log",
						on_result=lambda _: self._saved(path),
						on_error=lambda error: self.message_label.setText(
							f"not saved: {error if isinstance(error, SidecarError) else error_text('save', error)}"))

	def _saved(self, path: Path) -> None:
		self.refresh()
		self.message_label.setText(f"saved to {path.with_suffix('.json')}")


def _grams_spin() -> QDoubleSpinBox:
	spin = QDoubleSpinBox()
	spin.setRange(0.0, 1_000_000.0)
	spin.setDecimals(2)
	spin.setSuffix(" g")
	return spin

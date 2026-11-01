"""Materials: the materials file as a table, edited in place and saved whole. A row that is not a
valid material is named with every problem and nothing is saved, so the file is never half
written. A broken file is reported and never overwritten (as in the shell)."""

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
	QComboBox, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..config import Config
from ..materials import (
	FIELDS, Kind, MaterialError, MaterialRegistry, load_materials, material_from_text, material_texts,
	save_materials,
)
from .bridge import Bridge
from .messages import error_text

HEADERS = {
	"name": "name", "kind": "kind", "dispenser": "dispenser", "flow_min_g_s": "flow min [g/s]",
	"flow_max_g_s": "flow max [g/s]", "kp": "kp", "ki": "ki", "kd": "kd", "ff_offset_duty": "ff offset [duty]",
	"ff_gain_duty_per_hz": "ff gain [duty/Hz]", "note": "note",
}
KIND_COLUMN = FIELDS.index("kind")


class MaterialsPage(QWidget):
	materials_changed = Signal(object)
	"""The MaterialRegistry just saved."""

	def __init__(self, config: Config, bridge: Bridge):
		super().__init__()
		self.bridge = bridge
		self.path = config.resolve(config.paths.materials)
		self.registry = MaterialRegistry()
		self.table = QTableWidget(0, len(FIELDS))
		self.table.setHorizontalHeaderLabels([HEADERS[name] for name in FIELDS])
		self.table.verticalHeader().setVisible(False)
		self.add_button = QPushButton("Add material")
		self.remove_button = QPushButton("Remove selected")
		self.save_button = QPushButton("Save")
		self.file_label = QLabel(f"file: {self.path}")
		self.message_label = QLabel("")
		self.message_label.setWordWrap(True)

		buttons = QHBoxLayout()
		for button in (self.add_button, self.remove_button, self.save_button):
			buttons.addWidget(button)
		buttons.addStretch(1)
		layout = QVBoxLayout(self)
		layout.addWidget(QLabel("Gains and feedforward left blank use the board's from the config; "
								"the flow window limits the g/s a job may ask of this material."))
		layout.addLayout(buttons)
		layout.addWidget(self.table, 1)
		layout.addWidget(self.file_label)
		layout.addWidget(self.message_label)
		self.add_button.clicked.connect(self.add_row)
		self.remove_button.clicked.connect(self.remove_selected)
		self.save_button.clicked.connect(self.save)
		self.reload()

	def reload(self) -> None:
		try:
			self.registry = load_materials(self.path)
		except MaterialError as error:
			self.registry = MaterialRegistry()
			self.table.setRowCount(0)
			self.save_button.setEnabled(False)
			self.message_label.setText(f"{error}\nThe file is left as it is: fix it by hand, then restart.")
			return
		self.save_button.setEnabled(True)
		self.table.setRowCount(0)
		for material in self.registry.materials():
			self._append(material_texts(material))
		self.message_label.setText("" if self.path.exists() else "no materials file yet: Save writes one")

	def add_row(self) -> None:
		self._append({name: "" for name in FIELDS} | {"kind": Kind.POLYMER.value})
		self.table.setCurrentCell(self.table.rowCount() - 1, 0)

	def remove_selected(self) -> None:
		for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
			self.table.removeRow(row)

	def rows(self) -> list[dict[str, str]]:
		return [{name: self._cell(row, column) for column, name in enumerate(FIELDS)}
				for row in range(self.table.rowCount())]

	def save(self) -> None:
		registry = MaterialRegistry()
		problems = []
		seen: dict[str, int] = {}
		for number, texts in enumerate(self.rows(), start=1):
			try:
				material = material_from_text(texts)
				if material.name in seen:
					raise MaterialError(f"{material.name} is also row {seen[material.name]}")
				registry.put(material)
			except MaterialError as error:
				problems.append(f"row {number}: {error}")
				continue
			seen[material.name] = number
		if problems:
			self.message_label.setText("not saved:\n" + "\n".join(problems))
			return
		self.message_label.setText("")
		self.bridge.run(save_materials, registry, self.path, name="save materials",
						on_result=lambda _: self._saved(registry),
						on_error=lambda error: self.message_label.setText(f"not saved: {error_text('save materials', error)}"))

	def _saved(self, registry: MaterialRegistry) -> None:
		self.registry = registry
		count = len(registry.materials())
		self.message_label.setText(f"saved {count} material{'' if count == 1 else 's'} to {self.path}")
		self.materials_changed.emit(registry)

	def _append(self, texts: dict[str, str]) -> None:
		row = self.table.rowCount()
		self.table.insertRow(row)
		for column, name in enumerate(FIELDS):
			if column == KIND_COLUMN:
				combo = QComboBox()
				combo.addItems([kind.value for kind in Kind])
				combo.setCurrentText(texts[name])
				self.table.setCellWidget(row, column, combo)
			else:
				self.table.setItem(row, column, QTableWidgetItem(texts[name]))

	def _cell(self, row: int, column: int) -> str:
		if column == KIND_COLUMN:
			return self.table.cellWidget(row, column).currentText()
		item = self.table.item(row, column)
		return item.text() if item is not None else ""


def materials_path(config: Config) -> Path:
	return config.resolve(config.paths.materials)

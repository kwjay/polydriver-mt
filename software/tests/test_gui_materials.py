import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem

from polydriver.config import Config
from polydriver.gui.bridge import Bridge
from polydriver.gui.materials_page import KIND_COLUMN, MaterialsPage
from polydriver.materials import FIELDS, Kind, Material, MaterialRegistry, load_materials, save_materials


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def config(tmp_path):
	return Config(base_dir=tmp_path)


def write_materials(config, *materials):
	registry = MaterialRegistry()
	for material in materials:
		registry.put(material)
	save_materials(registry, config.resolve(config.paths.materials))


def make_page(qtbot, config, bridge):
	page = MaterialsPage(config, bridge)
	qtbot.addWidget(page)
	return page


def set_cell(page, row, name, text):
	column = FIELDS.index(name)
	if column == KIND_COLUMN:
		page.table.cellWidget(row, column).setCurrentText(text)
	else:
		page.table.setItem(row, column, QTableWidgetItem(text))


def save_and_wait(qtbot, page, bridge):
	page.save_button.click()
	qtbot.waitUntil(lambda: not bridge.busy, timeout=2000)


def test_the_file_is_shown_one_row_per_material(qtbot, config, bridge):
	write_materials(config, Material("PLA", Kind.POLYMER, dispenser="A", kp=0.4, ki=1.0, kd=0.0),
					Material("Red", Kind.MASTERBATCH))
	page = make_page(qtbot, config, bridge)
	rows = page.rows()
	assert [row["name"] for row in rows] == ["PLA", "Red"]
	assert rows[0]["kp"] == "0.4" and rows[0]["dispenser"] == "A"
	assert rows[1]["kind"] == "masterbatch" and rows[1]["kp"] == ""


def test_no_file_yet_says_save_writes_one(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	assert page.table.rowCount() == 0
	assert "Save writes one" in page.message_label.text()


def test_an_added_row_is_saved_to_the_file(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	changed = []
	page.materials_changed.connect(changed.append)
	page.add_button.click()
	set_cell(page, 0, "name", "TPU")
	set_cell(page, 0, "flow_min_g_s", "0,1")
	set_cell(page, 0, "flow_max_g_s", "2")
	save_and_wait(qtbot, page, bridge)
	material = load_materials(config.resolve(config.paths.materials)).get("TPU")
	assert material.kind is Kind.POLYMER
	assert (material.flow_min_g_s, material.flow_max_g_s) == (0.1, 2.0)
	assert "saved 1 material to" in page.message_label.text()
	assert [m.name for m in changed[0].materials()] == ["TPU"]


def test_the_kind_is_chosen_from_a_list(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	page.add_button.click()
	set_cell(page, 0, "name", "Blue")
	set_cell(page, 0, "kind", "masterbatch")
	save_and_wait(qtbot, page, bridge)
	assert load_materials(config.resolve(config.paths.materials)).get("Blue").kind is Kind.MASTERBATCH


def test_a_bad_row_saves_nothing_and_names_every_problem(qtbot, config, bridge):
	write_materials(config, Material("PLA", Kind.POLYMER))
	path = config.resolve(config.paths.materials)
	before = path.read_bytes()
	page = make_page(qtbot, config, bridge)
	page.add_button.click()
	set_cell(page, 1, "name", "TPU")
	set_cell(page, 1, "kp", "fast")
	set_cell(page, 1, "flow_min_g_s", "1")
	page.save_button.click()
	text = page.message_label.text()
	assert text.startswith("not saved:")
	assert "row 2: TPU" in text and "kp 'fast' is not a number" in text and "flow" in text
	assert path.read_bytes() == before


def test_a_row_with_no_name_is_refused(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	page.add_button.click()
	page.save_button.click()
	assert "row 1: a material with no name" in page.message_label.text()


def test_two_rows_with_one_name_are_refused(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	page.add_button.click()
	page.add_button.click()
	set_cell(page, 0, "name", "PLA")
	set_cell(page, 1, "name", "PLA")
	page.save_button.click()
	assert "row 2: PLA is also row 1" in page.message_label.text()
	assert not config.resolve(config.paths.materials).exists()


def test_removing_a_row_and_saving_drops_it(qtbot, config, bridge):
	write_materials(config, Material("PLA", Kind.POLYMER), Material("Red", Kind.MASTERBATCH))
	page = make_page(qtbot, config, bridge)
	page.table.setCurrentCell(0, 0)
	page.remove_button.click()
	save_and_wait(qtbot, page, bridge)
	assert [m.name for m in load_materials(config.resolve(config.paths.materials)).materials()] == ["Red"]


def test_a_broken_file_is_reported_and_never_overwritten(qtbot, config, bridge):
	path = config.resolve(config.paths.materials)
	path.write_text("{not json", encoding="utf-8")
	page = make_page(qtbot, config, bridge)
	assert not page.save_button.isEnabled()
	assert "fix it by hand" in page.message_label.text()
	assert path.read_text(encoding="utf-8") == "{not json"


def test_a_save_that_fails_says_so(qtbot, config, bridge, monkeypatch):
	import polydriver.gui.materials_page as module

	def refuse(registry, path):
		raise OSError("disk full")

	monkeypatch.setattr(module, "save_materials", refuse)
	page = make_page(qtbot, config, bridge)
	page.add_button.click()
	set_cell(page, 0, "name", "PLA")
	save_and_wait(qtbot, page, bridge)
	assert "not saved" in page.message_label.text() and "disk full" in page.message_label.text()


def test_what_is_saved_reads_back_as_shown(qtbot, config, bridge):
	write_materials(config, Material("PLA", Kind.POLYMER, dispenser="A", flow_min_g_s=0.2, flow_max_g_s=3.0,
									kp=0.4, ki=1.5, kd=0.01, ff_offset_duty=120.0, ff_gain_duty_per_hz=1.6, note="dry"))
	path = config.resolve(config.paths.materials)
	before = json.loads(path.read_text(encoding="utf-8"))
	page = make_page(qtbot, config, bridge)
	save_and_wait(qtbot, page, bridge)
	assert json.loads(path.read_text(encoding="utf-8")) == before

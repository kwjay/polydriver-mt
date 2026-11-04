import json
from pathlib import Path

import pytest

from polydriver.config import Config
from polydriver.gui.bridge import Bridge
from polydriver.gui.history_page import HistoryPage
from polydriver.protocol.constants import Role
from polydriver.telemetry import RunLog


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def config(tmp_path):
	return Config(base_dir=tmp_path)


def log_folder(config):
	return config.resolve(config.logging.folder)


def make_log(config, name, *, job=None, result="DONE"):
	log = RunLog.create(log_folder(config), (Role.POLYMER, Role.COLOUR), name=name, note="a note")
	if job is not None:
		log.set_job(job)
	log.close(result=result)
	return log


def make_page(qtbot, config, bridge, opened=None):
	page = HistoryPage(config, bridge, open_url=(opened.append if opened is not None else lambda url: None))
	qtbot.addWidget(page)
	return page


def cells(page, row):
	return [page.table.item(row, column).text() for column in range(page.table.columnCount())]


def test_no_logs_yet_says_so(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	assert page.table.rowCount() == 0 and "no log folder yet" in page.check_label.text()


def test_runs_are_listed_with_job_result_and_duration(qtbot, config, bridge):
	make_log(config, "run-1", job={"name": "PP red"})
	make_log(config, "run-2", result="ABORTED")
	page = make_page(qtbot, config, bridge)
	rows = [cells(page, row) for row in range(page.table.rowCount())]
	whats = sorted(row[1] for row in rows)
	assert whats == ["PP red", "a note"]
	assert {row[2] for row in rows} == {"DONE", "ABORTED"}
	assert all(row[3].endswith(" s") and row[4] == "-" for row in rows)


def test_choosing_a_run_shows_its_check_and_files(qtbot, config, bridge):
	log = make_log(config, "run-1")
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	assert page.check_label.text().startswith("run log: DONE")
	assert str(log.csv_path) in page.files_label.text()
	assert page.note.text() == "a note" and page.save_button.isEnabled()


def test_weighed_masses_are_saved_into_the_sidecar(qtbot, config, bridge):
	log = make_log(config, "run-1")
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	page.weighed["colour"].setValue(1.25)
	page.note.setText("weighed later")
	page.save_button.click()
	qtbot.waitUntil(lambda: page.message_label.text().startswith("saved to"), timeout=2000)
	data = json.loads(log.sidecar_path.read_text(encoding="utf-8"))
	assert data["weighed_mass_g"] == {"polymer": None, "colour": 1.25} and data["note"] == "weighed later"
	assert cells(page, 0)[4] == "colour 1.25 g"
	assert page.selected().csv_path == log.csv_path
	assert page.weighed["colour"].value() == 1.25


def test_a_broken_sidecar_is_shown_and_cannot_be_saved(qtbot, config, bridge):
	make_log(config, "good")
	(log_folder(config) / "x.csv").write_text("", encoding="utf-8")
	(log_folder(config) / "x.json").write_text("{", encoding="utf-8")
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	assert page.save_button.isEnabled()
	page.table.selectRow(1)
	assert "does not read" in page.check_label.text() and not page.save_button.isEnabled()
	assert cells(page, 1)[2] == "does not read"


def test_open_folder_opens_the_log_folder(qtbot, config, bridge):
	opened = []
	page = make_page(qtbot, config, bridge, opened)
	page.open_folder_button.click()
	assert Path(opened[0].toLocalFile()) == log_folder(config)

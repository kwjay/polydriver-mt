import pytest

from polydriver.calibration import CalibrationStore, load_calibration, save_calibration
from polydriver.config import Config
from polydriver.gui.bridge import Bridge
from polydriver.gui.jobs_page import JobsPage
from polydriver.jobs import load_job, save_job
from polydriver.jobs.editing import JobForm, form_job
from polydriver.materials import Kind, Material, MaterialRegistry, save_materials


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


@pytest.fixture
def config(tmp_path):
	config = Config(base_dir=tmp_path)
	registry = MaterialRegistry()
	registry.put(Material("PP", Kind.POLYMER, dispenser="A"))
	registry.put(Material("Red", Kind.MASTERBATCH, dispenser="B"))
	registry.put(Material("Blue", Kind.MASTERBATCH))
	save_materials(registry, config.resolve(config.paths.materials))
	save_calibration(make_store(), config.resolve(config.paths.calibration))
	return config


def make_store():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 30.0), (20.0, 60.0), (40.0, 120.0)):
		store.add_measurement("A", "PP", speed_hz, grams, 60.0)
	for speed_hz, grams in ((10.0, 1.2), (10.0, 1.2), (20.0, 2.4), (40.0, 4.8)):
		store.add_measurement("B", "Red", speed_hz, grams, 60.0)
	return store


def jobs_folder(config):
	return config.resolve(config.paths.jobs)


def write_job(config, name="PP red", **changes):
	values = dict(name=name, polymer_material="PP", polymer_dispenser="A", colour_material="Red",
				colour_dispenser="B", duration_s=600.0, total_mass_g=None, total_flow_g_s=1.0, phi_start=0.02,
				phi_end=0.05, curve="linear")
	job = form_job(JobForm(**(values | changes)), make_store(), created="2026-10-01T09:00:00+02:00")
	path = jobs_folder(config) / f"{name}.json"
	save_job(job, path)
	return path


def make_page(qtbot, config, bridge):
	page = JobsPage(config, bridge)
	qtbot.addWidget(page)
	return page


def names(page):
	return [page.table.item(row, 0).text() for row in range(page.table.rowCount())]


def test_the_list_shows_the_jobs_in_the_folder(qtbot, config, bridge):
	write_job(config, "b job")
	write_job(config, "A job", total_mass_g=900.0, total_flow_g_s=None)
	page = make_page(qtbot, config, bridge)
	assert names(page) == ["A job", "b job"]
	row = [page.table.item(0, column).text() for column in range(page.table.columnCount())]
	assert row[1:5] == ["PP", "Red", "600 s", "900 g"]


def test_a_file_that_does_not_load_is_listed_with_why(qtbot, config, bridge):
	jobs_folder(config).mkdir()
	(jobs_folder(config) / "broken.json").write_text("{", encoding="utf-8")
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	assert "not valid JSON" in page.list_label.text()
	assert not page.run_button.isEnabled() and page.delete_button.isEnabled()


def test_selecting_a_job_opens_it_in_the_editor(qtbot, config, bridge):
	write_job(config, curve="s-curve", curve_param=8.0, hold_start_s=30.0)
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	editor = page.editor
	assert editor.name.text() == "PP red"
	assert (editor.phi_start.text(), editor.phi_end.text()) == ("2", "5")
	assert editor.curve.currentText() == "s-curve" and editor.curve_param.text() == "8"
	assert editor.hold_start.text() == "30"
	assert editor.title_label.text() == "editing PP red.json"


def test_a_new_job_is_saved_with_its_calibration_hashes(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	page.new_button.click()
	editor.name.setText("first")
	editor.colour_material.setCurrentText("Red")
	assert editor.colour_dispenser.text() == "B"
	editor.save_button.click()
	path = jobs_folder(config) / "first.json"
	job = load_job(path)
	store = load_calibration(config.resolve(config.paths.calibration))
	assert job.polymer.hash == store.curve("A", "PP").hash and job.colour.hash == store.curve("B", "Red").hash
	assert names(page) == ["first"]
	assert editor.message_label.text() == "saved first.json"


def test_saving_an_open_job_keeps_its_file_and_when_it_was_created(qtbot, config, bridge):
	path = write_job(config)
	created = load_job(path).created
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	page.editor.name.setText("PP red renamed")
	page.editor.phi_end.setText("8")
	page.editor.save_button.click()
	job = load_job(path)
	assert job.name == "PP red renamed" and job.phi_end == pytest.approx(0.08) and job.created == created
	assert created == "2026-10-01T09:00:00+02:00" and job.modified != created
	assert [p.name for p in jobs_folder(config).iterdir()] == ["PP red.json"]


def test_save_as_new_writes_another_file(qtbot, config, bridge):
	write_job(config)
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	page.editor.save_as_button.click()
	assert sorted(p.name for p in jobs_folder(config).iterdir()) == ["PP red-2.json", "PP red.json"]


def test_a_job_without_a_calibration_is_not_saved(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	editor.name.setText("blue")
	editor.colour_material.setCurrentText("Blue")
	editor.colour_dispenser.setText("C")
	editor.save_button.click()
	assert "there is no calibration C / Blue; make one on Calibration" in editor.message_label.text()
	assert not jobs_folder(config).exists()


def test_words_where_numbers_go_are_all_named(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	editor.name.setText("x")
	editor.duration.setText("ten")
	editor.phi_end.setText("")
	editor.save_button.click()
	text = editor.message_label.text()
	assert "duration 'ten' is not a number" in text and "end: give a value" in text


def test_a_nameless_job_is_not_saved(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	page.editor.save_button.click()
	assert page.editor.message_label.text() == "not saved: give the job a name"


def test_the_preview_shows_both_channels_and_the_third_amount(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	editor.duration.setText("100")
	editor.flow.setText("2")
	editor.phi_start.setText("2")
	editor.phi_end.setText("2")
	editor.update_preview()
	assert "= 200 g" in editor.amount_label.text() and "colour 4 g" in editor.amount_label.text()
	_, colour = editor.flow_data("colour")
	_, polymer = editor.flow_data("polymer")
	assert colour[0] == pytest.approx(0.04) and polymer[-1] == pytest.approx(1.96)
	assert "no problems" in editor.check_label.text() and "segments: polymer " in editor.check_label.text()


def test_points_outside_the_calibration_are_shown_and_reported(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	editor.flow.setText("5")
	editor.update_preview()
	assert not editor.check.ok
	assert "error: polymer" in editor.check_label.text() and "above the calibrated" in editor.check_label.text()
	assert editor.bad_points.data.size > 0
	assert "not runnable" in editor.check_label.text()


def test_the_curve_parameter_is_asked_for_only_when_the_curve_has_one(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	editor.curve.setCurrentText("linear")
	assert not editor.curve_param.isVisibleTo(editor)
	editor.curve.setCurrentText("steps")
	assert editor.curve_param.isVisibleTo(editor) and editor.curve_param.text() == "5"
	assert editor.profile_form.labelForField(editor.curve_param).text() == "curve levels"
	editor.curve.setCurrentText("table")
	assert editor.table.isVisibleTo(editor) and not editor.curve_param.isVisibleTo(editor)


def test_a_table_curve_is_saved_with_its_points(qtbot, config, bridge):
	page = make_page(qtbot, config, bridge)
	editor = page.editor
	editor.name.setText("table")
	editor.curve.setCurrentText("table")
	editor.table.setText("0 0; 0.2 0.6; 1 1")
	editor.save_button.click()
	assert load_job(jobs_folder(config) / "table.json").curve.progress(0.2) == pytest.approx(0.6)


def test_duplicate_copies_the_selected_job(qtbot, config, bridge):
	write_job(config)
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	page.duplicate_button.click()
	assert names(page) == ["PP red", "PP red copy"]
	assert page.selected_path().name == "PP red copy.json"


def test_delete_asks_once_more_before_it_deletes(qtbot, config, bridge):
	path = write_job(config)
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	page.delete_button.click()
	assert path.exists() and page.delete_button.text() == "Really delete PP red.json?"
	page.cancel_delete_button.click()
	assert page.delete_button.text() == "Delete"
	page.delete_button.click()
	page.delete_button.click()
	assert not path.exists() and names(page) == []


def test_choosing_another_job_disarms_delete(qtbot, config, bridge):
	first = write_job(config, "a")
	write_job(config, "b")
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	page.delete_button.click()
	page.table.selectRow(1)
	assert page.delete_button.text() == "Delete"
	page.delete_button.click()
	assert first.exists() and page.delete_button.text() == "Really delete b.json?"


def test_run_asks_for_the_selected_job(qtbot, config, bridge):
	path = write_job(config)
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	with qtbot.waitSignal(page.run_requested) as signal:
		page.run_button.click()
	assert signal.args == [path]


def test_a_changed_calibration_is_warned_about(qtbot, config, bridge):
	write_job(config)
	store = make_store()
	store.add_measurement("A", "PP", 30.0, 90.0, 60.0)
	save_calibration(store, config.resolve(config.paths.calibration))
	page = make_page(qtbot, config, bridge)
	page.table.selectRow(0)
	assert "has changed since the job was saved" in page.editor.check_label.text()

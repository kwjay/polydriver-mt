from datetime import datetime, timedelta, timezone

import pytest

from polydriver.calibration import CalibrationStore
from polydriver.jobs import JobError, load_job, save_job
from polydriver.jobs.editing import (
	JobForm, duplicate_job, form_job, form_profile, free_job_path, job_file_name, job_form, list_jobs, parse_table,
	table_text,
)

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=1)))


@pytest.fixture
def store():
	store = CalibrationStore()
	for speed_hz, grams in ((10.0, 30.0), (20.0, 60.0), (40.0, 120.0)):
		store.add_measurement("A", "PP", speed_hz, grams, 60.0)
		store.add_measurement("B", "Red", speed_hz, grams / 10, 60.0)
	return store


def form(**changes):
	values = dict(name="PP red", polymer_material="PP", polymer_dispenser="A", colour_material="Red",
				colour_dispenser="B", duration_s=600.0, total_mass_g=None, total_flow_g_s=1.0, phi_start=0.01,
				phi_end=0.05, curve="linear")
	return JobForm(**(values | changes))


def test_two_amounts_give_the_third():
	profile = form_profile(form())
	assert profile.amount.total_mass_g == pytest.approx(600.0)


def test_a_profile_that_is_not_one_says_why():
	with pytest.raises(JobError, match="give two of"):
		form_profile(form(total_flow_g_s=None))
	with pytest.raises(JobError, match="phi_end"):
		form_profile(form(phi_end=1.5))
	with pytest.raises(JobError, match="levels"):
		form_profile(form(curve="steps", curve_param=1.0))


def test_a_curve_parameter_goes_to_its_curve():
	assert form_profile(form(curve="exponential", curve_param=2.0)).curve.params() == {"k": 2.0}
	assert form_profile(form(curve="steps", curve_param=4.0)).curve.params() == {"levels": 4}
	assert form_profile(form(curve="exponential")).curve.params() == {"k": 3.0}


def test_a_table_curve_takes_its_points():
	table = parse_table("0 0; 0,5 0.8\n1 1")
	assert table == ((0.0, 0.0), (0.5, 0.8), (1.0, 1.0))
	assert form_profile(form(curve="table", table=table)).curve.progress(0.5) == pytest.approx(0.8)
	assert table_text(table) == "0 0; 0.5 0.8; 1 1"


def test_a_table_that_is_not_points_is_refused():
	with pytest.raises(JobError, match="point 2 '0.5': give x and progress"):
		parse_table("0 0; 0.5; 1 1")
	with pytest.raises(JobError, match="must be numbers"):
		parse_table("0 zero")


def test_the_job_names_each_calibration_with_its_hash(store):
	job = form_job(form(), store, now=NOW)
	assert (job.polymer.dispenser, job.polymer.material, job.polymer.hash) == ("A", "PP", store.curve("A", "PP").hash)
	assert job.colour.hash == store.curve("B", "Red").hash
	assert job.created == job.modified == "2026-11-01T12:00:00+01:00"


def test_an_edited_job_keeps_when_it_was_created(store):
	job = form_job(form(), store, created="2026-10-01T09:00:00+02:00", now=NOW)
	assert job.created == "2026-10-01T09:00:00+02:00" and job.modified == "2026-11-01T12:00:00+01:00"


def test_a_job_without_its_calibration_is_refused(store):
	with pytest.raises(JobError, match="colour: there is no calibration A / Red; make one on Calibration"):
		form_job(form(colour_dispenser="A"), store)
	with pytest.raises(JobError, match="polymer: choose a material"):
		form_job(form(polymer_material=" "), store)
	with pytest.raises(JobError, match="name the dispenser"):
		form_job(form(polymer_dispenser=""), store)


def test_a_job_reads_back_into_the_same_form(store):
	original = form(curve="s-curve", curve_param=8.0, hold_start_s=30.0, hold_end_s=10.0, log=False)
	assert job_form(form_job(original, store)) == original
	table = form(curve="table", table=((0.0, 0.0), (0.3, 0.5), (1.0, 1.0)))
	assert job_form(form_job(table, store)) == table


def test_file_names_keep_only_safe_characters():
	assert job_file_name("PP red 1-5 %") == "PP red 1-5 _.json"
	assert job_file_name("a/b\\c:d") == "a_b_c_d.json"
	assert job_file_name("  ") == "job.json"


def test_a_name_already_taken_gets_a_number(tmp_path, store):
	first = free_job_path(tmp_path, "PP red")
	save_job(form_job(form(), store), first)
	assert free_job_path(tmp_path, "PP red") == tmp_path / "PP red-2.json"
	assert free_job_path(tmp_path, "PP red", keep=first) == first


def test_the_folder_lists_every_job_and_says_why_one_does_not_load(tmp_path, store):
	save_job(form_job(form(name="b job"), store), tmp_path / "b job.json")
	save_job(form_job(form(name="A job"), store), tmp_path / "A job.json")
	(tmp_path / "broken.json").write_text("{", encoding="utf-8")
	entries = list_jobs(tmp_path)
	assert [e.path.name for e in entries] == ["A job.json", "b job.json", "broken.json"]
	assert entries[0].job.name == "A job"
	assert entries[2].job is None and "not valid JSON" in entries[2].problem
	assert list_jobs(tmp_path / "missing") == []


def test_a_duplicate_is_a_new_file_and_a_new_job(tmp_path, store):
	path = tmp_path / "PP red.json"
	save_job(form_job(form(), store, now=NOW - timedelta(days=3)), path)
	copy = duplicate_job(path, now=NOW)
	assert copy == tmp_path / "PP red copy.json"
	job = load_job(copy)
	assert job.name == "PP red copy" and job.created == "2026-11-01T12:00:00+01:00"
	assert load_job(path).name == "PP red"

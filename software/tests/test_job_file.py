import json
from pathlib import Path

import pytest

from polydriver.calibration import CalibrationStore
from polydriver.jobs import (
	JOB_SCHEMA,
	CalibrationRef,
	GradientJob,
	JobError,
	JobFileError,
	LinearCurve,
	SCurve,
	TableCurve,
	load_job,
	save_job,
)

EXAMPLE = Path(__file__).parent / "data" / "gradient_job_example.json"


def example_data():
	return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def write(tmp_path, data):
	path = tmp_path / "job.json"
	path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
	return path


# --- The example from GUI_gradient_job_flow.md --------------------------------------------------

def test_the_documented_example_loads():
	job = load_job(EXAMPLE)
	assert job.name == "PP red 1-5 % linear"
	assert (job.polymer.material, job.polymer.dispenser) == ("PP", "B")
	assert (job.colour.material, job.colour.dispenser) == ("MB-red", "A")
	assert (job.created, job.modified) == ("2026-10-03T15:00:00+02:00", "2026-10-03T15:20:00+02:00")
	assert (job.log, job.live_plot) == (True, True)


def test_the_example_amount_gives_the_mass_and_the_profile():
	job = load_job(EXAMPLE)
	assert job.amount_given == {"duration_s": 600, "total_flow_g_s": 1.5}
	profile = job.profile
	assert profile.amount.total_mass_g == 900.0
	assert (profile.phi_start, profile.phi_end, profile.hold_start_s, profile.hold_end_s) == (0.01, 0.05, 30, 30)
	assert isinstance(profile.curve, LinearCurve)
	assert profile.colour_mass_g == pytest.approx(27.0)


def test_the_example_keeps_its_layout_when_saved(tmp_path):
	save_job(load_job(EXAMPLE), tmp_path / "job.json")
	assert json.loads((tmp_path / "job.json").read_text(encoding="utf-8")) == example_data()


# --- Round trips ---------------------------------------------------------------------------

def job(**changes):
	values = dict(
		name="TPU blue s-curve",
		polymer=CalibrationRef("B", "TPU", "a" * 64),
		colour=CalibrationRef("A", "MB-blue", "b" * 64),
		amount_given={"total_mass_g": 500.0, "total_flow_g_s": 1.0},
		phi_start=0.02, phi_end=0.04, curve=SCurve(8.0), hold_start_s=10.0, hold_end_s=0.0,
		created="2026-10-26T17:00:00+01:00", modified="2026-10-26T17:05:00+01:00", log=False, live_plot=None,
	)
	values.update(changes)
	return GradientJob(**values)


@pytest.mark.parametrize("curve", [LinearCurve(), SCurve(8.0), TableCurve([(0, 0), (0.4, 0.8), (1, 1)])], ids=repr)
def test_save_and_load_round_trip(tmp_path, curve):
	path = tmp_path / "jobs" / "job.json"
	save_job(job(curve=curve), path)
	assert load_job(path) == job(curve=curve)
	assert not list(path.parent.glob("*.tmp"))


def test_only_the_amounts_given_are_saved(tmp_path):
	save_job(job(), tmp_path / "job.json")
	amount = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))["amount"]
	assert amount == {"duration_s": None, "total_mass_g": 500.0, "total_flow_g_s": 1.0}


def test_an_amount_left_empty_is_not_one_of_the_given(tmp_path):
	given = job(amount_given={"duration_s": None, "total_mass_g": 500.0, "total_flow_g_s": 1.0})
	assert given.amount_given == {"total_mass_g": 500.0, "total_flow_g_s": 1.0}
	save_job(given, tmp_path / "job.json")
	assert load_job(tmp_path / "job.json") == job()


def test_a_calibration_reference_is_taken_from_a_curve():
	store = CalibrationStore()
	store.add_measurement("B", "PP", 20.0, 10.0, 10.0)
	curve = store.curve("B", "PP")
	assert CalibrationRef.of(curve) == CalibrationRef("B", "PP", curve.hash)


# --- Refused jobs -------------------------------------------------------------------------------

@pytest.mark.parametrize("changes, message", [
	(dict(name=" "), "name"),
	(dict(polymer=CalibrationRef("B", "TPU", "")), "polymer calibration .*hash"),
	(dict(colour=CalibrationRef("", "MB", "x")), "colour calibration .*dispenser"),
	(dict(amount_given={"duration_s": 60.0}), "give two of"),
	(dict(amount_given={"weight_g": 60.0, "duration_s": 60.0}), "unknown amount 'weight_g'"),
	(dict(phi_end=2.0), "phi_end"),
	(dict(hold_start_s=600.0), "no time for the gradient"),
	(dict(created="yesterday"), "created 'yesterday' .*ISO 8601"),
	(dict(modified="2026-10-26T17:05:00"), "modified .*UTC offset"),
	(dict(log="yes"), "log .*true, false or null"),
])
def test_a_bad_job_is_refused(changes, message):
	with pytest.raises(JobError, match=message):
		job(**changes)


# --- Broken files, named by file and field ---------------------------------------------------

def edited(edit):
	data = example_data()
	edit(data)
	return data


@pytest.mark.parametrize("data, message", [
	(edited(lambda d: d.update(schema=2)), "schema 2"),
	(edited(lambda d: d.pop("profile")), "missing 'profile'"),
	(edited(lambda d: d.update(colour="red")), "unknown key 'colour'"),
	(edited(lambda d: d["profile"].update(curv="linear")), "profile: unknown key 'curv'"),
	(edited(lambda d: d["calibration"]["colour"].pop("hash")), "calibration.colour: missing 'hash'"),
	(edited(lambda d: d["materials"].update(colour="MB-blue")),
	"calibration.colour is for MB-red but materials.colour is MB-blue"),
	(edited(lambda d: d["materials"].pop("colour")), "materials: missing 'colour'"),
	(edited(lambda d: d.update(materials=["PP"])), "materials: must be an object"),
	(edited(lambda d: d.update(pid_override={"kp": 1})), "pid_override .*the gains come from the materials"),
	(edited(lambda d: d["profile"].update(curve="cubic")), "profile: unknown curve 'cubic'"),
	(edited(lambda d: d["profile"].update(phi_start="1%")), "profile: phi_start"),
	(edited(lambda d: d["amount"].update(duration_s=None)), "amount: give two of"),
	(edited(lambda d: d["run_defaults"].update(log=1)), "run_defaults: log"),
	(edited(lambda d: d.update(created="soon")), "created 'soon'"),
])
def test_a_broken_file_names_the_file_and_the_field(tmp_path, data, message):
	path = write(tmp_path, data)
	with pytest.raises(JobFileError, match=message) as raised:
		load_job(path)
	assert str(path) in str(raised.value)


@pytest.mark.parametrize("text, message", [("{nope", "not valid JSON"), ("[1]", "must hold a JSON object")])
def test_a_file_that_is_not_a_job(tmp_path, text, message):
	path = tmp_path / "job.json"
	path.write_text(text, encoding="utf-8")
	with pytest.raises(JobFileError, match=message):
		load_job(path)


def test_a_missing_job_file_is_an_error(tmp_path):
	with pytest.raises(JobFileError, match="no job file"):
		load_job(tmp_path / "none.json")


def test_the_schema_is_1():
	assert JOB_SCHEMA == 1

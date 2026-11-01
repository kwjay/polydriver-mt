import json

import pytest

from polydriver.protocol.constants import Role
from polydriver.telemetry import RunLog, SidecarError, list_run_logs, update_sidecar


def make_log(folder, name, roles=(Role.POLYMER, Role.COLOUR), note=""):
	log = RunLog.create(folder, roles, name=name, note=note)
	log.close(result="DONE")
	return log


def test_runs_are_listed_newest_first_and_a_csv_without_sidecar_is_not_one(tmp_path):
	first = make_log(tmp_path, "a")
	second = make_log(tmp_path, "b")
	for path, started in ((first.sidecar_path, "2026-10-30T10:00:00+01:00"), (second.sidecar_path, "2026-11-01T10:00:00+01:00")):
		data = json.loads(path.read_text(encoding="utf-8"))
		data["started"] = started
		path.write_text(json.dumps(data), encoding="utf-8")
	(tmp_path / "sweep-polymer.csv").write_text("duty,speed\n", encoding="utf-8")
	entries = list_run_logs(tmp_path)
	assert [e.csv_path.name for e in entries] == ["b.csv", "a.csv"]
	assert entries[0].started == "2026-11-01T10:00:00+01:00" and entries[0].job_name is None


def test_a_sidecar_that_does_not_read_is_listed_with_why(tmp_path):
	(tmp_path / "x.csv").write_text("", encoding="utf-8")
	(tmp_path / "x.json").write_text("{", encoding="utf-8")
	(entry,) = list_run_logs(tmp_path)
	assert entry.sidecar is None and "does not read" in entry.problem
	assert list_run_logs(tmp_path / "missing") == []


def test_weighed_masses_and_a_note_are_set_afterwards(tmp_path):
	log = make_log(tmp_path, "run")
	update_sidecar(log.csv_path, weighed_mass_g={"polymer": 55.5}, note="weighed cold")
	data = json.loads(log.sidecar_path.read_text(encoding="utf-8"))
	assert data["weighed_mass_g"] == {"polymer": 55.5, "colour": None}
	assert data["note"] == "weighed cold" and data["result"] == "DONE"
	update_sidecar(log.sidecar_path, weighed_mass_g={"polymer": None})
	assert json.loads(log.sidecar_path.read_text(encoding="utf-8"))["weighed_mass_g"]["polymer"] is None
	assert not log.sidecar_path.with_suffix(".json.tmp").exists()


def test_a_board_not_in_the_run_or_a_negative_mass_is_refused(tmp_path):
	log = make_log(tmp_path, "run", roles=(Role.POLYMER,))
	before = log.sidecar_path.read_bytes()
	with pytest.raises(SidecarError, match="the colour board was not in this run"):
		update_sidecar(log.csv_path, weighed_mass_g={"colour": 1.0})
	with pytest.raises(SidecarError, match=">= 0 g"):
		update_sidecar(log.csv_path, weighed_mass_g={"polymer": -1.0})
	assert log.sidecar_path.read_bytes() == before


def test_a_file_that_is_not_a_sidecar_is_refused(tmp_path):
	(tmp_path / "x.json").write_text('{"schema": 99}', encoding="utf-8")
	with pytest.raises(SidecarError, match="not a run log sidecar"):
		update_sidecar(tmp_path / "x.csv", note="n")
	with pytest.raises(SidecarError, match="cannot read it"):
		update_sidecar(tmp_path / "missing.csv", note="n")

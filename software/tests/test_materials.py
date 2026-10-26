import json
from pathlib import Path

import pytest

from polydriver.calibration import CalibrationStore
from polydriver.config import BoardSettings
from polydriver.materials import (
	SCHEMA,
	Kind,
	Material,
	MaterialError,
	MaterialFileError,
	MaterialRegistry,
	UnknownMaterialError,
	load_materials,
	save_materials,
)
from polydriver.protocol.constants import Role

STARTER_FILE = Path(__file__).resolve().parents[1] / "materials.json"


def pla(**changes):
	values = dict(name="PLA", kind=Kind.POLYMER, dispenser="B", flow_min_g_s=0.5, flow_max_g_s=3.0)
	values.update(changes)
	return Material(**values)


# --- The starter file -------------------------------------------------------------------------

def test_the_starter_file_loads_and_holds_the_thesis_materials():
	registry = load_materials(STARTER_FILE)
	assert [m.name for m in registry.materials()] == ["Masterbatch", "PLA", "PP", "TPU"]
	assert [m.name for m in registry.of_kind(Kind.POLYMER)] == ["PLA", "PP", "TPU"]
	assert [m.name for m in registry.of_kind(Kind.MASTERBATCH)] == ["Masterbatch"]


def test_the_starter_file_claims_no_numbers_that_were_not_measured():
	for material in load_materials(STARTER_FILE).materials():
		assert not material.has_gains and not material.has_feedforward and not material.has_flow_window
		assert material.note


def test_the_starter_file_is_already_in_the_saved_layout(tmp_path):
	save_materials(load_materials(STARTER_FILE), tmp_path / "materials.json")
	saved = (tmp_path / "materials.json").read_text(encoding="utf-8")
	assert saved == STARTER_FILE.read_text(encoding="utf-8").replace("\r\n", "\n")


# --- One material -----------------------------------------------------------------------------

def test_a_material_needs_only_a_name_and_a_kind():
	material = Material("PLA", "polymer")
	assert material.kind is Kind.POLYMER
	assert (material.dispenser, material.has_gains, material.has_flow_window) == (None, False, False)


def test_names_are_trimmed():
	material = Material("  PLA ", Kind.POLYMER, dispenser=" B ")
	assert (material.name, material.dispenser) == ("PLA", "B")


def test_the_kind_picks_the_board():
	assert Material("PLA", Kind.POLYMER).role is Role.POLYMER
	assert Material("Red", Kind.MASTERBATCH).role is Role.COLOUR


@pytest.mark.parametrize("changes, message", [
	(dict(name=""), "name"),
	(dict(name="PLA\nred"), "name"),
	(dict(name=5), "name"),
	(dict(kind="dye"), "kind 'dye'.*polymer or masterbatch"),
	(dict(dispenser=""), "dispenser"),
	(dict(flow_max_g_s=None), "flow_min_g_s and flow_max_g_s together"),
	(dict(flow_min_g_s=-0.1), "flow_min_g_s"),
	(dict(flow_min_g_s=3.0), "flow_min_g_s .* below flow_max_g_s"),
	(dict(flow_max_g_s=float("inf")), "flow_max_g_s"),
	(dict(flow_min_g_s=True), "flow_min_g_s"),
	(dict(kp=1.0), "kp, ki and kd together"),
	(dict(kp=-1.0, ki=0.0, kd=0.0), "kp"),
	(dict(kp=1.0, ki=float("nan"), kd=0.0), "ki"),
	(dict(ff_offset_duty=10.0), "ff_offset_duty and ff_gain_duty_per_hz together"),
	(dict(ff_offset_duty=300.0, ff_gain_duty_per_hz=0.1), "ff_offset_duty .* 0 to 255"),
	(dict(ff_offset_duty=10.0, ff_gain_duty_per_hz=-0.1), "ff_gain_duty_per_hz"),
	(dict(note=None), "note"),
])
def test_a_bad_material_is_refused_with_its_name(changes, message):
	with pytest.raises(MaterialError, match=message):
		pla(**changes)


def test_every_problem_is_named_at_once():
	with pytest.raises(MaterialError, match="flow_min_g_s.*kp, ki and kd"):
		pla(flow_min_g_s=-1.0, kp=1.0)


def test_the_flow_window_includes_its_ends():
	material = pla()
	assert material.allows_flow(0.5) and material.allows_flow(3.0) and material.allows_flow(1.7)
	assert not material.allows_flow(0.49) and not material.allows_flow(3.01)


def test_without_a_flow_window_every_flow_is_allowed():
	assert Material("PLA", Kind.POLYMER).allows_flow(100.0)


# --- Gains and feedforward over the board config -------------------------------------------------

BOARD = BoardSettings(kp=0.5, ki=1.0, kd=0.0, alpha=0.3, ff_offset_duty=20.0, ff_gain_duty_per_hz=0.4)


def test_a_material_without_tuning_keeps_the_board_settings():
	assert Material("PLA", Kind.POLYMER).board_settings(BOARD) == BOARD


def test_the_material_gains_replace_the_board_gains_and_nothing_else():
	settings = pla(kp=2.0, ki=3.0, kd=0.1).board_settings(BOARD)
	assert (settings.kp, settings.ki, settings.kd) == (2.0, 3.0, 0.1)
	assert (settings.alpha, settings.ff_offset_duty, settings.ff_gain_duty_per_hz) == (0.3, 20.0, 0.4)


def test_the_material_feedforward_replaces_the_board_feedforward_and_nothing_else():
	settings = pla(ff_offset_duty=60.0, ff_gain_duty_per_hz=0.2).board_settings(BOARD)
	assert (settings.ff_offset_duty, settings.ff_gain_duty_per_hz) == (60.0, 0.2)
	assert (settings.kp, settings.ki, settings.kd, settings.alpha) == (0.5, 1.0, 0.0, 0.3)


def test_material_gains_configure_a_board_that_has_none():
	settings = pla(kp=2.0, ki=3.0, kd=0.0).board_settings(BoardSettings())
	assert settings.has_gains


# --- Which calibration ----------------------------------------------------------------------------

def test_the_calibration_is_found_on_the_default_dispenser():
	store = CalibrationStore()
	store.add_measurement("B", "PLA", 20.0, 3.0, 10.0)
	assert pla().curve(store).key == ("B", "PLA")


def test_another_dispenser_can_be_asked_for():
	store = CalibrationStore()
	store.add_measurement("A", "PLA", 20.0, 3.0, 10.0)
	assert pla().curve(store, dispenser="A").key == ("A", "PLA")


def test_no_dispenser_known_is_a_named_error():
	with pytest.raises(MaterialError, match="PLA has no default dispenser"):
		Material("PLA", Kind.POLYMER).curve(CalibrationStore())


# --- The registry -------------------------------------------------------------------------------------

def test_put_adds_and_then_replaces_by_name():
	registry = MaterialRegistry()
	registry.put(pla())
	registry.put(pla(flow_max_g_s=4.0))
	assert len(registry.materials()) == 1
	assert registry.get("PLA").flow_max_g_s == 4.0


def test_get_trims_the_name_and_an_unknown_name_lists_the_known_ones():
	registry = MaterialRegistry()
	registry.put(pla())
	registry.put(Material("TPU", Kind.POLYMER))
	assert registry.get(" PLA ").name == "PLA"
	with pytest.raises(UnknownMaterialError, match="no material 'PET'; there are: PLA, TPU"):
		registry.get("PET")


def test_an_empty_registry_says_so():
	with pytest.raises(UnknownMaterialError, match="no materials yet"):
		MaterialRegistry().get("PLA")


def test_a_name_differing_only_in_case_is_refused():
	registry = MaterialRegistry()
	registry.put(pla())
	with pytest.raises(MaterialError, match="'pla' differs from 'PLA' only in case"):
		registry.put(pla(name="pla"))


def test_remove():
	registry = MaterialRegistry()
	registry.put(pla())
	registry.remove("PLA")
	assert registry.materials() == []
	with pytest.raises(UnknownMaterialError):
		registry.remove("PLA")


# --- Files -------------------------------------------------------------------------------------------------

def full_registry():
	registry = MaterialRegistry()
	registry.put(pla(kp=2.0, ki=3.0, kd=0.1, ff_offset_duty=60.0, ff_gain_duty_per_hz=0.2, note="dried 4 h"))
	registry.put(Material("Red", Kind.MASTERBATCH, dispenser="A"))
	return registry


def test_save_and_load_round_trip(tmp_path):
	path = tmp_path / "sub" / "materials.json"
	save_materials(full_registry(), path)
	assert load_materials(path).materials() == full_registry().materials()
	assert not list(path.parent.glob("*.tmp"))


def test_the_file_is_versioned_and_lists_every_key(tmp_path):
	path = tmp_path / "materials.json"
	save_materials(full_registry(), path)
	data = json.loads(path.read_text(encoding="utf-8"))
	assert data["schema"] == SCHEMA
	assert set(data["materials"][1]) == {"name", "kind", "dispenser", "flow_min_g_s", "flow_max_g_s", "kp", "ki",
		"kd", "ff_offset_duty", "ff_gain_duty_per_hz", "note"}
	assert data["materials"][1]["kp"] is None


def test_a_missing_file_is_an_empty_registry(tmp_path):
	assert load_materials(tmp_path / "materials.json").materials() == []


def test_missing_optional_keys_are_empty(tmp_path):
	path = tmp_path / "materials.json"
	path.write_text(json.dumps({"schema": 1, "materials": [{"name": "PLA", "kind": "polymer"}]}), encoding="utf-8")
	assert load_materials(path).get("PLA") == Material("PLA", Kind.POLYMER)


ENTRY = '{"name": "PLA", "kind": "polymer"}'


@pytest.mark.parametrize("text, message", [
	("{not json", "not valid JSON"),
	("[]", "object"),
	('{"schema": 7, "materials": []}', "schema 7"),
	('{"schema": 1}', "'materials' must be a list"),
	('{"schema": 1, "materials": [5]}', "material 0: must be an object"),
	('{"schema": 1, "materials": [{"kind": "polymer"}]}', "material 0: missing 'name'"),
	('{"schema": 1, "materials": [{"name": "PLA"}]}', "material 0 .PLA.: missing 'kind'"),
	('{"schema": 1, "materials": [{"name": "PLA", "kind": "dye"}]}', "material 0 .PLA.: kind 'dye'"),
	('{"schema": 1, "materials": [{"name": "PLA", "kind": "polymer", "flow_gs": 1}]}',
	"material 0 .PLA.: unknown key 'flow_gs'"),
	('{"schema": 1, "materials": [{"name": "PLA", "kind": "polymer", "kp": "1", "ki": 0, "kd": 0}]}',
	"material 0 .PLA.: kp = '1': must be a finite number"),
	('{"schema": 1, "materials": [' + ENTRY + ', ' + ENTRY + ']}', "material 1: PLA is already material 0"),
	('{"schema": 1, "materials": [' + ENTRY + ', {"name": "pla", "kind": "polymer"}]}', "material 1.*only in case"),
])
def test_a_broken_file_names_the_problem_and_the_file(tmp_path, text, message):
	path = tmp_path / "materials.json"
	path.write_text(text, encoding="utf-8")
	with pytest.raises(MaterialFileError, match=message) as raised:
		load_materials(path)
	assert str(path) in str(raised.value)

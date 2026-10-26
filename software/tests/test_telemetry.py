import csv
import json
import time

import pytest

from polydriver.devices import BoardStatus, Event, EventKind, Segment, Session
from polydriver.devices.fleet import BoardTiming, FleetCommand
from polydriver.protocol.constants import Command, JobState, Role, StatusFlag
from polydriver.requests import StatusReport
from polydriver.telemetry import COLUMNS, RunLog, RunLogError, start_run_log

from fakes import Boards, make_config, wait_until


class Clock:
	def __init__(self, now_s: float = 100.0):
		self.now_s = now_s

	def __call__(self) -> float:
		return self.now_s


def status(speed_hz: float = 12.0, device_us: int = 1_000_000, *, flags=StatusFlag.REGULATING, job=JobState.RUNNING,
		segment: int | None = 0, restarted: bool = False) -> BoardStatus:
	report = StatusReport(speed_hz, speed_hz - 0.5, 100, flags, device_us % 2**32, 40, 1, 10010, 0, 2, job, segment, 250)
	return BoardStatus(report, device_us, restarted)


def read_rows(path):
	with open(path, newline="", encoding="utf-8") as file:
		return list(csv.DictReader(file))


def read_json(path):
	return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def clock():
	return Clock()


@pytest.fixture
def log(tmp_path, clock):
	log = RunLog.create(tmp_path / "logs", [Role.POLYMER, Role.COLOUR], clock=clock, name="run-test")
	yield log
	log.close()


# --- Files ------------------------------------------------------------------------------

def test_create_makes_the_folder_a_csv_and_a_sidecar(tmp_path, clock):
	log = RunLog.create(tmp_path / "logs" / "deep", [Role.POLYMER], clock=clock, name="run-a")
	log.close()
	assert log.csv_path == tmp_path / "logs" / "deep" / "run-a.csv"
	assert log.sidecar_path == tmp_path / "logs" / "deep" / "run-a.json"
	assert read_rows(log.csv_path) == []
	assert read_json(log.sidecar_path)["csv"] == "run-a.csv"


def test_the_default_name_is_the_start_time(tmp_path, clock):
	log = RunLog.create(tmp_path, [Role.POLYMER], clock=clock)
	log.close()
	assert log.csv_path.name.startswith("run-") and log.csv_path.suffix == ".csv"


def test_an_existing_run_is_never_overwritten(tmp_path, clock):
	first = RunLog.create(tmp_path, [Role.POLYMER], clock=clock, name="same")
	first.close()
	second = RunLog.create(tmp_path, [Role.POLYMER], clock=clock, name="same")
	second.close()
	assert second.csv_path.name == "same-2.csv"
	assert first.csv_path.exists()


def test_a_left_over_sidecar_is_not_overwritten_either(tmp_path, clock):
	(tmp_path / "same.json").write_text("{}", encoding="utf-8")
	log = RunLog.create(tmp_path, [Role.POLYMER], clock=clock, name="same")
	log.close()
	assert log.csv_path.name == "same-2.csv"
	assert (tmp_path / "same.json").read_text(encoding="utf-8") == "{}"


def test_a_folder_that_cannot_be_made_is_named(tmp_path, clock):
	blocker = tmp_path / "file"
	blocker.write_text("x", encoding="utf-8")
	with pytest.raises(RunLogError, match="file"):
		RunLog.create(blocker / "logs", [Role.POLYMER], clock=clock)


def test_the_header_lists_both_boards_side_by_side(log):
	log.close()
	with open(log.csv_path, newline="", encoding="utf-8") as file:
		header = next(csv.reader(file))
	assert header == COLUMNS
	assert header[0] == "t_s"
	assert "polymer_raw_hz" in header and "colour_raw_hz" in header
	assert header.index("polymer_raw_hz") < header.index("colour_host_s")


# --- Rows ---------------------------------------------------------------------------------

def test_one_row_holds_one_poll_of_each_board(log, clock):
	log.record_status(Role.POLYMER, status(12.0, 1_000_000), 100.10)
	log.record_status(Role.COLOUR, status(3.0, 2_000_000), 100.13)
	log.close()
	[row] = read_rows(log.csv_path)
	assert float(row["t_s"]) == pytest.approx(0.10)
	assert float(row["polymer_host_s"]) == pytest.approx(0.10)
	assert float(row["colour_host_s"]) == pytest.approx(0.13)
	assert row["polymer_raw_hz"] == "12.0" and row["colour_raw_hz"] == "3.0"
	assert row["polymer_device_us"] == "1000000" and row["colour_device_us"] == "2000000"
	assert row["polymer_flags"] == "REGULATING"
	assert row["polymer_job_state"] == "RUNNING"
	assert row["polymer_segment"] == "0" and row["polymer_job_elapsed_ms"] == "250"
	assert row["polymer_edges"] == "40" and row["polymer_restarted"] == "0"


def test_every_poll_lands_in_exactly_one_row(log):
	for tick in range(10):
		log.record_status(Role.POLYMER, status(device_us=tick), 100 + tick * 0.1)
		log.record_status(Role.COLOUR, status(device_us=tick), 100.05 + tick * 0.1)
	log.close()
	rows = read_rows(log.csv_path)
	assert len(rows) == 10
	assert [r["polymer_device_us"] for r in rows] == [str(t) for t in range(10)]
	assert [r["colour_device_us"] for r in rows] == [str(t) for t in range(10)]


def test_a_missed_poll_leaves_that_side_empty_instead_of_repeating(log):
	log.record_status(Role.POLYMER, status(device_us=1), 100.0)
	log.record_status(Role.COLOUR, status(device_us=1), 100.05)
	log.record_status(Role.POLYMER, status(device_us=2), 100.1)
	log.record_status(Role.POLYMER, status(device_us=3), 100.2)
	log.record_status(Role.COLOUR, status(device_us=3), 100.25)
	log.close()
	rows = read_rows(log.csv_path)
	assert [(r["polymer_device_us"], r["colour_device_us"]) for r in rows] == [("1", "1"), ("2", ""), ("3", "3")]


def test_one_board_alone_gives_a_row_per_poll(tmp_path, clock):
	log = RunLog.create(tmp_path, [Role.COLOUR], clock=clock, name="solo")
	for tick in range(3):
		log.record_status(Role.COLOUR, status(device_us=tick), 100 + tick)
	log.close()
	rows = read_rows(log.csv_path)
	assert len(rows) == 3
	assert all(r["polymer_raw_hz"] == "" for r in rows)


def test_one_board_alone_writes_each_row_at_once(tmp_path, clock):
	log = RunLog.create(tmp_path, [Role.COLOUR], clock=clock, name="solo")
	log.record_status(Role.COLOUR, status(), 100.0)
	assert len(read_rows(log.csv_path)) == 1
	log.close()


def test_a_board_that_is_not_in_the_run_is_ignored(tmp_path, clock):
	log = RunLog.create(tmp_path, [Role.COLOUR], clock=clock, name="solo")
	log.record_status(Role.POLYMER, status(), 100.0)
	log.close()
	assert read_rows(log.csv_path) == []


def test_close_writes_the_last_half_row(log):
	log.record_status(Role.POLYMER, status(device_us=7), 100.0)
	log.close()
	[row] = read_rows(log.csv_path)
	assert row["polymer_device_us"] == "7" and row["colour_device_us"] == ""


def test_a_restart_is_marked_in_its_row(log):
	log.record_status(Role.POLYMER, status(restarted=True), 100.0)
	log.close()
	assert read_rows(log.csv_path)[0]["polymer_restarted"] == "1"


def test_several_flags_are_joined_and_no_flags_is_empty(log):
	log.record_status(Role.POLYMER, status(flags=StatusFlag.REGULATING | StatusFlag.OPEN_LOOP), 100.0)
	log.record_status(Role.COLOUR, status(flags=StatusFlag(0), segment=None, job=JobState.EMPTY), 100.0)
	log.close()
	[row] = read_rows(log.csv_path)
	assert row["polymer_flags"] == "REGULATING|OPEN_LOOP"
	assert row["colour_flags"] == ""
	assert row["colour_segment"] == "" and row["colour_job_state"] == "EMPTY"


def test_rows_after_close_are_dropped(log):
	log.close()
	log.record_status(Role.POLYMER, status(), 100.0)
	log.record_status(Role.COLOUR, status(), 100.0)
	assert read_rows(log.csv_path) == []


def test_each_row_is_on_disk_before_the_next_poll(log):
	log.record_status(Role.POLYMER, status(), 100.0)
	log.record_status(Role.COLOUR, status(), 100.0)
	assert len(read_rows(log.csv_path)) == 1


# --- Sidecar --------------------------------------------------------------------------------

def test_the_sidecar_records_the_run(tmp_path, clock):
	log = RunLog.create(tmp_path, [Role.POLYMER, Role.COLOUR], clock=clock, name="r", note="first wet run",
						settings={"comms_timeout_s": 2.0, "poll_hz": 10.0})
	clock.now_s = 160.0
	log.close(result="DONE")
	side = read_json(log.sidecar_path)
	assert side["schema"] == 1
	assert side["note"] == "first wet run"
	assert side["roles"] == ["polymer", "colour"]
	assert side["settings"] == {"comms_timeout_s": 2.0, "poll_hz": 10.0}
	assert side["duration_s"] == pytest.approx(60.0)
	assert side["result"] == "DONE"
	assert side["rows"] == 0
	assert side["started"] and side["ended"]
	assert side["weighed_mass_g"] == {"polymer": None, "colour": None}


def test_board_details_and_the_uploaded_job_go_in_the_sidecar(log):
	log.set_board(Role.POLYMER, port="COM6", firmware="3.0.0", protocol=3, settings={"kp": 0.5})
	log.set_segments(Role.POLYMER, [Segment(20.0, 1000), Segment(30.0, 2000, ramp=True)], calibration_hash="abc")
	log.set_job({"name": "PP red 1-5 %"})
	log.close()
	side = read_json(log.sidecar_path)
	board = side["boards"]["polymer"]
	assert board["port"] == "COM6" and board["firmware"] == "3.0.0" and board["protocol"] == 3
	assert board["settings"] == {"kp": 0.5}
	assert board["segments"] == [
		{"value": 20.0, "duration_ms": 1000, "ramp": False, "duty": False},
		{"value": 30.0, "duration_ms": 2000, "ramp": True, "duty": False},
	]
	assert board["calibration_hash"] == "abc"
	assert side["job"] == {"name": "PP red 1-5 %"}


def test_segments_without_board_details_are_kept_too(log):
	log.set_segments(Role.COLOUR, [Segment(100, 3000, duty=True)])
	log.close()
	assert read_json(log.sidecar_path)["boards"]["colour"]["segments"][0]["duty"] is True


def test_an_event_is_on_disk_at_once(log):
	log.record_event(Event(EventKind.LINK_LOST, Role.POLYMER, "COM6 lost", 101.0))
	assert read_json(log.sidecar_path)["events"][0]["kind"] == "link lost"


def test_session_events_go_in_the_sidecar_on_the_run_clock(log):
	log.record_event(Event(EventKind.RESTARTED, Role.COLOUR, "the board restarted", 102.5))
	log.close()
	[event] = read_json(log.sidecar_path)["events"]
	assert event == {"t_s": 2.5, "role": "colour", "kind": "restarted", "message": "the board restarted"}


def test_run_and_stop_timings_go_in_the_sidecar(log):
	run = FleetCommand(Command.JOB_RUN, {
		Role.POLYMER: BoardTiming(Role.POLYMER, 101.0, 101.0, 101.004, 1),
		Role.COLOUR: BoardTiming(Role.COLOUR, 101.001, 101.001, 101.005, 1),
	})
	log.add_fleet_command(run)
	log.close()
	[entry] = read_json(log.sidecar_path)["fleet"]
	assert entry["command"] == "JOB_RUN"
	assert entry["gap_s"] == pytest.approx(0.001)
	assert entry["gap_bounds_s"] == pytest.approx([-0.003, 0.005])
	assert entry["boards"]["polymer"]["first_sent_s"] == pytest.approx(1.0)
	assert entry["boards"]["colour"]["error"] is None


def test_the_weighed_mass_is_added_after_the_run(log):
	log.close()
	log.set_weighed_mass(Role.POLYMER, 81.25)
	assert read_json(log.sidecar_path)["weighed_mass_g"] == {"polymer": 81.25, "colour": None}


def test_a_mass_for_a_board_outside_the_run_is_refused(tmp_path, clock):
	log = RunLog.create(tmp_path, [Role.POLYMER], clock=clock, name="solo")
	log.close()
	with pytest.raises(ValueError, match="colour board was not in this run"):
		log.set_weighed_mass(Role.COLOUR, 5.0)


def test_a_negative_mass_is_refused(log):
	with pytest.raises(ValueError, match="mass"):
		log.set_weighed_mass(Role.POLYMER, -1.0)


def test_the_note_can_be_changed_after_the_run(log):
	log.close()
	log.set_note("screw jammed at 40 s")
	assert read_json(log.sidecar_path)["note"] == "screw jammed at 40 s"


def test_the_sidecar_exists_from_the_start(log):
	side = read_json(log.sidecar_path)
	assert side["ended"] is None and side["result"] is None


def test_close_twice_keeps_the_first_result(log):
	log.close(result="DONE")
	log.close(result="ABORTED")
	assert read_json(log.sidecar_path)["result"] == "DONE"


def test_a_sidecar_write_leaves_no_temporary_file(log, tmp_path):
	log.set_note("x")
	log.close()
	assert sorted(p.name for p in (tmp_path / "logs").iterdir()) == ["run-test.csv", "run-test.json"]


# --- With a session --------------------------------------------------------------------------

def test_start_run_log_follows_a_session(tmp_path):
	boards = Boards({"COM6": Role.POLYMER, "COM5": Role.COLOUR})
	config = make_config(poll_hz=50.0, base_dir=tmp_path)
	with Session(config, link_factory=boards) as session:
		session.connect(Role.POLYMER)
		session.connect(Role.COLOUR)
		log = start_run_log(session, config, note="dry run")
		assert wait_until(lambda: log.rows >= 20)
		log.close()
		rows = log.rows
		time.sleep(0.1)
	csv_rows = read_rows(log.csv_path)
	assert len(csv_rows) == rows
	assert all(r["polymer_raw_hz"] and r["colour_raw_hz"] for r in csv_rows[:-1])
	side = read_json(log.sidecar_path)
	assert log.csv_path.parent == tmp_path / "logs"
	assert side["boards"]["polymer"]["port"] == "COM6"
	assert side["boards"]["polymer"]["settings"]["alpha"] == pytest.approx(0.4)
	assert side["boards"]["colour"]["firmware"] == "3.0.0"
	assert side["settings"]["poll_hz"] == 50.0 and side["settings"]["comms_timeout_s"] == 2.0
	assert side["note"] == "dry run"


def test_start_run_log_needs_a_connected_board(tmp_path):
	boards = Boards({"COM6": Role.POLYMER})
	config = make_config(base_dir=tmp_path)
	with Session(config, link_factory=boards) as session:
		with pytest.raises(RunLogError, match="no board"):
			start_run_log(session, config)


def test_closing_the_log_detaches_it_from_the_session(tmp_path):
	boards = Boards({"COM6": Role.POLYMER})
	config = make_config(poll_hz=50.0, base_dir=tmp_path)
	with Session(config, link_factory=boards) as session:
		session.connect(Role.POLYMER)
		log = start_run_log(session, config)
		assert wait_until(lambda: log.rows >= 2)
		log.close()
		assert log not in session._sinks

import pytest

from polydriver.devices import Board, DeviceClock, JobUploadError, Segment
from polydriver.devices.board import MIN_SEGMENT_MS
from polydriver.protocol.constants import JobState, Mode, Role, StatusFlag
from polydriver.requests import (
	ClearEstop, Estop, GetId, GetVersion, JobClear, JobCommit, JobInfo, JobRun, JobSegment, RequestSettings,
	RequestStatus, SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetPwm, SetSpeed,
	StatusReport, Stop, VersionReport,
)

VERSION = VersionReport(protocol_version=3, firmware_major=3, firmware_minor=0, firmware_patch=0, max_segments=4)


def status(device_time_us: int = 0, flags: StatusFlag = StatusFlag(0)) -> StatusReport:
	return StatusReport(
		raw_speed_hz=10.0, filtered_speed_hz=9.5, duty=100, flags=flags, device_time_us=device_time_us,
		edge_count=5, control_resyncs=0, longest_loop_interval_us=10010, dropped_frames=0, bad_crc_frames=0,
		job_state=JobState.EMPTY, segment_index=None, job_elapsed_ms=0,
	)


class FakeLink:
	def __init__(self):
		self.version = VERSION
		self.sent = []
		self.statuses = []

	def call(self, request, timeout_s=None):
		self.sent.append(request)
		if isinstance(request, RequestStatus):
			return self.statuses.pop(0)
		if isinstance(request, JobCommit):
			return JobInfo(job_state=JobState.READY, segment_count=request.segment_count, total_ms=request.total_ms)
		if isinstance(request, GetId):
			return Role.COLOUR
		if isinstance(request, GetVersion):
			return VERSION
		return None


@pytest.fixture
def link():
	return FakeLink()


@pytest.fixture
def board(link):
	return Board(link)


# --- Typed calls ------------------------------------------------------------------

def test_each_call_sends_its_request(board, link):
	board.set_speed(42.5)
	board.set_pid(1.0, 0.5, 0.0)
	board.set_filter(0.3)
	board.set_feedforward(110.0, 0.8)
	board.set_mode(Mode.OPEN_LOOP)
	board.set_pwm(128)
	board.set_comms_timeout_ms(1500)
	board.stop()
	board.estop()
	board.clear_estop()
	board.set_role(Role.POLYMER)
	assert link.sent == [
		SetSpeed(42.5), SetPid(1.0, 0.5, 0.0), SetFilter(0.3), SetFeedforward(110.0, 0.8),
		SetMode(Mode.OPEN_LOOP), SetPwm(128), SetCommsTimeout(1500), Stop(), Estop(), ClearEstop(),
		SetId(Role.POLYMER),
	]


def test_reads_return_the_decoded_reply(board, link):
	assert board.role() is Role.COLOUR
	assert board.version() == VERSION
	board.settings()
	assert link.sent[-1] == RequestSettings()


def test_comms_timeout_can_be_switched_off(board, link):
	board.set_comms_timeout_ms(0)
	assert link.sent == [SetCommsTimeout(0)]


# --- Status and the device clock --------------------------------------------------------

def test_status_carries_the_unwrapped_device_time(board, link):
	link.statuses = [status(4_294_000_000), status(1_000_000)]
	first = board.status()
	second = board.status()
	assert first.report.raw_speed_hz == 10.0
	assert first.device_time_us == 4_294_000_000
	assert second.device_time_us == 2**32 + 1_000_000
	assert not second.device_restarted


def test_device_clock_counts_forward_across_several_wraps():
	clock = DeviceClock()
	total = 0
	raw = 0
	for _ in range(10):
		raw = (raw + 1_500_000_000) % 2**32
		total += 1_500_000_000
		assert clock.unwrap(raw) == total
	assert clock.restarts == 0


def test_device_clock_sees_a_board_restart():
	clock = DeviceClock()
	clock.unwrap(600_000_000)
	assert clock.unwrap(2_000_000) == 600_000_000 + 2_000_000
	assert clock.restarts == 1
	assert clock.unwrap(3_000_000) == 600_000_000 + 3_000_000


def test_a_restart_is_flagged_on_that_status(board, link):
	link.statuses = [status(600_000_000), status(1_000)]
	board.status()
	assert board.status().device_restarted
	assert board.device_restarts == 1


def test_a_reappearing_unconfigured_flag_also_counts_as_a_restart(board, link):
	link.statuses = [status(1_000_000, StatusFlag(0)), status(900_000_000, StatusFlag.UNCONFIGURED)]
	board.status()
	assert board.status().device_restarted


def test_unconfigured_at_the_first_status_is_not_a_restart(board, link):
	link.statuses = [status(1_000, StatusFlag.UNCONFIGURED)]
	assert not board.status().device_restarted


# --- Jobs -----------------------------------------------------------------------

def test_upload_sends_clear_segments_and_commit(board, link):
	info = board.upload_job([Segment(20.0, 1000), Segment(40.0, 500, ramp=True)])
	assert link.sent == [
		JobClear(),
		JobSegment(0, 20.0, 1000),
		JobSegment(1, 40.0, 500, ramp=True),
		JobCommit(2, 1500),
	]
	assert info.job_state is JobState.READY


def test_a_duty_job_sets_the_duty_flag(board, link):
	board.upload_job([Segment(64, 5000, duty=True)])
	assert link.sent[1] == JobSegment(0, 64, 5000, duty=True)


def test_run_and_clear(board, link):
	board.run_job()
	board.clear_job()
	assert link.sent == [JobRun(), JobClear()]


@pytest.mark.parametrize("segments, message", [
	([], "empty"),
	([Segment(10.0, MIN_SEGMENT_MS - 1)], "shorter than"),
	([Segment(10.0, 100)] * 5, "4 segments"),
], ids=["empty", "too short", "too many"])
def test_jobs_the_board_cannot_run_are_refused_before_sending(board, link, segments, message):
	with pytest.raises(JobUploadError, match=message):
		board.upload_job(segments)
	assert link.sent == []


def test_the_shortest_segment_is_one_control_tick():
	assert MIN_SEGMENT_MS == 10

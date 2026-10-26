import struct
import threading
import time

import pytest

from polydriver.config import BoardSettings
from polydriver.devices import Board, DeviceClock, JobUploadError, Segment, SettingsMismatchError
from polydriver.devices.board import MIN_SEGMENT_MS
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Role, StatusFlag
from polydriver.requests import (
	ClearEstop, Estop, GetId, GetVersion, JobClear, JobCommit, JobInfo, JobRun, JobSegment, RequestSettings,
	RequestStatus, SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetPwm, SetSpeed,
	SettingsReport, StatusReport, Stop, VersionReport, RequestNackError,
)

VERSION = VersionReport(protocol_version=3, firmware_major=3, firmware_minor=0, firmware_patch=0, max_segments=4)


def status(device_time_us: int = 0, flags: StatusFlag = StatusFlag(0)) -> StatusReport:
	return StatusReport(
		raw_speed_hz=10.0, filtered_speed_hz=9.5, duty=100, flags=flags, device_time_us=device_time_us,
		edge_count=5, control_resyncs=0, longest_loop_interval_us=10010, dropped_frames=0, bad_crc_frames=0,
		job_state=JobState.EMPTY, segment_index=None, job_elapsed_ms=0,
	)


def f32(value: float) -> float:
	return struct.unpack("<f", struct.pack("<f", value))[0]


class FakeLink:
	"""Keeps the settings like the board does (as f32) and reports them back in SETTINGS."""

	def __init__(self):
		self.version = VERSION
		self.sent = []
		self.statuses = []
		self.board = {"kp": 0.0, "ki": 0.0, "kd": 0.0, "alpha": 1.0, "offset": 0.0, "gain": 0.0, "timeout_ms": 0}
		self.ignore_filter = False

	def call(self, request, timeout_s=None):
		self.sent.append(request)
		match request:
			case SetPid(kp=kp, ki=ki, kd=kd):
				self.board.update(kp=f32(kp), ki=f32(ki), kd=f32(kd))
			case SetFilter(alpha=alpha) if not self.ignore_filter:
				self.board["alpha"] = f32(alpha)
			case SetFeedforward(offset_duty=offset, gain_duty_per_hz=gain):
				self.board.update(offset=f32(offset), gain=f32(gain))
			case SetCommsTimeout(timeout_ms=timeout_ms):
				self.board["timeout_ms"] = timeout_ms
			case RequestSettings():
				b = self.board
				return SettingsReport(b["kp"], b["ki"], b["kd"], 0.0, b["timeout_ms"], Mode.CLOSED_LOOP, 0, b["alpha"],
									b["offset"], b["gain"])
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


class HostClock:
	def __init__(self):
		self.now_s = 0.0

	def __call__(self) -> float:
		return self.now_s


@pytest.fixture
def host():
	return HostClock()


@pytest.fixture
def board(link, host):
	return Board(link, clock=host)


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

def test_status_carries_the_unwrapped_device_time(board, link, host):
	link.statuses = [status(4_294_000_000), status(1_000_000)]
	first = board.status()
	host.now_s += (2**32 - 4_294_000_000 + 1_000_000) / 1e6
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


def test_a_reappearing_unconfigured_flag_also_counts_as_a_restart(board, link, host):
	link.statuses = [status(1_000_000, StatusFlag(0)), status(900_000_000, StatusFlag.UNCONFIGURED)]
	board.status()
	host.now_s += 899.0
	assert board.status().device_restarted


def test_status_from_two_threads_is_unwrapped_in_order():
	class SlowFirstReply(FakeLink):
		def __init__(self):
			super().__init__()
			self.raw_us = 1_000_000
			self.slow_next = False

		def call(self, request, timeout_s=None):
			self.raw_us += 100_000
			raw_us = self.raw_us
			if self.slow_next:
				self.slow_next = False
				time.sleep(0.05)
			return status(raw_us)

	link = SlowFirstReply()
	board = Board(link, clock=time.perf_counter)
	board.status()
	link.slow_next = True
	results = []
	threads = [threading.Thread(target=lambda: results.append(board.status())) for _ in range(2)]
	for thread in threads:
		thread.start()
		time.sleep(0.01)
	for thread in threads:
		thread.join()
	assert not any(r.device_restarted for r in results)


def test_a_restart_after_a_long_uptime_is_seen(board, link, host):
	link.statuses = [status(3_000_000_000), status(3_000_000)]
	board.status()
	host.now_s += 3.0
	assert board.status().device_restarted


def test_a_wrap_while_polling_is_not_a_restart(board, link, host):
	link.statuses = [status(2**32 - 50_000), status(50_000)]
	board.status()
	host.now_s += 0.1
	second = board.status()
	assert not second.device_restarted
	assert second.device_time_us == 2**32 + 50_000


def test_a_board_clock_that_fell_behind_the_host_restarted(board, link, host):
	link.statuses = [status(1_000_000), status(3_000_000)]
	board.status()
	host.now_s += 10.0
	assert board.status().device_restarted


def test_crystal_drift_is_not_a_restart(board, link, host):
	link.statuses = [status(1_000_000), status(1_000_000 + 3_600_000_000 + 677_000)]
	board.status()
	host.now_s += 3600.0
	assert not board.status().device_restarted


def test_a_host_away_for_several_wraps_counts_them(board, link, host):
	link.statuses = [status(1_000_000), status(2_000_000)]
	board.status()
	host.now_s += (3 * 2**32 + 1_000_000) / 1e6
	second = board.status()
	assert not second.device_restarted
	assert second.device_time_us == 3 * 2**32 + 2_000_000


def test_unconfigured_at_the_first_status_is_not_a_restart(board, link):
	link.statuses = [status(1_000, StatusFlag.UNCONFIGURED)]
	assert not board.status().device_restarted


# --- Reporting changes -------------------------------------------------------------

@pytest.fixture
def heard(link, host):
	changes = []
	return Board(link, clock=host, on_change=changes.append), changes


def test_each_change_is_reported_after_it_was_sent(heard, link):
	board, changes = heard
	board.set_speed(20.0)
	board.set_pid(0.5, 1.0, 0.0)
	board.set_mode(Mode.OPEN_LOOP)
	board.set_pwm(64)
	board.set_filter(0.4)
	board.set_feedforward(128.0, 1.5)
	board.set_comms_timeout_ms(0)
	board.set_role(Role.POLYMER)
	board.stop()
	board.estop()
	board.clear_estop()
	board.run_job()
	board.clear_job()
	assert changes == [
		"speed 20 Hz", "pid 0.5 1 0", "mode OPEN_LOOP", "duty 64", "filter alpha 0.4", "feedforward 128 duty + 1.5 duty/Hz",
		"comms timeout off", "role stored: polymer", "STOP", "ESTOP", "ESTOP cleared", "job started", "job cleared",
	]


def test_an_upload_is_one_change(heard):
	board, changes = heard
	board.upload_job([Segment(20.0, 1000), Segment(40.0, 500, ramp=True)])
	assert changes == ["job uploaded: 2 segments, 1500 ms"]


def test_reads_and_apply_settings_report_nothing(heard):
	board, changes = heard
	board.settings()
	board.apply_settings(BoardSettings(kp=0.8, ki=2.0, kd=0.1), 1500)
	assert changes == []


def test_a_refused_command_is_not_reported(heard, link):
	board, changes = heard

	def refuse(request, timeout_s=None):
		raise RequestNackError(Command.SET_SPEED, NackReason.NOT_CONFIGURED)

	link.call = refuse
	with pytest.raises(RequestNackError):
		board.set_speed(5.0)
	assert changes == []


def test_a_failing_listener_does_not_fail_the_command(link, host):
	def broken(change):
		raise RuntimeError("listener bug")

	Board(link, clock=host, on_change=broken).stop()
	assert link.sent == [Stop()]


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
	([Segment(10.0, 100), Segment(50, 100, duty=True)], "mixes speed and duty"),
	([Segment(-1.0, 100)], "segment 0 has the value -1"),
	([Segment(10.0, 100), Segment(float("nan"), 100)], "segment 1 has the value nan"),
	([Segment(256, 100, duty=True)], "duty 256"),
	([Segment(float("inf"), 100)], "the value inf"),
], ids=["empty", "too short", "too many", "mixed", "negative", "nan", "duty above 255", "infinite"])
def test_jobs_the_board_cannot_run_are_refused_before_sending(board, link, segments, message):
	with pytest.raises(JobUploadError, match=message):
		board.upload_job(segments)
	assert link.sent == []


def test_the_shortest_segment_is_one_control_tick():
	assert MIN_SEGMENT_MS == 10


# --- Settings from polydriver.toml ---------------------------------------------------

def test_apply_sends_timeout_pid_filter_feedforward_then_reads_back(board, link):
	settings = BoardSettings(kp=0.8, ki=2.0, kd=0.1, alpha=0.4, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.6)
	report = board.apply_settings(settings, comms_timeout_ms=2000)
	assert link.sent == [
		SetCommsTimeout(2000), SetPid(0.8, 2.0, 0.1), SetFilter(0.4), SetFeedforward(128.0, 1.6), RequestSettings(),
	]
	assert report.alpha == pytest.approx(0.4)
	assert report.comms_timeout_ms == 2000


def test_apply_without_gains_leaves_the_pid_alone(board, link):
	board.apply_settings(BoardSettings(alpha=0.5), comms_timeout_ms=0)
	assert not any(isinstance(r, SetPid) for r in link.sent)
	assert link.sent[0] == SetCommsTimeout(0)


def test_a_value_the_board_did_not_take_is_reported(board, link):
	link.ignore_filter = True
	with pytest.raises(SettingsMismatchError, match="alpha"):
		board.apply_settings(BoardSettings(alpha=0.3), comms_timeout_ms=2000)


def test_float32_rounding_is_not_a_mismatch(board, link):
	board.apply_settings(BoardSettings(kp=0.1, ki=0.3, kd=0.7, alpha=0.1, ff_offset_duty=0.1, ff_gain_duty_per_hz=0.3), 1500)

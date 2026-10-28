"""C8.3: the simulated board speaks protocol v3 as the firmware does, turns a model screw, runs jobs
and has fault switches; a Session runs a gradient job on two of them."""

import itertools
import math
import re
import struct
import threading
import time

import pytest
import serial

from polydriver.config import BoardSettings, Config, LoggingSettings
from polydriver.devices import Session
from polydriver.devices.link import Link
from polydriver.protocol.constants import (
	BOARD_ID, ETX, REQUEST_FORMAT, STX, Command, JobState, Mode, NackReason, Reply, Role, StatusFlag,
)
from polydriver.protocol.link_layer import ReplyFrame, ReplyParser, build_frame, crc8
from polydriver.protocol.serial_worker import LinkLostError, LinkOpenError
from polydriver.requests import (
	ClearEstop, Estop, GetId, GetVersion, JobClear, JobCommit, JobInfo, JobRun, JobSegment, RequestNackError,
	RequestSettings, RequestStatus, SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetPwm,
	SetSpeed, Stop, VersionReport,
)
from polydriver.run import Runner, RunState
from polydriver.sim import MotorModel, SimBench, SimBoard
from test_firmware_sync import CORE, cpp_constant, source
from test_job_program import calibrations, materials, short

GAINS = BoardSettings(kp=0.5, ki=2.0, kd=0.0, alpha=0.5, ff_offset_duty=128.0, ff_gain_duty_per_hz=1.67)


class ManualClock:
	def __init__(self):
		self.now_s = 100.0

	def __call__(self) -> float:
		return self.now_s

	def advance(self, seconds: float) -> None:
		self.now_s += seconds


class Host:
	"""Talks to one port the way the SerialWorker does, without its threads."""

	def __init__(self, port):
		self.port = port
		self.parser = ReplyParser()
		self.next_sequence = 0

	def send_raw(self, frame: bytes) -> list[ReplyFrame]:
		self.port.write(frame)
		data = b""
		while self.port.in_waiting:
			data += self.port.read(self.port.in_waiting)
		return self.parser.feed(data, 0.0)

	def raw(self, command: int, *values, sequence: int | None = None) -> NackReason | None:
		"""Sends values packed as the command's request format, unchecked; None for an ACK."""
		(reply,) = self.send_raw(build_frame(command, self._sequence(sequence), struct.pack(REQUEST_FORMAT[command], *values)))
		return None if reply.command == Reply.ACK else NackReason(reply.payload[0])

	def call(self, request, sequence: int | None = None):
		sequence = self._sequence(sequence)
		frames = self.send_raw(build_frame(request.COMMAND, sequence, request.encode()))
		assert len(frames) == 1 and frames[0].sequence == sequence
		return request.handle(frames[0])

	def nack(self, request) -> NackReason:
		with pytest.raises(RequestNackError) as error:
			self.call(request)
		return error.value.reason

	def status(self):
		return self.call(RequestStatus())

	def settings(self):
		return self.call(RequestSettings())

	def _sequence(self, sequence: int | None) -> int:
		if sequence is not None:
			return sequence
		sequence, self.next_sequence = self.next_sequence, (self.next_sequence + 1) % 256
		return sequence


@pytest.fixture
def clock():
	return ManualClock()


@pytest.fixture
def board(clock):
	return SimBoard(Role.POLYMER, clock=clock)


@pytest.fixture
def host(board):
	return Host(board.open())


def configure(host):
	host.call(SetPid(GAINS.kp, GAINS.ki, GAINS.kd))
	host.call(SetFilter(GAINS.alpha))
	host.call(SetFeedforward(GAINS.ff_offset_duty, GAINS.ff_gain_duty_per_hz))


def upload(host, *segments, duty=False):
	"""segments: (value, duration_ms, ramp)."""
	host.call(JobClear())
	for index, (value, duration_ms, ramp) in enumerate(segments):
		host.call(JobSegment(index, value, duration_ms, ramp=ramp, duty=duty))
	return host.call(JobCommit(len(segments), sum(s[1] for s in segments)))


# --- Protocol --------------------------------------------------------------------------------

def test_it_answers_get_version_as_firmware_3_0_0_with_32_segments(host):
	assert host.call(GetVersion()) == VersionReport(3, 3, 0, 0, 32)


def test_a_link_opens_on_a_bench_port():
	bench = SimBench({"SIM1": SimBoard(Role.COLOUR)})
	with Link.open("SIM1", opener=bench.opener) as link:
		assert link.version.protocol_version == 3
		assert link.call(GetId()) is Role.COLOUR


def test_a_port_with_no_board_cannot_be_opened():
	bench = SimBench({"SIM1": SimBoard()})
	with pytest.raises(LinkOpenError, match="SIM9"):
		Link.open("SIM9", opener=bench.opener)


def test_a_port_already_open_is_in_use(board):
	board.open()
	with pytest.raises(serial.SerialException, match="in use"):
		board.open()


def test_a_closed_port_can_be_opened_again(board):
	board.open().close()
	assert Host(board.open()).call(GetVersion()).protocol_version == 3


def test_a_retry_gets_the_stored_reply_without_running_the_command_again(host):
	host.call(JobClear())
	host.call(JobSegment(0, 10.0, 100), sequence=7)
	host.call(JobSegment(0, 10.0, 100), sequence=7)
	assert host.call(JobCommit(1, 100)).segment_count == 1


def test_the_same_command_with_a_new_sequence_runs_again(host):
	host.call(JobClear())
	host.call(JobSegment(0, 10.0, 100), sequence=7)
	assert host.raw(Command.JOB_SEGMENT, 0, 0, 10.0, 100, sequence=8) is NackReason.BAD_INDEX


def test_a_request_with_a_bad_crc_is_nacked_counted_and_not_run(host):
	frame = bytearray(build_frame(Command.SET_ID, 5, bytes([Role.COLOUR])))
	frame[-2] ^= 0xFF
	assert host.send_raw(bytes(frame)) == [ReplyFrame(Reply.NACK, 5, bytes([NackReason.BAD_CRC]))]
	assert host.call(GetId()) is Role.POLYMER
	assert host.status().bad_crc_frames == 1


def test_a_bad_crc_nack_is_not_kept_as_the_reply_to_resend(host):
	host.call(SetId(Role.COLOUR), sequence=5)
	frame = bytearray(build_frame(Command.SET_ID, 5, bytes([Role.COLOUR])))
	frame[-2] ^= 0xFF
	host.send_raw(bytes(frame))
	(reply,) = host.send_raw(build_frame(Command.SET_ID, 5, bytes([Role.COLOUR])))
	assert reply.command == Reply.ACK


def test_an_unknown_command_and_a_wrong_length_are_refused(host):
	(unknown,) = host.send_raw(build_frame(0x99, 1))
	(short_payload,) = host.send_raw(build_frame(Command.SET_SPEED, 2, b"\x00\x00"))
	assert unknown == ReplyFrame(Reply.NACK, 1, bytes([NackReason.UNKNOWN_COMMAND]))
	assert short_payload == ReplyFrame(Reply.NACK, 2, bytes([NackReason.BAD_LENGTH]))


def test_a_frame_longer_than_a_request_is_dropped_and_counted(host):
	body = bytes([BOARD_ID, Command.SET_SPEED, 17, 3]) + bytes(17)
	assert host.send_raw(bytes([STX]) + body + bytes([crc8(body), ETX])) == []
	assert host.status().dropped_frames == 1


def test_a_frame_for_another_board_id_is_ignored(host):
	body = bytes([0x05, Command.GET_VERSION, 0, 3])
	assert host.send_raw(bytes([STX]) + body + bytes([crc8(body), ETX])) == []
	assert host.status().dropped_frames == 0


def test_a_frame_cut_off_for_longer_than_the_inter_byte_timeout_is_dropped(host, clock):
	frame = build_frame(Command.GET_ID, 4)
	assert host.send_raw(frame[:3]) == []
	clock.advance(0.011)
	assert [reply.sequence for reply in host.send_raw(frame)] == [4]
	assert host.status().dropped_frames == 1


def test_a_frame_split_within_the_inter_byte_timeout_is_answered(host, clock):
	frame = build_frame(Command.GET_ID, 4)
	host.send_raw(frame[:3])
	clock.advance(0.009)
	assert [reply.sequence for reply in host.send_raw(frame[3:])] == [4]


def test_a_frame_that_does_not_end_in_etx_is_dropped(host):
	frame = bytearray(build_frame(Command.GET_ID, 4))
	frame[-1] = 0x55
	assert host.send_raw(bytes(frame)) == []
	assert host.status().dropped_frames == 1


def test_a_stx_in_place_of_the_etx_starts_the_next_frame(host):
	frame = build_frame(Command.GET_ID, 4)
	(reply,) = host.send_raw(frame[:-1] + frame)
	assert reply.sequence == 4
	assert host.status().dropped_frames == 1


# --- Controller -------------------------------------------------------------------------------

def test_a_fresh_board_reports_the_firmware_defaults(host):
	s = host.settings()
	assert (s.kp, s.ki, s.kd, s.target_speed_hz, s.comms_timeout_ms, s.mode, s.duty_setpoint, s.alpha,
			s.feedforward_offset_duty, s.feedforward_gain_duty_per_hz) == (0, 0, 0, 0, 0, Mode.CLOSED_LOOP, 0, 1.0, 0, 0)
	assert host.status().flags == StatusFlag.UNCONFIGURED


def test_a_speed_needs_the_gains_first_but_0_hz_does_not(host):
	assert host.nack(SetSpeed(10.0)) is NackReason.NOT_CONFIGURED
	host.call(SetSpeed(0.0))
	host.call(SetPid(0.5, 2.0, 0.0))
	host.call(SetSpeed(10.0))
	assert host.status().flags == StatusFlag.REGULATING


def test_the_settings_report_what_was_set(host):
	configure(host)
	host.call(SetSpeed(12.5))
	host.call(SetCommsTimeout(1500))
	s = host.settings()
	assert (s.kp, s.ki, s.kd, s.target_speed_hz, s.comms_timeout_ms, s.alpha) == (0.5, 2.0, 0.0, 12.5, 1500, 0.5)
	assert (s.feedforward_offset_duty, s.feedforward_gain_duty_per_hz) == (128.0, pytest.approx(1.67, abs=1e-6))


@pytest.mark.parametrize("command, values", [
	(Command.SET_SPEED, (-1.0,)),
	(Command.SET_SPEED, (math.nan,)),
	(Command.SET_SPEED, (math.inf,)),
	(Command.SET_PID, (-0.1, 0.0, 0.0)),
	(Command.SET_PID, (0.0, math.inf, 0.0)),
	(Command.SET_PID, (0.0, 0.0, math.nan)),
	(Command.SET_FILTER, (0.0,)),
	(Command.SET_FILTER, (1.01,)),
	(Command.SET_FILTER, (math.nan,)),
	(Command.SET_FEEDFORWARD, (-1.0, 1.0)),
	(Command.SET_FEEDFORWARD, (255.5, 1.0)),
	(Command.SET_FEEDFORWARD, (0.0, -1.0)),
	(Command.SET_FEEDFORWARD, (0.0, math.inf)),
	(Command.SET_MODE, (2,)),
	(Command.SET_ID, (3,)),
])
def test_values_the_firmware_refuses_are_bad_values(host, command, values):
	host.call(SetPid(1.0, 1.0, 1.0))
	assert host.raw(command, *values) is NackReason.BAD_VALUE


@pytest.mark.parametrize("command, values", [
	(Command.SET_SPEED, (0.0,)),
	(Command.SET_PID, (0.0, 0.0, 0.0)),
	(Command.SET_FILTER, (1.0,)),
	(Command.SET_FEEDFORWARD, (255.0, 0.0)),
	(Command.SET_ID, (2,)),
])
def test_the_edges_of_what_the_firmware_takes_are_taken(host, command, values):
	assert host.raw(command, *values) is None


def test_a_refused_value_changes_nothing(host):
	configure(host)
	host.raw(Command.SET_PID, -1.0, 9.0, 9.0)
	host.raw(Command.SET_FILTER, 2.0)
	host.raw(Command.SET_FEEDFORWARD, 300.0, 9.0)
	s = host.settings()
	assert (s.kp, s.ki, s.alpha, s.feedforward_offset_duty) == (0.5, 2.0, 0.5, 128.0)


def test_the_mode_decides_which_setpoint_is_taken(host):
	configure(host)
	assert host.nack(SetPwm(100)) is NackReason.WRONG_MODE
	host.call(SetSpeed(10.0))
	host.call(SetMode(Mode.OPEN_LOOP))
	assert host.settings().target_speed_hz == 0.0
	host.call(SetPwm(100))
	assert host.nack(SetSpeed(5.0)) is NackReason.WRONG_MODE
	assert host.status().flags == StatusFlag.OPEN_LOOP
	host.call(SetMode(Mode.CLOSED_LOOP))
	assert host.settings().duty_setpoint == 0


def test_estop_latches_until_cleared_and_stops_the_setpoints(host):
	configure(host)
	host.call(SetSpeed(10.0))
	host.call(Estop())
	assert host.settings().target_speed_hz == 0.0
	assert host.status().flags == StatusFlag.ESTOP
	assert host.nack(SetSpeed(10.0)) is NackReason.WRONG_STATE
	assert host.nack(SetMode(Mode.OPEN_LOOP)) is NackReason.WRONG_STATE
	host.call(SetPid(1.0, 0.0, 0.0))
	host.call(ClearEstop())
	host.call(SetSpeed(10.0))


def test_estop_refuses_a_duty_too(host):
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(Estop())
	assert host.nack(SetPwm(10)) is NackReason.WRONG_STATE


def test_stop_zeroes_both_setpoints(host):
	configure(host)
	host.call(SetSpeed(10.0))
	host.call(Stop())
	assert host.settings().target_speed_hz == 0.0
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(200))
	host.call(Stop())
	assert host.settings().duty_setpoint == 0


# --- Motor and encoder ------------------------------------------------------------------------

def test_open_loop_turns_the_screw_with_the_models_lag(host, board, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(200))
	steady_hz = (200 - 128) * 0.6
	clock.advance(0.31)
	# The duty reaches the motor at the first control tick, 10 ms after the SET_PWM.
	assert board.state().motor_hz == pytest.approx(steady_hz * (1 - math.exp(-1)), rel=1e-9)
	# The raw speed is the last edge period's, behind the screw: edges 3 and 4 came at 0.23 and 0.27 s.
	assert steady_hz * (1 - math.exp(-0.2 / 0.3)) < host.status().raw_speed_hz < steady_hz * (1 - math.exp(-1))
	clock.advance(3.0)
	s = host.status()
	assert s.raw_speed_hz == pytest.approx(steady_hz, rel=0.01)
	assert s.duty == 200
	expected_edges = steady_hz * (3.3 - 0.3 * (1 - math.exp(-3.3 / 0.3)))
	assert s.edge_count == pytest.approx(expected_edges, abs=2)


def test_the_filtered_speed_is_the_ema_of_the_raw_one(host, clock):
	host.call(SetFilter(0.02))
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(200))
	clock.advance(0.5)
	s = host.status()
	assert 0 < s.filtered_speed_hz < s.raw_speed_hz * 0.9
	clock.advance(5.0)
	s = host.status()
	assert s.filtered_speed_hz == pytest.approx(s.raw_speed_hz, rel=0.01)


def test_the_raw_speed_is_measured_in_4_us_ticks(host, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(255))
	clock.advance(5.0)
	raw = host.status().raw_speed_hz
	period_ticks = 250_000 / raw
	assert period_ticks == pytest.approx(round(period_ticks), abs=1e-3)


def test_below_the_dead_band_the_screw_stands(host, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(128))
	clock.advance(2.0)
	s = host.status()
	assert (s.edge_count, s.raw_speed_hz) == (0, 0.0)
	assert StatusFlag.ENCODER_STALLED in s.flags


def test_the_encoder_reads_stalled_once_no_edge_came_for_two_timer_overflows(host, clock):
	clock.advance(0.52)
	assert StatusFlag.ENCODER_STALLED not in host.status().flags
	clock.advance(0.02)
	assert StatusFlag.ENCODER_STALLED in host.status().flags


def test_too_slow_to_measure_the_edges_count_but_the_speed_reads_0(host, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	# The firmware looks for a stall at each timer overflow (262 ms), so a period between 524 and
	# 786 ms may still be measured; 0.6 Hz is too slow either way.
	host.call(SetPwm(129))
	clock.advance(8.0)
	s = host.status()
	assert s.edge_count == pytest.approx(0.6 * 8.0, abs=1)
	assert s.raw_speed_hz == 0.0


def test_the_first_edge_after_a_stall_gives_no_speed_and_the_second_does(host, clock):
	clock.advance(0.6)
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(200))
	for _ in range(60):
		clock.advance(0.005)
		s = host.status()
		if s.edge_count == 1:
			assert s.raw_speed_hz == 0.0
		if s.edge_count >= 2:
			assert s.raw_speed_hz > 0.0
			return
	pytest.fail("no second edge in 300 ms")


def test_closed_loop_holds_the_target_speed(host, clock):
	configure(host)
	host.call(SetSpeed(20.0))
	clock.advance(5.0)
	s = host.status()
	assert s.filtered_speed_hz == pytest.approx(20.0, rel=0.01)
	assert s.flags == StatusFlag.REGULATING
	assert s.duty == pytest.approx(128 + 20 / 0.6, abs=3)


def test_the_motor_model_is_validated():
	for bad in (dict(lag_s=0.0), dict(hz_per_duty=-1.0), dict(dead_duty=256), dict(lag_s=math.nan)):
		with pytest.raises(ValueError):
			MotorModel(**bad)


def test_another_motor_model_turns_the_screw_its_way(clock):
	host = Host(SimBoard(motor=MotorModel(dead_duty=0, hz_per_duty=0.1, lag_s=0.01), clock=clock).open())
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(100))
	clock.advance(2.0)
	assert host.status().raw_speed_hz == pytest.approx(10.0, rel=0.01)


# --- Jobs --------------------------------------------------------------------------------------

def test_a_committed_job_reports_its_count_and_total(host):
	assert upload(host, (10.0, 1000, False), (20.0, 500, True)) == JobInfo(JobState.READY, 2, 1500)
	s = host.status()
	assert (s.job_state, s.segment_index, s.job_elapsed_ms) == (JobState.READY, None, 0)


def test_segments_must_come_in_order(host):
	host.call(JobClear())
	assert host.raw(Command.JOB_SEGMENT, 1, 0, 10.0, 100) is NackReason.BAD_INDEX
	host.call(JobSegment(0, 10.0, 100))
	assert host.status().job_state is JobState.LOADING


def test_the_33rd_segment_does_not_fit(host):
	host.call(JobClear())
	for index in range(32):
		host.call(JobSegment(index, 10.0, 100))
	assert host.raw(Command.JOB_SEGMENT, 32, 0, 10.0, 100) is NackReason.JOB_FULL


@pytest.mark.parametrize("flags, value, duration_ms", [
	(0x04, 10.0, 100),
	(0x00, -1.0, 100),
	(0x00, math.nan, 100),
	(0x00, 10.0, 0),
	(0x02, 255.5, 100),
])
def test_a_segment_the_firmware_refuses_is_a_bad_value(host, flags, value, duration_ms):
	host.call(JobClear())
	assert host.raw(Command.JOB_SEGMENT, 0, flags, value, duration_ms) is NackReason.BAD_VALUE


def test_the_total_may_not_pass_32_bits(host):
	host.call(JobClear())
	host.call(JobSegment(0, 1.0, 2**32 - 100))
	assert host.raw(Command.JOB_SEGMENT, 1, 0, 1.0, 100) is NackReason.BAD_VALUE
	host.call(JobSegment(1, 1.0, 99))


def test_a_commit_must_match_what_was_loaded(host):
	host.call(JobClear())
	assert host.raw(Command.JOB_COMMIT, 0, 0) is NackReason.WRONG_STATE
	host.call(JobSegment(0, 10.0, 100))
	assert host.raw(Command.JOB_COMMIT, 2, 100) is NackReason.BAD_JOB
	assert host.raw(Command.JOB_COMMIT, 1, 101) is NackReason.BAD_JOB
	host.call(JobCommit(1, 100))
	assert host.raw(Command.JOB_SEGMENT, 1, 0, 10.0, 100) is NackReason.WRONG_STATE
	assert host.raw(Command.JOB_COMMIT, 1, 100) is NackReason.WRONG_STATE


def test_speed_and_duty_segments_do_not_mix(host):
	host.call(JobClear())
	host.call(JobSegment(0, 10.0, 100))
	host.call(JobSegment(1, 100.0, 100, duty=True))
	assert host.raw(Command.JOB_COMMIT, 2, 200) is NackReason.BAD_JOB


def test_a_job_runs_its_segments_and_ends_done(host, clock):
	configure(host)
	upload(host, (10.0, 1000, False), (20.0, 1000, True))
	host.call(JobRun())
	clock.advance(0.5)
	s = host.status()
	assert (s.job_state, s.segment_index) == (JobState.RUNNING, 0)
	assert s.job_elapsed_ms == pytest.approx(500, abs=10)
	assert host.settings().target_speed_hz == 10.0
	clock.advance(1.0)
	assert host.status().segment_index == 1
	assert host.settings().target_speed_hz == pytest.approx(15.0, abs=0.1)
	clock.advance(1.0)
	s = host.status()
	assert (s.job_state, s.segment_index, s.job_elapsed_ms) == (JobState.DONE, None, 2000)
	assert host.settings().target_speed_hz == 0.0


def test_a_job_keeps_its_time_to_the_microsecond(host, clock):
	configure(host)
	upload(host, (10.0, 10_000, False))
	clock.advance(0.0037)
	host.call(JobRun())
	for _ in range(7):
		clock.advance(1.0)
		host.status()
	assert host.status().job_elapsed_ms == pytest.approx(7000, abs=10)


def test_a_duty_job_drives_the_motor_open_loop(host, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	upload(host, (200.0, 1000, False), (100.0, 1000, True), duty=True)
	host.call(JobRun())
	clock.advance(0.5)
	assert host.status().duty == 200
	clock.advance(1.0)
	assert host.status().duty == 150


@pytest.mark.parametrize("prepare, reason", [
	(lambda host: None, NackReason.WRONG_STATE),
	(lambda host: (configure(host), upload(host, (10.0, 100, False)), host.call(SetMode(Mode.OPEN_LOOP))),
	 NackReason.WRONG_MODE),
	(lambda host: upload(host, (10.0, 100, False)), NackReason.NOT_CONFIGURED),
	(lambda host: upload(host, (100.0, 100, False), duty=True), NackReason.WRONG_MODE),
	(lambda host: (configure(host), upload(host, (10.0, 100, False)), host.call(Estop())), NackReason.WRONG_STATE),
	(lambda host: (configure(host), host.call(JobClear()), host.call(JobSegment(0, 10.0, 100))), NackReason.WRONG_STATE),
])
def test_job_run_checks_as_the_firmware_does(host, prepare, reason):
	prepare(host)
	assert host.nack(JobRun()) is reason


def test_an_unconfigured_board_runs_a_duty_job(host):
	host.call(SetMode(Mode.OPEN_LOOP))
	upload(host, (100.0, 100, False), duty=True)
	host.call(JobRun())


@pytest.mark.parametrize("request_", [
	SetSpeed(5.0), SetPwm(10), SetMode(Mode.OPEN_LOOP), SetId(Role.COLOUR), JobClear(),
	JobSegment(0, 1.0, 100), JobCommit(1, 100), JobRun(),
])
def test_while_a_job_runs_its_setpoints_and_program_are_refused(host, request_):
	configure(host)
	upload(host, (10.0, 5000, False))
	host.call(JobRun())
	assert host.nack(request_) is NackReason.WRONG_STATE
	assert host.status().job_state is JobState.RUNNING


def test_while_a_job_runs_the_gains_may_change(host):
	configure(host)
	upload(host, (10.0, 5000, False))
	host.call(JobRun())
	host.call(SetPid(1.0, 1.0, 0.0))
	host.call(SetFilter(0.3))
	host.call(SetFeedforward(100.0, 1.0))
	host.call(SetCommsTimeout(1000))
	assert host.status().job_state is JobState.RUNNING


@pytest.mark.parametrize("request_", [Stop(), Estop()])
def test_stop_and_estop_abort_a_running_job_and_freeze_its_time(host, clock, request_):
	configure(host)
	upload(host, (10.0, 5000, False))
	host.call(JobRun())
	clock.advance(1.0)
	host.call(request_)
	clock.advance(1.0)
	s = host.status()
	assert (s.job_state, s.segment_index) == (JobState.ABORTED, None)
	assert s.job_elapsed_ms == pytest.approx(1000, abs=10)
	assert host.settings().target_speed_hz == 0.0


def test_job_clear_after_a_job_forgets_its_outcome(host, clock):
	configure(host)
	upload(host, (10.0, 100, False))
	host.call(JobRun())
	clock.advance(0.2)
	assert host.status().job_state is JobState.DONE
	host.call(JobClear())
	assert host.status().job_state is JobState.EMPTY
	upload(host, (10.0, 100, False))
	s = host.status()
	assert (s.job_state, s.job_elapsed_ms) == (JobState.READY, 0)


def test_a_finished_job_runs_again(host, clock):
	configure(host)
	upload(host, (10.0, 100, False))
	host.call(JobRun())
	clock.advance(0.2)
	host.call(JobRun())
	clock.advance(0.05)
	assert host.status().job_state is JobState.RUNNING


# --- Comms timeout -----------------------------------------------------------------------------

def test_silence_for_the_comms_timeout_stops_the_motor_and_aborts_the_job(host, clock):
	configure(host)
	host.call(SetCommsTimeout(500))
	upload(host, (10.0, 5000, False))
	host.call(JobRun())
	clock.advance(0.45)
	assert host.status().job_state is JobState.RUNNING
	clock.advance(0.6)
	s = host.status()
	assert s.job_state is JobState.ABORTED
	assert s.job_elapsed_ms == pytest.approx(500 + 450, abs=20)
	assert StatusFlag.COMMS_LOST in s.flags
	assert host.settings().target_speed_hz == 0.0


def test_a_new_setpoint_clears_comms_lost(host, clock):
	configure(host)
	host.call(SetCommsTimeout(500))
	host.call(SetSpeed(10.0))
	clock.advance(0.6)
	assert StatusFlag.COMMS_LOST in host.status().flags
	host.call(SetSpeed(10.0))
	assert StatusFlag.COMMS_LOST not in host.status().flags


def test_a_standing_motor_never_times_out(host, clock):
	host.call(SetCommsTimeout(500))
	clock.advance(2.0)
	assert StatusFlag.COMMS_LOST not in host.status().flags


def test_with_the_timeout_off_silence_is_fine(host, clock):
	configure(host)
	upload(host, (10.0, 5000, False))
	host.call(JobRun())
	clock.advance(3.0)
	assert host.status().job_state is JobState.RUNNING


def test_a_bad_crc_frame_is_not_host_activity(host, clock):
	configure(host)
	host.call(SetCommsTimeout(500))
	host.call(SetSpeed(10.0))
	frame = bytearray(build_frame(Command.GET_ID, 9))
	frame[-2] ^= 0xFF
	for _ in range(3):
		clock.advance(0.2)
		host.send_raw(bytes(frame))
	assert StatusFlag.COMMS_LOST in host.status().flags


# --- Restarts, role and time ------------------------------------------------------------------

def test_the_role_is_kept_over_a_restart_and_the_rest_is_not(host, board, clock):
	host.call(SetId(Role.COLOUR))
	configure(host)
	host.call(SetSpeed(10.0))
	clock.advance(2.0)
	board.restart()
	clock.advance(0.1)
	s = host.status()
	assert host.call(GetId()) is Role.COLOUR
	assert s.flags == StatusFlag.UNCONFIGURED
	assert s.device_time_us == pytest.approx(100_000, abs=1000)
	assert s.edge_count == 0
	assert host.settings().kp == 0.0


def test_after_a_restart_the_same_sequence_runs_again(host, board):
	host.call(JobClear())
	host.call(JobSegment(0, 10.0, 100), sequence=7)
	board.restart()
	host.call(JobClear())
	host.call(JobSegment(0, 10.0, 100), sequence=7)
	host.call(JobSegment(1, 10.0, 100), sequence=8)
	assert host.call(JobCommit(2, 200)).segment_count == 2


def test_a_watchdog_restart_is_flagged_until_the_next_restart(host, board):
	board.restart(watchdog=True)
	assert StatusFlag.WATCHDOG_RESET in host.status().flags
	board.restart()
	assert StatusFlag.WATCHDOG_RESET not in host.status().flags


def test_opening_the_port_restarts_the_board_as_on_an_uno(board, clock):
	host = Host(board.open())
	configure(host)
	host.port.close()
	clock.advance(1.0)
	assert Host(board.open()).status().flags == StatusFlag.UNCONFIGURED


def test_a_board_that_does_not_reset_on_open_keeps_its_state(clock):
	board = SimBoard(clock=clock, resets_on_open=False)
	port = board.open()
	configure(Host(port))
	port.close()
	clock.advance(1.0)
	s = Host(board.open()).status()
	assert StatusFlag.UNCONFIGURED not in s.flags
	assert s.device_time_us == pytest.approx(1_000_000, abs=1000)


def test_while_booting_the_board_does_not_answer(clock):
	board = SimBoard(clock=clock, boot_s=1.5)
	host = Host(board.open())
	assert host.send_raw(build_frame(Command.GET_VERSION, 1)) == []
	clock.advance(1.5)
	assert [reply.sequence for reply in host.send_raw(build_frame(Command.GET_VERSION, 2))] == [2]


def test_device_time_is_micros_since_the_restart(host, clock):
	clock.advance(2.5)
	assert host.status().device_time_us == 2_500_000


def test_device_time_wraps_after_2_to_the_32_us(host, clock):
	clock.advance(2**32 / 1e6 + 1.0)
	assert host.status().device_time_us == pytest.approx(1_000_000, abs=1)


def test_the_longest_loop_interval_is_one_control_period_and_is_taken_by_the_read(host, clock):
	clock.advance(0.1)
	assert host.status().longest_loop_interval_us == 10_000
	assert host.status().longest_loop_interval_us == 0


# --- Fault switches ---------------------------------------------------------------------------

@pytest.fixture
def bench():
	return SimBench({"SIM1": SimBoard(Role.POLYMER)})


def test_a_corrupted_reply_is_dropped_by_the_host_and_the_retry_gets_it(bench):
	board = bench["SIM1"]
	with Link.open("SIM1", opener=bench.opener, reply_timeout_s=0.05) as link:
		board.corrupt_replies = 1
		link.call(SetId(Role.COLOUR))
		assert link.call(GetId()) is Role.COLOUR
		stats = link.stats
		assert (stats.retries, stats.host_bad_crc_frames) == (1, 1)
	assert board.corrupt_replies == 0


def test_a_corrupted_request_is_nacked_and_retried(bench):
	board = bench["SIM1"]
	with Link.open("SIM1", opener=bench.opener) as link:
		board.corrupt_requests = 1
		assert link.call(GetId()) is Role.POLYMER
		assert link.stats.retries == 1
		assert link.call(RequestStatus()).bad_crc_frames == 1


def test_a_dropped_reply_is_resent_from_the_store_on_the_retry(bench):
	board = bench["SIM1"]
	with Link.open("SIM1", opener=bench.opener, reply_timeout_s=0.05) as link:
		link.call(JobClear())
		board.drop_replies = 2
		link.call(JobSegment(0, 10.0, 100))
		assert link.call(JobCommit(1, 100)).segment_count == 1
		assert (link.stats.retries, link.stats.reply_timeouts) == (2, 2)


def test_a_jammed_screw_stops_and_reads_stalled_until_freed(host, board, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(200))
	clock.advance(1.0)
	board.jammed = True
	edges = host.status().edge_count
	clock.advance(1.0)
	s = host.status()
	assert s.edge_count <= edges + 1
	assert (s.raw_speed_hz, StatusFlag.ENCODER_STALLED in s.flags) == (0.0, True)
	board.jammed = False
	clock.advance(1.0)
	assert host.status().raw_speed_hz > 30.0


def test_a_lost_link_fails_the_port_until_it_is_restored(board):
	port = board.open()
	board.lose_link()
	with pytest.raises(serial.SerialException):
		port.read(1)
	with pytest.raises(serial.SerialException):
		port.write(build_frame(Command.GET_ID, 1))
	with pytest.raises(serial.SerialException, match="not found"):
		board.open()
	board.restore_link()
	assert Host(board.open()).call(GetVersion()).protocol_version == 3


def test_a_read_with_nothing_to_read_gives_up_after_the_read_timeout(board):
	port = board.open()
	started = time.perf_counter()
	assert port.read(1) == b""
	assert 0.04 <= time.perf_counter() - started < 0.5


def test_cancel_read_and_a_lost_link_wake_a_waiting_read(board):
	port = board.open()
	results = []
	reader = threading.Thread(target=lambda: results.append(port.read(1)))
	reader.start()
	time.sleep(0.01)
	port.cancel_read()
	reader.join(0.03)
	assert results == [b""]

	def read_until_lost():
		try:
			while True:
				port.read(1)
		except serial.SerialException as error:
			results.append(error)

	reader = threading.Thread(target=read_until_lost)
	reader.start()
	time.sleep(0.01)
	board.lose_link()
	reader.join(0.03)
	assert isinstance(results[-1], serial.SerialException)


def test_a_lost_link_is_lost_for_the_link_on_top(bench):
	board = bench["SIM1"]
	link = Link.open("SIM1", opener=bench.opener)
	board.lose_link()
	with pytest.raises(LinkLostError):
		for _ in range(50):
			link.call(GetId())
			time.sleep(0.01)
	link.close()
	with pytest.raises(LinkOpenError):
		Link.open("SIM1", opener=bench.opener)


def test_the_board_runs_on_without_its_host_until_its_comms_timeout(host, board, clock):
	configure(host)
	host.call(SetCommsTimeout(500))
	upload(host, (10.0, 5000, False))
	host.call(JobRun())
	board.lose_link()
	clock.advance(1.0)
	state = board.state()
	assert (state.job_state, state.comms_lost, state.target_speed_hz) == (JobState.ABORTED, True, 0.0)


def test_a_fast_clock_runs_the_boards_time_and_jobs_fast(clock):
	host = Host(SimBoard(clock=clock, clock_error=0.005).open())
	configure(host)
	upload(host, (10.0, 10_000, False))
	host.call(JobRun())
	clock.advance(9.9)
	s = host.status()
	assert s.job_elapsed_ms == pytest.approx(9950, abs=10)
	assert s.device_time_us == pytest.approx(9.9 * 1.005e6, abs=1000)
	clock.advance(0.06)
	assert host.status().job_state is JobState.DONE


def test_the_clock_error_can_change_without_a_jump(host, board, clock):
	clock.advance(1.0)
	board.clock_error = 0.005
	assert board.state().time_us == pytest.approx(1_000_000, abs=1)
	clock.advance(1.0)
	assert board.state().time_us == pytest.approx(2_005_000, abs=1)


def test_the_state_shows_the_screw(host, board, clock):
	host.call(SetMode(Mode.OPEN_LOOP))
	host.call(SetPwm(200))
	clock.advance(3.0)
	state = board.state()
	assert state.motor_hz == pytest.approx(43.2, rel=0.01)
	assert (state.duty, state.mode, state.role) == (200, Mode.OPEN_LOOP, Role.POLYMER)
	assert state.edge_count == host.status().edge_count


def test_a_long_idle_stretch_is_cheap(host, clock):
	clock.advance(3600.0)
	started = time.perf_counter()
	host.status()
	assert time.perf_counter() - started < 0.5


# --- Firmware sync ----------------------------------------------------------------------------

def test_the_mirrored_firmware_constants_match_its_sources():
	from polydriver.sim import fake_board as sim
	main = (CORE.parents[1] / "src" / "main.cpp").read_text(encoding="utf-8")
	assert int(re.search(r"CONTROL_PERIOD_US = (\d+);", main).group(1)) == sim.CONTROL_PERIOD_US
	version = re.search(r"FIRMWARE_VERSION\{(\d+), (\d+), (\d+)\}", source("CommandDispatcher.h")).groups()
	assert tuple(map(int, version)) == sim.FIRMWARE_VERSION
	assert cpp_constant(source("JobProgram.h"), "MAX_SEGMENTS") == sim.MAX_SEGMENTS
	assert float(re.search(r"DEFAULT_ALPHA = ([\d.]+)f;", source("EMAFilter.h")).group(1)) == sim.DEFAULT_ALPHA
	assert float(re.search(r"OUTPUT_MAX = ([\d.]+)f;", source("PIDRegulator.h")).group(1)) == sim.PID_OUTPUT_MAX


def test_the_commands_refused_while_running_are_the_firmwares():
	from polydriver.sim import fake_board as sim
	body = re.search(r"isRefusedWhileRunning\(Command command\) \{(.*?)return true;", source("CommandDispatcher.cpp"), re.S)
	names = set(re.findall(r"case Command::(\w+):", body.group(1)))
	assert names == {command.name for command in sim.REFUSED_WHILE_RUNNING}


# --- A Session on two simulated boards --------------------------------------------------------

def bench_config(bench, tmp_path, **changes):
	values = dict(ports=bench.ports, comms_timeout_s=1.0, boards={Role.POLYMER: GAINS, Role.COLOUR: GAINS},
				logging=LoggingSettings(poll_hz=20.0), base_dir=tmp_path)
	values.update(changes)
	return Config(**values)


def quick_job(store):
	return short(store, amount_given={"duration_s": 4, "total_flow_g_s": 1.5}, hold_start_s=0.5, hold_end_s=0.5)


def test_a_pair_has_a_polymer_and_a_colour_board():
	bench = SimBench.pair()
	assert {role: bench[port].state().role for role, port in bench.ports.items()} == {
		Role.POLYMER: Role.POLYMER, Role.COLOUR: Role.COLOUR}


def test_a_session_connects_to_two_simulated_boards_and_runs_a_job(tmp_path):
	bench = SimBench.pair()
	store = calibrations()
	with Session(bench_config(bench, tmp_path), link_factory=bench.link_factory) as session:
		for role in (Role.POLYMER, Role.COLOUR):
			session.connect(role)
		runner = Runner(session, quick_job(store), materials(), store)
		program = runner.upload()
		runner.start()
		assert runner.wait(10.0) is RunState.DONE, runner.reason
		progress = runner.progress()
		assert progress.warnings == ()
		# The screws spin up with the PI's overshoot, which a 4 s job does not average out (+3 % on
		# the polymer, +8 % on the colour's 32 edges; +1 % and +3 % over 10 s).
		for name in ("polymer", "colour"):
			expected_g = getattr(program, name).expected_grams
			assert getattr(progress, name).delivered_g == pytest.approx(expected_g, rel=0.12)
		for port in bench.ports.values():
			state = bench[port].state()
			assert (state.job_state, state.job_elapsed_ms) == (JobState.DONE, program.total_ms)


def test_a_run_rides_out_corrupted_and_dropped_replies(tmp_path):
	bench = SimBench.pair()
	store = calibrations()
	with Session(bench_config(bench, tmp_path), link_factory=bench.link_factory) as session:
		for role in (Role.POLYMER, Role.COLOUR):
			session.connect(role)
		runner = Runner(session, quick_job(store), materials(), store)
		runner.upload()
		runner.start()
		faults = itertools.cycle(("corrupt_replies", "drop_replies", "corrupt_requests"))
		injected = 0
		while runner.state is RunState.RUNNING:
			for port in bench.ports.values():
				setattr(bench[port], next(faults), 1)
				injected += 1
			time.sleep(0.3)
		assert runner.wait(5.0) is RunState.DONE, runner.reason
		assert runner.progress().warnings == ()
		assert injected >= 20
		for port in bench.ports.values():
			board = bench[port]
			assert (board.corrupt_replies, board.drop_replies, board.corrupt_requests) == (0, 0, 0)

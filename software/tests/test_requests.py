import dataclasses
import math
import struct

import pytest

from polydriver.protocol import constants as c
from polydriver.protocol.constants import Command, JobState, Mode, NackReason, Reply, Role, StatusFlag
from polydriver.protocol.link_layer import ReplyFrame, ReplyParser
from polydriver.requests import (
	ClearEstop, Estop, GetId, GetVersion, JobClear, JobCommit, JobInfo, JobRun, JobSegment,
	PayloadFormatError, Request, RequestNackError, RequestSettings, RequestStatus, RequestValueError,
	SetCommsTimeout, SetFeedforward, SetFilter, SetId, SetMode, SetPid, SetPwm, SetSpeed,
	SettingsReport, StatusReport, Stop, UnexpectedReplyError, VersionReport, request_classes,
)

# Replies sent by the real firmware on the simulated Uno (fw/sim/vectors, fw/sim/harness).
FW_STATUS = "02 01 31 20 6C 00 00 C8 42 00 00 C8 42 80 90 B4 57 08 00 1F 00 00 00 00 00 1C 27 00 00 00 00 00 FF 00 00 00 00 99 03"
FW_SETTINGS = (
	"02 01 33 20 6A 00 00 C0 3F 00 00 80 3E CD CC 4C 3D 00 00 20 42 DC 05 00 00 9A 99 99 3E"
	" 00 00 C8 42 00 00 00 3F 1A 03"
)
FW_VERSION = "02 01 41 05 64 03 03 00 00 20 9D 03"
FW_ID_POLYMER = "02 01 43 01 6C 01 47 03"
FW_JOB_INFO = "02 01 75 06 6F 02 01 DC 05 00 00 B9 03"
FW_NACK_NOT_CONFIGURED = "02 01 12 01 64 0A 46 03"
FW_ACK = "02 01 11 00 65 34 03"

ONE_OF_EACH: list[Request] = [
	SetSpeed(50.0), SetPid(1.0, 0.5, 0.1), SetFilter(0.3), SetFeedforward(100.0, 0.5),
	RequestStatus(), RequestSettings(), GetVersion(), GetId(), SetId(Role.COLOUR),
	SetMode(Mode.OPEN_LOOP), SetPwm(128), SetCommsTimeout(1500), Stop(), Estop(), ClearEstop(),
	JobClear(), JobSegment(0, 25.0, 1500, ramp=True), JobCommit(1, 1500), JobRun(),
]


def fw_frame(hex_bytes: str) -> ReplyFrame:
	[frame] = ReplyParser().feed(bytes.fromhex(hex_bytes), 0.0)
	return frame


def reply(code: int, payload: bytes = b"") -> ReplyFrame:
	return ReplyFrame(command=code, sequence=0, payload=payload)


# --- One class per opcode -----------------------------------------------------

def test_every_command_has_exactly_one_request_class():
	assert sorted(cls.COMMAND for cls in request_classes()) == sorted(Command)


def test_the_samples_cover_every_command():
	assert sorted(r.COMMAND for r in ONE_OF_EACH) == sorted(Command)


@pytest.mark.parametrize("request_", ONE_OF_EACH, ids=lambda r: type(r).__name__)
def test_payload_length_matches_the_protocol(request_):
	assert len(request_.encode()) == c.REQUEST_PAYLOAD_LENGTH[request_.COMMAND]


@pytest.mark.parametrize("request_, expected", [
	(RequestStatus(), Reply.STATUS),
	(RequestSettings(), Reply.SETTINGS),
	(GetVersion(), Reply.VERSION),
	(GetId(), Reply.ID),
	(JobCommit(1, 10), Reply.JOB_INFO),
	(SetSpeed(1.0), Reply.ACK),
	(Stop(), Reply.ACK),
	(JobSegment(0, 1.0, 10), Reply.ACK),
])
def test_expected_reply(request_, expected):
	assert request_.REPLY == expected


def test_only_stop_and_estop_are_urgent():
	assert {type(r) for r in ONE_OF_EACH if r.URGENT} == {Stop, Estop}


# --- Payloads -------------------------------------------------------------------

def test_set_speed_payload_matches_the_firmware():
	assert SetSpeed(50.0).encode() == bytes.fromhex("00 00 48 42")


def test_job_segment_payload_matches_the_firmware():
	assert JobSegment(0, 120.5, 2500, ramp=True).encode() == bytes.fromhex("00 01 00 00 F1 42 C4 09 00 00")


def test_job_segment_flags():
	assert JobSegment(3, 10.0, 100).encode()[:2] == bytes([3, 0])
	assert JobSegment(3, 10.0, 100, ramp=True).encode()[1] == c.SegmentFlag.RAMP
	assert JobSegment(3, 200.0, 100, duty=True).encode()[1] == c.SegmentFlag.DUTY
	assert JobSegment(3, 200.0, 100, ramp=True, duty=True).encode()[1] == 0x03


def test_payloads_follow_the_request_formats():
	assert SetPid(1.0, 0.5, 0.25).encode() == struct.pack("<fff", 1.0, 0.5, 0.25)
	assert SetFeedforward(100.0, 0.5).encode() == struct.pack("<ff", 100.0, 0.5)
	assert SetCommsTimeout(1500).encode() == bytes([0xDC, 0x05])
	assert SetId(Role.POLYMER).encode() == bytes([1])
	assert SetMode(Mode.OPEN_LOOP).encode() == bytes([1])
	assert JobCommit(2, 1500).encode() == bytes([2, 0xDC, 0x05, 0, 0])
	assert Stop().encode() == b""


@pytest.mark.parametrize("make", [
	lambda: SetPwm(256),
	lambda: SetPwm(-1),
	lambda: SetCommsTimeout(65536),
	lambda: SetMode(7),
	lambda: SetId(5),
	lambda: JobSegment(256, 1.0, 10),
	lambda: JobSegment(0, 1.0, 2**32),
	lambda: JobCommit(0, -1),
	lambda: SetSpeed(1e40),
	lambda: SetPid(1.0, 1e40, 0.0),
], ids=["pwm 256", "pwm -1", "timeout 65536", "mode 7", "role 5", "index 256", "duration 2^32",
	"total -1", "speed too big for f32", "ki too big for f32"])
def test_values_that_cannot_be_sent_are_refused_at_construction(make):
	with pytest.raises(RequestValueError):
		make()


def test_values_the_board_judges_are_passed_through():
	assert math.isnan(struct.unpack("<f", SetSpeed(math.nan).encode())[0])
	assert struct.unpack("<f", SetSpeed(-5.0).encode())[0] == -5.0
	assert SetFilter(0.0).encode() == bytes(4)


def test_requests_are_immutable():
	with pytest.raises(dataclasses.FrozenInstanceError):
		SetSpeed(1.0).target_speed_hz = 2.0


# --- Replies: ACK, NACK and mistakes ------------------------------------------------

def test_an_ack_returns_none():
	assert SetPid(1.0, 0.0, 0.0).handle(fw_frame(FW_ACK)) is None


def test_a_nack_raises_with_the_command_and_the_reason():
	with pytest.raises(RequestNackError, match="SET_SPEED.*NOT_CONFIGURED") as raised:
		SetSpeed(40.0).handle(fw_frame(FW_NACK_NOT_CONFIGURED))
	assert raised.value.command is Command.SET_SPEED
	assert raised.value.reason is NackReason.NOT_CONFIGURED


def test_a_nack_from_a_data_request_raises_too():
	with pytest.raises(RequestNackError) as raised:
		JobCommit(2, 10).handle(reply(Reply.NACK, bytes([NackReason.BAD_JOB])))
	assert raised.value.reason is NackReason.BAD_JOB


def test_an_unknown_nack_reason_is_kept_as_a_number():
	with pytest.raises(RequestNackError, match="0x7F") as raised:
		Stop().handle(reply(Reply.NACK, bytes([0x7F])))
	assert raised.value.reason == 0x7F


def test_a_nack_without_its_reason_byte_is_a_format_error():
	with pytest.raises(PayloadFormatError):
		Stop().handle(reply(Reply.NACK))


def test_a_different_reply_code_is_unexpected():
	with pytest.raises(UnexpectedReplyError) as raised:
		RequestStatus().handle(reply(Reply.ACK))
	assert raised.value.expected is Reply.STATUS
	assert raised.value.got == Reply.ACK


def test_an_ack_with_a_payload_is_a_format_error():
	with pytest.raises(PayloadFormatError):
		Stop().handle(reply(Reply.ACK, b"\x00"))


def test_a_status_of_the_wrong_size_names_a_protocol_mismatch():
	with pytest.raises(PayloadFormatError, match="protocol"):
		RequestStatus().handle(reply(Reply.STATUS, bytes(26)))


# --- Decoded replies --------------------------------------------------------------

def test_report_fields_follow_the_protocol_field_lists():
	assert tuple(f.name for f in dataclasses.fields(StatusReport)) == c.STATUS_FIELDS
	assert tuple(f.name for f in dataclasses.fields(SettingsReport)) == c.SETTINGS_FIELDS
	assert tuple(f.name for f in dataclasses.fields(VersionReport)) == c.VERSION_FIELDS
	assert tuple(f.name for f in dataclasses.fields(JobInfo)) == c.JOB_INFO_FIELDS


def test_decodes_the_firmware_status():
	report = RequestStatus().handle(fw_frame(FW_STATUS))
	assert report == StatusReport(
		raw_speed_hz=100.0, filtered_speed_hz=100.0, duty=128,
		flags=StatusFlag.OPEN_LOOP | StatusFlag.UNCONFIGURED, device_time_us=546740, edge_count=31,
		control_resyncs=0, longest_loop_interval_us=10012, dropped_frames=0, bad_crc_frames=0,
		job_state=JobState.EMPTY, segment_index=None, job_elapsed_ms=0,
	)
	assert StatusFlag.OPEN_LOOP in report.flags


def test_status_segment_index_is_a_number_while_a_segment_runs():
	payload = struct.pack(c.STATUS_FORMAT, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, JobState.RUNNING, 4, 1200)
	report = RequestStatus().handle(reply(Reply.STATUS, payload))
	assert report.job_state is JobState.RUNNING
	assert report.segment_index == 4


def test_status_keeps_a_reserved_flag_bit():
	payload = struct.pack(c.STATUS_FORMAT, 0, 0, 0, 0x02, 0, 0, 0, 0, 0, 0, 0, 0xFF, 0)
	assert int(RequestStatus().handle(reply(Reply.STATUS, payload)).flags) == 0x02


def test_an_unknown_job_state_is_a_format_error():
	payload = struct.pack(c.STATUS_FORMAT, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 9, 0xFF, 0)
	with pytest.raises(PayloadFormatError, match="job state"):
		RequestStatus().handle(reply(Reply.STATUS, payload))


def test_decodes_the_firmware_settings():
	report = RequestSettings().handle(fw_frame(FW_SETTINGS))
	assert report.kp == 1.5
	assert report.ki == 0.25
	assert report.kd == pytest.approx(0.05)
	assert report.target_speed_hz == 40.0
	assert report.comms_timeout_ms == 1500
	assert report.mode is Mode.CLOSED_LOOP
	assert report.duty_setpoint == 0
	assert report.alpha == pytest.approx(0.3)
	assert report.feedforward_offset_duty == 100.0
	assert report.feedforward_gain_duty_per_hz == 0.5


def test_decodes_the_firmware_version():
	assert GetVersion().handle(fw_frame(FW_VERSION)) == VersionReport(
		protocol_version=3, firmware_major=3, firmware_minor=0, firmware_patch=0, max_segments=32,
	)


def test_decodes_the_firmware_role():
	assert GetId().handle(fw_frame(FW_ID_POLYMER)) is Role.POLYMER


def test_an_unknown_role_is_a_format_error():
	with pytest.raises(PayloadFormatError, match="role"):
		GetId().handle(reply(Reply.ID, bytes([9])))


def test_decodes_the_firmware_job_info():
	assert JobCommit(1, 1500).handle(fw_frame(FW_JOB_INFO)) == JobInfo(
		job_state=JobState.READY, segment_count=1, total_ms=1500,
	)

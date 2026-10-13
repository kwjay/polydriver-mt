import struct

from polydriver.protocol import constants as c
from polydriver.protocol.constants import Command, NackReason, Reply, StatusFlag


def test_frame_limits():
	assert c.FRAME_OVERHEAD == 7
	assert c.MAX_FRAME_LENGTH == 39


def test_every_command_has_a_payload_format():
	assert set(c.REQUEST_FORMAT) == set(Command)


def test_every_reply_has_a_payload_format():
	assert set(c.REPLY_FORMAT) == set(Reply)


def test_request_payload_lengths_match_the_spec():
	assert c.REQUEST_PAYLOAD_LENGTH[Command.SET_SPEED] == 4
	assert c.REQUEST_PAYLOAD_LENGTH[Command.SET_PID] == 12
	assert c.REQUEST_PAYLOAD_LENGTH[Command.SET_FEEDFORWARD] == 8
	assert c.REQUEST_PAYLOAD_LENGTH[Command.SET_COMMS_TIMEOUT] == 2
	assert c.REQUEST_PAYLOAD_LENGTH[Command.JOB_SEGMENT] == 10
	assert c.REQUEST_PAYLOAD_LENGTH[Command.JOB_COMMIT] == 5
	assert c.REQUEST_PAYLOAD_LENGTH[Command.STOP] == 0


def test_reply_payload_lengths_match_the_spec():
	assert c.REPLY_PAYLOAD_LENGTH[Reply.STATUS] == 32
	assert c.REPLY_PAYLOAD_LENGTH[Reply.SETTINGS] == 32
	assert c.REPLY_PAYLOAD_LENGTH[Reply.VERSION] == 5
	assert c.REPLY_PAYLOAD_LENGTH[Reply.JOB_INFO] == 6
	assert c.REPLY_PAYLOAD_LENGTH[Reply.NACK] == 1
	assert c.REPLY_PAYLOAD_LENGTH[Reply.ACK] == 0


def test_every_payload_fits_its_limit():
	assert max(c.REQUEST_PAYLOAD_LENGTH.values()) <= c.MAX_REQUEST_PAYLOAD
	assert max(c.REPLY_PAYLOAD_LENGTH.values()) <= c.MAX_REPLY_PAYLOAD


def test_formats_are_little_endian_without_padding():
	for fmt in [*c.REQUEST_FORMAT.values(), *c.REPLY_FORMAT.values()]:
		assert fmt.startswith("<")


def test_opcodes_and_reply_codes_do_not_collide():
	assert not {int(x) for x in Command} & {int(x) for x in Reply}


def test_status_flags_are_distinct_bits():
	combined = 0
	for flag in StatusFlag:
		assert flag & combined == 0
		combined |= flag
	assert combined == 0xFD


def test_nack_reasons_are_consecutive():
	assert [int(r) for r in NackReason] == list(range(1, 11))


def test_status_round_trip():
	values = (12.5, 11.25, 180, 0x85, 0x11223344, 4242, 3, 10250, 1, 2, 3, 4, 987654)
	payload = struct.pack(c.STATUS_FORMAT, *values)
	assert len(payload) == 32
	assert struct.unpack(c.STATUS_FORMAT, payload) == values

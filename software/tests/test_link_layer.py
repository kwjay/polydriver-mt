import struct

import pytest

from polydriver.protocol.constants import (
	BOARD_ID, ETX, MAX_REPLY_PAYLOAD, MAX_REQUEST_PAYLOAD, STX, Command, Reply,
)
from polydriver.protocol.link_layer import (
	REPLY_INTER_BYTE_TIMEOUT_S, FrameBuildError, ReplyFrame, ReplyParser, SequenceCounter,
	build_frame, crc8,
)

# Frames produced by the firmware: requests with its Crc8.h, replies by the real firmware
# running on the simulated Uno (fw/sim/harness).
FW_GET_VERSION_REQUEST = bytes.fromhex("02 01 40 00 64 BA 03")
FW_SET_SPEED_50_REQUEST = bytes.fromhex("02 01 10 04 67 00 00 48 42 33 03")
FW_JOB_SEGMENT_REQUEST = bytes.fromhex("02 01 72 0A FF 00 01 00 00 F1 42 C4 09 00 00 66 03")
FW_VERSION_REPLY = bytes.fromhex("02 01 41 05 64 03 03 00 00 20 9D 03")
FW_NACK_BAD_CRC_REPLY = bytes.fromhex("02 01 12 01 6F 01 45 03")
FW_STATUS_REPLY = bytes.fromhex(
	"02 01 31 20 6C 00 00 C8 42 00 00 C8 42 80 90 B4 57 08 00 1F 00 00 00 00 00"
	" 1C 27 00 00 00 00 00 FF 00 00 00 00 99 03"
)

T0 = 100.0


def reply_frame(command: int, sequence: int, payload: bytes = b"", board_id: int = BOARD_ID) -> bytes:
	body = bytes([board_id, command, len(payload), sequence]) + payload
	return bytes([STX]) + body + bytes([crc8(body), ETX])


def feed_bytewise(parser: ReplyParser, data: bytes, now_s: float = T0) -> list[ReplyFrame]:
	frames = []
	for byte in data:
		frames += parser.feed(bytes([byte]), now_s)
	return frames


# --- CRC-8 ------------------------------------------------------------------

def test_crc8_check_value():
	assert crc8(b"123456789") == 0xA1


def test_crc8_of_nothing_is_the_initial_value():
	assert crc8(b"") == 0x00


def test_crc8_matches_the_firmware_on_its_own_frames():
	for frame in [FW_GET_VERSION_REQUEST, FW_SET_SPEED_50_REQUEST, FW_VERSION_REPLY, FW_STATUS_REPLY]:
		assert crc8(frame[1:-2]) == frame[-2]


# --- build_frame ------------------------------------------------------------

def test_build_frame_matches_the_firmware_byte_for_byte():
	assert build_frame(Command.GET_VERSION, 0x64) == FW_GET_VERSION_REQUEST
	assert build_frame(Command.SET_SPEED, 0x67, struct.pack("<f", 50.0)) == FW_SET_SPEED_50_REQUEST
	segment = struct.pack("<BBfI", 0, 0x01, 120.5, 2500)
	assert build_frame(Command.JOB_SEGMENT, 0xFF, segment) == FW_JOB_SEGMENT_REQUEST


def test_build_frame_layout():
	payload = struct.pack("<f", 15.5)
	frame = build_frame(Command.SET_SPEED, 42, payload)
	assert frame[0] == STX
	assert frame[1] == BOARD_ID
	assert frame[2] == Command.SET_SPEED
	assert frame[3] == len(payload)
	assert frame[4] == 42
	assert frame[5:9] == payload
	assert frame[-2] == crc8(frame[1:-2])
	assert frame[-1] == ETX
	assert len(frame) == 7 + len(payload)


def test_build_frame_accepts_the_largest_request_payload():
	assert len(build_frame(Command.SET_SPEED, 0, bytes(MAX_REQUEST_PAYLOAD))) == 7 + MAX_REQUEST_PAYLOAD


def test_build_frame_rejects_an_oversized_payload():
	with pytest.raises(FrameBuildError, match="payload"):
		build_frame(Command.SET_SPEED, 0, bytes(MAX_REQUEST_PAYLOAD + 1))


@pytest.mark.parametrize("sequence", [-1, 256])
def test_build_frame_rejects_a_sequence_outside_one_byte(sequence):
	with pytest.raises(FrameBuildError, match="sequence"):
		build_frame(Command.STOP, sequence)


@pytest.mark.parametrize("command", [-1, 256])
def test_build_frame_rejects_a_command_outside_one_byte(command):
	with pytest.raises(FrameBuildError, match="command"):
		build_frame(command, 0)


def test_build_frame_accepts_a_raw_opcode_for_fault_injection():
	assert build_frame(0x99, 0)[2] == 0x99


# --- ReplyParser: good frames -----------------------------------------------

def test_parses_the_firmware_version_reply():
	assert ReplyParser().feed(FW_VERSION_REPLY, T0) == [
		ReplyFrame(command=Reply.VERSION, sequence=0x64, payload=bytes([3, 3, 0, 0, 32])),
	]


def test_parses_the_firmware_status_reply():
	[frame] = ReplyParser().feed(FW_STATUS_REPLY, T0)
	assert frame.command == Reply.STATUS
	assert frame.sequence == 0x6C
	assert len(frame.payload) == MAX_REPLY_PAYLOAD
	assert struct.unpack_from("<f", frame.payload, 0)[0] == 100.0


def test_parses_the_firmware_nack():
	assert ReplyParser().feed(FW_NACK_BAD_CRC_REPLY, T0) == [
		ReplyFrame(command=Reply.NACK, sequence=0x6F, payload=bytes([0x01])),
	]


def test_parses_a_frame_fed_one_byte_at_a_time():
	assert feed_bytewise(ReplyParser(), FW_VERSION_REPLY) == ReplyParser().feed(FW_VERSION_REPLY, T0)


def test_parses_an_empty_payload():
	assert ReplyParser().feed(reply_frame(Reply.ACK, 7), T0) == [ReplyFrame(Reply.ACK, 7, b"")]


def test_returns_every_frame_in_one_chunk():
	data = reply_frame(Reply.ACK, 1) + reply_frame(Reply.ACK, 2) + reply_frame(Reply.ACK, 3)
	assert [f.sequence for f in ReplyParser().feed(data, T0)] == [1, 2, 3]


def test_a_frame_split_across_chunks_inside_the_timeout_survives():
	parser = ReplyParser()
	assert parser.feed(FW_STATUS_REPLY[:20], T0) == []
	[frame] = parser.feed(FW_STATUS_REPLY[20:], T0 + REPLY_INTER_BYTE_TIMEOUT_S)
	assert frame.sequence == 0x6C
	assert parser.abandoned_frames == 0


def test_accepts_the_largest_reply_payload():
	payload = bytes(range(MAX_REPLY_PAYLOAD))
	[frame] = ReplyParser().feed(reply_frame(Reply.STATUS, 9, payload), T0)
	assert frame.payload == payload


def test_keeps_an_unknown_reply_code_for_the_caller_to_judge():
	assert ReplyParser().feed(reply_frame(0x99, 5), T0)[0].command == 0x99


def test_a_request_frame_parses_as_well():
	assert ReplyParser().feed(FW_SET_SPEED_50_REQUEST, T0) == [
		ReplyFrame(Command.SET_SPEED, 0x67, struct.pack("<f", 50.0)),
	]


# --- ReplyParser: damaged frames ----------------------------------------------

def test_a_bad_crc_is_dropped_and_counted():
	frame = bytearray(reply_frame(Reply.ACK, 3))
	frame[2] = Reply.NACK
	parser = ReplyParser()
	assert parser.feed(bytes(frame), T0) == []
	assert parser.bad_crc_frames == 1
	assert parser.abandoned_frames == 0


def test_a_length_above_the_reply_limit_is_abandoned_and_the_next_frame_parses():
	parser = ReplyParser()
	assert parser.feed(bytes([STX, BOARD_ID, Reply.STATUS, MAX_REPLY_PAYLOAD + 1]), T0) == []
	assert parser.abandoned_frames == 1
	assert parser.feed(reply_frame(Reply.ACK, 4), T0) == [ReplyFrame(Reply.ACK, 4, b"")]


def test_a_missing_etx_is_abandoned():
	frame = bytearray(reply_frame(Reply.ACK, 3))
	frame[-1] = 0x55
	parser = ReplyParser()
	assert parser.feed(bytes(frame), T0) == []
	assert parser.abandoned_frames == 1
	assert parser.bad_crc_frames == 0


def test_stx_in_place_of_etx_starts_the_next_frame():
	truncated_etx = reply_frame(Reply.ACK, 3)[:-1]
	parser = ReplyParser()
	assert parser.feed(truncated_etx + reply_frame(Reply.ACK, 4), T0) == [ReplyFrame(Reply.ACK, 4, b"")]
	assert parser.abandoned_frames == 1


def test_a_frame_for_another_id_is_ignored_without_counting():
	parser = ReplyParser()
	assert parser.feed(reply_frame(Reply.ACK, 3, board_id=0x07), T0) == []
	assert parser.abandoned_frames == 0
	assert parser.bad_crc_frames == 0


def test_repeated_stx_before_the_id_restarts_the_frame():
	assert ReplyParser().feed(bytes([STX, STX]) + reply_frame(Reply.ACK, 3)[1:], T0) == [
		ReplyFrame(Reply.ACK, 3, b""),
	]


def test_noise_before_a_frame_is_skipped():
	noise = bytes([0xFF, 0x55, 0x00, ETX, BOARD_ID])
	assert ReplyParser().feed(noise + FW_VERSION_REPLY, T0)[0].command == Reply.VERSION


def test_a_partial_frame_older_than_the_timeout_is_abandoned_and_the_next_frame_parses():
	parser = ReplyParser()
	assert parser.feed(FW_STATUS_REPLY[:15], T0) == []
	later = T0 + REPLY_INTER_BYTE_TIMEOUT_S + 0.001
	assert parser.feed(FW_VERSION_REPLY, later) == [ReplyFrame(Reply.VERSION, 0x64, bytes([3, 3, 0, 0, 32]))]
	assert parser.abandoned_frames == 1


def test_without_the_timeout_a_truncated_frame_would_swallow_the_next_one():
	parser = ReplyParser()
	parser.feed(FW_STATUS_REPLY[:15], T0)
	assert parser.feed(FW_VERSION_REPLY, T0 + 0.001) == []


def test_a_long_gap_while_idle_is_not_counted():
	parser = ReplyParser()
	parser.feed(FW_VERSION_REPLY, T0)
	parser.feed(FW_VERSION_REPLY, T0 + 60.0)
	assert parser.abandoned_frames == 0


def test_the_timeout_can_be_set():
	parser = ReplyParser(inter_byte_timeout_s=0.5)
	parser.feed(FW_STATUS_REPLY[:15], T0)
	assert len(parser.feed(FW_STATUS_REPLY[15:], T0 + 0.4)) == 1


# --- SequenceCounter ----------------------------------------------------------

class FixedStart:
	def __init__(self, value: int):
		self.value = value

	def randrange(self, stop: int) -> int:
		assert stop == 256
		return self.value


def test_sequence_counter_starts_where_the_random_source_says():
	assert SequenceCounter(FixedStart(17)).next_sequence() == 17


def test_sequence_counter_counts_up_and_wraps():
	counter = SequenceCounter(FixedStart(254))
	assert [counter.next_sequence() for _ in range(4)] == [254, 255, 0, 1]


def test_sequence_counter_starts_at_a_random_value_by_default():
	starts = {SequenceCounter().next_sequence() for _ in range(64)}
	assert len(starts) > 1
	assert all(0 <= s <= 255 for s in starts)

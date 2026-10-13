"""D10: the Python protocol constants must match the firmware.

Reads the C++ sources directly, so a change on one side without the other fails here instead
of on the wire.
"""

import re
import struct
from pathlib import Path

import pytest

from polydriver.protocol import constants as c

CORE = Path(__file__).resolve().parents[2] / "firmware" / "polydriver" / "lib" / "core"

_SIZE_OF = {"f": 4, "I": 4, "H": 2, "B": 1}
_PUT = {"putFloat": "f", "putU32": "I", "putU16": "H"}
_GET = {"getFloat": "f", "getU32": "I", "getU16": "H"}


def source(name: str) -> str:
	path = CORE / name
	assert path.exists(), f"firmware source not found: {path}"
	text = path.read_text(encoding="utf-8")
	text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
	return re.sub(r"//[^\n]*", "", text)


def number(text: str) -> int:
	return int(text.strip().rstrip("uUlL"), 0)


def cpp_enum(text: str, name: str) -> dict[str, int]:
	match = re.search(rf"enum class {name}\s*:\s*\w+\s*\{{(.*?)\}};", text, re.S)
	assert match, f"enum class {name} not found"
	entries = re.findall(r"(\w+)\s*=\s*(\w+)", match.group(1))
	return {key: number(value) for key, value in entries}


def cpp_namespace_constants(text: str, name: str) -> dict[str, int]:
	match = re.search(rf"namespace {name} \{{(.*?)\n\}}", text, re.S)
	assert match, f"namespace {name} not found"
	entries = re.findall(r"constexpr \w+ (\w+) = (\w+);", match.group(1))
	return {key: number(value) for key, value in entries}


def cpp_constant(text: str, name: str) -> int:
	match = re.search(rf"constexpr \w+ {name} = (\w+);", text)
	assert match, f"constant {name} not found"
	return number(match.group(1))


def python_enum(enum_type) -> dict[str, int]:
	return {member.name: int(member.value) for member in enum_type}


def layout_of(fmt: str) -> list[tuple[int, str]]:
	layout, offset = [], 0
	for code in fmt.lstrip("<"):
		layout.append((offset, code))
		offset += _SIZE_OF[code]
	return layout


def function_body(text: str, signature: str) -> str:
	start = text.index(signature)
	end = text.index("\n}", start)
	return text[start:end]


def written_layout(body: str) -> list[tuple[int, str]]:
	layout = [(int(offset), _PUT[call]) for call, offset in re.findall(r"(put\w+)\(&payload\[(\d+)\]", body)]
	layout += [(int(offset), "B") for offset in re.findall(r"\bpayload\[(\d+)\] =", body)]
	return sorted(layout)


def camel_to_snake(name: str) -> str:
	return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def written_fields(body: str) -> list[str]:
	"""Names of the values written into the payload, in offset order."""
	writes = re.findall(r"put\w+\(&payload\[(\d+)\],\s*([^;]+)\);", body)
	writes += re.findall(r"\bpayload\[(\d+)\] = ([^;]+);", body)
	names = []
	for offset, expression in sorted(writes, key=lambda w: int(w[0])):
		member = re.search(r"report\.(\w+)", expression)
		names.append(camel_to_snake(member.group(1) if member else expression.strip()))
	return names


def read_layout(block: str) -> list[tuple[int, str]]:
	layout = [(int(offset), _GET[call]) for call, offset in re.findall(r"(get\w+)\(&p\[(\d+)\]\)", block)]
	layout += [(int(offset), "B") for offset in re.findall(r"(?<![&\w])p\[(\d+)\]", block)]
	return sorted(layout)


# --- ProtocolDefs.h ---------------------------------------------------------

DEFS = source("ProtocolDefs.h")


@pytest.mark.parametrize("name", [
	"PROTOCOL_VERSION", "BOARD_ID", "STX", "ETX", "FRAME_OVERHEAD",
	"MAX_REQUEST_PAYLOAD", "MAX_REPLY_PAYLOAD", "INTER_BYTE_TIMEOUT_MS",
])
def test_scalar_constant(name):
	assert cpp_constant(DEFS, name) == getattr(c, name)


@pytest.mark.parametrize("cpp_name, python_type", [
	("Command", c.Command),
	("Reply", c.Reply),
	("NackReason", c.NackReason),
	("Role", c.Role),
])
def test_enum(cpp_name, python_type):
	assert cpp_enum(DEFS, cpp_name) == python_enum(python_type)


@pytest.mark.parametrize("cpp_name, python_type", [
	("StatusFlag", c.StatusFlag),
	("SegmentFlag", c.SegmentFlag),
])
def test_flags(cpp_name, python_type):
	assert cpp_namespace_constants(DEFS, cpp_name) == python_enum(python_type)


def test_request_payload_lengths():
	named = cpp_namespace_constants(DEFS, "PayloadLength")
	switch = function_body(DEFS, "inline bool requestPayloadLength")
	explicit = dict(re.findall(r"case Command::(\w+): length = PayloadLength::(\w+);", switch))
	every_case = re.findall(r"case Command::(\w+):", switch)
	firmware = {cmd: named[explicit[cmd]] if cmd in explicit else 0 for cmd in every_case}
	assert firmware == {cmd.name: length for cmd, length in c.REQUEST_PAYLOAD_LENGTH.items()}


def test_reply_payload_lengths():
	named = cpp_namespace_constants(DEFS, "PayloadLength")
	for reply in [c.Reply.NACK, c.Reply.STATUS, c.Reply.SETTINGS, c.Reply.VERSION, c.Reply.ID, c.Reply.JOB_INFO]:
		assert named[reply.name] == c.REPLY_PAYLOAD_LENGTH[reply], reply.name


# --- Values the core classes put on the wire --------------------------------

def test_control_mode_values():
	assert cpp_enum(source("DispenserController.h"), "ControlMode") == python_enum(c.Mode)


def test_job_state_values():
	assert cpp_enum(source("JobProgram.h"), "JobState") == python_enum(c.JobState)


def test_job_program_constants():
	job = source("JobProgram.h")
	assert cpp_constant(job, "FLAG_RAMP") == c.SegmentFlag.RAMP
	assert cpp_constant(job, "FLAG_DUTY") == c.SegmentFlag.DUTY
	assert cpp_constant(job, "NO_SEGMENT") == c.NO_SEGMENT


# --- Payload layouts ----------------------------------------------------------

REPLIES = source("ReplyBuilder.cpp")


@pytest.mark.parametrize("signature, fmt", [
	("void status(", c.STATUS_FORMAT),
	("void settings(", c.SETTINGS_FORMAT),
	("void jobInfo(", c.JOB_INFO_FORMAT),
])
def test_reply_layout(signature, fmt):
	assert written_layout(function_body(REPLIES, signature)) == layout_of(fmt)


@pytest.mark.parametrize("signature, fields", [
	("void status(", c.STATUS_FIELDS),
	("void settings(", c.SETTINGS_FIELDS),
	("void jobInfo(", c.JOB_INFO_FIELDS),
])
def test_reply_field_order(signature, fields):
	assert written_fields(function_body(REPLIES, signature)) == list(fields)


@pytest.mark.parametrize("fmt, fields", [
	(c.STATUS_FORMAT, c.STATUS_FIELDS),
	(c.SETTINGS_FORMAT, c.SETTINGS_FIELDS),
	(c.VERSION_FORMAT, c.VERSION_FIELDS),
	(c.JOB_INFO_FORMAT, c.JOB_INFO_FIELDS),
])
def test_every_field_has_a_format_code(fmt, fields):
	assert len(fmt.lstrip("<")) == len(fields)


DISPATCH = source("CommandDispatcher.cpp")


def dispatcher_case(command: str) -> str:
	start = DISPATCH.index(f"case Command::{command}:")
	following = DISPATCH.find("case Command::", start + 1)
	return DISPATCH[start:following]


@pytest.mark.parametrize("command", [
	cmd for cmd, fmt in c.REQUEST_FORMAT.items() if fmt != "<" and cmd is not c.Command.SET_ID
])
def test_request_layout(command):
	assert read_layout(dispatcher_case(command.name)) == layout_of(c.REQUEST_FORMAT[command])


def test_set_id_reads_one_byte():
	body = function_body(DISPATCH, "void CommandDispatcher::setRole(")
	assert re.findall(r"frame\.payload\[(\d+)\]", body) == ["0"]
	assert c.REQUEST_FORMAT[c.Command.SET_ID] == "<B"


def test_every_layout_size_matches_its_format():
	for fmt in [*c.REQUEST_FORMAT.values(), *c.REPLY_FORMAT.values()]:
		layout = layout_of(fmt)
		end = layout[-1][0] + _SIZE_OF[layout[-1][1]] if layout else 0
		assert end == struct.calcsize(fmt)

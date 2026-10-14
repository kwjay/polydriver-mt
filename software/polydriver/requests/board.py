"""Requests that read the board or set its identity (protocol.md sections 4-5)."""

from dataclasses import dataclass
from typing import ClassVar

from ..protocol.constants import Command, Reply, Role
from .base import Request
from .reports import SettingsReport, StatusReport, VersionReport, decode_role


@dataclass(frozen=True)
class RequestStatus(Request[StatusReport]):
	COMMAND: ClassVar[Command] = Command.REQ_STAT
	REPLY: ClassVar[Reply] = Reply.STATUS

	def decode(self, payload: bytes) -> StatusReport:
		return StatusReport.decode(payload)


@dataclass(frozen=True)
class RequestSettings(Request[SettingsReport]):
	COMMAND: ClassVar[Command] = Command.REQ_SETTINGS
	REPLY: ClassVar[Reply] = Reply.SETTINGS

	def decode(self, payload: bytes) -> SettingsReport:
		return SettingsReport.decode(payload)


@dataclass(frozen=True)
class GetVersion(Request[VersionReport]):
	COMMAND: ClassVar[Command] = Command.GET_VERSION
	REPLY: ClassVar[Reply] = Reply.VERSION

	def decode(self, payload: bytes) -> VersionReport:
		return VersionReport.decode(payload)


@dataclass(frozen=True)
class GetId(Request[Role]):
	COMMAND: ClassVar[Command] = Command.GET_ID
	REPLY: ClassVar[Reply] = Reply.ID

	def decode(self, payload: bytes) -> Role:
		return decode_role(payload)


@dataclass(frozen=True)
class SetId(Request[None]):
	COMMAND: ClassVar[Command] = Command.SET_ID
	role: Role

	def values(self) -> tuple:
		return (int(Role(self.role)),)

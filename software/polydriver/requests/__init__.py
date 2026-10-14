from .base import (
	PayloadFormatError, Request, RequestError, RequestNackError, RequestValueError, UnexpectedReplyError,
)
from .board import GetId, GetVersion, RequestSettings, RequestStatus, SetId
from .control import (
	ClearEstop, Estop, SetCommsTimeout, SetFeedforward, SetFilter, SetMode, SetPid, SetPwm, SetSpeed, Stop,
)
from .job import JobClear, JobCommit, JobRun, JobSegment
from .reports import JobInfo, SettingsReport, StatusReport, VersionReport

__all__ = [
	"PayloadFormatError",
	"Request",
	"RequestError",
	"RequestNackError",
	"RequestValueError",
	"UnexpectedReplyError",
	"GetId",
	"GetVersion",
	"RequestSettings",
	"RequestStatus",
	"SetId",
	"ClearEstop",
	"Estop",
	"SetCommsTimeout",
	"SetFeedforward",
	"SetFilter",
	"SetMode",
	"SetPid",
	"SetPwm",
	"SetSpeed",
	"Stop",
	"JobClear",
	"JobCommit",
	"JobRun",
	"JobSegment",
	"JobInfo",
	"SettingsReport",
	"StatusReport",
	"VersionReport",
	"request_classes",
]


def request_classes() -> list[type[Request]]:
	"""Every concrete request class, one per opcode."""
	found: list[type[Request]] = []
	pending = list(Request.__subclasses__())
	while pending:
		cls = pending.pop()
		pending.extend(cls.__subclasses__())
		if "COMMAND" in vars(cls):
			found.append(cls)
	return found

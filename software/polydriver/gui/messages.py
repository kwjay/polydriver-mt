"""What the window tells the user about an error: the shell's wording (`describe`), or, for a bug,
that it was unexpected, with the traceback in the program log."""

import logging

from ..cli.base import describe
from ..protocol.constants import Role
from ..startup import LOG_FILE_NAME

logger = logging.getLogger(__name__)


def error_text(what: str, error: BaseException, role: Role | None = None) -> str:
	text = describe(error, role)
	if text is None:
		logger.error("%s failed", what, exc_info=(type(error), error, error.__traceback__))
		text = f"unexpected error ({type(error).__name__}: {error}); the details are in {LOG_FILE_NAME}"
	return text


def role_title(role: Role) -> str:
	return role.name.capitalize()

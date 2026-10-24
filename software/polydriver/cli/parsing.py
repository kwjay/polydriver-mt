"""Turning the words of a command into values. Every mistake is a UsageError, which the shell prints
as the command's usage line; it never ends the shell (C2.15)."""

import math
import shlex

from ..protocol.constants import Role

DISPENSERS = (Role.POLYMER, Role.COLOUR)
ROLE_WORDS = {"polymer": Role.POLYMER, "colour": Role.COLOUR, "color": Role.COLOUR}


class UsageError(ValueError):
	"""usage, when given, replaces the command's own usage line (a sub-command's is more precise)."""

	def __init__(self, message: str, usage: str | None = None):
		super().__init__(message)
		self.usage = usage


def split_args(arg: str) -> list[str]:
	"""Splits like a shell but keeps backslashes, so Windows paths survive; quotes group words."""
	lexer = shlex.shlex(arg, posix=True)
	lexer.whitespace_split = True
	lexer.escape = ""
	try:
		return list(lexer)
	except ValueError as error:
		raise UsageError(str(error)) from None


def parse_role(token: str) -> Role:
	"""polymer or colour, or any start of either (p, poly, c, col); color is accepted too."""
	word = token.lower()
	matches = {role for name, role in ROLE_WORDS.items() if word and name.startswith(word)}
	if not matches:
		raise UsageError(f"{token!r} is not a board: polymer or colour")
	return matches.pop()


def is_role(token: str) -> bool:
	try:
		parse_role(token)
	except UsageError:
		return False
	return True


def parse_roles(tokens: list[str]) -> list[Role]:
	"""No tokens means both boards."""
	if not tokens:
		return list(DISPENSERS)
	roles = [parse_role(token) for token in tokens]
	return sorted(set(roles))


def parse_number(token: str, name: str) -> float:
	try:
		value = float(token)
	except ValueError:
		raise UsageError(f"{name} must be a number, not {token!r}") from None
	if not math.isfinite(value):
		raise UsageError(f"{name} must be a finite number, not {token!r}")
	return value


def parse_duty(token: str, name: str) -> int:
	try:
		duty = int(token)
	except ValueError:
		duty = -1
	if not 0 <= duty <= 255:
		raise UsageError(f"{name} must be a whole number from 0 to 255, not {token!r}")
	return duty


def looks_like_number(token: str) -> bool:
	try:
		float(token)
	except ValueError:
		return False
	return True


def role_name(role: Role) -> str:
	return role.name.lower()

"""'Was Enter pressed?' without waiting, for ending `dev watch` and `dev sweep`."""

from typing import TextIO


class KeyWatcher:
	"""Works on Windows consoles and POSIX terminals; never 'pressed' when stdin is not a terminal."""

	def __init__(self, stdin: TextIO):
		self._stdin = stdin

	def pressed(self) -> bool:
		if not self._stdin.isatty():
			return False
		try:
			import msvcrt
		except ImportError:
			import select
			ready, _, _ = select.select([self._stdin], [], [], 0)
			if ready:
				self._stdin.readline()
				return True
			return False
		hit = False
		while msvcrt.kbhit():
			if msvcrt.getwch() in ("\r", "\n"):
				hit = True
		return hit

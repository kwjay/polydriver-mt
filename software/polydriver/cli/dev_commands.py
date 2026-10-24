"""Developer commands (C9.2), all under `dev`: open loop and direct duty, the comms timeout, filter
and feedforward, watching the polls, duty sweeps and link counters. What `dev` sets on a board lasts
until `config reload` or a restart, which send the config again (mode and duty are not in the
config: a restart brings closed loop and duty 0)."""

import csv
import enum
import queue
import statistics
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

from ..config import comms_timeout_problem
from ..devices import BoardStatus
from ..protocol.constants import STATUS_FIELDS, Mode, Role, StatusFlag
from .base import describe
from .board_commands import flag_names, timeout_text
from .parsing import (
	DISPENSERS, UsageError, is_role, looks_like_number, parse_duty, parse_number, parse_role, parse_roles, role_name,
	split_args,
)

WATCH_COLUMNS = ["host_time_s", "role", "device_time_us_unwrapped", *STATUS_FIELDS]
SWEEP_COLUMNS = ["duty", "mean_raw_hz", "stdev_raw_hz", "min_raw_hz", "max_raw_hz", "mean_filtered_hz", "samples",
				"stalled_samples"]
WAIT_S = 0.05
MIN_POLLS_PER_HOLD = 20

DEV_OPTIONS = ("mode", "pwm", "timeout", "filter", "ff", "watch", "sweep", "stats")


class StatusFeed:
	"""Session sink that hands the polls of some boards to the command waiting for them."""

	def __init__(self, roles: list[Role]):
		self._roles = set(roles)
		self._queue: queue.Queue = queue.Queue()

	def record_status(self, role: Role, status: BoardStatus, host_time_s: float) -> None:
		if role in self._roles:
			self._queue.put((role, status, host_time_s))

	def record_event(self, event) -> None:
		pass

	def get(self, timeout_s: float):
		try:
			return self._queue.get(timeout=timeout_s)
		except queue.Empty:
			return None

	def drain(self) -> None:
		while self.get(0) is not None:
			pass


def watch_line(role: Role, status: BoardStatus, elapsed_s: float) -> str:
	r = status.report
	job = f"  job {r.job_state.name}"
	if r.segment_index is not None:
		job += f" seg {r.segment_index} {r.job_elapsed_ms} ms"
	return (f"  {elapsed_s:8.2f} s  {role_name(role):<7}  raw {r.raw_speed_hz:8.2f} Hz  filt {r.filtered_speed_hz:8.2f} Hz"
			f"  duty {r.duty:3d}  [{flag_names(r.flags)}]{job}")


def watch_row(role: Role, status: BoardStatus, elapsed_s: float) -> list:
	values = [getattr(status.report, name) for name in STATUS_FIELDS]
	values = [int(v) if isinstance(v, enum.Enum) else v for v in values]
	return [f"{elapsed_s:.4f}", role_name(role), status.device_time_us, *values]


def sweep_row(duty: int, samples: list[BoardStatus]) -> list:
	"""Mean over the second half of the hold, after the speed has settled."""
	settled = samples[len(samples) // 2:]
	raw = [s.report.raw_speed_hz for s in settled]
	filtered = [s.report.filtered_speed_hz for s in settled]
	stalled = sum(1 for s in settled if StatusFlag.ENCODER_STALLED in s.report.flags)
	stdev = statistics.stdev(raw) if len(raw) > 1 else 0.0
	return [duty, statistics.fmean(raw), stdev, min(raw), max(raw), statistics.fmean(filtered), len(settled), stalled]


@contextmanager
def csv_writer(path: Path | None, columns: list[str]) -> Iterator:
	if path is None:
		with nullcontext() as nothing:
			yield nothing
		return
	with path.open("w", newline="", encoding="utf-8") as file:
		writer = csv.writer(file)
		writer.writerow(columns)
		yield writer


class DevCommands:
	def do_dev(self, arg: str) -> None:
		"""dev mode|pwm|timeout|filter|ff|watch|sweep|stats ...  - developer options, `help dev` explains each

		dev mode ROLE open|closed  - open loop takes `dev pwm`, closed loop takes `speed`; switching stops the motor
		dev pwm ROLE DUTY  - duty 0-255, open loop only
		dev timeout [ROLE] SECONDS|off  - stop the motor when the board hears nothing for SECONDS
		dev filter ROLE ALPHA  - EMA alpha, 0 < ALPHA <= 1 (1 = no filtering)
		dev ff ROLE OFFSET GAIN  - feedforward: duty = OFFSET + GAIN * target + PID (closed loop, target > 0)
		dev watch [ROLE] [SECONDS] [FILE.csv]  - print every poll ([logging] poll_hz) until Enter, or for SECONDS
		dev sweep ROLE FROM TO STEP HOLD_S [FILE.csv]  - open-loop duty steps; mean speed over the second half of each hold
		dev stats [ROLE]  - link counters on the PC and on the board

		What `dev` sets lasts until `config reload` or a board restart. Enter ends watch and sweep; Ctrl+C
		also sends STOP to every board.
		"""
		words = split_args(arg)
		if not words:
			raise UsageError("which option?")
		option = words[0]
		if option not in DEV_OPTIONS:
			raise UsageError(f"{option!r} is not a dev option")
		handler = getattr(self, f"_dev_{option}")
		try:
			handler(words[1:])
		except UsageError as error:
			if error.usage is None:
				error.usage = handler.__doc__.strip().splitlines()[0].split("  - ")[0]
			raise

	# --- Settings ---------------------------------------------------------------

	def _dev_mode(self, words: list[str]) -> None:
		"""dev mode ROLE open|closed"""
		modes = {"open": Mode.OPEN_LOOP, "closed": Mode.CLOSED_LOOP}
		if len(words) != 2 or words[1].lower() not in modes:
			raise UsageError("a board, then open or closed")
		role, mode = parse_role(words[0]), modes[words[1].lower()]

		def set_mode(role: Role) -> None:
			self.session.board(role).set_mode(mode)
			after = "; `dev mode {} closed` or a restart brings closed loop back".format(role_name(role))
			self.say(f"  {role_name(role)}: mode {mode.name} (the motor stopped{after if mode is Mode.OPEN_LOOP else ''})")

		self.each([role], set_mode)

	def _dev_pwm(self, words: list[str]) -> None:
		"""dev pwm ROLE DUTY"""
		if len(words) != 2:
			raise UsageError("a board and a duty")
		role, duty = parse_role(words[0]), parse_duty(words[1], "DUTY")

		def set_pwm(role: Role) -> None:
			self.session.board(role).set_pwm(duty)
			self.say(f"  {role_name(role)}: duty {duty} ({duty / 255 * 100:.1f} %)")

		self.each([role], set_pwm)

	def _dev_timeout(self, words: list[str]) -> None:
		"""dev timeout [ROLE] SECONDS|off"""
		if len(words) not in (1, 2):
			raise UsageError("maybe a board, then seconds or off")
		roles = [parse_role(words[0])] if len(words) == 2 else self.session.roles()
		value = words[-1].lower()
		timeout_s = 0.0 if value == "off" else parse_number(value, "SECONDS")
		problem = comms_timeout_problem(timeout_s, self.config.logging.poll_hz, name="timeout")
		if problem:
			raise UsageError(problem)
		timeout_ms = round(timeout_s * 1000)
		failures = self.each(roles, lambda role: self.session.board(role).set_comms_timeout_ms(timeout_ms))
		done = [role_name(role) for role in roles if role not in failures]
		if done:
			self.say(f"  comms timeout {timeout_text(timeout_ms)} on {', '.join(done)}; `config reload` or a restart "
					f"sends the config's {timeout_text(self.config.comms_timeout_ms)} again")
		elif not roles:
			self.say("  no board connected")

	def _dev_filter(self, words: list[str]) -> None:
		"""dev filter ROLE ALPHA"""
		if len(words) != 2:
			raise UsageError("a board and an alpha")
		role, alpha = parse_role(words[0]), parse_number(words[1], "ALPHA")

		def set_filter(role: Role) -> None:
			self.session.board(role).set_filter(alpha)
			self.say(f"  {role_name(role)}: filter alpha {alpha:g}, until `config reload` or a restart")

		self.each([role], set_filter)

	def _dev_ff(self, words: list[str]) -> None:
		"""dev ff ROLE OFFSET GAIN"""
		if len(words) != 3:
			raise UsageError("a board, an offset and a gain")
		role = parse_role(words[0])
		offset, gain = parse_number(words[1], "OFFSET"), parse_number(words[2], "GAIN")

		def set_ff(role: Role) -> None:
			self.session.board(role).set_feedforward(offset, gain)
			self.say(f"  {role_name(role)}: feedforward {offset:g} duty + {gain:g} duty/Hz, until `config reload` "
					"or a restart")

		self.each([role], set_ff)

	# --- Watching ------------------------------------------------------------------

	@contextmanager
	def status_feed(self, roles: list[Role]) -> Iterator[StatusFeed]:
		feed = StatusFeed(roles)
		self.session.add_sink(feed)
		try:
			yield feed
		finally:
			self.session.remove_sink(feed)

	def _dev_watch(self, words: list[str]) -> None:
		"""dev watch [ROLE] [SECONDS] [FILE.csv]"""
		words = list(words)
		role_words = []
		while words and is_role(words[0]):
			role_words.append(words.pop(0))
		seconds = None
		if words and looks_like_number(words[0]):
			seconds = parse_number(words.pop(0), "SECONDS")
			if seconds <= 0:
				raise UsageError("SECONDS must be > 0")
		path = Path(words.pop(0)) if words else None
		if words:
			raise UsageError("too many words")
		wanted = parse_roles(role_words) if role_words else list(DISPENSERS)
		roles = [role for role in wanted if role in self.session.roles()]
		if not roles:
			self.say("  no board to watch (`connect` first)")
			return
		for role in set(wanted) - set(roles):
			if role_words:
				self.say(f"  {role_name(role)} is not connected")

		names = ", ".join(role_name(role) for role in roles)
		saving = f", saving to {path}" if path else ""
		self.say(f"  watching {names} at {self.config.logging.poll_hz:g} Hz, Enter ends{saving}")
		with self.status_feed(roles) as feed, csv_writer(path, WATCH_COLUMNS) as writer:
			started_s = self.clock()
			while True:
				if seconds is not None and self.clock() - started_s >= seconds:
					break
				if self.keys.pressed():
					self.say("  ended")
					break
				if not any(role in self.session.roles() for role in roles):
					self.say("  no board left to watch")
					break
				item = feed.get(WAIT_S)
				if item is None:
					continue
				role, status, host_time_s = item
				elapsed_s = host_time_s - started_s
				self.say(watch_line(role, status, elapsed_s))
				if writer:
					writer.writerow(watch_row(role, status, elapsed_s))

	# --- Sweep -----------------------------------------------------------------------

	def _dev_sweep(self, words: list[str]) -> None:
		"""dev sweep ROLE FROM TO STEP HOLD_S [FILE.csv]"""
		if len(words) not in (5, 6):
			raise UsageError("a board, FROM TO STEP HOLD_S and maybe a file")
		role = parse_role(words[0])
		start, stop = parse_duty(words[1], "FROM"), parse_duty(words[2], "TO")
		try:
			step = int(words[3])
		except ValueError:
			raise UsageError(f"STEP must be a whole number, not {words[3]!r}") from None
		hold_s = parse_number(words[4], "HOLD_S")
		path = Path(words[5]) if len(words) == 6 else None
		if step == 0 or (stop - start) * step < 0:
			raise UsageError("STEP must be non-zero and point from FROM towards TO")
		if hold_s <= 0:
			raise UsageError("HOLD_S must be > 0")
		duties = list(range(start, stop + (1 if step > 0 else -1), step))
		self.each([role], lambda role: self._sweep(role, duties, hold_s, path))

	def _sweep(self, role: Role, duties: list[int], hold_s: float, path: Path | None) -> None:
		name = role_name(role)
		board = self.session.board(role)
		if board.settings().mode is not Mode.OPEN_LOOP:
			self.say(f"  sweep runs in open loop: `dev mode {name} open` first")
			return
		saving = f", saving to {path}" if path else ""
		self.say(f"  sweeping {name}: {len(duties)} steps of {hold_s:g} s, Enter ends early{saving}")
		polls = hold_s * self.config.logging.poll_hz
		if polls < MIN_POLLS_PER_HOLD:
			self.say(f"  note: {hold_s:g} s at {self.config.logging.poll_hz:g} Hz is about {polls:.0f} polls per step, and the "
					f"mean uses the second half; a hold of {MIN_POLLS_PER_HOLD / self.config.logging.poll_hz:g} s or more "
					"gives a steadier mean")
		self.say(f"  {'duty':>4}  {'mean Hz':>9}  {'stdev':>7}  {'min':>8}  {'max':>8}  stalled")
		with self.status_feed([role]) as feed, csv_writer(path, SWEEP_COLUMNS) as writer:
			try:
				for duty in duties:
					board.set_pwm(duty)
					samples = self._hold(feed, hold_s)
					if samples is None:
						self.say("  ended early")
						break
					if not samples:
						self.say(f"  {duty:4d}  no STATUS arrived during the hold")
						continue
					row = sweep_row(duty, samples)
					self.say(f"  {duty:4d}  {row[1]:9.2f}  {row[2]:7.2f}  {row[3]:8.2f}  {row[4]:8.2f}  {row[7]}/{row[6]}")
					if writer:
						writer.writerow(row)
			finally:
				try:
					board.set_pwm(0)
					self.say("  duty back to 0")
				except Exception as error:
					self.say(f"  duty NOT set back to 0 ({describe(error, role) or error}) - {self._without_stop()}")

	def _hold(self, feed: StatusFeed, hold_s: float) -> list[BoardStatus] | None:
		"""The polls that arrive during hold_s; None when Enter ends the sweep."""
		feed.drain()
		samples = []
		started_s = self.clock()
		while self.clock() - started_s < hold_s:
			if self.keys.pressed():
				return None
			item = feed.get(WAIT_S)
			if item is not None:
				samples.append(item[1])
		return samples

	# --- Counters ---------------------------------------------------------------------

	def _dev_stats(self, words: list[str]) -> None:
		"""dev stats [ROLE]"""
		roles = parse_roles(words) if words else (self.session.roles() or list(DISPENSERS))
		self.each(roles, self._show_stats)

	def _show_stats(self, role: Role) -> None:
		board = self.session.board(role)
		report = self.session.poll_now(role).report
		state = self.session.state(role)
		self.say(f"  {role_name(role)}:")
		counters = getattr(board.link, "stats", None)
		if counters is not None:
			nacks = ", ".join(f"{getattr(k, 'name', k)} {n}" for k, n in counters.nacks.items()) or "none"
			self.say(f"    PC: frames sent {counters.frames_sent}, retries {counters.retries}, reply timeouts "
					f"{counters.reply_timeouts}, late replies {counters.late_replies}")
			self.say(f"    PC: replies with bad CRC {counters.host_bad_crc_frames}, abandoned "
					f"{counters.host_abandoned_frames}; NACKs: {nacks}")
		self.say(f"    board: dropped {report.dropped_frames}, bad CRC {report.bad_crc_frames}")
		self.say(f"    session: polls {state.polls}, missed {state.poll_failures}, restarts {state.device_restarts}")

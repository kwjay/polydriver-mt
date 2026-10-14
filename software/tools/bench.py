"""Bench tool for one board, until the CLI (C9) exists. From software/:

	python tools/bench.py            then: connect COM5
	python tools/bench.py COM5

Ctrl+C at any moment sends STOP. Enter ends `watch` and `sweep`. `help` lists the commands.
"""

import argparse
import cmd
import csv
import enum
import logging
import shlex
import statistics
import sys
import threading
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import TextIO

from polydriver.devices import Board, BoardStatus, DeviceError, Link, Segment
from polydriver.protocol.constants import STATUS_FIELDS, Mode, NackReason, Role, StatusFlag
from polydriver.protocol.serial_worker import LinkError, LinkOpenError
from polydriver.requests import RequestError, RequestNackError
from polydriver.requests.request_queue import RequestTimeoutError

KEEPALIVE_S = 0.5
WATCH_HZ = 10.0
SWEEP_SAMPLE_HZ = 10.0

NACK_HINTS = {
	NackReason.NOT_CONFIGURED: "closed loop needs gains first: `pid KP KI KD` (they are 0 after every reset)",
	NackReason.WRONG_MODE: "`pwm` needs `mode open`, `speed` needs `mode closed`",
	NackReason.WRONG_STATE: "a job is running (`stop`) or ESTOP is latched (`clear`)",
	NackReason.BAD_VALUE: "the board refused the value (range, NaN, negative)",
	NackReason.BAD_JOB: "segment count or total time does not match, or speed and duty segments are mixed",
}

STATUS_COLUMNS = ["host_time_s", "device_time_us_unwrapped", *STATUS_FIELDS]


def flag_names(flags: StatusFlag) -> str:
	names = [flag.name for flag in StatusFlag if flag in flags]
	return ",".join(names) if names else "-"


def status_line(status: BoardStatus, elapsed_s: float) -> str:
	r = status.report
	job = f"  job {r.job_state.name}" + (f" seg {r.segment_index} {r.job_elapsed_ms} ms" if r.segment_index is not None else "")
	return (
		f"{elapsed_s:8.2f} s  raw {r.raw_speed_hz:8.2f} Hz  filt {r.filtered_speed_hz:8.2f} Hz  "
		f"duty {r.duty:3d}  [{flag_names(r.flags)}]{job}"
	)


def status_row(status: BoardStatus, host_time_s: float) -> list:
	r = status.report
	values = [getattr(r, name) for name in STATUS_FIELDS]
	values = [int(v) if isinstance(v, enum.Enum) else v for v in values]
	return [f"{host_time_s:.4f}", status.device_time_us, *values]


class KeyWatcher:
	"""Non-blocking 'was Enter pressed?' for Windows consoles and POSIX terminals."""

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


def command(needs_board: bool = True):
	def decorate(method: Callable) -> Callable:
		@wraps(method)
		def run(self: "Bench", arg: str):
			if needs_board and self.board is None:
				self.say("not connected: `connect PORT` (`ports` lists them)")
				return None
			try:
				return method(self, arg)
			except RequestNackError as error:
				hint = NACK_HINTS.get(error.reason, "")
				self.say(f"refused: {error}" + (f"\n  hint: {hint}" if hint else ""))
			except RequestTimeoutError as error:
				self.say(f"no reply: {error}")
			except LinkOpenError as error:
				self.say(f"{error}  (`ports` lists the ports; is another program holding it open?)")
			except LinkError as error:
				self.say(f"link lost: {error}")
				self.drop_connection()
			except (DeviceError, RequestError) as error:
				self.say(f"error: {error}")
			except (ValueError, IndexError, KeyError) as error:
				self.say(f"usage: {method.__doc__.strip().splitlines()[0]}  ({error})")
			return None
		return run
	return decorate


class Bench(cmd.Cmd):
	prompt = "bench> "

	def __init__(
		self,
		link_factory: Callable[[str], Link] = Link.open,
		stdout: TextIO = sys.stdout,
		stdin: TextIO = sys.stdin,
		keepalive_s: float | None = KEEPALIVE_S,
	):
		super().__init__(stdin=stdin, stdout=stdout)
		self._link_factory = link_factory
		self._keepalive_s = keepalive_s
		self._keys = KeyWatcher(stdin)
		self.board: Board | None = None
		self.link: Link | None = None
		self.job: list[Segment] = []
		self.last_status: BoardStatus | None = None
		self._keepalive_stop = threading.Event()
		self._keepalive: threading.Thread | None = None

	def say(self, text: str) -> None:
		self.stdout.write(text + "\n")
		self.stdout.flush()

	def emptyline(self) -> bool:
		return False

	def default(self, line: str) -> None:
		self.say(f"unknown command: {line.split()[0]}  (`help` lists them)")

	# --- Connection ----------------------------------------------------------------

	@command(needs_board=False)
	def do_ports(self, arg: str) -> None:
		"""ports  - list serial ports"""
		from serial.tools import list_ports
		found = list(list_ports.comports())
		for port in found:
			self.say(f"  {port.device:10} {port.description}")
		if not found:
			self.say("  no serial ports found")

	@command(needs_board=False)
	def do_connect(self, arg: str) -> None:
		"""connect PORT  - open the port (resets an Uno), wait for the firmware, check the protocol"""
		port = shlex.split(arg)[0]
		if self.board is not None:
			self.drop_connection(stop_motor=True)
		self.say(f"opening {port} (the board resets, give it ~2 s) ...")
		self.link = self._link_factory(port)
		self.board = Board(self.link)
		v = self.link.version
		self.say(f"connected: protocol {v.protocol_version}, firmware {v.firmware_major}.{v.firmware_minor}."
				f"{v.firmware_patch}, up to {v.max_segments} job segments, role {self.board.role().name}")
		self._show_status(self.board.status())
		self._start_keepalive()

	@command()
	def do_disconnect(self, arg: str) -> None:
		"""disconnect  - send STOP and close the port"""
		self.drop_connection(stop_motor=True)
		self.say("disconnected")

	def drop_connection(self, stop_motor: bool = False) -> None:
		self._stop_keepalive()
		if self.board is not None and stop_motor:
			try:
				self.board.stop()
			except Exception as error:
				self.say(f"STOP failed: {error}")
		if self.link is not None:
			self.link.close()
		self.board = None
		self.link = None

	def _start_keepalive(self) -> None:
		if self._keepalive_s is None:
			return
		self._keepalive_stop.clear()
		self._keepalive = threading.Thread(target=self._keepalive_loop, name="bench-keepalive", daemon=True)
		self._keepalive.start()

	def _stop_keepalive(self) -> None:
		self._keepalive_stop.set()
		if self._keepalive is not None and self._keepalive is not threading.current_thread():
			self._keepalive.join(2.0)
		self._keepalive = None

	def _keepalive_loop(self) -> None:
		while not self._keepalive_stop.wait(self._keepalive_s):
			board = self.board
			if board is None:
				return
			try:
				self.note(board.status())
			except LinkError as error:
				self.say(f"\n! link lost: {error}  (`connect PORT` again)")
				return
			except RequestError:
				pass

	def note(self, status: BoardStatus) -> None:
		self.last_status = status
		if status.device_restarted:
			self.say("\n! the board restarted: gains, mode, filter, feedforward and timeout are back at boot values")

	# --- Reading ---------------------------------------------------------------------

	@command()
	def do_status(self, arg: str) -> None:
		"""status  - one STATUS, all fields"""
		self._show_status(self.board.status())

	def _show_status(self, status: BoardStatus) -> None:
		self.note(status)
		r = status.report
		self.say(f"  speed raw {r.raw_speed_hz:.2f} Hz, filtered {r.filtered_speed_hz:.2f} Hz, duty {r.duty}")
		self.say(f"  flags {flag_names(r.flags)}")
		self.say(f"  edges {r.edge_count}, device time {status.device_time_us / 1e6:.3f} s")
		self.say(f"  loop: longest {r.longest_loop_interval_us} us since last STATUS, resyncs {r.control_resyncs}")
		self.say(f"  board link: dropped {r.dropped_frames}, bad CRC {r.bad_crc_frames}")
		segment = "-" if r.segment_index is None else r.segment_index
		self.say(f"  job {r.job_state.name}, segment {segment}, elapsed {r.job_elapsed_ms} ms")
		if StatusFlag.UNCONFIGURED in r.flags:
			self.say("  (UNCONFIGURED: closed loop needs `pid` first; open loop works without)")

	@command()
	def do_settings(self, arg: str) -> None:
		"""settings  - what the board is set to"""
		s = self.board.settings()
		self.say(f"  mode {s.mode.name}, duty setpoint {s.duty_setpoint}, target {s.target_speed_hz:.2f} Hz")
		self.say(f"  pid kp {s.kp:g} ki {s.ki:g} kd {s.kd:g}, filter alpha {s.alpha:g}")
		self.say(f"  feedforward offset {s.feedforward_offset_duty:g} duty, gain {s.feedforward_gain_duty_per_hz:g} duty/Hz")
		timeout = "off" if s.comms_timeout_ms == 0 else f"{s.comms_timeout_ms / 1000:g} s"
		self.say(f"  comms timeout {timeout}")

	@command()
	def do_stats(self, arg: str) -> None:
		"""stats  - link counters on the PC side and on the board"""
		c = self.link.stats
		nacks = ", ".join(f"{getattr(k, 'name', k)} {n}" for k, n in c.nacks.items()) or "none"
		self.say(f"  PC: frames sent {c.frames_sent}, retries {c.retries}, reply timeouts {c.reply_timeouts}, "
				f"late replies {c.late_replies}")
		self.say(f"  PC: replies with bad CRC {c.host_bad_crc_frames}, abandoned {c.host_abandoned_frames}; NACKs: {nacks}")
		r = self.board.status().report
		self.say(f"  board: dropped frames {r.dropped_frames}, bad CRC {r.bad_crc_frames}")

	@command()
	def do_watch(self, arg: str) -> None:
		"""watch [HZ] [SECONDS] [FILE.csv]  - print STATUS at HZ (default 10) until Enter, or for SECONDS"""
		args = shlex.split(arg)
		hz = float(args[0]) if args else WATCH_HZ
		duration_s = float(args[1]) if len(args) > 1 and float(args[1]) > 0 else None
		path = Path(args[2]) if len(args) > 2 else None
		if hz <= 0:
			raise ValueError("HZ must be > 0")
		self.say("watching, Enter to end" + (f", saving to {path}" if path else ""))
		with _csv_writer(path, STATUS_COLUMNS) as writer:
			started_s = time.monotonic()
			next_s = started_s
			while True:
				now_s = time.monotonic()
				if (duration_s is not None and now_s - started_s >= duration_s) or self._keys.pressed():
					break
				status = self.board.status()
				self.note(status)
				self.say(status_line(status, now_s - started_s))
				if writer:
					writer.writerow(status_row(status, now_s - started_s))
				next_s += 1.0 / hz
				time.sleep(max(0.0, next_s - time.monotonic()))

	# --- Settings --------------------------------------------------------------------

	@command()
	def do_mode(self, arg: str) -> None:
		"""mode open|closed  - open loop takes `pwm`, closed loop takes `speed` (switching stops the motor)"""
		choice = {"open": Mode.OPEN_LOOP, "closed": Mode.CLOSED_LOOP}[arg.strip().lower()]
		self.board.set_mode(choice)
		self.say(f"  mode {choice.name}")

	@command()
	def do_pid(self, arg: str) -> None:
		"""pid KP KI KD  - set the gains (clears UNCONFIGURED)"""
		kp, ki, kd = (float(x) for x in shlex.split(arg))
		self.board.set_pid(kp, ki, kd)
		self.say(f"  pid {kp:g} {ki:g} {kd:g}")

	@command()
	def do_filter(self, arg: str) -> None:
		"""filter ALPHA  - EMA alpha, 0 < ALPHA <= 1 (1 = no filtering, the boot value)"""
		alpha = float(arg)
		self.board.set_filter(alpha)
		self.say(f"  filter alpha {alpha:g}")

	@command()
	def do_ff(self, arg: str) -> None:
		"""ff OFFSET GAIN  - feedforward: duty = OFFSET + GAIN * target + PID (closed loop, target > 0)"""
		offset, gain = (float(x) for x in shlex.split(arg))
		self.board.set_feedforward(offset, gain)
		self.say(f"  feedforward {offset:g} duty + {gain:g} duty/Hz")

	@command()
	def do_timeout(self, arg: str) -> None:
		"""timeout SECONDS|off  - stop the motor when the board hears nothing for SECONDS"""
		text = arg.strip().lower()
		timeout_ms = 0 if text in ("off", "0") else round(float(text) * 1000)
		self.board.set_comms_timeout_ms(timeout_ms)
		if timeout_ms == 0:
			self.say("  comms timeout off")
		else:
			self.say(f"  comms timeout {timeout_ms} ms (this tool polls every {self._keepalive_s or '-'} s while connected)")

	@command()
	def do_id(self, arg: str) -> None:
		"""id [none|polymer|colour]  - read or store the board's role (stored in EEPROM)"""
		text = arg.strip().lower()
		if text:
			self.board.set_role(Role[text.upper()])
		self.say(f"  role {self.board.role().name}")

	# --- Motor ---------------------------------------------------------------------

	@command()
	def do_pwm(self, arg: str) -> None:
		"""pwm DUTY  - duty 0-255 (open loop only)"""
		duty = int(arg)
		self.board.set_pwm(duty)
		self.say(f"  duty {duty} ({duty / 255 * 100:.1f} %)")

	@command()
	def do_speed(self, arg: str) -> None:
		"""speed HZ  - closed-loop target in encoder Hz"""
		target = float(arg)
		self.board.set_speed(target)
		self.say(f"  target {target:g} Hz")

	@command()
	def do_stop(self, arg: str) -> None:
		"""stop  - target 0, duty 0, a running job is aborted"""
		self.board.stop()
		self.say("  STOP")

	@command()
	def do_estop(self, arg: str) -> None:
		"""estop  - STOP and latch until `clear` (a software latch, not a safety stop)"""
		self.board.estop()
		self.say("  ESTOP latched; `clear` releases it")

	@command()
	def do_clear(self, arg: str) -> None:
		"""clear  - release the ESTOP latch (the motor stays stopped)"""
		self.board.clear_estop()
		self.say("  ESTOP cleared")

	def emergency_stop(self) -> None:
		if self.board is None:
			return
		try:
			self.board.stop()
			self.say("\nCtrl+C: STOP sent")
		except Exception as error:
			self.say(f"\nCtrl+C: STOP FAILED ({error}) - cut the power")

	@command()
	def do_sweep(self, arg: str) -> None:
		"""sweep FROM TO STEP HOLD_S [FILE.csv]  - open-loop duty steps; mean speed over the 2nd half of each hold"""
		args = shlex.split(arg)
		start, stop, step = int(args[0]), int(args[1]), int(args[2])
		hold_s = float(args[3])
		path = Path(args[4]) if len(args) > 4 else None
		if step == 0 or (stop - start) * step < 0:
			raise ValueError("STEP must be non-zero and point from FROM towards TO")
		if hold_s <= 0:
			raise ValueError("HOLD_S must be > 0")
		if self.board.settings().mode is not Mode.OPEN_LOOP:
			self.say("sweep runs in open loop: `mode open` first")
			return
		duties = list(range(start, stop + (1 if step > 0 else -1), step))
		columns = ["duty", "mean_raw_hz", "stdev_raw_hz", "min_raw_hz", "max_raw_hz", "mean_filtered_hz",
				"samples", "stalled_samples"]
		self.say(f"sweeping {len(duties)} steps of {hold_s:g} s, Enter to end early" + (f", saving to {path}" if path else ""))
		self.say(f"  {'duty':>4}  {'mean Hz':>9}  {'stdev':>7}  {'min':>8}  {'max':>8}  stalled")
		try:
			with _csv_writer(path, columns) as writer:
				for duty in duties:
					self.board.set_pwm(duty)
					samples = self._hold(hold_s)
					if samples is None:
						self.say("  ended early")
						break
					row = _summarise(duty, samples)
					self.say(f"  {duty:4d}  {row[1]:9.2f}  {row[2]:7.2f}  {row[3]:8.2f}  {row[4]:8.2f}  {row[7]}/{row[6]}")
					if writer:
						writer.writerow(row)
		finally:
			self.board.set_pwm(0)
			self.say("  duty back to 0")

	def _hold(self, hold_s: float) -> list[BoardStatus] | None:
		samples = []
		started_s = time.monotonic()
		next_s = started_s
		while time.monotonic() - started_s < hold_s:
			if self._keys.pressed():
				return None
			status = self.board.status()
			self.note(status)
			samples.append(status)
			next_s += 1.0 / SWEEP_SAMPLE_HZ
			time.sleep(max(0.0, next_s - time.monotonic()))
		return samples

	# --- Jobs -----------------------------------------------------------------------

	@command()
	def do_job(self, arg: str) -> None:
		"""job new | add [duty] VALUE SECONDS [ramp] | show | upload | run  - a job run by the board"""
		args = shlex.split(arg)
		action = args[0]
		if action == "new":
			self.job = []
			self.say("  job emptied (here; `upload` replaces the board's job)")
		elif action == "add":
			rest = args[1:]
			duty = rest[0] == "duty"
			if duty:
				rest = rest[1:]
			ramp = len(rest) > 2 and rest[2] == "ramp"
			value = float(rest[0])
			duration_ms = round(float(rest[1]) * 1000)
			self.job.append(Segment(value, duration_ms, ramp=ramp, duty=duty))
			self._show_job()
		elif action == "show":
			self._show_job()
		elif action == "upload":
			info = self.board.upload_job(self.job)
			self.say(f"  board: {info.job_state.name}, {info.segment_count} segments, {info.total_ms} ms")
		elif action == "run":
			self.board.run_job()
			self.say("  running (`watch` to follow it, `stop` aborts)")
		else:
			raise ValueError(f"unknown job action {action}")

	def _show_job(self) -> None:
		if not self.job:
			self.say("  (empty)")
		for index, s in enumerate(self.job):
			unit = "duty" if s.duty else "Hz"
			shape = "ramp to" if s.ramp else "step"
			self.say(f"  {index:2d}  {shape:7} {s.value:g} {unit} for {s.duration_ms / 1000:g} s")

	# --- Leaving --------------------------------------------------------------------

	@command(needs_board=False)
	def do_quit(self, arg: str) -> bool:
		"""quit  - send STOP, close the port and leave"""
		if self.board is not None:
			self.drop_connection(stop_motor=True)
		return True

	do_exit = do_quit
	do_EOF = do_quit


class _NoWriter:
	def __enter__(self):
		return None

	def __exit__(self, *exc):
		return False


class _CsvFile:
	def __init__(self, path: Path, columns: list[str]):
		self._path = path
		self._columns = columns

	def __enter__(self):
		self._file = self._path.open("w", newline="", encoding="utf-8")
		writer = csv.writer(self._file)
		writer.writerow(self._columns)
		return writer

	def __exit__(self, *exc):
		self._file.close()
		return False


def _csv_writer(path: Path | None, columns: list[str]):
	return _CsvFile(path, columns) if path else _NoWriter()


def _summarise(duty: int, samples: list[BoardStatus]) -> list:
	settled = samples[len(samples) // 2:] or samples
	raw = [s.report.raw_speed_hz for s in settled]
	filtered = [s.report.filtered_speed_hz for s in settled]
	stalled = sum(1 for s in settled if StatusFlag.ENCODER_STALLED in s.report.flags)
	stdev = statistics.stdev(raw) if len(raw) > 1 else 0.0
	return [duty, statistics.fmean(raw), stdev, min(raw), max(raw), statistics.fmean(filtered), len(settled), stalled]


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="PolydriverMT bench tool (one board)")
	parser.add_argument("port", nargs="?", help="serial port, e.g. COM5")
	args = parser.parse_args(argv)
	logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

	bench = Bench()
	bench.say("PolydriverMT bench. Ctrl+C sends STOP. `help` lists commands, `help CMD` explains one.")
	if args.port:
		bench.onecmd(f"connect {args.port}")
	while True:
		try:
			bench.cmdloop(intro="")
			return 0
		except KeyboardInterrupt:
			bench.emergency_stop()


if __name__ == "__main__":
	sys.exit(main())

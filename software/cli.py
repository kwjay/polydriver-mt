"""Terminal control shell for the Polydriver bus.

A thin interactive layer over control.session.PolydriverSession: it turns
typed commands into session calls and prints the result. This is meant
for manual testing and PID tuning against real hardware without needing
the (future) GUI; a GUI built later talks to the same PolydriverSession.
"""
import argparse
import cmd
import shlex

from control.session import PolydriverSession, SessionError, SessionLike
from control.telemetry_logger import TelemetryLogger


class PolydriverShell(cmd.Cmd):
	intro = "Polydriver control shell. Type help or ? to list commands.\n"
	prompt = "(polydriver) "

	def __init__(self, session: SessionLike, **kwargs):
		super().__init__(**kwargs)
		self.session = session
		self._telemetry_logger: TelemetryLogger | None = None

	# --- connection ---------------------------------------------------------

	def do_connect(self, arg):
		"connect <port> [baudrate]  -- open the serial port"
		parts = shlex.split(arg)
		if not parts:
			print("usage: connect <port> [baudrate]", file=self.stdout)
			return
		port = parts[0]
		baud = int(parts[1]) if len(parts) > 1 else 115200
		try:
			self.session.connect(port, baud)
			print(f"connected to {port} @ {baud}", file=self.stdout)
		except Exception as exc:
			print(f"connect failed: {exc}", file=self.stdout)

	def do_disconnect(self, arg):
		"disconnect  -- close the serial port"
		self.session.disconnect()
		print("disconnected", file=self.stdout)

	# --- target tracking ------------------------------------------------

	def do_add(self, arg):
		"add <target_id> [name]  -- start tracking a dispenser"
		parts = shlex.split(arg)
		if not parts:
			print("usage: add <target_id> [name]", file=self.stdout)
			return
		target_id = int(parts[0], 0)
		name = parts[1] if len(parts) > 1 else None
		state = self.session.add_target(target_id, name)
		print(f"tracking target {state.target_id} ({state.name})", file=self.stdout)

	def _resolve_target_id(self, token: str) -> int:
		"""Accept either a numeric target_id or the friendly name given to
		'add', so a dispenser named on the way in doesn't have to be
		remembered by number everywhere else. A token that parses as an
		integer is always treated as an id first - a target named the same
		as another target's id has to be addressed by its id."""
		try:
			return int(token, 0)
		except ValueError:
			pass
		matches = [state for state in self.session.targets() if state.name == token]
		if len(matches) == 1:
			return matches[0].target_id
		if len(matches) > 1:
			ids = [state.target_id for state in matches]
			raise ValueError(f"name {token!r} is ambiguous - matches targets {ids}")
		raise ValueError(f"no tracked target with id or name {token!r}")

	def do_list(self, arg):
		"list  -- show every tracked dispenser and its last known status"
		targets = self.session.targets()
		if not targets:
			print("no dispensers tracked yet (use 'add')", file=self.stdout)
			return
		for state in targets:
			status = state.last_status
			target_speed = (
				"none commanded yet" if state.last_commanded_speed is None else state.last_commanded_speed
			)
			if status is None:
				print(
					f"{state.target_id} ({state.name}): no status yet (target_speed={target_speed})",
					file=self.stdout,
				)
			else:
				print(
					f"{state.target_id} ({state.name}): "
					f"target_speed={target_speed} frequency={status.frequency:.2f} pwm={status.pwm} "
					f"stalled={status.is_stalled}",
					file=self.stdout,
				)
			if state.last_error:
				print(f"    last error: {state.last_error}", file=self.stdout)

	# --- commands ------------------------------------------------------------

	def do_speed(self, arg):
		"speed <target_id|name> <value>  -- set a dispenser's target speed"
		parts = shlex.split(arg)
		if len(parts) != 2:
			print("usage: speed <target_id|name> <value>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			self.session.set_speed(target_id, float(parts[1]))
			print("ok", file=self.stdout)
		except (SessionError, ValueError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_pid(self, arg):
		"pid <target_id|name> <kp> <ki> <kd>  -- set a dispenser's PID tunings"
		parts = shlex.split(arg)
		if len(parts) != 4:
			print("usage: pid <target_id|name> <kp> <ki> <kd>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			kp, ki, kd = (float(x) for x in parts[1:])
			self.session.set_pid(target_id, kp, ki, kd)
			print("ok", file=self.stdout)
		except (SessionError, ValueError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_status(self, arg):
		"status <target_id|name>  -- request fresh telemetry right now"
		parts = shlex.split(arg)
		if len(parts) != 1:
			print("usage: status <target_id|name>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			report = self.session.request_status(target_id)
			print(
				f"frequency={report.frequency:.2f} pwm={report.pwm} stalled={report.is_stalled}",
				file=self.stdout,
			)
		except (SessionError, ValueError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_settings(self, arg):
		"settings <target_id|name>  -- read back a dispenser's active PID + speed"
		parts = shlex.split(arg)
		if len(parts) != 1:
			print("usage: settings <target_id|name>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			report = self.session.request_settings(target_id)
			print(
				f"kp={report.kp} ki={report.ki} kd={report.kd} speed={report.speed}",
				file=self.stdout,
			)
		except (SessionError, ValueError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	# --- telemetry logging -----------------------------------------------

	def do_log(self, arg):
		"log start <path> | log stop  -- record polled telemetry to a CSV file"
		parts = shlex.split(arg)
		if not parts:
			print("usage: log start <path> | log stop", file=self.stdout)
			return
		if parts[0] == "start":
			if len(parts) != 2:
				print("usage: log start <path>", file=self.stdout)
				return
			try:
				self._telemetry_logger = TelemetryLogger(parts[1])
			except OSError as exc:
				print(f"failed to open log file: {exc}", file=self.stdout)
				return
			self.session.attach_telemetry_sink(self._telemetry_logger)
			print(f"logging telemetry to {parts[1]}", file=self.stdout)
		elif parts[0] == "stop":
			self.session.detach_telemetry_sink()
			if self._telemetry_logger is not None:
				self._telemetry_logger.close()
				self._telemetry_logger = None
			print("logging stopped", file=self.stdout)
		else:
			print("usage: log start <path> | log stop", file=self.stdout)

	# --- exit ----------------------------------------------------------------

	def do_quit(self, arg):
		"quit  -- disconnect and exit"
		return self._shutdown()

	do_exit = do_quit
	do_EOF = do_quit

	def _shutdown(self):
		if self._telemetry_logger is not None:
			self._telemetry_logger.close()
			self._telemetry_logger = None
		self.session.disconnect()
		print("bye", file=self.stdout)
		return True


def main():
	parser = argparse.ArgumentParser(description="Polydriver terminal control")
	parser.add_argument("--port", help="serial port to connect to immediately")
	parser.add_argument("--baud", type=int, default=115200)
	args = parser.parse_args()

	session = PolydriverSession()
	shell = PolydriverShell(session)
	if args.port:
		shell.onecmd(f"connect {args.port} {args.baud}")
	try:
		shell.cmdloop()
	except KeyboardInterrupt:
		shell._shutdown()


if __name__ == "__main__":
	main()

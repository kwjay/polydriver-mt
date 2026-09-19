import argparse
import cmd
import shlex

from control.calibration import CalibrationError, CalibrationStore
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

	# --- timed runs & flow-rate calibration -------------------------------

	def do_run(self, arg):
		"run <target_id|name> <speed> <duration_s>  -- run at a fixed speed, then auto-stop"
		parts = shlex.split(arg)
		if len(parts) != 3:
			print("usage: run <target_id|name> <speed> <duration_s>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			speed = float(parts[1])
			duration_s = float(parts[2])
			self.session.run_for(target_id, duration_s, speed=speed)
			print(f"running at speed={speed} for {duration_s}s, will auto-stop", file=self.stdout)
		except (SessionError, ValueError, CalibrationError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_dose(self, arg):
		"dose <target_id|name> <rate_g_s> <duration_s>  -- run at a calibrated flow rate, then auto-stop"
		parts = shlex.split(arg)
		if len(parts) != 3:
			print("usage: dose <target_id|name> <rate_g_s> <duration_s>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			rate_g_s = float(parts[1])
			duration_s = float(parts[2])
			resolved_speed = self.session.run_for(target_id, duration_s, rate_g_s=rate_g_s)
			print(
				f"dosing at {rate_g_s}g/s (speed={resolved_speed:.3f}) for {duration_s}s, will auto-stop",
				file=self.stdout,
			)
		except (SessionError, ValueError, CalibrationError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_rate(self, arg):
		"rate <target_id|name> <rate_g_s>  -- set speed via calibration to hit a flow rate (runs until stopped)"
		parts = shlex.split(arg)
		if len(parts) != 2:
			print("usage: rate <target_id|name> <rate_g_s>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			rate_g_s = float(parts[1])
			resolved_speed = self.session.set_rate(target_id, rate_g_s)
			print(f"ok (speed={resolved_speed:.3f})", file=self.stdout)
		except (SessionError, ValueError, CalibrationError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_stop(self, arg):
		"stop <target_id|name>  -- cancel any timed run and immediately set speed to 0"
		parts = shlex.split(arg)
		if len(parts) != 1:
			print("usage: stop <target_id|name>", file=self.stdout)
			return
		try:
			target_id = self._resolve_target_id(parts[0])
			self.session.stop_run(target_id)
			self.session.set_speed(target_id, 0.0)
			print("ok", file=self.stdout)
		except (SessionError, ValueError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	def do_calibrate(self, arg):
		"""calibrate add <target_id|name> <speed> <duration_s> <grams>
		calibrate show <target_id|name>
		calibrate save <path>
		calibrate load <path>"""
		parts = shlex.split(arg)
		usage = (
			"usage: calibrate add <target_id|name> <speed> <duration_s> <grams> "
			"| calibrate show <target_id|name> | calibrate save <path> | calibrate load <path>"
		)
		if not parts:
			print(usage, file=self.stdout)
			return
		subcmd, rest = parts[0], parts[1:]
		try:
			if subcmd == "add":
				if len(rest) != 4:
					print("usage: calibrate add <target_id|name> <speed> <duration_s> <grams>", file=self.stdout)
					return
				target_id = self._resolve_target_id(rest[0])
				speed, duration_s, grams = float(rest[1]), float(rest[2]), float(rest[3])
				point = self.session.record_calibration_point(target_id, speed, grams, duration_s)
				detail = f"recorded speed={speed} -> {point.rate_g_s:.4f} g/s"
				if point.repeats > 1:
					detail += f" (avg of {point.repeats}, spread {point.spread_g_s:.4f} g/s)"
				print(detail, file=self.stdout)
			elif subcmd == "show":
				if len(rest) != 1:
					print("usage: calibrate show <target_id|name>", file=self.stdout)
					return
				target_id = self._resolve_target_id(rest[0])
				points = self.session.calibration_points(target_id)
				if not points:
					print(f"no calibration points recorded for target {target_id}", file=self.stdout)
					return
				for point in points:
					line = f"  speed={point.speed} -> {point.rate_g_s:.4f} g/s"
					if point.repeats > 1:
						line += f" (avg of {point.repeats}, spread {point.spread_g_s:.4f} g/s)"
					print(line, file=self.stdout)
				deadband = self.session.calibration_deadband(target_id)
				if deadband is not None:
					print(f"lowest speed known to produce output: {deadband}", file=self.stdout)
			elif subcmd == "save":
				if len(rest) != 1:
					print("usage: calibrate save <path>", file=self.stdout)
					return
				self.session.save_calibration(rest[0])
				print(f"saved calibration to {rest[0]}", file=self.stdout)
			elif subcmd == "load":
				if len(rest) != 1:
					print("usage: calibrate load <path>", file=self.stdout)
					return
				self.session.load_calibration(rest[0])
				print(f"loaded calibration from {rest[0]}", file=self.stdout)
			else:
				print(usage, file=self.stdout)
		except (SessionError, ValueError, CalibrationError) as exc:
			print(f"failed: {exc}", file=self.stdout)

	# --- telemetry logging -----------------------------------------------

	def do_log(self, arg):
		"log start <path> | log stop  -- record polled telemetry to a CSV file"
		arg = arg.strip()
		if not arg:
			print("usage: log start <path> | log stop", file=self.stdout)
			return
		subcmd, _, rest = arg.partition(" ")
		rest = rest.strip()
		if subcmd == "start":
			if not rest:
				print("usage: log start <path>", file=self.stdout)
				return
			path = rest
			if len(path) >= 2 and path[0] == path[-1] and path[0] in ("'", '"'):
				path = path[1:-1]
			try:
				self._telemetry_logger = TelemetryLogger(path)
			except OSError as exc:
				print(f"failed to open log file: {exc}", file=self.stdout)
				return
			self.session.attach_telemetry_sink(self._telemetry_logger)
			print(f"logging telemetry to {path}", file=self.stdout)
		elif subcmd == "stop":
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

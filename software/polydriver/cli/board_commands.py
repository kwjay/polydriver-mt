"""Board commands (C9.1): connecting, reading, driving, the config. ROLE is polymer or colour, or
any start of either; where ROLE is optional, leaving it out means both boards."""

from pathlib import Path

from ..config import BoardSettings, ConfigError, load_config, write_default_config
from ..devices import BoardState, BoardStatus, JobRunningError, NotConnectedError, SettingsApplyError
from ..protocol.constants import Role, StatusFlag
from ..requests import SettingsReport
from .parsing import DISPENSERS, UsageError, is_role, parse_number, parse_role, parse_roles, role_name, split_args


def flag_names(flags: StatusFlag) -> str:
	names = [flag.name for flag in StatusFlag if flag in flags]
	return ",".join(names) if names else "-"


def timeout_text(timeout_ms: int) -> str:
	return "off" if timeout_ms == 0 else f"{timeout_ms / 1000:g} s"


def settings_text(s: SettingsReport) -> str:
	return (f"pid {s.kp:g} {s.ki:g} {s.kd:g}, alpha {s.alpha:g}, ff {s.feedforward_offset_duty:g} + "
			f"{s.feedforward_gain_duty_per_hz:g}/Hz, timeout {timeout_text(s.comms_timeout_ms)}")


def board_settings_text(b: BoardSettings) -> str:
	gains = f"pid {b.kp:g} {b.ki:g} {b.kd:g}" if b.has_gains else "no gains"
	return f"{gains}, alpha {b.alpha:g}, ff {b.ff_offset_duty:g} + {b.ff_gain_duty_per_hz:g}/Hz"


def firmware_text(state: BoardState) -> str:
	v = state.version
	return f"firmware {v.firmware_major}.{v.firmware_minor}.{v.firmware_patch}"


def status_summary(status: BoardStatus) -> str:
	r = status.report
	return (f"{r.raw_speed_hz:.2f} Hz (filtered {r.filtered_speed_hz:.2f})  duty {r.duty}  [{flag_names(r.flags)}]  "
			f"job {r.job_state.name}")


class BoardCommands:
	# --- Connecting -------------------------------------------------------------

	def do_ports(self, arg: str) -> None:
		"""ports  - list the serial ports and which board the config puts on each"""
		from serial.tools import list_ports
		found = sorted(list_ports.comports(), key=lambda port: port.device)
		used = {port: role_name(role) for role, port in self.config.ports.items() if port}
		for port in found:
			owner = f"  ({used[port.device]} in the config)" if port.device in used else ""
			self.say(f"  {port.device:12} {port.description}{owner}")
		if not found:
			self.say("  no serial ports found")

	def do_connect(self, arg: str) -> None:
		"""connect [ROLE] [PORT]  - open a board (port from the config unless given), check its role, send its settings"""
		words = split_args(arg)
		roles = [parse_role(word) for word in words if is_role(word)]
		ports = [word for word in words if not is_role(word)]
		if len(roles) > 1 or len(ports) > 1:
			raise UsageError("one board and at most one port")
		if ports and not roles:
			raise UsageError(f"say which board is on {ports[0]}: connect ROLE {ports[0]}")
		port = ports[0] if ports else None
		self.each(roles or DISPENSERS, lambda role: self._connect(role, port))

	def _connect(self, role: Role, port: str | None) -> None:
		name = role_name(role)
		shown_port = port or self.config.ports.get(role) or "?"
		self.say(f"  {name}: opening {shown_port} (an Uno resets on open, about 2 s) ...")
		state = self.session.connect(role, port)
		self.say(f"  {name}: connected on {state.port}, {firmware_text(state)} (protocol "
				f"{state.version.protocol_version}), settings applied: {settings_text(state.settings)}")
		if not self.config.boards[role].has_gains:
			self.say(f"  {name}: no gains in polydriver.toml, closed loop is refused until [board.{name}] has "
					f"kp, ki, kd or `pid {name} KP KI KD`")

	def do_disconnect(self, arg: str) -> None:
		"""disconnect [ROLE]  - send STOP and close the port"""
		words = split_args(arg)
		roles = parse_roles(words)

		def disconnect(role: Role) -> None:
			try:
				self.session.disconnect(role)
			except NotConnectedError:
				if words:
					raise
				return
			self.say(f"  {role_name(role)}: disconnected")

		self.each(roles, disconnect)

	def do_id(self, arg: str) -> None:
		"""id ROLE  - store ROLE in the EEPROM of the board connected as ROLE, so a swapped port is caught"""
		words = split_args(arg)
		if len(words) != 1:
			raise UsageError("one board")
		role = parse_role(words[0])

		def store(role: Role) -> None:
			self.session.board(role).set_role(role)
			self.say(f"  stored {role_name(role)} on {self.session.state(role).port}")

		self.each([role], store)

	# --- Reading ---------------------------------------------------------------

	def do_list(self, arg: str) -> None:
		"""list  - both boards: port, firmware, last status, polls"""
		for role in DISPENSERS:
			name = role_name(role)
			try:
				state = self.session.state(role)
			except NotConnectedError:
				port = self.config.ports.get(role)
				where = f"{port} in the config" if port else "no port in the config"
				self.say(f"  {name:<8} not connected ({where})")
				continue
			if not state.connected:
				self.say(f"  {name:<8} {state.port}  link lost: {state.last_error}; `connect {name}` once it is back")
				continue
			status = status_summary(state.last_status) if state.last_status else "no status yet"
			polls = f"polls {state.polls}, missed {state.poll_failures}"
			line = f"  {name:<8} {state.port}  {firmware_text(state)}  {status}  {polls}"
			if state.device_restarts:
				line += f", restarts {state.device_restarts}"
			self.say(line)
			if state.poll_failures and state.last_error:
				self.say(f"           last error: {state.last_error}")

	def do_status(self, arg: str) -> None:
		"""status [ROLE]  - poll now and show every STATUS field"""
		words = split_args(arg)
		roles = parse_roles(words) if words else self._connected_or_both()
		self.each(roles, self._show_status)

	def _show_status(self, role: Role) -> None:
		status = self.session.poll_now(role)
		r = status.report
		segment = "-" if r.segment_index is None else r.segment_index
		self.say(f"  {role_name(role)}:")
		self.say(f"    speed raw {r.raw_speed_hz:.2f} Hz, filtered {r.filtered_speed_hz:.2f} Hz, duty {r.duty}")
		self.say(f"    flags {flag_names(r.flags)}")
		self.say(f"    edges {r.edge_count}, device time {status.device_time_us / 1e6:.3f} s")
		self.say(f"    loop: longest {r.longest_loop_interval_us} us since the last STATUS, resyncs {r.control_resyncs}")
		self.say(f"    board link: dropped {r.dropped_frames}, bad CRC {r.bad_crc_frames}")
		self.say(f"    job {r.job_state.name}, segment {segment}, elapsed {r.job_elapsed_ms} ms")

	def do_settings(self, arg: str) -> None:
		"""settings [ROLE]  - what the board is set to (read from the board)"""
		words = split_args(arg)
		roles = parse_roles(words) if words else self._connected_or_both()

		def show(role: Role) -> None:
			s = self.session.board(role).settings()
			self.say(f"  {role_name(role)}:")
			self.say(f"    mode {s.mode.name}, target {s.target_speed_hz:g} Hz, duty setpoint {s.duty_setpoint}")
			self.say(f"    pid kp {s.kp:g} ki {s.ki:g} kd {s.kd:g}, filter alpha {s.alpha:g}")
			self.say(f"    feedforward {s.feedforward_offset_duty:g} duty + {s.feedforward_gain_duty_per_hz:g} duty/Hz")
			self.say(f"    comms timeout {timeout_text(s.comms_timeout_ms)}")

		self.each(roles, show)

	def _connected_or_both(self) -> list[Role]:
		return self.session.roles() or list(DISPENSERS)

	# --- Driving ---------------------------------------------------------------

	def do_speed(self, arg: str) -> None:
		"""speed ROLE HZ  - closed-loop target in encoder Hz (0 stops)"""
		words = split_args(arg)
		if len(words) != 2:
			raise UsageError("a board and a speed")
		role, target_hz = parse_role(words[0]), parse_number(words[1], "HZ")

		def set_speed(role: Role) -> None:
			self.session.board(role).set_speed(target_hz)
			self.say(f"  {role_name(role)}: target {target_hz:g} Hz")

		self.each([role], set_speed)

	def do_pid(self, arg: str) -> None:
		"""pid ROLE KP KI KD  - set the gains on the board (the config is not changed)"""
		words = split_args(arg)
		if len(words) != 4:
			raise UsageError("a board and three gains")
		role = parse_role(words[0])
		kp, ki, kd = (parse_number(word, name) for word, name in zip(words[1:], ("KP", "KI", "KD")))

		def set_pid(role: Role) -> None:
			self.session.board(role).set_pid(kp, ki, kd)
			if self.config.boards[role].has_gains:
				kept = "until the next `config reload` or board restart, which send the config's gains"
			else:
				kept = "until the board restarts (the config has no gains for it)"
			self.say(f"  {role_name(role)}: pid {kp:g} {ki:g} {kd:g}, kept {kept}")

		self.each([role], set_pid)

	def do_stop(self, arg: str) -> None:
		"""stop [ROLE]  - target 0, duty 0, a running job is aborted"""
		words = split_args(arg)
		if not words:
			self.stop_every_board()
			return
		roles = parse_roles(words)

		def stop(role: Role) -> None:
			self.session.board(role).stop()
			self.say(f"  {role_name(role)}: STOP")

		self.each(roles, stop)

	def do_estop(self, arg: str) -> None:
		"""estop [ROLE]  - STOP and latch until `clear` (a software latch, not a safety stop)"""
		words = split_args(arg)
		roles = parse_roles(words) if words else self.session.roles()
		failures = self.each(roles, lambda role: self.session.board(role).estop())
		latched = [role_name(role) for role in roles if role not in failures]
		if latched:
			self.say(f"  ESTOP latched on {', '.join(latched)}; `clear` releases it")
		elif not roles:
			self.say("  no board connected")

	def do_clear(self, arg: str) -> None:
		"""clear [ROLE]  - release the ESTOP latch (the motor stays stopped)"""
		words = split_args(arg)
		roles = parse_roles(words) if words else self.session.roles()

		def clear(role: Role) -> None:
			self.session.board(role).clear_estop()
			self.say(f"  {role_name(role)}: ESTOP cleared")

		self.each(roles, clear)

	# --- Config ----------------------------------------------------------------

	def do_config(self, arg: str) -> None:
		"""config [reload | new [FILE]]  - show the config, read it again and send it to the boards, or write a template"""
		words = split_args(arg)
		if not words:
			self._show_config()
		elif words == ["reload"]:
			self._reload_config()
		elif words[0] == "new" and len(words) <= 2:
			path = Path(words[1]) if len(words) == 2 else self.config_path
			try:
				write_default_config(path)
			except FileExistsError:
				self.say(f"  {path} already exists; edit it, or remove it first")
				return
			self.say(f"  wrote {path} with every setting and its default; edit it, then `config reload`")
		else:
			raise UsageError("reload, or new [FILE]")

	def _show_config(self) -> None:
		c = self.config
		found = "" if self.config_path.exists() else " (not found, so the defaults)"
		self.say(f"  config {self.config_path}{found}")
		for role in DISPENSERS:
			port = c.ports.get(role) or "not set"
			self.say(f"  {role_name(role)}: port {port}, {board_settings_text(c.boards[role])}")
		self.say(f"  comms timeout {timeout_text(c.comms_timeout_ms)}, poll {c.logging.poll_hz:g} Hz, "
				f"logs in {c.resolve(c.logging.folder)}")

	def _reload_config(self) -> None:
		try:
			config = load_config(self.config_path)
		except ConfigError as error:
			self.say(f"  {error}\n  still using the previous config")
			return
		roles = self.session.roles()
		try:
			self.session.reload_config(config)
		except JobRunningError as error:
			self.say(f"  {error}\n  still using the previous config")
			return
		except SettingsApplyError as error:
			for role, failure in error.failures.items():
				self.say(f"  {role_name(role)}: settings not applied: {failure}")
		self.config = config
		sent = ", ".join(role_name(role) for role in roles) or "no board connected"
		self.say(f"  reloaded {self.config_path}; settings sent to: {sent}")

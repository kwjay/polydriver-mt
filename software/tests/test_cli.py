import io
import unittest

from cli import PolydriverShell
from control.session import DispenserState, SessionError
from jobs.jobs import SettingsReport, StatusReport


class _FakeSession:
	"""Stands in for PolydriverSession: test_session.py already covers the
	real session's behaviour, this only checks that the shell parses
	arguments correctly and reports success/failure the right way."""

	def __init__(self):
		self._targets: dict[int, DispenserState] = {}
		self.speed_calls = []
		self.pid_calls = []
		self.next_status = StatusReport(frequency=1.0, pwm=2, is_stalled=False)
		self.next_settings = SettingsReport(kp=1.0, ki=2.0, kd=3.0, speed=4.0)
		self.raise_on_command: Exception | None = None
		self.sink = None

	def connect(self, port: str, baudrate: int = 115200):
		pass

	def disconnect(self):
		pass

	def add_target(self, target_id, name=None):
		state = self._targets.get(target_id)
		if state is None:
			state = DispenserState(target_id=target_id, name=name or f"driver-{target_id}")
			self._targets[target_id] = state
		return state

	def targets(self):
		return list(self._targets.values())

	def set_speed(self, target_id, speed):
		if self.raise_on_command:
			raise self.raise_on_command
		self.speed_calls.append((target_id, speed))
		self.add_target(target_id).last_commanded_speed = speed

	def set_pid(self, target_id, kp, ki, kd):
		if self.raise_on_command:
			raise self.raise_on_command
		self.pid_calls.append((target_id, kp, ki, kd))

	def request_status(self, target_id):
		if self.raise_on_command:
			raise self.raise_on_command
		return self.next_status

	def request_settings(self, target_id):
		if self.raise_on_command:
			raise self.raise_on_command
		return self.next_settings

	def attach_telemetry_sink(self, sink):
		self.sink = sink

	def detach_telemetry_sink(self):
		self.sink = None


def make_shell():
	session = _FakeSession()
	stdout = io.StringIO()
	shell = PolydriverShell(session, stdout=stdout)
	return shell, session, stdout


class TestPolydriverShell(unittest.TestCase):
	def test_speed_valid_args_calls_session(self):
		shell, session, out = make_shell()
		shell.onecmd("speed 1 12.5")
		self.assertEqual(session.speed_calls, [(1, 12.5)])
		self.assertIn("ok", out.getvalue())

	def test_speed_wrong_arg_count_prints_usage_and_does_not_call_session(self):
		shell, session, out = make_shell()
		shell.onecmd("speed 1")
		self.assertEqual(session.speed_calls, [])
		self.assertIn("usage:", out.getvalue())

	def test_speed_session_error_is_reported_not_raised(self):
		shell, session, out = make_shell()
		session.raise_on_command = SessionError("no response")
		shell.onecmd("speed 1 12.5")
		self.assertIn("failed:", out.getvalue())
		self.assertIn("no response", out.getvalue())

	def test_pid_valid_args_calls_session(self):
		shell, session, out = make_shell()
		shell.onecmd("pid 2 1.0 0.5 0.1")
		self.assertEqual(session.pid_calls, [(2, 1.0, 0.5, 0.1)])
		self.assertIn("ok", out.getvalue())

	def test_speed_accepts_the_target_name_given_to_add_instead_of_its_id(self):
		shell, session, out = make_shell()
		shell.onecmd("add 9 pump-a")
		shell.onecmd("speed pump-a 12.5")
		self.assertEqual(session.speed_calls, [(9, 12.5)])
		self.assertIn("ok", out.getvalue())

	def test_pid_status_and_settings_also_accept_a_target_name(self):
		shell, session, out = make_shell()
		shell.onecmd("add 9 pump-a")

		shell.onecmd("pid pump-a 1.0 0.5 0.1")
		self.assertEqual(session.pid_calls, [(9, 1.0, 0.5, 0.1)])

		out.truncate(0)
		out.seek(0)
		shell.onecmd("status pump-a")
		self.assertIn("frequency=1.00", out.getvalue())

		out.truncate(0)
		out.seek(0)
		shell.onecmd("settings pump-a")
		self.assertIn("kp=1.0", out.getvalue())

	def test_a_numeric_token_is_always_treated_as_an_id_not_a_name(self):
		# add_target(9, "1") names target 9 "1" - a later "speed 1 ..." must
		# still mean "the id 1", not "the target named 1".
		shell, session, out = make_shell()
		session.add_target(9, "1")
		shell.onecmd("speed 1 12.5")
		self.assertEqual(session.speed_calls, [(1, 12.5)])

	def test_an_unknown_target_name_fails_cleanly_instead_of_raising(self):
		shell, session, out = make_shell()
		shell.onecmd("speed nope 12.5")
		self.assertEqual(session.speed_calls, [])
		self.assertIn("failed:", out.getvalue())
		self.assertIn("no tracked target", out.getvalue())

	def test_an_ambiguous_target_name_fails_cleanly_instead_of_guessing(self):
		shell, session, out = make_shell()
		session.add_target(1, "left")
		session.add_target(2, "left")
		shell.onecmd("speed left 12.5")
		self.assertEqual(session.speed_calls, [])
		self.assertIn("ambiguous", out.getvalue())

	def test_status_prints_report_fields(self):
		shell, session, out = make_shell()
		shell.onecmd("status 1")
		text = out.getvalue()
		# frequency/pwm/stalled from _FakeSession.next_status
		self.assertIn("frequency=1.00", text)
		self.assertIn("pwm=2", text)
		self.assertIn("stalled=False", text)

	def test_settings_prints_report_fields(self):
		shell, session, out = make_shell()
		shell.onecmd("settings 1")
		text = out.getvalue()
		self.assertIn("kp=1.0", text)
		self.assertIn("speed=4.0", text)

	def test_add_and_list(self):
		shell, session, out = make_shell()
		shell.onecmd("add 9 pump-a")
		shell.onecmd("list")
		text = out.getvalue()
		self.assertIn("tracking target 9 (pump-a)", text)
		self.assertIn("9 (pump-a): no status yet", text)

	def test_list_with_no_targets(self):
		shell, session, out = make_shell()
		shell.onecmd("list")
		self.assertIn("no dispensers tracked yet", out.getvalue())

	def test_list_shows_the_commanded_target_speed_after_a_speed_command(self):
		shell, session, out = make_shell()
		shell.onecmd("speed 9 12.5")
		out.truncate(0)
		out.seek(0)
		shell.onecmd("list")
		self.assertIn("target_speed=12.5", out.getvalue())

	def test_quit_returns_true_and_disconnects(self):
		shell, session, out = make_shell()
		result = shell.onecmd("quit")
		self.assertTrue(result)
		self.assertIn("bye", out.getvalue())

	def test_log_start_creates_missing_directories_and_attaches_the_sink(self):
		import tempfile
		import shutil
		import os

		shell, session, out = make_shell()
		tmp_dir = tempfile.mkdtemp()
		self.addCleanup(lambda: shutil.rmtree(tmp_dir, ignore_errors=True))
		path = os.path.join(tmp_dir, "nested", "run.csv")

		shell.onecmd(f"log start {path}")
		self.assertIn(f"logging telemetry to {path}", out.getvalue())
		self.assertIsNotNone(session.sink)
		self.assertTrue(os.path.exists(path))

		shell.onecmd("log stop")
		self.assertIsNone(session.sink)

	def test_log_start_with_an_unusable_path_fails_cleanly(self):
		import tempfile
		import os

		shell, session, out = make_shell()
		# a regular file can't be used as a directory component of a path
		fd, blocking_file = tempfile.mkstemp()
		os.close(fd)
		self.addCleanup(lambda: os.path.exists(blocking_file) and os.remove(blocking_file))

		bad_path = os.path.join(blocking_file, "sub", "run.csv")
		shell.onecmd(f"log start {bad_path}")
		self.assertIn("failed to open log file", out.getvalue())


if __name__ == "__main__":
	unittest.main()

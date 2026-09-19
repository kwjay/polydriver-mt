import io
import os
import unittest

from cli import PolydriverShell
from control.calibration import CalibrationError
from control.session import DispenserState, SessionError
from jobs.jobs import SettingsReport, StatusReport


TEST_LOGS_DIR = os.path.join(os.path.dirname(__file__), "test_logs")


class _FakeSession:

	def __init__(self):
		self._targets: dict[int, DispenserState] = {}
		self.speed_calls = []
		self.pid_calls = []
		self.next_status = StatusReport(frequency=1.0, pwm=2, is_stalled=False)
		self.next_settings = SettingsReport(kp=1.0, ki=2.0, kd=3.0, speed=4.0)
		self.raise_on_command: Exception | None = None
		self.sink = None

		self.run_for_calls = []
		self.stop_run_calls = []
		self.set_rate_calls = []
		self.calibration_point_calls = []
		self.save_calibration_calls = []
		self.load_calibration_calls = []
		self._calibration_points: dict[int, list] = {}
		self._calibration_deadband: dict[int, float] = {}
		self._raw_rates: dict[tuple[int, float], list] = {}
		self.run_for_result: float | None = None
		self.set_rate_result: float | None = None
		self.stop_run_result = False

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


	def run_for(self, target_id, duration_s, speed=None, rate_g_s=None):
		if self.raise_on_command:
			raise self.raise_on_command
		self.run_for_calls.append((target_id, duration_s, speed, rate_g_s))
		if self.run_for_result is not None:
			return self.run_for_result
		return speed if speed is not None else rate_g_s

	def stop_run(self, target_id):
		self.stop_run_calls.append(target_id)
		return self.stop_run_result

	def set_rate(self, target_id, rate_g_s):
		if self.raise_on_command:
			raise self.raise_on_command
		self.set_rate_calls.append((target_id, rate_g_s))
		return self.set_rate_result if self.set_rate_result is not None else rate_g_s

	def record_calibration_point(self, target_id, speed, grams, duration_s):
		if self.raise_on_command:
			raise self.raise_on_command
		self.calibration_point_calls.append((target_id, speed, grams, duration_s))

		class _Point:
			def __init__(self, speed, rate_g_s, repeats, spread_g_s):
				self.speed = speed
				self.rate_g_s = rate_g_s
				self.repeats = repeats
				self.spread_g_s = spread_g_s

		raw = self._raw_rates.setdefault((target_id, speed), [])
		raw.append(grams / duration_s)
		point = _Point(
			speed=speed,
			rate_g_s=sum(raw) / len(raw),
			repeats=len(raw),
			spread_g_s=(max(raw) - min(raw)) if len(raw) > 1 else 0.0,
		)
		self._calibration_points[target_id] = [
			p for p in self._calibration_points.get(target_id, []) if p.speed != speed
		] + [point]
		return point

	def calibration_points(self, target_id):
		return self._calibration_points.get(target_id, [])

	def calibration_deadband(self, target_id):
		return self._calibration_deadband.get(target_id)

	def save_calibration(self, path):
		self.save_calibration_calls.append(path)

	def load_calibration(self, path):
		self.load_calibration_calls.append(path)


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

		os.makedirs(TEST_LOGS_DIR, exist_ok=True)
		shell, session, out = make_shell()
		tmp_dir = tempfile.mkdtemp(dir=TEST_LOGS_DIR)
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

		os.makedirs(TEST_LOGS_DIR, exist_ok=True)
		shell, session, out = make_shell()
		fd, blocking_file = tempfile.mkstemp(dir=TEST_LOGS_DIR)
		os.close(fd)
		self.addCleanup(lambda: os.path.exists(blocking_file) and os.remove(blocking_file))

		bad_path = os.path.join(blocking_file, "sub", "run.csv")
		shell.onecmd(f"log start {bad_path}")
		self.assertIn("failed to open log file", out.getvalue())


class TestPolydriverShellTimedRunsAndCalibration(unittest.TestCase):
	def test_run_passes_speed_and_duration_through(self):
		shell, session, out = make_shell()
		shell.onecmd("run 1 20 5")
		self.assertEqual(session.run_for_calls, [(1, 5.0, 20.0, None)])
		self.assertIn("running at speed=20.0 for 5.0s", out.getvalue())

	def test_run_wrong_arg_count_prints_usage(self):
		shell, session, out = make_shell()
		shell.onecmd("run 1 20")
		self.assertEqual(session.run_for_calls, [])
		self.assertIn("usage:", out.getvalue())

	def test_dose_passes_rate_and_duration_through(self):
		shell, session, out = make_shell()
		session.run_for_result = 42.0
		shell.onecmd("dose 1 0.5 5")
		self.assertEqual(session.run_for_calls, [(1, 5.0, None, 0.5)])
		self.assertIn("dosing at 0.5g/s (speed=42.000) for 5.0s", out.getvalue())

	def test_dose_reports_calibration_error_cleanly(self):
		shell, session, out = make_shell()
		session.raise_on_command = CalibrationError("no calibration recorded yet for target 1")
		shell.onecmd("dose 1 0.5 5")
		self.assertIn("failed:", out.getvalue())
		self.assertIn("no calibration recorded", out.getvalue())

	def test_rate_sets_speed_via_calibration(self):
		shell, session, out = make_shell()
		session.set_rate_result = 17.5
		shell.onecmd("rate 1 0.5")
		self.assertEqual(session.set_rate_calls, [(1, 0.5)])
		self.assertIn("ok (speed=17.500)", out.getvalue())

	def test_stop_cancels_the_run_and_forces_speed_zero(self):
		shell, session, out = make_shell()
		shell.onecmd("stop 1")
		self.assertEqual(session.stop_run_calls, [1])
		self.assertEqual(session.speed_calls, [(1, 0.0)])
		self.assertIn("ok", out.getvalue())

	def test_stop_accepts_a_target_name(self):
		shell, session, out = make_shell()
		shell.onecmd("add 9 pump-a")
		shell.onecmd("stop pump-a")
		self.assertEqual(session.stop_run_calls, [9])
		self.assertEqual(session.speed_calls, [(9, 0.0)])

	def test_calibrate_add_records_a_point(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate add 1 20 10 5")
		self.assertEqual(session.calibration_point_calls, [(1, 20.0, 5.0, 10.0)])
		self.assertIn("recorded speed=20.0 -> 0.5000 g/s", out.getvalue())

	def test_calibrate_add_wrong_arg_count_prints_usage(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate add 1 20 10")
		self.assertEqual(session.calibration_point_calls, [])
		self.assertIn("usage:", out.getvalue())

	def test_calibrate_add_reports_the_average_and_spread_on_a_repeat(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate add 1 20 10 9")   # 0.9 g/s
		out.truncate(0)
		out.seek(0)
		shell.onecmd("calibrate add 1 20 10 11")  # 1.1 g/s -> avg 1.0, spread 0.2
		text = out.getvalue()
		self.assertIn("recorded speed=20.0 -> 1.0000 g/s", text)
		self.assertIn("avg of 2, spread 0.2000 g/s", text)

	def test_calibrate_add_a_single_measurement_does_not_mention_an_average(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate add 1 20 10 5")
		self.assertNotIn("avg of", out.getvalue())

	def test_calibrate_show_lists_points_and_deadband(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate add 1 20 10 5")
		out.truncate(0)
		out.seek(0)
		session._calibration_deadband[1] = 20.0

		shell.onecmd("calibrate show 1")
		text = out.getvalue()
		self.assertIn("speed=20.0 -> 0.5000 g/s", text)
		self.assertIn("lowest speed known to produce output: 20.0", text)

	def test_calibrate_show_includes_repeats_and_spread(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate add 1 20 10 9")
		shell.onecmd("calibrate add 1 20 10 11")
		out.truncate(0)
		out.seek(0)

		shell.onecmd("calibrate show 1")
		self.assertIn("avg of 2, spread 0.2000 g/s", out.getvalue())

	def test_calibrate_show_with_no_points_says_so(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate show 1")
		self.assertIn("no calibration points recorded", out.getvalue())

	def test_calibrate_save_and_load(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate save cal.json")
		self.assertEqual(session.save_calibration_calls, ["cal.json"])
		self.assertIn("saved calibration to cal.json", out.getvalue())

		out.truncate(0)
		out.seek(0)
		shell.onecmd("calibrate load cal.json")
		self.assertEqual(session.load_calibration_calls, ["cal.json"])
		self.assertIn("loaded calibration from cal.json", out.getvalue())

	def test_calibrate_with_no_subcommand_prints_usage(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate")
		self.assertIn("usage:", out.getvalue())

	def test_calibrate_with_an_unknown_subcommand_prints_usage(self):
		shell, session, out = make_shell()
		shell.onecmd("calibrate frobnicate 1")
		self.assertIn("usage:", out.getvalue())


if __name__ == "__main__":
	unittest.main()

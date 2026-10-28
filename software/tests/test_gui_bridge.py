import threading
import time

import pytest

from polydriver.gui.bridge import Bridge, BridgeClosedError


@pytest.fixture
def bridge(qtbot):
	bridge = Bridge()
	yield bridge
	bridge.close(2.0)


def test_a_call_runs_off_the_gui_thread_and_its_result_comes_back_on_it(qtbot, bridge):
	ran_on = []
	results = []

	def work():
		ran_on.append(threading.current_thread())
		return 42

	bridge.run(work, on_result=lambda value: results.append((value, threading.current_thread())))
	qtbot.waitUntil(lambda: bool(results), timeout=2000)
	assert ran_on[0] is not threading.main_thread()
	assert results == [(42, threading.main_thread())]


def test_a_call_passes_its_arguments(qtbot, bridge):
	results = []
	bridge.run(lambda a, b=0: a + b, 2, b=3, on_result=results.append)
	qtbot.waitUntil(lambda: bool(results), timeout=2000)
	assert results == [5]


def test_an_error_goes_to_on_error_on_the_gui_thread(qtbot, bridge):
	errors = []
	failed = []
	bridge.failed.connect(failed.append)

	def work():
		raise ValueError("bad port")

	bridge.run(work, on_error=lambda error: errors.append((error, threading.current_thread())))
	qtbot.waitUntil(lambda: bool(errors), timeout=2000)
	assert str(errors[0][0]) == "bad port"
	assert errors[0][1] is threading.main_thread()
	assert failed == []


def test_an_error_without_on_error_is_reported_through_failed(qtbot, bridge):
	def work():
		raise ValueError("bad port")

	with qtbot.waitSignal(bridge.failed, timeout=2000) as signal:
		call = bridge.run(work, name="connect polymer")
	assert signal.args[0] is call
	assert call.name == "connect polymer"
	assert str(call.error) == "bad port"
	assert call.done


def test_finished_carries_the_call_with_its_result(qtbot, bridge):
	with qtbot.waitSignal(bridge.finished, timeout=2000) as signal:
		call = bridge.run(lambda: "v3")
	assert signal.args[0] is call
	assert call.done and call.result == "v3" and call.error is None


def test_a_call_is_named_after_its_function_unless_named(bridge):
	def connect_both():
		pass

	assert bridge.run(connect_both).name == "connect_both"


def test_calls_run_one_at_a_time_in_order(qtbot, bridge):
	log = []
	lock = threading.Lock()

	def work(n):
		with lock:
			log.append(("start", n))
		time.sleep(0.02)
		with lock:
			log.append(("end", n))

	for n in range(4):
		bridge.run(work, n)
	qtbot.waitUntil(lambda: len(log) == 8, timeout=3000)
	assert log == [(edge, n) for n in range(4) for edge in ("start", "end")]


def test_a_slow_call_does_not_freeze_the_gui(qtbot, bridge):
	from PySide6.QtCore import QTimer

	ticks = []
	timer = QTimer()
	timer.setInterval(20)
	timer.timeout.connect(lambda: ticks.append(time.monotonic()))
	timer.start()
	try:
		with qtbot.waitSignal(bridge.finished, timeout=3000):
			bridge.run(time.sleep, 0.6)
	finally:
		timer.stop()
	assert len(ticks) >= 15
	assert max(b - a for a, b in zip(ticks, ticks[1:])) < 0.2


def test_run_now_does_not_wait_behind_a_busy_call(qtbot, bridge):
	release = threading.Event()
	stopped = []
	bridge.run(release.wait, 5.0)
	bridge.run_now(lambda: "STOP", on_result=stopped.append)
	qtbot.waitUntil(lambda: bool(stopped), timeout=1000)
	assert not release.is_set()
	release.set()


def test_run_now_errors_come_back_too(qtbot, bridge):
	def work():
		raise ValueError("COM6 lost")

	with qtbot.waitSignal(bridge.failed, timeout=2000) as signal:
		bridge.run_now(work)
	assert str(signal.args[0].error) == "COM6 lost"


def test_busy_turns_on_with_the_first_call_and_off_after_the_last(qtbot, bridge):
	changes = []
	bridge.busy_changed.connect(changes.append)
	assert not bridge.busy
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	bridge.run(lambda: None)
	assert bridge.busy
	assert changes == [True]
	release.set()
	qtbot.waitUntil(lambda: not bridge.busy, timeout=2000)
	assert changes == [True, False]


def test_run_now_does_not_make_the_bridge_busy(qtbot, bridge):
	changes = []
	bridge.busy_changed.connect(changes.append)
	with qtbot.waitSignal(bridge.finished, timeout=2000):
		bridge.run_now(lambda: None)
	assert changes == []
	assert not bridge.busy


def test_close_waits_for_the_running_call(qtbot):
	bridge = Bridge()
	started = threading.Event()
	done = []

	def work():
		started.set()
		time.sleep(0.1)
		done.append(True)

	bridge.run(work)
	assert started.wait(2.0)
	assert bridge.close(2.0)
	assert done == [True]


def test_close_drops_calls_that_have_not_started(qtbot):
	bridge = Bridge()
	release = threading.Event()
	ran = []
	bridge.run(release.wait, 5.0)
	later = bridge.run(lambda: ran.append(True))
	threading.Timer(0.05, release.set).start()
	assert bridge.close(2.0)
	assert ran == []
	assert not later.done


def test_close_waits_for_run_now_calls(qtbot):
	bridge = Bridge()
	done = []

	def work():
		time.sleep(0.1)
		done.append(True)

	bridge.run_now(work)
	assert bridge.close(2.0)
	assert done == [True]


def test_close_gives_up_after_its_timeout(qtbot):
	bridge = Bridge()
	started = threading.Event()
	release = threading.Event()

	def work():
		started.set()
		release.wait(5.0)

	bridge.run(work)
	assert started.wait(2.0)
	closing = time.monotonic()
	assert not bridge.close(0.1)
	assert time.monotonic() - closing < 1.0
	release.set()


def test_nothing_runs_after_close(qtbot):
	bridge = Bridge()
	bridge.close(1.0)
	with pytest.raises(BridgeClosedError):
		bridge.run(lambda: None)
	with pytest.raises(BridgeClosedError):
		bridge.run_now(lambda: None)


def test_close_twice_is_harmless(qtbot):
	bridge = Bridge()
	assert bridge.close(1.0)
	assert bridge.close(1.0)


def test_a_run_now_call_finishing_leaves_a_busy_bridge_busy(qtbot, bridge):
	changes = []
	bridge.busy_changed.connect(changes.append)
	release = threading.Event()
	bridge.run(release.wait, 5.0)
	with qtbot.waitSignal(bridge.finished, timeout=2000):
		bridge.run_now(lambda: None)
	assert bridge.busy
	assert changes == [True]
	release.set()
	qtbot.waitUntil(lambda: not bridge.busy, timeout=2000)

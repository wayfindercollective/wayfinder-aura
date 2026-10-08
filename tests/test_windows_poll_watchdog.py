"""Windows poll watchdog: logs a stalled poll_events chain (8 Oct 2026: hotkey
presses queued and never handled after a sleep/wake, with nothing logged)."""
import queue

import threading
from pathlib import Path

from wayfinder.utils import windows_poll_watchdog as wd

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock, beat, pending=lambda: False, recovered=None):
    lines, spawned, offers = [], [], []
    dog = wd.PollWatchdog(
        heartbeat=lambda: beat[0],
        log=lines.append,
        probe=lambda: "probe-result",
        pending_hotkey=pending,
        offer_restart=lambda: offers.append(1),
        on_recover=lambda: recovered is not None and recovered.append(1),
        stack=lambda: "mainloop (__init__.py:1505)",
        clock=clock,
        spawn=lambda fn, name: (spawned.append(name), fn()),
    )
    return dog, lines, spawned, offers


def run_for(dog, clock, seconds, beat=None, alive=False):
    """Tick every CHECK_EVERY_S; if alive, the poll keeps beating."""
    for _ in range(int(seconds / wd.CHECK_EVERY_S)):
        clock.t += wd.CHECK_EVERY_S
        if alive:
            beat[0] = clock.t
        dog.tick()


def test_healthy_poll_logs_nothing():
    clock = Clock()
    beat = [clock.t]
    dog, lines, _, _ = make(clock, beat)
    run_for(dog, clock, 60, beat, alive=True)
    assert lines == []


def test_stall_is_logged_once_with_stack_and_probe_then_recovery():
    clock = Clock()
    beat = [clock.t]
    dog, lines, spawned, _ = make(clock, beat)
    run_for(dog, clock, 30)
    assert dog.stalled
    assert len(lines) == 3
    assert lines[0].startswith("⚠ Aura stopped checking for the hotkey at ")
    assert "presses are not being handled" in lines[0]
    assert lines[1] == "   main thread: mainloop (__init__.py:1505)"
    assert lines[2] == "   Tk check: probe-result"
    assert spawned == ["poll-watchdog-probe"]
    beat[0] = clock.t + 1
    clock.t += wd.CHECK_EVERY_S
    dog.tick()
    assert not dog.stalled
    assert lines[-1].startswith("✓ Hotkey checks resumed after ")


def test_no_alarm_right_after_sleep():
    """Machine asleep: the poll's silence only counts once we've been awake."""
    clock = Clock()
    beat = [clock.t]
    dog, lines, _, _ = make(clock, beat)
    dog.tick()
    clock.t += 3600          # one hour of Modern Standby
    dog.tick()
    beat[0] = clock.t + 0.2  # the poll runs moments after wake
    run_for(dog, clock, 30, beat, alive=True)
    assert lines == []


def test_restart_offered_once_only_when_a_press_is_waiting():
    clock = Clock()
    beat = [clock.t]
    waiting = [False]
    dog, lines, spawned, offers = make(clock, beat, pending=lambda: waiting[0])
    run_for(dog, clock, 40)
    assert offers == []
    waiting[0] = True
    run_for(dog, clock, 20)
    assert offers == [1]
    assert "offering a restart" in lines[-1]
    assert spawned.count("poll-watchdog-restart") == 1


def test_slow_start_up_stall_gets_no_restart_offer():
    """A 19-34 s stall (start-up on a starved laptop) recovers by itself."""
    clock = Clock()
    beat = [clock.t]
    dog, _, _, offers = make(clock, beat, pending=lambda: True)
    run_for(dog, clock, 28)
    assert dog.stalled and offers == []
    run_for(dog, clock, 4)
    assert offers == [1]


def test_broken_pending_check_never_offers():
    clock = Clock()
    beat = [clock.t]
    dog, _, _, offers = make(clock, beat, pending=lambda: 1 / 0)
    run_for(dog, clock, 60)
    assert offers == []


def test_ask_with_timeout_survives_a_hung_call():
    gate = threading.Event()
    ok, value = wd.ask_with_timeout(gate.wait, 0.1)
    assert (ok, value) == (False, None)
    gate.set()
    assert wd.ask_with_timeout(lambda: 42, 1.0) == (True, 42)


def test_main_thread_stack_names_frames():
    assert "test_main_thread_stack_names_frames" in wd.main_thread_stack()


def test_hotkey_press_waiting_peeks_without_consuming():
    import wayfinder_main as wm

    class App:
        event_queue = queue.Queue()

    App.event_queue.put((wm.EventType.LOG_MESSAGE, "x"))
    assert not wm.WayfinderApp._hotkey_press_waiting(App)
    App.event_queue.put((wm.EventType.HOTKEY_PRESSED, None))
    assert wm.WayfinderApp._hotkey_press_waiting(App)
    assert App.event_queue.qsize() == 2


def test_poll_events_beats_first_and_watchdog_is_windows_only():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    body = src.split("    def poll_events(self):", 1)[1].split("\n    def ", 1)[0]
    first = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")][0]
    assert first == "self._poll_heartbeat = time.monotonic()"
    assert "self._poll_after_id = self.after(interval, self.poll_events)" in body
    start = src.split("        self.poll_events()\n", 1)[1].split("\n\n", 1)[0]
    assert "if IS_WINDOWS and not IS_MACOS:" in start
    # after mainloop is up, not during main.py's pre-mainloop start-up work
    assert "self.after(2000, self._start_windows_poll_watchdog)" in start


def test_recovery_withdraws_the_restart_offer():
    clock = Clock()
    beat = [clock.t]
    recovered = []
    dog, _, _, _ = make(clock, beat, recovered=recovered)
    run_for(dog, clock, 30)
    assert recovered == []
    beat[0] = clock.t + 1
    clock.t += wd.CHECK_EVERY_S
    dog.tick()
    assert recovered == [1]


# --- the app side: restart dialog and Tk probe ---------------------------------

import ctypes  # noqa: E402

import pytest  # noqa: E402

windows_only = pytest.mark.skipif(not hasattr(ctypes, "windll"), reason="Windows only")


@pytest.fixture
def wm():
    import wayfinder_main
    return wayfinder_main


class FakeApp:
    def __init__(self, wm, *, stalled=True, state=None):
        self.lines = []
        self.app_state = state if state is not None else wm.AppState.IDLE
        self._restart_offer_lock = threading.Lock()
        self._poll_watchdog = type("Dog", (), {"stalled": stalled})()

    def log(self, line):
        self.lines.append(line)


def answer_with(monkeypatch, answer, seen=None):
    def box(_hwnd, text, _title, _flags):
        if seen is not None:
            seen.append(text)
        return answer
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", box)


@windows_only
def test_restart_skipped_when_aura_recovered_before_the_answer(wm, monkeypatch):
    exits = []
    monkeypatch.setattr(wm.os, "_exit", exits.append)
    answer_with(monkeypatch, 6)  # Yes, but the poll is back
    app = FakeApp(wm, stalled=False)
    wm.WayfinderApp._offer_windows_restart(app)
    assert exits == []
    assert app.lines == ["ℹ Aura recovered by itself — restart skipped"]
    assert not app._restart_offer_lock.locked()


@windows_only
def test_declined_restart_is_logged_and_nothing_exits(wm, monkeypatch):
    exits = []
    monkeypatch.setattr(wm.os, "_exit", exits.append)
    answer_with(monkeypatch, 7)  # No
    app = FakeApp(wm)
    wm.WayfinderApp._offer_windows_restart(app)
    assert exits == []
    assert app.lines[-1].startswith("ℹ Restart declined")


@windows_only
def test_dialog_warns_when_a_dictation_would_be_lost(wm, monkeypatch):
    seen = []
    answer_with(monkeypatch, 7, seen)
    wm.WayfinderApp._offer_windows_restart(FakeApp(wm, state=wm.AppState.RECORDING))
    wm.WayfinderApp._offer_windows_restart(FakeApp(wm))
    assert "will be lost" in seen[0]
    assert "will be lost" not in seen[1]


@windows_only
def test_only_one_restart_dialog_at_a_time(wm, monkeypatch):
    seen = []
    answer_with(monkeypatch, 7, seen)
    app = FakeApp(wm)
    app._restart_offer_lock.acquire()  # a dialog is already up
    wm.WayfinderApp._offer_windows_restart(app)
    assert seen == []


def test_probe_reports_a_tk_that_never_answers(wm):
    hang = threading.Event()

    class Tk:
        def call(self, *args):
            hang.wait()

        def splitlist(self, value):
            return value

    class App:
        tk = Tk()
        _poll_after_id = "after#1"

    try:
        assert wm.WayfinderApp._windows_poll_probe(App) == "Tk did not answer a direct call within 3s"
    finally:
        hang.set()

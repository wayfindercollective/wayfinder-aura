"""Windows poll watchdog: logs a stalled poll_events chain (8 Oct 2026: hotkey
presses queued and never handled after a sleep/wake, with nothing logged)."""
import os
import queue
import threading
import time
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
    assert lines[1] == "   main thread at 1s: mainloop (__init__.py:1505)"
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
    assert "offering a restart" in lines[-2]
    assert lines[-1].startswith("   main thread at ")   # where it is stuck at the offer
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


def test_stall_report_shows_where_it_was_stuck_from_the_start():
    """16 Oct: a press held the main thread ~10 s opening the mic, then moved
    on to the error banner; the report must show both, earliest first."""
    clock = Clock()
    beat = [clock.t]
    where = ["start_recording (wayfinder_main.py:1)"]
    lines = []
    dog = wd.PollWatchdog(
        heartbeat=lambda: beat[0], log=lines.append, probe=lambda: "p",
        pending_hotkey=lambda: False, offer_restart=lambda: None,
        stack=lambda: where[0], clock=clock, spawn=lambda fn, name: fn(),
    )
    run_for(dog, clock, 3)
    assert lines == []                      # sampled, not yet reported
    where[0] = "_show_error_banner (wayfinder_main.py:2)"
    run_for(dog, clock, 3)
    assert lines[0].startswith("⚠ Aura stopped checking for the hotkey")
    assert lines[1] == "   main thread at 1s: start_recording (wayfinder_main.py:1)"
    assert lines[2] == "   main thread at 4s: _show_error_banner (wayfinder_main.py:2)"


def test_short_freeze_is_sampled_but_never_reported():
    clock = Clock()
    beat = [clock.t]
    dog, lines, _, _ = make(clock, beat)
    run_for(dog, clock, 4)
    beat[0] = clock.t
    run_for(dog, clock, 20, beat, alive=True)
    assert lines == []


# --- automatic restart (3 Nov: a 75-minute stall waited behind an unseen dialog) ---

def make_auto(clock, beat, pending=lambda: False, allowed=lambda: True, restarted=True):
    lines, restarts, offers = [], [], []

    def auto_restart(reason):
        restarts.append(reason)
        return restarted   # True stands for "the process is gone"

    dog = wd.PollWatchdog(
        heartbeat=lambda: beat[0], log=lines.append, probe=lambda: "p",
        pending_hotkey=pending, offer_restart=lambda: offers.append(1),
        auto_restart=auto_restart, auto_restart_allowed=allowed,
        stack=lambda: "mainloop (__init__.py:1505)", clock=clock,
        spawn=lambda fn, name: fn(),
    )
    return dog, lines, restarts, offers


def settle(clock, beat, dog, seconds):
    """Healthy running time before the stall (past the start-up grace)."""
    run_for(dog, clock, seconds, beat, alive=True)


def test_a_press_during_a_stall_restarts_without_a_dialog():
    clock = Clock()
    beat = [clock.t]
    dog, lines, restarts, offers = make_auto(clock, beat, pending=lambda: True)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, wd.PRESS_RESTART_AFTER_S + 2)
    assert len(restarts) == 1 and "hotkey was pressed" in restarts[0]
    assert offers == []
    assert any(l.startswith("🔄 Restarting Aura automatically") for l in lines)
    run_for(dog, clock, 300)
    assert len(restarts) == 1                      # once per stall


def test_a_long_stall_restarts_even_with_no_press():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, _ = make_auto(clock, beat)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, wd.IDLE_RESTART_AFTER_S - 5)
    assert restarts == []
    run_for(dog, clock, 10)
    assert len(restarts) == 1


def test_no_automatic_restart_during_start_up():
    """19-45 s start-up stalls recover by themselves: the old dialog path only."""
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, offers = make_auto(clock, beat, pending=lambda: True)
    run_for(dog, clock, wd.PRESS_RESTART_AFTER_S + 5)
    assert restarts == [] and offers == [1]


def test_rate_limited_restart_falls_back_to_the_dialog():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, offers = make_auto(clock, beat, pending=lambda: True,
                                         allowed=lambda: False)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, wd.IDLE_RESTART_AFTER_S + 10)
    assert restarts == [] and offers == [1]


def test_a_recovered_stall_never_restarts():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, _ = make_auto(clock, beat)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, 20)                         # stalled, not long enough
    beat[0] = clock.t
    run_for(dog, clock, 200, beat, alive=True)
    assert restarts == []


@windows_only
def test_app_rate_limits_automatic_restarts(wm, tmp_path, monkeypatch):
    class App:
        _AUTO_RESTART_EVERY_S = wm.WayfinderApp._AUTO_RESTART_EVERY_S

        def _auto_restart_marker(self):
            return tmp_path / "last-auto-restart"

    app = App()
    assert wm.WayfinderApp._auto_restart_allowed(app)
    (tmp_path / "last-auto-restart").write_text("x")
    assert not wm.WayfinderApp._auto_restart_allowed(app)
    old = time.time() - 700
    os.utime(tmp_path / "last-auto-restart", (old, old))
    assert wm.WayfinderApp._auto_restart_allowed(app)


def test_a_hiccup_after_sleep_never_restarts():
    """Monotonic time runs through sleep: 8 h asleep + an 8 s hiccup on wake
    must not read as an 8-hour stall (review r1, finding 1)."""
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, _ = make_auto(clock, beat)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    clock.t += 8 * 3600          # asleep: no ticks, no beats
    run_for(dog, clock, 8)       # awake, main thread busy for 8 s
    beat[0] = clock.t
    run_for(dog, clock, 30, beat, alive=True)
    assert restarts == []


def test_a_starved_watchdog_restarts_the_awake_count():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, _ = make_auto(clock, beat)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, 100)
    clock.t += 600               # watchdog thread starved / machine asleep
    run_for(dog, clock, 100)     # 100 awake seconds since: under the 120 s bar
    assert restarts == []
    run_for(dog, clock, 30)
    assert len(restarts) == 1


def test_a_failed_automatic_restart_offers_the_dialog():
    clock = Clock()
    beat = [clock.t]
    dog, lines, restarts, offers = make_auto(clock, beat, restarted=False)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, wd.IDLE_RESTART_AFTER_S + 5)
    assert len(restarts) == 1 and offers == [1]
    assert any("automatic restart not possible" in l for l in lines)


def test_after_recovery_the_next_stall_can_restart_again():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, _ = make_auto(clock, beat, restarted=False)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, wd.IDLE_RESTART_AFTER_S + 5)
    beat[0] = clock.t
    run_for(dog, clock, 10, beat, alive=True)
    run_for(dog, clock, wd.IDLE_RESTART_AFTER_S + 5)
    assert len(restarts) == 2


def test_declining_the_dialog_stops_the_automatic_restart_for_that_stall():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, offers = make_auto(clock, beat, pending=lambda: True)
    run_for(dog, clock, wd.PROMPT_AFTER_S + 2)          # inside the start-up grace
    assert offers == [1]
    dog.note_declined()
    run_for(dog, clock, wd.AUTO_RESTART_MIN_UPTIME_S + wd.IDLE_RESTART_AFTER_S)
    assert restarts == []


def test_a_slow_but_working_moment_under_a_minute_does_not_restart():
    clock = Clock()
    beat = [clock.t]
    dog, _, restarts, _ = make_auto(clock, beat, pending=lambda: True)
    settle(clock, beat, dog, wd.AUTO_RESTART_MIN_UPTIME_S)
    run_for(dog, clock, 45)       # e.g. Settings building on a starved laptop
    assert restarts == []


@windows_only
def test_app_refuses_to_restart_mid_dictation_or_without_its_marker(wm, tmp_path, monkeypatch):
    calls = []

    class App:
        app_state = wm.AppState.RECORDING
        lines = []

        def log(self, line):
            self.lines.append(line)

        def _auto_restart_marker(self):
            return tmp_path / "sub" / "last-auto-restart"

        def _restart_windows_now(self):
            calls.append(1)

    app = App()
    assert wm.WayfinderApp._auto_restart_windows(app, "stuck") is False
    assert calls == [] and "Dictation in progress" in app.lines[-1]
    app.app_state = wm.AppState.IDLE
    (tmp_path / "sub").write_text("a file where the folder should be")
    assert wm.WayfinderApp._auto_restart_windows(app, "stuck") is False
    assert calls == [] and "asking instead" in app.lines[-1]


@windows_only
def test_app_rate_limit_allows_a_clock_set_back(wm, tmp_path):
    class App:
        _AUTO_RESTART_EVERY_S = wm.WayfinderApp._AUTO_RESTART_EVERY_S

        def _auto_restart_marker(self):
            return tmp_path / "m"

    (tmp_path / "m").write_text("x")
    future = time.time() + 3600
    os.utime(tmp_path / "m", (future, future))
    assert wm.WayfinderApp._auto_restart_allowed(App())


@windows_only
def test_only_one_restart_runs(wm, monkeypatch):
    starts = []
    monkeypatch.setattr(wm.sys, "frozen", True, raising=False)
    monkeypatch.setattr(wm.subprocess, "Popen", lambda *a, **k: starts.append(1))
    monkeypatch.setattr(wm.os, "_exit", lambda code: None)
    monkeypatch.setattr(wm, "_flush_windows_clipboard_restore", lambda: None)

    class App:
        _restart_now_lock = threading.Lock()

        def log(self, line):
            pass

        def _close_audio_ducking(self):
            pass

    app = App()
    app._restart_now_lock.acquire()          # another path is already restarting
    wm.WayfinderApp._restart_windows_now(app)
    assert starts == []
    app._restart_now_lock.release()
    wm.WayfinderApp._restart_windows_now(app)
    assert starts == [1]


def test_automatic_restart_is_only_wired_in_the_installed_app():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    assert 'if getattr(sys, "frozen", False) else None),' in src


def test_one_failing_event_never_ends_the_poll_chain(wm):
    """3 Nov: a lazy import inside handle_event raised (Defender had deleted
    the frozen exe) and the exception skipped the re-arm: hotkey dead."""
    handled, reported, scheduled = [], [], []

    class App:
        event_queue = queue.Queue()
        app_state = wm.AppState.IDLE

        def handle_event(self, event_type, data):
            if data == "boom":
                raise OSError(22, "Invalid argument")
            handled.append(data)

        def report_callback_exception(self, *exc):
            reported.append(exc[0])

        def _drain_tray_actions(self):
            pass

        def after(self, ms, fn):
            scheduled.append(ms)
            return "after#1"

        def poll_events(self):
            pass

    app = App()
    for data in ("boom", "next"):
        app.event_queue.put((wm.EventType.HOTKEY_PRESSED, data))
    wm.WayfinderApp.poll_events(app)
    assert handled == ["next"]            # the event after the failure still runs
    assert reported == [OSError]          # and the failure is reported, not swallowed
    assert scheduled == [250]             # and the chain re-arms


def test_the_poll_chain_re_arms_even_if_the_tray_drain_raises(wm):
    scheduled = []

    class App:
        event_queue = queue.Queue()
        app_state = wm.AppState.IDLE

        def _drain_tray_actions(self):
            raise RuntimeError("tray")

        def after(self, ms, fn):
            scheduled.append(ms)

        def poll_events(self):
            pass

    with pytest.raises(RuntimeError):
        wm.WayfinderApp.poll_events(App())
    assert scheduled == [250]

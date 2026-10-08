"""Windows poll watchdog: logs a stalled poll_events chain (8 Oct 2026: hotkey
presses queued and never handled after a sleep/wake, with nothing logged)."""
import queue
import re
import threading
from pathlib import Path

from wayfinder.utils import windows_poll_watchdog as wd

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock, beat, pending=lambda: False):
    lines, spawned, offers = [], [], []
    dog = wd.PollWatchdog(
        heartbeat=lambda: beat[0],
        log=lines.append,
        probe=lambda: "probe-result",
        pending_hotkey=pending,
        offer_restart=lambda: offers.append(1),
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
    assert re.search(r"self\.poll_events\(\)\n\s+if IS_WINDOWS and not IS_MACOS:\n"
                     r"\s+self\._start_windows_poll_watchdog\(\)", src)

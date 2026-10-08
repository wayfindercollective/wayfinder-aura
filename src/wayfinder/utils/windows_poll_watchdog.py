"""Windows: notice when the Tk event poll stops, so hotkey presses go nowhere.

8 Oct 2026: some time after a Modern Standby wake, Aura's ``poll_events``
chain stopped re-arming while Tk itself kept pumping window messages. Every
hotkey press after that was queued and never handled, and nothing reached any
log. This watchdog runs on its own thread and:

* logs when the poll goes quiet, with what the main thread is doing and (via
  ``probe``) what Tcl still has scheduled;
* logs when the poll comes back;
* if a hotkey press is waiting during a stall, offers a restart once.

It only observes. ``tick`` is the whole decision so tests drive it with a
fake clock.
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Callable, Optional

CHECK_EVERY_S = 2.0
STALL_AFTER_S = 10.0     # poll runs every 100-250 ms; 10 s quiet is not a hiccup
PROMPT_AFTER_S = 15.0    # a press waiting this long into a stall gets the restart offer
# A tick gap this long means the machine slept or this thread was starved:
# the poll needs a moment after that before its silence means anything.
_ASLEEP_GAP_S = CHECK_EVERY_S * 2.5


def main_thread_stack(thread_id: Optional[int] = None, limit: int = 8) -> str:
    """The main thread's innermost frames, innermost first, on one line."""
    if thread_id is None:
        thread_id = threading.main_thread().ident
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return "unknown"
    frames = traceback.extract_stack(frame)[-limit:]
    return " ← ".join(f"{f.name} ({f.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{f.lineno})"
                      for f in reversed(frames))


class PollWatchdog:
    def __init__(
        self,
        *,
        heartbeat: Callable[[], Optional[float]],
        log: Callable[[str], None],
        probe: Callable[[], str],
        pending_hotkey: Callable[[], bool],
        offer_restart: Callable[[], None],
        stack: Callable[[], str] = main_thread_stack,
        clock: Callable[[], float] = time.monotonic,
        spawn: Optional[Callable[[Callable[[], None], str], None]] = None,
    ) -> None:
        self._heartbeat = heartbeat
        self._log = log
        self._probe = probe
        self._pending_hotkey = pending_hotkey
        self._offer_restart = offer_restart
        self._stack = stack
        self._clock = clock
        self._spawn = spawn or _spawn_daemon
        self._last_tick: Optional[float] = None
        self._awake_since = clock()
        self._stalled_at: Optional[float] = None   # heartbeat value when the stall began
        self._offered = False
        self._stop = threading.Event()

    @property
    def stalled(self) -> bool:
        return self._stalled_at is not None

    def tick(self) -> None:
        now = self._clock()
        if self._last_tick is not None and now - self._last_tick > _ASLEEP_GAP_S:
            self._awake_since = now
        self._last_tick = now
        beat = self._heartbeat()
        if beat is None:
            return
        quiet = now - beat
        if self._stalled_at is None:
            if quiet >= STALL_AFTER_S and now - self._awake_since >= STALL_AFTER_S:
                self._stalled_at = beat
                self._offered = False
                since = datetime.now() - timedelta(seconds=quiet)
                self._log(f"⚠ Aura stopped checking for the hotkey at {since:%H:%M:%S} "
                          f"({quiet:.0f}s ago) — presses are not being handled")
                self._log(f"   main thread: {_safe(self._stack)}")
                self._spawn(lambda: self._log(f"   Tk check: {_safe(self._probe)}"),
                            "poll-watchdog-probe")
            return
        if beat > self._stalled_at:
            self._log(f"✓ Hotkey checks resumed after {beat - self._stalled_at:.0f}s")
            self._stalled_at = None
            return
        if not self._offered and quiet >= PROMPT_AFTER_S and _safe_bool(self._pending_hotkey):
            self._offered = True
            self._log("⚠ Hotkey pressed while Aura was stuck — offering a restart")
            self._spawn(self._offer_restart, "poll-watchdog-restart")

    def start(self) -> "PollWatchdog":
        def run() -> None:
            while not self._stop.wait(CHECK_EVERY_S):
                try:
                    self.tick()
                except Exception:
                    pass  # diagnostics must never take the app down
        _spawn_daemon(run, "poll-watchdog")
        return self

    def stop(self) -> None:
        self._stop.set()


def _spawn_daemon(fn: Callable[[], None], name: str) -> None:
    threading.Thread(target=fn, name=name, daemon=True).start()


def _safe(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception as exc:
        return f"unavailable ({exc})"


def _safe_bool(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False


def ask_with_timeout(fn: Callable[[], object], timeout: float) -> tuple[bool, object]:
    """Run *fn* on a daemon thread; (True, result) or (False, None) if it hangs.

    Tk calls from another thread are marshalled to the main thread and wait
    for it with no timeout, so a stuck main thread must not hang the probe.
    """
    box: dict = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:
            box["value"] = exc

    t = threading.Thread(target=run, name="poll-watchdog-ask", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    return True, box.get("value")

"""Windows: keep the pill above ordinary windows.

20 Oct 2026: Windows ranked the pill (still flagged WS_EX_TOPMOST) below
whichever ordinary window was active, so it sat hidden behind VS Code,
Explorer and Slack. The taskbar and an LG utility were demoted the same way,
so the cause is outside Aura. Setting HWND_TOPMOST again brought the pill
back. The overlay checks its place on every foreground change (plus a 1 s
fallback), puts itself back on top without activating, and reports each
restore with the app that was in front.
"""
from __future__ import annotations

import ctypes
import sys
import time
from typing import Callable, Optional

_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x8
_GW_HWNDNEXT = 2
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE, _SWP_NOOWNERZORDER = 0x1, 0x2, 0x10, 0x200
_RESTORE_FLAGS = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER
_DWMWA_CLOAKED = 14
_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_MAX_WALK = 5000  # z-order walk bound (a desktop has a few hundred windows)

REPORT_EVERY_S = 30.0
# A restore that doesn't hold means something re-demotes the pill at once (or
# a window in a higher band sits above it): stop for BACKOFF_S rather than
# fight it every second. Restores that hold are cheap and never paused, even
# when they come on every window switch, or the pill would stay hidden.
BACKOFF_S = 60.0
# A restore can hold at the instant check() verifies it and still be undone
# before the next tick: the same foreground window re-demoting the pill every
# turn for this long (20 Oct: 120 restores in 120 s, held=True every time, no
# back-off) is window-order fighting, not a burst of real switches — those
# settle well under this. Back off exponentially once it's been this long.
CONTENTION_AFTER_S = 10.0
MAX_BACKOFF_S = 8 * BACKOFF_S
# The delay is min(BACKOFF_S * 2**level, MAX_BACKOFF_S), so any level past the
# one that first reaches MAX_BACKOFF_S is equivalent — but the exponent itself
# must still be bounded, or float(BACKOFF_S * 2**level) eventually raises
# OverflowError (Sol pill r2: level 1024, after ~5.7 days of continuous contention,
# did; check()'s blanket except swallowed it, so self._paused_until was never
# set and repairs then ran every second forever). 16 is already far past the
# saturation point and leaves headroom even if BACKOFF_S/MAX_BACKOFF_S change.
_MAX_CONTEND_LEVEL = 16
# No repair action runs more than once every REPAIR_COOLDOWN_S, whatever
# resets the pause or the contention key — a foreground change included (Sol
# pill r4: two ordinary windows alternating visibility/foreground every tick
# each looked like a brand new contender, so the fresh-try reset fired every
# time and skipped BACKOFF_S/CONTENTION_AFTER_S entirely: 120 repairs in 120
# ticks). This is a flat floor underneath that back-off, not a replacement
# for it, and it also bounds ACTIVE_MAX_PAUSE_S below.
REPAIR_COOLDOWN_S = 2.0
# While keeper.set_active(True) — a dictation is in progress — any pause the
# keeper sets is capped at this, so a wrong (or simply unknown) cause for a
# demotion can never leave the pill buried for the rest of a long back-off
# while recording continues (Sol pill r4: attributing the demotion to
# whichever window was found "above" the pill misattributed an unseen
# demoter's work to an innocent window it happened to leave visible behind
# it, and that window's own pause then held for 479 ticks with zero repairs
# since it never moved or closed; the pill stayed buried the whole
# recording). Not used outside a dictation — the back-off is the same as
# before there, for the same reason restores that hold are never paused.
ACTIVE_MAX_PAUSE_S = 5.0

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.GetTopWindow.restype = wintypes.HWND
    _user32.GetTopWindow.argtypes = (wintypes.HWND,)
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    _user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    _user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                       wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    _user32.SetWinEventHook.restype = wintypes.HANDLE
    _user32.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                        _WINEVENTPROC, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD)
    _user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    try:
        _dwm_get = ctypes.windll.dwmapi.DwmGetWindowAttribute
        _dwm_get.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
    except Exception:
        _dwm_get = None
else:
    # Keep these names real attributes (not just absent) so tests can fake
    # them with monkeypatch on macOS/Linux too — the keeper's logic is pure
    # Python and is tested that way there; only the two tests that need a
    # real HWND/windll are Windows-only.
    _user32 = None
    _kernel32 = None
    _dwm_get = None


def _cloaked(hwnd) -> bool:
    """DWM-cloaked windows (other virtual desktops, suspended Store apps)
    report as visible but aren't drawn."""
    if _dwm_get is None:
        return False
    value = ctypes.c_int(0)
    try:
        if _dwm_get(hwnd, _DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) != 0:
            return False
    except Exception:
        return False
    return bool(value.value)


def normal_windows_above(hwnd: int) -> int:
    """How many shown windows without WS_EX_TOPMOST sit above *hwnd*.

    A topmost window can't legitimately have one above it, so any count above
    zero means Windows has demoted it. Returns 0 if *hwnd* isn't found.
    """
    count = 0
    h = _user32.GetTopWindow(None)
    for _ in range(_MAX_WALK):
        if not h:
            return 0  # walked off the end without meeting hwnd
        if h == hwnd:
            return count
        if (_user32.IsWindowVisible(h)
                and not _user32.GetWindowLongW(h, _GWL_EXSTYLE) & _WS_EX_TOPMOST
                and not _cloaked(h)):
            count += 1
        h = _user32.GetWindow(h, _GW_HWNDNEXT)
    return 0


def topmost_above(hwnd: int) -> Optional[int]:
    """The nearest OTHER legitimately-topmost window above *hwnd*, or None.

    With [legit topmost A, ordinary B, pill], a repair must never place the
    pill above A (20 Oct: HWND_TOPMOST always goes to the very top, giving
    [pill, A, B] and un-topping A). Returns the lowest such window above
    *hwnd* so the repair can insert the pill just below it instead.
    """
    last = None
    h = _user32.GetTopWindow(None)
    for _ in range(_MAX_WALK):
        if not h:
            return None
        if h == hwnd:
            return last
        if (_user32.IsWindowVisible(h)
                and _user32.GetWindowLongW(h, _GWL_EXSTYLE) & _WS_EX_TOPMOST
                and not _cloaked(h)):
            last = h
        h = _user32.GetWindow(h, _GW_HWNDNEXT)
    return None


def foreground_exe() -> str:
    """The front window's program name (e.g. Code.exe), or "" if unknown."""
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
        handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if not _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.replace(chr(92), "/").rsplit("/", 1)[-1]
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        return ""


def foreground_hwnd() -> Optional[int]:
    """The foreground window's HWND, or None if unknown.

    Used to key contention per-window rather than per-executable: two
    windows of the same app (e.g. two VS Code windows) must not share one
    offender's back-off, and a closed offender's HWND can never recur as
    the foreground window, so it can't keep a pause going by coincidence.
    """
    try:
        return _user32.GetForegroundWindow() or None
    except Exception:
        return None


def restore_topmost(hwnd: int) -> bool:
    """Put *hwnd* back at the top of the topmost band, without activating it
    — but just below another app's own legitimate topmost window, if one is
    above it, rather than above it (see topmost_above)."""
    above = topmost_above(hwnd)
    after = above if above is not None else _HWND_TOPMOST
    return bool(_user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, _RESTORE_FLAGS))


class TopmostKeeper:
    """Restores the pill when demoted and batches the reports.

    *hwnd* returns the pill's window handle (None while hidden). *report*
    receives a dict: restores, above (most windows seen above), fronts (apps in
    front, in order), held (False when a restore didn't stick and the keeper
    backed off). The first restore is reported at once, later ones at most
    every REPORT_EVERY_S, flushed by check() so a quiet spell loses nothing.
    Install the hook on a thread with a message loop (the Qt GUI thread).
    """

    def __init__(self, hwnd: Callable[[], Optional[int]], report: Callable[[dict], None],
                 *, schedule: Callable[[Callable[[], None]], None] = lambda fn: fn(),
                 clock: Callable[[], float] = time.monotonic,
                 front: Callable[[], str] = lambda: foreground_exe(),
                 front_hwnd: Callable[[], Optional[int]] = foreground_hwnd) -> None:
        self._hwnd = hwnd
        self._report = report
        self._schedule = schedule
        self._clock = clock
        self._front = front          # executable name: logging/reporting only
        self._front_hwnd = front_hwnd  # HWND: contention identity
        self._pending: Optional[dict] = None
        self._last_report = float("-inf")
        self._paused_until = float("-inf")
        # Which foreground window the current run of demotions is against, and
        # how long it's been going: a restore that holds right after check()
        # verifies it can still be undone before the next tick, by the same
        # window, over and over (see CONTENTION_AFTER_S above). Keyed by HWND
        # (falling back to the executable name only when no HWND source is
        # available, e.g. off Windows) so a second window of the same app
        # doesn't inherit another window's back-off.
        self._contend_key: Optional[object] = None
        self._contend_since = float("-inf")
        self._contend_level = 0
        # The last time an actual repair ran (restore_topmost() was called),
        # whatever triggered it — the floor REPAIR_COOLDOWN_S enforces (Sol
        # pill r4). -inf so the very first check() is never gated by it.
        self._last_repair_at = float("-inf")
        # Set by set_active(): a dictation is in progress (overlay LISTENING
        # through PROCESSING), so any pause below is capped at
        # ACTIVE_MAX_PAUSE_S (Sol pill r4).
        self._active = False
        self._hook = None
        self._proc = None  # keep the ctypes callback alive while hooked
        self._suspended = False  # set by suspend(); guards queued callbacks too

    def check(self) -> int:
        """Restore the pill if demoted; how many windows were above it."""
        try:
            if self._suspended:
                return 0  # hidden: also makes an already-queued callback a no-op
            now = self._clock()
            self._flush(now)
            # The exe name is resolved at most once per tick (it's only used
            # as the contention key's fallback, and for the report), same as
            # the HWND-based key itself.
            exe_cache: list = []

            def key_now() -> object:
                hwnd_val = self._front_hwnd()
                if hwnd_val is not None:
                    return hwnd_val
                if not exe_cache:
                    exe_cache.append(self._front())
                return exe_cache[0]

            key = None
            if now < self._paused_until:
                key = key_now()
                if key == self._contend_key:
                    return 0
                # A different window is in front now (or the offender that
                # set this pause is gone — closing a window always changes
                # what the foreground HWND is, so a closed offender can't
                # keep the pause going): give it an immediate, fresh try
                # rather than inheriting the old pause. REPAIR_COOLDOWN_S
                # below still bounds how often that can actually repair
                # anything, so this reset alone can't turn into a storm.
                self._paused_until = float("-inf")
            hwnd = self._hwnd()
            if not hwnd:
                return 0
            above = normal_windows_above(hwnd)
            if not above:
                self._contend_key = None  # held on its own: contention is over
                return 0
            if key is None:
                key = key_now()
            if key != self._contend_key:
                self._contend_key, self._contend_since, self._contend_level = key, now, 0
            # No reset above bypasses this: at most one repair every
            # REPAIR_COOLDOWN_S, full stop (Sol pill r4). Two ordinary
            # windows alternating visibility/foreground every tick reset
            # _contend_key on every single check() and so got a "fresh try"
            # every time, never even reaching BACKOFF_S/CONTENTION_AFTER_S —
            # this is the floor underneath that.
            if now - self._last_repair_at < REPAIR_COOLDOWN_S:
                return above
            self._last_repair_at = now
            restore_topmost(hwnd)
            held = normal_windows_above(hwnd) == 0
            pause = None
            if not held:
                delay = BACKOFF_S
                if self._active:
                    delay = min(delay, ACTIVE_MAX_PAUSE_S)
                self._paused_until = now + delay
                pause = delay
            elif now - self._contend_since >= CONTENTION_AFTER_S:
                # held right now, but front has been re-demoting it on every
                # turn for a while — back off with a growing pause instead of
                # fighting it forever. The exponent is clamped at the point
                # of use (not just on increment below) so even a level that
                # somehow got very large can't make the float conversion
                # raise — min() already saturates the delay past a small
                # level anyway.
                level = min(self._contend_level, _MAX_CONTEND_LEVEL)
                delay = min(BACKOFF_S * (2 ** level), MAX_BACKOFF_S)
                if self._active:
                    delay = min(delay, ACTIVE_MAX_PAUSE_S)
                self._paused_until = now + delay
                self._contend_level = min(self._contend_level + 1, _MAX_CONTEND_LEVEL)
            self._note(now, above, held, exe_cache[0] if exe_cache else self._front(), pause)
            return above
        except Exception:
            return 0  # never let this break the overlay

    def set_active(self, active: bool) -> None:
        """Track whether a dictation is in progress (Sol pill r4; replaces
        note_recording_started() and the per-window blocker-attribution
        heuristic it was paired with — nearest_blocker_above(), _blocker_hwnd
        and _blocker_gone() are gone, with no replacement that tries to guess
        which window is doing the demoting; this flat cap is the fix
        instead). Call with True when the overlay enters LISTENING and it
        should stay True through PROCESSING; call with False once it's back
        to idle (READY/HIDDEN) or in the ERROR banner. Wire it from wherever
        overlay.py's set_state()
        actually crosses into/out of {LISTENING, PROCESSING} — every path
        (show, a transient overlay's remap, the idle watchdog, an error
        auto-return), not just the "show" command handler alone.

        While active, check() caps every pause it sets at ACTIVE_MAX_PAUSE_S
        regardless of why the repair didn't hold, so the pill can never stay
        buried for a whole long back-off during a recording — attributing
        the cause and trusting that attribution is exactly what let a demoter
        that had already exited look identical to a persistent one.

        Turning active on clears any pause/contention tracking, including
        REPAIR_COOLDOWN_S's own clock, and makes one immediate attempt — the
        same as note_recording_started() used to, and still subject to
        check()'s suspended/hidden guard, just not to a cooldown left over
        from whatever was contending before active turned on. That one reset
        can't itself turn into a repair storm: it only ever fires on the
        False-to-True edge, and every check() after it is back to the normal
        REPAIR_COOLDOWN_S-gated pace.
        """
        if active and not self._active:
            self._paused_until = float("-inf")
            self._contend_key = None
            self._contend_since = float("-inf")
            self._contend_level = 0
            self._last_repair_at = float("-inf")
            self._active = True
            self.check()
        else:
            self._active = active

    def _note(self, now: float, above: int, held: bool, front: str,
              pause: float | None = None) -> None:
        p = self._pending or {"restores": 0, "above": 0, "fronts": [], "held": True}
        if pause is not None:
            p["pause_s"] = pause   # how long it now waits, for the log line
        p["restores"] += 1
        p["above"] = max(p["above"], above)
        if front and (not p["fronts"] or p["fronts"][-1] != front) and len(p["fronts"]) < 5:
            p["fronts"].append(front)
        p["held"] = p["held"] and held
        self._pending = p
        self._flush(now)

    def _flush(self, now: float) -> None:
        if self._pending and (now - self._last_report >= REPORT_EVERY_S
                              or not self._pending["held"]):
            pending, self._pending = self._pending, None
            self._last_report = now
            self._report(pending)

    def install_foreground_hook(self) -> bool:
        if self._hook:
            return True

        def _on_foreground(*_args) -> None:
            # Windows reorders the z-order around this event; check once it
            # has settled (the schedule callback runs it on a later Qt turn).
            self._schedule(self.check)

        self._proc = _WINEVENTPROC(_on_foreground)
        self._hook = _user32.SetWinEventHook(
            _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None, self._proc, 0, 0,
            _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
        return bool(self._hook)

    def uninstall(self) -> None:
        if self._hook:
            _user32.UnhookWinEvent(self._hook)
        self._hook = None
        self._proc = None

    def suspend(self) -> None:
        """Stop reacting to foreground changes until resume(): uninstalls
        the hook so no new check gets scheduled, and sets a flag so a
        callback already queued before this call (e.g. Qt's singleShot,
        fired from a foreground change that beat the uninstall) is a no-op
        when it runs. Safe to call repeatedly — only the first call (per
        resume()) does anything, so hide -> hide can't leave two hooks."""
        if self._suspended:
            return
        self._suspended = True
        self.uninstall()

    def resume(self) -> None:
        """Undo suspend(): reinstalls the hook. Safe to call repeatedly or
        when never suspended — show -> show can't install the hook twice."""
        if not self._suspended:
            return
        self._suspended = False
        self.install_foreground_hook()

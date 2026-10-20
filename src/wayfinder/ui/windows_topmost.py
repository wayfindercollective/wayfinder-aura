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
# A restore that doesn't hold, or more than BURST restores in BURST_WINDOW_S,
# means something keeps pushing the pill down: stop for BACKOFF_S rather than
# fight it every second (and flicker).
BURST = 3
BURST_WINDOW_S = 10.0
BACKOFF_S = 60.0

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


def restore_topmost(hwnd: int) -> bool:
    """Put *hwnd* back at the top of the topmost band, without activating it."""
    return bool(_user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _RESTORE_FLAGS))


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
                 front: Callable[[], str] = lambda: foreground_exe()) -> None:
        self._hwnd = hwnd
        self._report = report
        self._schedule = schedule
        self._clock = clock
        self._front = front
        self._pending: Optional[dict] = None
        self._last_report = float("-inf")
        self._recent: list[float] = []
        self._paused_until = float("-inf")
        self._hook = None
        self._proc = None  # keep the ctypes callback alive while hooked

    def check(self) -> int:
        """Restore the pill if demoted; how many windows were above it."""
        try:
            now = self._clock()
            self._flush(now)
            if now < self._paused_until:
                return 0
            hwnd = self._hwnd()
            if not hwnd:
                return 0
            above = normal_windows_above(hwnd)
            if not above:
                return 0
            restore_topmost(hwnd)
            held = normal_windows_above(hwnd) == 0
            self._recent = [t for t in self._recent if now - t < BURST_WINDOW_S] + [now]
            if not held or len(self._recent) > BURST:
                self._paused_until = now + BACKOFF_S
                self._recent = []
            self._note(now, above, held)
            return above
        except Exception:
            return 0  # never let this break the overlay

    def _note(self, now: float, above: int, held: bool) -> None:
        p = self._pending or {"restores": 0, "above": 0, "fronts": [], "held": True}
        p["restores"] += 1
        p["above"] = max(p["above"], above)
        front = self._front()
        if front and (not p["fronts"] or p["fronts"][-1] != front) and len(p["fronts"]) < 5:
            p["fronts"].append(front)
        p["held"] = p["held"] and held and now >= self._paused_until
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

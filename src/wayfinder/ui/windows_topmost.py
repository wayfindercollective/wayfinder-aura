"""Windows: keep the pill above ordinary windows.

20 Oct 2026: with Magnifier running, Windows ranked the pill (still flagged
WS_EX_TOPMOST) below whichever ordinary window was activated, so it sat hidden
behind VS Code, Explorer and Slack. Setting HWND_TOPMOST again brought it back.
Nothing in Aura can stop the demotion, so the overlay notices it (on every
foreground change, plus a 1 s fallback) and puts the pill back on top without
activating it.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Callable, Optional

_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x8
_GW_HWNDNEXT = 2
_HWND_TOPMOST = -1
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE, _SWP_NOOWNERZORDER = 0x1, 0x2, 0x10, 0x200
_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_MAX_WALK = 5000  # z-order walk bound (a desktop has a few hundred windows)

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _user32.GetTopWindow.restype = wintypes.HWND
    _user32.GetTopWindow.argtypes = (wintypes.HWND,)
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
    _user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    _user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT)
    _WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
                                       wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    _user32.SetWinEventHook.restype = wintypes.HANDLE
    _user32.SetWinEventHook.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
                                        _WINEVENTPROC, wintypes.DWORD, wintypes.DWORD,
                                        wintypes.DWORD)
    _user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
    _user32.GetForegroundWindow.restype = wintypes.HWND


def normal_windows_above(hwnd: int) -> int:
    """How many visible windows without WS_EX_TOPMOST sit above *hwnd*.

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
                and not _user32.GetWindowLongW(h, _GWL_EXSTYLE) & _WS_EX_TOPMOST):
            count += 1
        h = _user32.GetWindow(h, _GW_HWNDNEXT)
    return 0


def foreground_exe() -> str:
    """The front window's program name (e.g. Code.exe), or "" if unknown."""
    try:
        hwnd = _user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value.replace(chr(92), "/").rsplit("/", 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def restore_topmost(hwnd: int) -> bool:
    """Put *hwnd* back at the top of the topmost band, without activating it."""
    flags = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE | _SWP_NOOWNERZORDER
    return bool(_user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags))


class TopmostKeeper:
    """Checks the pill's place on foreground changes and on demand.

    *hwnd* returns the pill's window handle (None when hidden or not created),
    *report* gets the number of ordinary windows found above it whenever it
    had to be put back. Install the hook on a thread with a message loop (the
    Qt GUI thread).
    """

    def __init__(self, hwnd: Callable[[], Optional[int]], report: Callable[[int], None],
                 *, schedule: Callable[[Callable[[], None]], None] = lambda fn: fn()) -> None:
        self._hwnd = hwnd
        self._report = report
        self._schedule = schedule
        self._hook = None
        self._proc = None  # keep the ctypes callback alive while hooked

    def check(self) -> int:
        """Restore the pill if it was demoted; how many windows were above it."""
        try:
            hwnd = self._hwnd()
            if not hwnd:
                return 0
            above = normal_windows_above(hwnd)
            if above:
                restore_topmost(hwnd)
                self._report(above)
            return above
        except Exception:
            return 0  # never let a diagnostic-grade helper break the overlay

    def install_foreground_hook(self) -> bool:
        if self._hook:
            return True

        def _on_foreground(*_args) -> None:
            # Windows reorders the z-order around this event; check once it
            # has settled (the schedule callback runs it on the next Qt turn).
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

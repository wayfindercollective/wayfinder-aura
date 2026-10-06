"""Windows sleep, wake, and display-change notifications.

The counterpart of ``macos_lifecycle``: same callbacks, same use (cancel a
live recording before sleep; after wake rebuild ducking and the mic and put
the pill back; reposition it when displays change). Windows broadcasts
``WM_POWERBROADCAST`` and ``WM_DISPLAYCHANGE`` to top-level windows, so a
hidden, never-shown top-level window on its own thread receives them.
Callbacks run on that thread: callers must marshal onto Tk (the app queues
them as UI_CALLBACK events). Windows-only; ``start`` returns None elsewhere.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

_WM_QUERYENDSESSION = 0x0011
_WM_ENDSESSION = 0x0016
_WM_DESTROY = 0x0002
_WM_CLOSE = 0x0010
_WM_DISPLAYCHANGE = 0x007E
_WM_SETTINGCHANGE = 0x001A
_WM_POWERBROADCAST = 0x0218
_PBT_APMSUSPEND = 0x0004
_PBT_APMRESUMEAUTOMATIC = 0x0012
_PBT_APMRESUMESUSPEND = 0x0007      # resumed by the user (after APMRESUMEAUTOMATIC)
_PBT_POWERSETTINGCHANGE = 0x8013
# GUID_CONSOLE_DISPLAY_STATE {6FE69556-704A-47A0-8F24-C28D936FDA47}: the
# display turning off/on/dimming. Modern Standby laptops can sleep and wake
# with only these (no APMSUSPEND), so they are logged for diagnostics.
_GUID_CONSOLE_DISPLAY_STATE = bytes.fromhex("5695e66f4a70a0478f24c28d936fda47")
_DISPLAY_STATES = {0: "display off", 1: "display on", 2: "display dimmed"}
_SPI_SETWORKAREA = 0x002F

_LRESULT = ctypes.c_ssize_t
# WINFUNCTYPE exists only on Windows; CFUNCTYPE keeps the module importable
# elsewhere (dispatch() is unit-tested on every platform).
_WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


def _read_power_setting(lparam: int):
    """(GUID bytes, first DWORD of data) from a POWERBROADCAST_SETTING*, or None."""
    if not lparam:
        return None
    try:
        guid = ctypes.string_at(lparam, 16)
        length = int.from_bytes(ctypes.string_at(lparam + 16, 4), "little")
        if length < 4:
            return guid, None
        return guid, int.from_bytes(ctypes.string_at(lparam + 20, 4), "little")
    except Exception:
        return None


def dispatch(msg: int, wparam: int, lparam: int, *, on_sleep=None, on_wake=None,
             on_screens_changed=None, on_end_session=None, on_power_event=None) -> None:
    """Map one window message to a callback (pure; unit-tested).

    *on_power_event(text)* is diagnostics only: every sleep/wake and display
    on/off/dimmed, so a Modern Standby wake the app missed shows in the log."""
    if msg == _WM_ENDSESSION:
        # Sign-out, shutdown, or an installer's Restart Manager closing Aura
        # (ENDSESSION_CLOSEAPP). The window's X only hides to the tray, so this
        # is how Windows asks Aura to really quit (the Mac's Quit Apple event).
        if wparam and on_end_session:
            on_end_session()
    elif msg == _WM_POWERBROADCAST:
        if on_power_event:
            if wparam == _PBT_APMSUSPEND:
                on_power_event("going to sleep")
            elif wparam == _PBT_APMRESUMEAUTOMATIC:
                on_power_event("woke up")
            elif wparam == _PBT_APMRESUMESUSPEND:
                on_power_event("woke up (user present)")
            elif wparam == _PBT_POWERSETTINGCHANGE:
                setting = _read_power_setting(lparam)
                if setting and setting[0] == _GUID_CONSOLE_DISPLAY_STATE:
                    on_power_event(_DISPLAY_STATES.get(setting[1], f"display state {setting[1]}"))
        if wparam == _PBT_APMSUSPEND and on_sleep:
            on_sleep()
        elif wparam == _PBT_APMRESUMEAUTOMATIC and on_wake:
            on_wake()
    elif msg == _WM_DISPLAYCHANGE or (msg == _WM_SETTINGCHANGE and wparam == _SPI_SETWORKAREA):
        if on_screens_changed:
            on_screens_changed()


class WindowsLifecycleObserver:
    """Hidden top-level window + message loop on a daemon thread."""

    _CLASS_NAME = "WayfinderAuraLifecycle"

    def __init__(self, callbacks: dict):
        self._callbacks = callbacks
        self._hwnd = None
        self._ready = threading.Event()
        self._proc = _WNDPROC(self._wndproc)  # keep a reference for the thread's lifetime
        self._thread = threading.Thread(target=self._run, daemon=True, name="wayfinder-lifecycle")

    @classmethod
    def start(cls, *, on_sleep=None, on_wake=None, on_screens_changed=None,
              on_end_session=None, on_power_event=None):
        if sys.platform != "win32":
            return None
        observer = cls({"on_sleep": on_sleep, "on_wake": on_wake,
                        "on_screens_changed": on_screens_changed,
                        "on_end_session": on_end_session,
                        "on_power_event": on_power_event})
        observer._thread.start()
        observer._ready.wait(timeout=2.0)
        return observer if observer._hwnd else None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            dispatch(msg, wparam, lparam, **self._callbacks)
        except Exception:
            pass
        user32 = ctypes.windll.user32
        if msg == _WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == _WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        if msg in (_WM_POWERBROADCAST, _WM_QUERYENDSESSION):
            return 1  # TRUE: never veto a suspend, sign-out or app close
        if msg == _WM_ENDSESSION:
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = _LRESULT
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = _WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self._CLASS_NAME
            user32.RegisterClassW(ctypes.byref(wc))  # already registered is fine
            self._hwnd = user32.CreateWindowExW(
                0, self._CLASS_NAME, "Wayfinder Aura lifecycle", 0,
                0, 0, 0, 0, None, None, hinst, None)
        finally:
            self._ready.set()
        if not self._hwnd:
            return
        if self._callbacks.get("on_power_event"):
            # Display on/off/dimmed arrive only to windows that ask for them.
            try:
                user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
                user32.RegisterPowerSettingNotification.argtypes = [
                    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
                self._display_guid = ctypes.create_string_buffer(_GUID_CONSOLE_DISPLAY_STATE, 16)
                self._power_notify = user32.RegisterPowerSettingNotification(
                    self._hwnd, self._display_guid, 0)  # DEVICE_NOTIFY_WINDOW_HANDLE
            except Exception:
                self._power_notify = None
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, _WM_CLOSE, 0, 0)

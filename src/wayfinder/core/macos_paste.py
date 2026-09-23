"""Native macOS paste primitives for the clipboard injector.

* ``accessibility_trusted`` — without Accessibility, macOS silently drops
  synthetic key events, so a paste must be refused (and the text left on the
  clipboard) instead of being reported as typed.
* ``post_command_v`` — Cmd+V posted straight through Quartz. The V keycode
  comes from the active keyboard layout (on Dvorak, QWERTY's V position is K,
  so a fixed keycode sent Cmd+K: Terminal clears scrollback, Slack searches).
  No PyAutoGUI: its ~140ms of built-in pauses and its corner fail-safe (which
  aborts the paste whenever the pointer rests in a screen corner) are gone.
* ``DeferredRestore`` — the previous clipboard comes back ~0.8s after the paste
  on a background timer, so slow Electron/remote apps read the dictation, and
  the app returns to idle immediately. A new paste flushes a pending restore
  first so its snapshot never captures Aura's own text.

macOS-only; imported lazily by ``injector`` on darwin.
"""

from __future__ import annotations

import ctypes
import threading

_KVK_ANSI_V = 0x09
_KVK_COMMAND = 0x37
_KVK_RETURN = 0x24
_CARBON = "/System/Library/Frameworks/Carbon.framework/Carbon"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_UC_KEY_ACTION_DISPLAY = 3
_UC_KEY_TRANSLATE_NO_DEAD_KEYS = 1

RESTORE_DELAY_S = 0.8


def accessibility_trusted() -> bool | None:
    """True/False from AXIsProcessTrusted, or None if it cannot be queried."""
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        return None


def keycode_for_character(character: str) -> int | None:
    """Virtual keycode that types ``character`` (no modifiers) in the active layout."""
    try:
        carbon = ctypes.cdll.LoadLibrary(_CARBON)
        cf = ctypes.cdll.LoadLibrary(_CORE_FOUNDATION)
        carbon.TISCopyCurrentKeyboardLayoutInputSource.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
        carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        carbon.LMGetKbdType.restype = ctypes.c_uint8
        cf.CFDataGetBytePtr.restype = ctypes.c_void_p
        cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        translate = carbon.UCKeyTranslate
        translate.restype = ctypes.c_int32
        translate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        layout_key = ctypes.c_void_p.in_dll(carbon, "kTISPropertyUnicodeKeyLayoutData")

        source = carbon.TISCopyCurrentKeyboardLayoutInputSource()
        if not source:
            return None
        try:
            data = carbon.TISGetInputSourceProperty(source, layout_key)
            if not data:
                return None  # e.g. some input methods expose no uchr layout
            layout = cf.CFDataGetBytePtr(data)
            kbd_type = carbon.LMGetKbdType()
            for code in range(128):
                dead = ctypes.c_uint32(0)
                length = ctypes.c_ulong(0)
                chars = (ctypes.c_uint16 * 4)()
                status = translate(
                    layout, code, _UC_KEY_ACTION_DISPLAY, 0, kbd_type,
                    _UC_KEY_TRANSLATE_NO_DEAD_KEYS, ctypes.byref(dead), 4,
                    ctypes.byref(length), ctypes.cast(chars, ctypes.c_void_p),
                )
                if status == 0 and length.value == 1 and chr(chars[0]) == character:
                    return code
            return None
        finally:
            cf.CFRelease(source)
    except Exception:
        return None


def post_command_v() -> None:
    """Post Cmd+V (layout-aware V) to the frontmost app via Quartz.

    Sends the full physical sequence — Command down, V down, V up, Command up
    (flags cleared) — like a real keyboard. Posting only flagged V events
    leaves the HID system believing Command is still held, which turns the
    user's next clicks and keys into Cmd-clicks/shortcuts.
    """
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        CGEventSetFlags,
        kCGEventFlagMaskCommand,
        kCGHIDEventTap,
    )

    code = keycode_for_character("v")
    if code is None:
        code = _KVK_ANSI_V
    sequence = (
        (_KVK_COMMAND, True, kCGEventFlagMaskCommand),
        (code, True, kCGEventFlagMaskCommand),
        (code, False, kCGEventFlagMaskCommand),
        (_KVK_COMMAND, False, 0),
    )
    for keycode, pressed, flags in sequence:
        event = CGEventCreateKeyboardEvent(None, keycode, pressed)
        if event is None:
            raise RuntimeError("could not create the Cmd+V key event")
        CGEventSetFlags(event, flags)
        CGEventPost(kCGHIDEventTap, event)


def post_return() -> None:
    """Post a plain Return (no modifiers) to the frontmost app via Quartz."""
    from Quartz import CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags, kCGHIDEventTap

    for pressed in (True, False):
        event = CGEventCreateKeyboardEvent(None, _KVK_RETURN, pressed)
        if event is None:
            raise RuntimeError("could not create the Return key event")
        CGEventSetFlags(event, 0)
        CGEventPost(kCGHIDEventTap, event)


def frontmost_window_id() -> str | None:
    """"<pid>:<window number>" for the frontmost app's front window, or None.

    Window numbers and owner pids need no Screen Recording permission (only
    titles do). Falls back to "<pid>:" when the app has no on-screen window.
    """
    try:
        from AppKit import NSWorkspace
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListExcludeDesktopElements,
            kCGWindowListOptionOnScreenOnly,
        )

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = int(app.processIdentifier())
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
            kCGNullWindowID,
        ) or []
        for info in windows:  # front-to-back
            if int(info.get("kCGWindowOwnerPID", -1)) == pid and int(info.get("kCGWindowLayer", 1)) == 0:
                return f"{pid}:{int(info.get('kCGWindowNumber', 0))}"
        return f"{pid}:"
    except Exception:
        return None


class DeferredRestore:
    """One pending clipboard restore, run on a timer or flushed early."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._job = None

    def schedule(self, restore, delay: float = RESTORE_DELAY_S) -> None:
        """Run ``restore()`` after ``delay`` (flushing any earlier pending one)."""
        self.flush()
        with self._lock:
            self._job = restore
            self._timer = threading.Timer(delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> None:
        """Run the pending restore now (before a new paste snapshots the clipboard)."""
        with self._lock:
            timer, job = self._timer, self._job
            self._timer = self._job = None
        if timer is not None:
            timer.cancel()
        if job is not None:
            try:
                job()
            except Exception:
                pass

    def _fire(self) -> None:
        with self._lock:
            job = self._job
            self._timer = self._job = None
        if job is not None:
            try:
                job()
            except Exception:
                pass


pending_restore = DeferredRestore()

"""Windows text-injection adapter for Wayfinder Aura.

Windows-only. Never imported unconditionally — ``injector.py`` imports this
lazily from inside ``sys.platform == "win32"`` branches so the Win32/ctypes
surface never loads on Linux or macOS (platform-development contract).

Primary path types the transcript at the cursor with the Win32 ``SendInput``
API using ``KEYEVENTF_UNICODE`` scan codes. That injects real Unicode text into
whatever control has focus without touching the clipboard, handles characters
outside the BMP via UTF-16 surrogate pairs, and needs no external tools. A
clipboard-preserving Ctrl+V paste path backs the Game Mode fallback and the
explicit clipboard-paste entry point, matching the macOS/Linux behavior.

Safety, per the contract's Windows checklist:
- Unicode throughout (surrogate-pair aware).
- The paste path saves and restores the clipboard.
- Injection fails closed when no window has focus, or while a Ctrl/Alt/Shift/
  Win modifier is still physically held (a leftover from the Ctrl+Alt+Space
  hotkey would otherwise turn typed letters into shortcuts or wrong case).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from .injector import InjectionError

# ---------------------------------------------------------------------------
# Win32 SendInput plumbing
# ---------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = wintypes.WPARAM  # pointer-sized unsigned (matches dwExtraInfo)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_RETURN = 0x0D
VK_TAB = 0x09
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_SHIFT = 0x10
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56

# Tag on injected events so the app can recognize its own synthetic input.
_WAYFINDER_INJECT_SIGNATURE = 0x57464149  # "WFAI"

# Send at most this many INPUT events per SendInput call.
_SEND_CHUNK = 512


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetForegroundWindow.restype = wintypes.HWND


def _keyboard_input(*, wVk: int = 0, wScan: int = 0, flags: int = 0) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki = _KEYBDINPUT(
        wVk=wVk,
        wScan=wScan,
        dwFlags=flags,
        time=0,
        dwExtraInfo=_WAYFINDER_INJECT_SIGNATURE,
    )
    return inp


def _unicode_inputs(code_unit: int) -> list[_INPUT]:
    """Down+up INPUT pair that types one UTF-16 code unit as Unicode."""
    return [
        _keyboard_input(wScan=code_unit, flags=KEYEVENTF_UNICODE),
        _keyboard_input(wScan=code_unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def _vkey_inputs(vk: int) -> list[_INPUT]:
    """Down+up INPUT pair for a virtual key (e.g. Enter, Tab)."""
    return [
        _keyboard_input(wVk=vk),
        _keyboard_input(wVk=vk, flags=KEYEVENTF_KEYUP),
    ]


def _char_inputs(ch: str) -> list[_INPUT]:
    """INPUT events that reproduce one character at the cursor.

    Newlines become a real Enter keypress (a literal U+000A is inserted
    inconsistently across apps); a lone carriage return is dropped so CRLF
    yields a single Enter. Everything else is typed as Unicode, splitting
    astral code points into their UTF-16 surrogate pair.
    """
    if ch == "\r":
        return []
    if ch == "\n":
        return _vkey_inputs(VK_RETURN)
    if ch == "\t":
        return _vkey_inputs(VK_TAB)
    inputs: list[_INPUT] = []
    encoded = ch.encode("utf-16-le")
    for i in range(0, len(encoded), 2):
        code_unit = encoded[i] | (encoded[i + 1] << 8)
        inputs.extend(_unicode_inputs(code_unit))
    return inputs


def _send(inputs: list[_INPUT]) -> None:
    """Send INPUT events through SendInput in bounded chunks. Raises on failure."""
    if not inputs:
        return
    for start in range(0, len(inputs), _SEND_CHUNK):
        chunk = inputs[start : start + _SEND_CHUNK]
        n = len(chunk)
        arr = (_INPUT * n)(*chunk)
        sent = _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))
        if sent != n:
            err = ctypes.get_last_error()
            raise InjectionError(
                f"SendInput injected {sent}/{n} events (WinError {err}). "
                "The foreground app may block synthetic input (e.g. an elevated "
                "window while Aura runs unelevated)."
            )


# ---------------------------------------------------------------------------
# Modifier-release gate (fail closed while Ctrl/Alt/Shift/Win are held)
# ---------------------------------------------------------------------------

_MODIFIER_VKS = (VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN)


def modifiers_held_windows() -> bool:
    """True while any Ctrl/Alt/Shift/Win key is physically down."""
    return any(_user32.GetAsyncKeyState(vk) & 0x8000 for vk in _MODIFIER_VKS)


def wait_for_modifier_release_windows(timeout: float = 2.0, poll: float = 0.1) -> bool:
    """Bounded wait until no modifier is held. True when clear, False on timeout.

    A transient, bounded wait immediately before injection — not a repeating
    idle timer — so the 100ms poll honors the no-sub-100ms-polling rule. The
    common case (hotkey already released) returns on the first check.
    """
    deadline = time.monotonic() + timeout
    while True:
        if not modifiers_held_windows():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def require_modifier_release_windows() -> None:
    """Raise InjectionError if a modifier is still held after the bounded wait.

    Typing through a live Ctrl fires app shortcuts instead of inserting text;
    a live Shift changes every letter's case and punctuation. The full
    transcript stays available in Aura's Last Transcription UI on this error.
    """
    if not wait_for_modifier_release_windows():
        raise InjectionError(
            "A Ctrl, Alt, Shift, or Windows key is still held. "
            "Release it, then dictate again; the transcript was not typed."
        )


def _require_foreground_window() -> None:
    """Fail closed when nothing has focus, so we never type into the void."""
    if not _user32.GetForegroundWindow():
        raise InjectionError(
            "No foreground window to receive the text; click into a text field "
            "and dictate again. The transcript was not typed."
        )


# ---------------------------------------------------------------------------
# Public injection entry points
# ---------------------------------------------------------------------------

# Seconds between keystrokes per speed preset (mirrors the Linux/macOS presets).
_TYPING_INTERVALS = {
    "instant": 0.0,
    "fast": 0.0,
    "normal": 0.012,
    "slow": 0.05,
    "very_slow": 0.1,
}


def inject_text_windows(text: str, typing_speed: str = "instant") -> None:
    """Type *text* at the cursor via SendInput Unicode injection.

    Fails closed (no window focused, or a modifier still held) rather than
    typing into the wrong place or with corrupted casing.
    """
    if not text:
        return
    _require_foreground_window()
    require_modifier_release_windows()

    interval = _TYPING_INTERVALS.get(typing_speed, 0.0)
    if interval <= 0:
        # Instant: one batched stream of events.
        inputs: list[_INPUT] = []
        for ch in text:
            inputs.extend(_char_inputs(ch))
        _send(inputs)
        return

    # Paced: emit each character, sleeping between them.
    for ch in text:
        _send(_char_inputs(ch))
        time.sleep(interval)


def press_enter_windows() -> None:
    """Synthesize a single Enter keypress (Auto-press-Enter setting)."""
    _require_foreground_window()
    # A held Shift would send Shift+Enter — a newline instead of submit.
    require_modifier_release_windows()
    _send(_vkey_inputs(VK_RETURN))


# ---------------------------------------------------------------------------
# Clipboard paste path (Win32 clipboard, save + restore)
# ---------------------------------------------------------------------------

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

_user32.OpenClipboard.argtypes = (wintypes.HWND,)
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = (wintypes.UINT,)
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
_user32.SetClipboardData.restype = wintypes.HANDLE
_kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalUnlock.restype = wintypes.BOOL


def _open_clipboard(retries: int = 5) -> bool:
    """OpenClipboard with brief retries (another app may hold it momentarily)."""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _clipboard_get_windows() -> str | None:
    """Current clipboard text, or None if empty/unavailable/non-text."""
    if not _open_clipboard():
        return None
    try:
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def _clipboard_set_windows(text: str) -> bool:
    """Replace clipboard contents with *text* (CF_UNICODETEXT). True on success."""
    if not _open_clipboard():
        return False
    try:
        if not _user32.EmptyClipboard():
            return False
        buffer = ctypes.create_unicode_buffer(text)  # NUL-terminated UTF-16
        size = ctypes.sizeof(buffer)
        h_global = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not h_global:
            return False
        ptr = _kernel32.GlobalLock(h_global)
        if not ptr:
            return False
        ctypes.memmove(ptr, buffer, size)
        _kernel32.GlobalUnlock(h_global)
        # Ownership of h_global passes to the system on success.
        if not _user32.SetClipboardData(_CF_UNICODETEXT, h_global):
            return False
        return True
    finally:
        _user32.CloseClipboard()


def inject_text_paste_windows(text: str) -> None:
    """Inject *text* via clipboard + Ctrl+V, restoring the prior clipboard.

    Used for the Game Mode type-failure fallback and the explicit
    clipboard-paste entry point (apps that reject synthetic per-character input).
    """
    if not text:
        return
    _require_foreground_window()
    require_modifier_release_windows()

    previous = _clipboard_get_windows()
    if not _clipboard_set_windows(text):
        raise InjectionError("Could not write to the Windows clipboard for paste.")
    try:
        time.sleep(0.03)
        _send(
            [
                _keyboard_input(wVk=VK_CONTROL),
                _keyboard_input(wVk=VK_V),
                _keyboard_input(wVk=VK_V, flags=KEYEVENTF_KEYUP),
                _keyboard_input(wVk=VK_CONTROL, flags=KEYEVENTF_KEYUP),
            ]
        )
    finally:
        # Best-effort restore, only if the clipboard still holds our text
        # (the user may have copied something new in the meantime).
        if previous is not None:
            try:
                time.sleep(0.08)
                if _clipboard_get_windows() == text:
                    _clipboard_set_windows(previous)
            except Exception:
                pass

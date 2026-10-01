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
import os
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes

from .injector import InjectionError

# ---------------------------------------------------------------------------
# Win32 SendInput plumbing
# ---------------------------------------------------------------------------

# hasattr(ctypes, "WinDLL") is True only on real Windows, regardless of any
# patched sys.platform — so this module stays importable on Linux/macOS (e.g.
# tests that fake win32). The functions below are only ever called on Windows.
if hasattr(ctypes, "WinDLL"):
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - import-safety shim for non-Windows
    _user32 = None
    _kernel32 = None

ULONG_PTR = wintypes.WPARAM  # pointer-sized unsigned (matches dwExtraInfo)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

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


if _user32 is not None:
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT
    _user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
    _user32.MapVirtualKeyW.restype = wintypes.UINT


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


def _foreground_is_own_process() -> bool:
    """True when Aura's own main window is in front (the pill never takes focus)."""
    if _user32 is None:
        return False
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == os.getpid()


def _refuse_own_window(text: str) -> None:
    """The Mac's self-target check: a global hotkey pressed while Aura itself
    is in front would paste into Aura and report success though nothing reached
    the intended app. Keep the text on the clipboard and say so instead."""
    if not _foreground_is_own_process():
        return
    copied = _clipboard_set_windows(text)
    where = ("on the clipboard — click a text field in another app and press Ctrl+V"
             if copied else "in Aura History")
    raise InjectionError(
        "Wayfinder Aura was frontmost, so there was no external paste target. "
        f"Your text is {where}."
    )


def foreground_window_id() -> str | None:
    """The foreground window handle as a string, or None.

    Backs ``injector.get_active_window`` so the Auto-Enter focus guard can see
    a window switch between injecting the text and pressing Return.
    """
    if _user32 is None:
        return None
    hwnd = _user32.GetForegroundWindow()
    return str(hwnd) if hwnd else None


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
    _refuse_own_window(text)
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


# Game chat (Gamer mode): some engines sample the keyboard once per frame and
# miss a key whose down and up arrive together, and read hardware scan codes
# rather than virtual keys. While hold_keys() is active, Ctrl+V and Enter go
# out as scan codes and each key is held (the Mac's macos_paste.hold_keys).
_hold = threading.local()


@contextmanager
def hold_keys(seconds: float):
    """Hold each synthesized Ctrl+V / Enter key for *seconds* (game chat)."""
    previous = getattr(_hold, "seconds", 0.0)
    _hold.seconds = max(0.0, float(seconds))
    try:
        yield
    finally:
        _hold.seconds = previous


def _held_seconds() -> float:
    return getattr(_hold, "seconds", 0.0)


def _scan_input(vk: int, *, up: bool) -> _INPUT:
    scan = _user32.MapVirtualKeyW(vk, 0) if _user32 is not None else 0  # MAPVK_VK_TO_VSC
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    return _keyboard_input(wScan=scan, flags=flags)


def _press_keys(vks: list[int]) -> None:
    """Press *vks* in order and release in reverse.

    Normally one batched SendInput (as before). Inside hold_keys(): scan codes,
    with the keys held down for the configured time before release.
    """
    hold = _held_seconds()
    if hold <= 0:
        _send([_keyboard_input(wVk=vk) for vk in vks]
              + [_keyboard_input(wVk=vk, flags=KEYEVENTF_KEYUP) for vk in reversed(vks)])
        return
    _send([_scan_input(vk, up=False) for vk in vks])
    time.sleep(hold)
    _send([_scan_input(vk, up=True) for vk in reversed(vks)])


def press_enter_windows() -> None:
    """Synthesize a single Enter keypress (Auto-press-Enter setting)."""
    _require_foreground_window()
    # A held Shift would send Shift+Enter — a newline instead of submit.
    require_modifier_release_windows()
    if _held_seconds() > 0:
        _press_keys([VK_RETURN])
        return
    _send(_vkey_inputs(VK_RETURN))


# ---------------------------------------------------------------------------
# Clipboard paste path (Win32 clipboard, save + restore)
# ---------------------------------------------------------------------------

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002

if _user32 is not None:
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
    _user32.RegisterClipboardFormatW.argtypes = (wintypes.LPCWSTR,)
    _user32.RegisterClipboardFormatW.restype = wintypes.UINT

# Registered formats Windows' clipboard history (Win+V), Cloud Clipboard sync
# and well-behaved clipboard managers honour. Dictations and the restored
# previous clipboard are marked with them so each dictation doesn't leave two
# history entries or sync to the user's other devices.
_TRANSIENT_FORMATS = (
    ("ExcludeClipboardContentFromMonitorProcessing", None),
    ("CanIncludeInClipboardHistory", 0),
    ("CanUploadToCloudClipboard", 0),
)


def _global_copy(raw: bytes):
    """A GMEM_MOVEABLE block holding *raw*, or None."""
    h_global = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, max(1, len(raw)))
    if not h_global:
        return None
    ptr = _kernel32.GlobalLock(h_global)
    if not ptr:
        return None
    if raw:
        ctypes.memmove(ptr, raw, len(raw))
    _kernel32.GlobalUnlock(h_global)
    return h_global


def _mark_transient() -> None:
    """Add the clipboard-history exclusion formats (clipboard already open)."""
    for name, dword in _TRANSIENT_FORMATS:
        try:
            fmt = _user32.RegisterClipboardFormatW(name)
            if not fmt:
                continue
            raw = b"\x00" if dword is None else int(dword).to_bytes(4, "little")
            h_global = _global_copy(raw)
            if h_global:
                _user32.SetClipboardData(fmt, h_global)
        except Exception:
            pass  # best effort: the paste itself must not fail over a hint


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


def _clipboard_set_windows(text: str, transient: bool = False) -> bool:
    """Replace clipboard contents with *text* (CF_UNICODETEXT). True on success.

    *transient* also marks it as excluded from clipboard history and sync.
    """
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
        if transient:
            _mark_transient()
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
    diag = _new_paste_diagnostics(text)
    try:
        _require_foreground_window()
        _refuse_own_window(text)
        diag.mark("mods")
        require_modifier_release_windows()
        diag.mark("mods_free")

        previous = _clipboard_get_windows()
        diag.previous(previous)
        if not _clipboard_set_windows(text, transient=True):
            raise InjectionError("Could not write to the Windows clipboard for paste.")
        diag.mark("set")
        try:
            time.sleep(0.03)
            diag.start_watch()
            _press_keys([VK_CONTROL, VK_V])
            diag.mark("ctrl_v")
        finally:
            # Best-effort restore, only if the clipboard still holds our text
            # (the user may have copied something new in the meantime).
            if previous is not None:
                try:
                    time.sleep(0.08)
                    current = _clipboard_get_windows()
                    diag.at_restore(current == text)
                    if current == text:
                        _clipboard_set_windows(previous, transient=True)
                        diag.mark("restored")
                except Exception:
                    pass
    except Exception as exc:
        diag.failed(exc)
        raise
    finally:
        diag.finish()


# ---------------------------------------------------------------------------
# Paste diagnostics (observation only: never changes what is pasted or when)
# ---------------------------------------------------------------------------
#
# One "🔎 Paste" line per clipboard paste, for chasing reports like "it pasted
# my old clipboard" or "nothing pasted". A watcher thread notes which app opens
# the clipboard after Ctrl+V and when: a target that reads only after the old
# clipboard was put back pastes the old text (LATE READ). Logs lengths, app
# names and timings, never the dictated or clipboard text.

_paste_reporter = None
_WATCH_SECONDS = 1.5

if _user32 is not None:
    _user32.GetOpenClipboardWindow.restype = wintypes.HWND
    _user32.GetOpenClipboardWindow.argtypes = ()
    _user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


def set_paste_reporter(reporter) -> None:
    """Route paste diagnostics to *reporter(line)* (the app's activity log)."""
    global _paste_reporter
    _paste_reporter = reporter


def _window_app(hwnd) -> str:
    """'exe/WindowClass' for *hwnd* (or '-')."""
    if _user32 is None or not hwnd:
        return "-"
    try:
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = "?"
        handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(520)
                size = wintypes.DWORD(len(buf))
                if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    exe = buf.value.replace("/", "\\").rsplit("\\", 1)[-1]
            finally:
                _kernel32.CloseHandle(handle)
        if pid.value == os.getpid():
            exe = "Aura"
        cls = ctypes.create_unicode_buffer(128)
        _user32.GetClassNameW(hwnd, cls, 128)
        return f"{exe}/{cls.value}"
    except Exception:
        return "?"


def _best_effort(method):
    """Diagnostics never raise into the paste (a failed thread start, a ctypes
    error...): the paste's own steps and exceptions are all that count."""
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            return None
    wrapper.__name__ = method.__name__
    return wrapper


class _NullDiagnostics:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _new_paste_diagnostics(text: str):
    try:
        return _PasteDiagnostics(text)
    except Exception:
        return _NullDiagnostics()


class _PasteDiagnostics:
    def __init__(self, text: str):
        self._t0 = time.perf_counter()
        self._marks: dict[str, float] = {}
        self._chars = len(text)
        self._prev = "?"
        self._held_at_restore: bool | None = None
        self._error: str | None = None
        self._reads: list[tuple[float, str]] = []
        self._watch: threading.Thread | None = None
        self._stop = threading.Event()
        self._target = _window_app(_user32.GetForegroundWindow()) if _user32 else "-"

    def _ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    @_best_effort
    def mark(self, name: str) -> None:
        self._marks[name] = self._ms()

    @_best_effort
    def previous(self, value) -> None:
        self._prev = "none" if value is None else f"{len(value)} chars"

    @_best_effort
    def at_restore(self, still_ours: bool) -> None:
        self.mark("restore_check")
        self._held_at_restore = still_ours

    @_best_effort
    def failed(self, exc: Exception) -> None:
        self._error = f"{type(exc).__name__}: {exc}"[:160]

    @_best_effort
    def start_watch(self) -> None:
        if _user32 is None:
            return

        def watch():
            last = None
            end = time.perf_counter() + _WATCH_SECONDS
            while time.perf_counter() < end and not self._stop.is_set():
                try:
                    hwnd = _user32.GetOpenClipboardWindow()
                except Exception:
                    break
                if hwnd and hwnd != last:
                    app = _window_app(hwnd)
                    if not app.startswith("Aura/"):
                        self._reads.append((self._ms(), app))
                last = hwnd
                time.sleep(0.0005)

        self._watch = threading.Thread(target=watch, name="paste-diag", daemon=True)
        self._watch.start()

    @_best_effort
    def finish(self) -> None:
        reporter = _paste_reporter
        if reporter is None:
            self._stop.set()
            return
        fg_after = _window_app(_user32.GetForegroundWindow()) if _user32 else "-"

        def emit():
            if self._watch is not None:
                self._watch.join(_WATCH_SECONDS + 0.5)
            try:
                reporter(self._line(fg_after))
            except Exception:
                pass

        threading.Thread(target=emit, name="paste-diag-report", daemon=True).start()

    def _line(self, fg_after: str) -> str:
        m = self._marks
        ctrl_v = m.get("ctrl_v")
        restored = m.get("restored")
        parts = [f"{self._chars} chars → {self._target}"]
        if fg_after != self._target:
            parts.append(f"front after: {fg_after}")
        parts.append(f"old clipboard {self._prev}")
        if "mods_free" in m and "mods" in m:
            wait = m["mods_free"] - m["mods"]
            if wait >= 50:
                parts.append(f"waited {wait:.0f}ms for keys up")
        if ctrl_v is not None:
            parts.append(f"Ctrl+V at {ctrl_v:.0f}ms")
        if self._held_at_restore is False:
            parts.append("clipboard changed by another app before restore (not restored)")
        elif restored is not None:
            parts.append(f"old clipboard back at {restored:.0f}ms")
        target_exe = self._target.split("/", 1)[0]
        late = False
        if self._reads:
            reads = []
            for t, app in self._reads[:5]:
                own = app.split("/", 1)[0] == target_exe
                after = restored is not None and t > restored
                late = late or (own and after)
                tag = " LATE READ" if own and after else ("" if own else " (background)")
                reads.append(f"{app} at {t:.0f}ms{tag}")
            parts.append("clipboard opened by " + ", ".join(reads))
        elif ctrl_v is not None:
            parts.append("no clipboard read seen")
        if self._error:
            parts.append(f"FAILED {self._error}")
        verdict = "⚠ " if (self._error or late) else ""
        return f"🔎 Paste {verdict}" + " · ".join(parts)

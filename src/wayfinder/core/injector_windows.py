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

import atexit
import ctypes
import os
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes

from .injector import InjectionCancelled, InjectionError, _refuse_if_cancelled, cancellable

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
    """Send INPUT events through SendInput in bounded chunks. Raises on failure.

    None with a key press for a retired dictation, whatever waited before it
    (a modifier wait, a typing pace, a clipboard settle: Sol mac r12). Key
    releases always go out: a key left down repeats in the target app."""
    if not inputs:
        return
    for start in range(0, len(inputs), _SEND_CHUNK):
        chunk = inputs[start : start + _SEND_CHUNK]
        # Each native batch with a press asks, so a retirement while an
        # earlier one ran sends no more (Sol mac r13). Keys the batches sent
        # so far left down go back up first.
        if any(not (inp.u.ki.dwFlags & KEYEVENTF_KEYUP) for inp in chunk):
            try:
                _refuse_if_cancelled()
            except InjectionCancelled:
                _send_quietly(_releases_for(inputs[:start]))
                raise
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


def _releases_for(sent: list[_INPUT]) -> list[_INPUT]:
    """Key-ups for the keys *sent* leaves down, last pressed first. Typing
    sends each key's press and release together, so normally none."""
    down: dict = {}
    for inp in sent:
        ki = inp.u.ki
        key = (ki.wVk, ki.wScan, ki.dwFlags & ~KEYEVENTF_KEYUP)
        if ki.dwFlags & KEYEVENTF_KEYUP:
            down.pop(key, None)
        else:
            down[key] = ki
    return [_keyboard_input(wVk=ki.wVk, wScan=ki.wScan, flags=ki.dwFlags | KEYEVENTF_KEYUP)
            for ki in reversed(list(down.values()))]


def _send_quietly(inputs: list[_INPUT]) -> None:
    # Releases only, so never refused; a failure must not replace why the
    # send stopped.
    try:
        _send(inputs)
    except Exception:
        pass


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
    _refuse_if_cancelled()   # not a retired dictation's text (Sol mac r12)
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


def press_enter_windows(cancelled=None) -> None:
    """Synthesize a single Enter keypress (Auto-press-Enter setting).
    *cancelled* is asked at the send, as a scope (Sol mac r12)."""
    with cancellable(cancelled):
        _press_enter_windows()


def _press_enter_windows() -> None:
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
    _user32.CreateWindowExW.argtypes = (
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DestroyWindow.argtypes = (wintypes.HWND,)
    _user32.DestroyWindow.restype = wintypes.BOOL
    _kernel32.GlobalSize.argtypes = (wintypes.HGLOBAL,)
    _kernel32.GlobalSize.restype = ctypes.c_size_t

_HWND_MESSAGE = -3

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


def _open_clipboard(retries: int = 5, owner=None) -> bool:
    """OpenClipboard with brief retries (another app may hold it momentarily)."""
    for _ in range(retries):
        if _user32.OpenClipboard(owner):
            return True
        time.sleep(0.02)
    return False


def _clipboard_window():
    """A message-only window of Aura's own on this thread to open the
    clipboard with, or None."""
    try:
        return _user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0,
                                       _HWND_MESSAGE, None, None, None) or None
    except Exception:
        return None


_OWNER_WINDOW_ATTEMPTS = 3
_OWNER_WINDOW_RETRY_DELAY_S = 0.03


def _clipboard_window_insist():
    """_clipboard_window(), retried a couple of times briefly: used wherever a
    hold must never fall back to having no owner (Finding 5) — a transient
    failure to create the window gets a couple of quick extra tries before
    giving up."""
    for attempt in range(_OWNER_WINDOW_ATTEMPTS):
        window = _clipboard_window()
        if window:
            return window
        if attempt + 1 < _OWNER_WINDOW_ATTEMPTS:
            time.sleep(_OWNER_WINDOW_RETRY_DELAY_S)
    return None


_clipboard_hold = threading.local()


@contextmanager
def _clipboard_held(*, require_owner: bool = False):
    """One OpenClipboard for the reads and writes inside it; yields whether
    the clipboard opened. Nested uses share the outer one. Opened by a window
    of Aura's own, every other app's OpenClipboard fails while Aura holds it,
    a script's with no window too, so no copy lands between a check and the
    write that depends on it.

    *require_owner* (every write, and the restore's compare-and-write):
    measured on real Windows 11 (node 4), a hold with no owner does NOT
    exclude another ownerless OpenClipboard(NULL) — so a write made under one
    is not really exclusive, and another ownerless opener's copy can land
    even inside this same hold. This never opens an ownerless hold for such a
    use: it retries creating the owner window a couple of times briefly, and
    if there is still none, yields False without ever calling OpenClipboard,
    leaving the clipboard untouched. A read-only hold (no writes inside it)
    still falls back to an ownerless open when no window can be made, as
    before — the text and a private format both outlive the window (both
    checked on Windows 11, node 4)."""
    if getattr(_clipboard_hold, "open", False):
        if require_owner and not getattr(_clipboard_hold, "owner", False):
            # Defensive: a write must never ride along inside an outer hold
            # that opened without an owner.
            yield False
            return
        yield True
        return
    owner = _clipboard_window_insist() if require_owner else _clipboard_window()
    if require_owner and not owner:
        yield False
        return
    try:
        if not _open_clipboard(owner=owner):
            yield False
            return
        _clipboard_hold.open = True
        _clipboard_hold.owner = bool(owner)
        try:
            yield True
        finally:
            _clipboard_hold.open = False
            _clipboard_hold.owner = False
            _user32.CloseClipboard()
    finally:
        if owner:
            _user32.DestroyWindow(owner)


def _clipboard_get_windows() -> str | None:
    """Current clipboard text, or None if empty/unavailable/non-text."""
    with _clipboard_held() as held:
        if not held:
            return None
        return _read_open_clipboard()


def _read_open_clipboard() -> str | None:
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


def _clipboard_set_windows(text: str, transient: bool = False, *, restoring: bool = False) -> bool:
    """Replace clipboard contents with *text* (CF_UNICODETEXT). True on success.

    *transient* also marks it as excluded from clipboard history and sync.
    A dictation's write is refused for a retired dictation once the
    clipboard is open, after OpenClipboard's retries and right before it is
    emptied (Sol mac r13); a *restoring* write always goes out. Never writes
    under an ownerless hold (Finding 5): False when no owner window could be
    made, even after retries.
    """
    with _clipboard_held(require_owner=True) as held:
        if not held:
            return False
        if not restoring:
            _refuse_if_cancelled()
        return _write_open_clipboard(text, transient)


def _write_open_clipboard(text: str, transient: bool) -> bool:
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


RESTORE_DELAY_S = 0.8   # as macos_paste.RESTORE_DELAY_S


class _DeferredRestore:
    """One pending clipboard restore, run on a timer or flushed early
    (Windows twin of macos_paste.DeferredRestore)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Held while a restore runs, by the timer and by flush(): a paste that
        # starts while the timer is mid-restore waits for it to finish, instead
        # of racing it on the clipboard (game chat pastes parts ~0.85 s apart).
        self._run = threading.Lock()
        self._timer: threading.Timer | None = None
        self._job = None

    def schedule(self, restore, delay: float = RESTORE_DELAY_S) -> None:
        self.flush()
        try:
            with self._lock:
                self._job = restore
                self._timer = threading.Timer(delay, self._fire)
                self._timer.daemon = True
                self._timer.start()
        except Exception:
            # No thread (resource pressure): restore now rather than never,
            # so the user's clipboard is not left holding the dictation.
            with self._lock:
                self._timer = self._job = None
            try:
                restore()
            except Exception:
                pass

    def flush(self) -> None:
        """Run the pending restore now (before a new paste snapshots the
        clipboard), or wait for one the timer is already running."""
        with self._run:
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
        with self._run:
            with self._lock:
                job = self._job
                self._timer = self._job = None
            if job is not None:
                try:
                    job()
                except Exception:
                    pass


_pending_restore = _DeferredRestore()
# Quitting within the delay must still give the user their clipboard back.
atexit.register(_pending_restore.flush)


# Written beside each dictation, a fresh random value each time, in a format
# only Aura reads: any other write empties the clipboard and takes it away, a
# "Copy" of the same words included (Sol mac r14).
_OWNER_MARK_FORMAT = "WayfinderAuraDictationMark"
_OWNER_MARK_BYTES = 16


def _mark_open_clipboard() -> bytes | None:
    """Put a fresh mark beside Aura's text (clipboard already open); the mark,
    or None if it could not be written."""
    try:
        fmt = _user32.RegisterClipboardFormatW(_OWNER_MARK_FORMAT)
        mark = os.urandom(_OWNER_MARK_BYTES)
        handle = _global_copy(mark) if fmt else None
        if handle and _user32.SetClipboardData(fmt, handle):
            return mark
    except Exception:
        pass
    return None


def _clipboard_mark() -> bytes | None:
    """The mark on the clipboard now, or None."""
    with _clipboard_held() as held:
        if not held:
            return None
        fmt = _user32.RegisterClipboardFormatW(_OWNER_MARK_FORMAT)
        handle = _user32.GetClipboardData(fmt) if fmt else None
        if not handle or _kernel32.GlobalSize(handle) < _OWNER_MARK_BYTES:
            return None
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.string_at(ptr, _OWNER_MARK_BYTES)
        finally:
            _kernel32.GlobalUnlock(handle)


def _clipboard_untouched(text: str, mark: bytes) -> bool:
    """Whether the clipboard is as Aura left it: still our write's mark and
    still our text. A paste always has a mark — Finding 6 refuses the paste
    rather than writing one without it — so there is no other case to check."""
    return _clipboard_mark() == mark and _clipboard_get_windows() == text


def _log_no_owner_at_restore() -> None:
    """One line, no clipboard content: Finding 5's restore guard found no
    owner window even after retries, so nothing was written back."""
    reporter = _paste_reporter
    if reporter is None:
        return
    try:
        reporter("🔎 Paste ⚠ no clipboard owner window for restore — left as is")
    except Exception:
        pass


def _restore_clipboard(previous: str, text: str, mark: bytes) -> tuple[bool, bool]:
    """Put *previous* back if the clipboard is as Aura left it, with the check
    and the write under one OpenClipboard: a copy made during the check
    (even of the same words) stays (Sol mac r13). Requires Aura's own owner
    window (Finding 5): an ownerless hold would not exclude another ownerless
    opener's copy landing between the check and the write, so without one
    this does not restore at all — the clipboard, dictated text included, is
    left exactly as it is. Returns (untouched, restored)."""
    with _clipboard_held(require_owner=True) as held:
        if not held:
            _log_no_owner_at_restore()
            return False, False
        if not _clipboard_untouched(text, mark):
            return False, False
        return True, _clipboard_set_windows(previous, transient=True, restoring=True)


def inject_text_paste_windows(text: str, cancelled=None) -> None:
    """Inject *text* via clipboard + Ctrl+V, restoring the prior clipboard.

    Used for the Game Mode type-failure fallback and the explicit
    clipboard-paste entry point (apps that reject synthetic per-character input).
    *cancelled* is asked at every send too, as a scope (Sol mac r12).
    """
    with cancellable(cancelled):
        _inject_text_paste_windows(text, cancelled)


def _inject_text_paste_windows(text: str, cancelled=None) -> None:
    if not text:
        return
    # A restore still pending from the last paste runs first (before even the
    # own-window refusal, which leaves the text on the clipboard), so nothing
    # here races it and the snapshot below is the user's clipboard.
    _pending_restore.flush()
    diag = _new_paste_diagnostics(text)
    try:
        _require_foreground_window()
        _refuse_own_window(text)
        diag.mark("mods")
        require_modifier_release_windows()
        diag.mark("mods_free")
        # Retired while a modifier was held: the clipboard stays untouched
        # (Sol mac r11).
        _refuse_if_cancelled(cancelled)

        previous = _clipboard_get_windows()
        diag.previous(previous)
        # Never under an ownerless hold (Finding 5): without an owner window,
        # another ownerless opener is not excluded, so the write is refused
        # through the same path as any other clipboard-write failure.
        with _clipboard_held(require_owner=True) as held:
            if not held or not _clipboard_set_windows(text, transient=True):
                raise InjectionError("Could not write to the Windows clipboard for paste.")
            mark = _mark_open_clipboard()
            if mark is None:
                # Without a mark nothing proves later that the clipboard is
                # still ours once closed — even a same-words copy right after
                # would look like it (Finding 6). Put the user's clipboard
                # back now, inside this same hold, and refuse instead of
                # pasting unmarked.
                if previous is not None:
                    _clipboard_set_windows(previous, transient=True, restoring=True)
                raise InjectionError("Could not write to the Windows clipboard for paste.")
        diag.mark("set")
        time.sleep(0.03)
        diag.start_watch()
        # A failed paste raises here and leaves the dictation on the clipboard:
        # the error tells the user to press Ctrl+V themselves (as on macOS).
        try:
            _press_keys([VK_CONTROL, VK_V])
        except InjectionCancelled:
            # ... or during the clipboard writes or the settle (Sol mac r12):
            # no Ctrl+V; the user's clipboard back now, unless they copied
            # something meanwhile.
            if previous is not None:
                _restore_clipboard(previous, text, mark)
            raise
        diag.mark("ctrl_v")
        # Put the old clipboard back later, on a timer (the Mac's
        # DeferredRestore): VS Code and other Electron apps read the clipboard
        # 30-90 ms after Ctrl+V, more when busy, and a restore at 80 ms made
        # them paste the OLD clipboard or nothing (owner, Oct 2026). Only if
        # the clipboard still holds our text: the user may have copied
        # something new in the meantime.
        if previous is not None:
            def restore(previous=previous, text=text, diag=diag, mark=mark):
                # Untouched since Aura wrote it: still our mark (a "Copy" of
                # the same words takes it away) and still our text.
                untouched, restored = _restore_clipboard(previous, text, mark)
                diag.at_restore(untouched)
                if restored:
                    diag.mark("restored")

            _pending_restore.schedule(restore)
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
_watch_reads = False   # the 0.5 ms clipboard-read watcher: opt-in (CLAUDE.md rule 1)
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


def set_paste_reporter(reporter, *, watch_reads: bool = False) -> None:
    """Route paste diagnostics to *reporter(line)* (the app's activity log).

    ``watch_reads`` adds which app opened the clipboard after Ctrl+V and when.
    That polls every 0.5 ms for 1.5 s per paste, so it is off unless the
    ``paste_diagnostics_watch`` setting turns it on while chasing a report.
    """
    global _paste_reporter, _watch_reads
    _paste_reporter = reporter
    _watch_reads = bool(watch_reads)


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
        if _user32 is None or _paste_reporter is None or not _watch_reads:
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
        elif ctrl_v is not None and self._watch is not None:
            parts.append("no clipboard read seen")
        if self._error:
            parts.append(f"FAILED {self._error}")
        verdict = "⚠ " if (self._error or late) else ""
        return f"🔎 Paste {verdict}" + " · ".join(parts)

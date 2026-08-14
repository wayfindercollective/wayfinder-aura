"""
Text injection module for Wayfinder Aura.

Platform dispatch:
- Linux/X11: xdotool (preferred — no daemon, no uinput, present in stock SteamOS image)
- Linux/Wayland: ydotool when its daemon is live (kernel-level, compositor-proof), else wtype
  (virtual-keyboard protocol — refused outright by GNOME/Mutter and revocable by KWin); a
  wtype failure at injection time falls back to ydotool when possible. (A RemoteDesktop-portal
  backend — the universal path — is planned but NOT yet implemented.)
- Linux/X11 fallback: ydotool if xdotool unavailable
- macOS: clipboard paste via pbcopy + Cmd-V
"""

import ctypes
import os
import subprocess
import shutil
import sys
import time
from pathlib import Path

IS_MACOS = sys.platform == 'darwin'


class InjectionError(Exception):
    """Raised when text injection fails."""

    pass


# Set once a wtype attempt reveals the compositor refuses the virtual-keyboard
# protocol (GNOME/Mutter always does; some KWin setups deny it). Selection
# happens before any attempt, so this session cache lets later injections skip
# straight to ydotool instead of re-failing every dictation.
_WTYPE_UNSUPPORTED = False

# A Bazzite/SteamOS user service can start before logind applies the session's
# uaccess ACL to /dev/uinput. ydotoold then exhausts systemd's restart limit and
# remains dead even though the device becomes writable moments later. Retry at
# most once from the app after the graphical session is fully established.
_YDOTOOL_USER_SERVICE_RESTART_ATTEMPTED = False


def _note_wtype_failure(detail: str) -> None:
    global _WTYPE_UNSUPPORTED
    if "virtual keyboard protocol" in detail.lower():
        _WTYPE_UNSUPPORTED = True


def _path_is_under(path: str, root: str) -> bool:
    """Real-path containment check (no string-prefix collisions, symlink-safe)."""
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(root)]
        ) == os.path.realpath(root)
    except (ValueError, OSError):
        return False


def _get_ydotool_binary() -> "str | None":
    """HOST ydotool client only — never a bundled one. None when absent.

    The client must protocol-match the host's ydotoold. Field bug: a bundled
    (Ubuntu-built) client against a Fedora daemon printed "ydotoold backend
    unavailable", silently switched to a throwaway direct-uinput device, and
    exited 0 while nothing reached the compositor. Worse, the CI (jammy) build
    would bundle ydotool 0.1.8, whose CLI predates the code's `key code:state`
    / `--key-hold` syntax entirely (Codex review). Policy: use the host's
    client, which matches the host's daemon, or report ydotool unavailable so
    selection uses wtype / the Setup wizard self-provisions.
    """
    appdir = os.environ.get("APPDIR", "")
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue  # never interpret empty PATH entries as CWD
        d_abs = os.path.realpath(d)
        if appdir and _path_is_under(d_abs, appdir):
            continue  # AppRun prepends the bundle dir — we want the HOST client
        cand = os.path.join(d_abs, "ydotool")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _ydotool_socket_candidates() -> list:
    """Common ydotoold socket locations, system service first."""
    return [
        "/run/ydotool/ydotool.sock",  # System service (Bazzite/Fedora)
        f"/run/user/{os.getuid()}/.ydotool_socket",  # User service
        "/tmp/.ydotool_socket",  # Fallback
    ]


def _get_ydotool_env() -> dict:
    """Get environment with correct ydotool socket path.

    Selection prefers a socket something actually LISTENS on: a stale
    system-socket FILE must not mask a live user socket (e.g. right after
    Setup self-provisions the user service — Codex review). Falls back to the
    first existing path when nothing is connectable, so error messages still
    name a concrete socket.

    Built on host_env(): the ydotool client is host-only by contract, and a
    host binary must not inherit the bundle's LD_LIBRARY_PATH.
    """
    from wayfinder.utils.hostexec import host_env
    env = host_env()

    # A user-set YDOTOOL_SOCKET is authoritative (custom daemon setups) —
    # only probe the common locations when it's unset or stale.
    preset = env.get("YDOTOOL_SOCKET")
    if preset and Path(preset).exists():
        return env

    existing = [p for p in _ydotool_socket_candidates() if Path(p).exists()]
    for socket_path in existing:
        if _probe_unix_socket(socket_path):
            env["YDOTOOL_SOCKET"] = socket_path
            return env
    if existing:
        env["YDOTOOL_SOCKET"] = existing[0]

    return env


# Typing speed presets: (key_delay_ms, key_hold_ms)
# Minimum 1ms delays to prevent ydotool Shift key race conditions
# (0ms causes Shift to bleed into adjacent keys: a→A, comma→<, period→>)
TYPING_SPEEDS = {
    "instant": (1, 1),       # 1ms delays — prevents Shift bleed, effectively instant
    "fast": (1, 1),          # Same as instant (safe minimum)
    "normal": (12, 12),      # Comfortable speed
    "slow": (50, 20),        # Slower, more natural
    "very_slow": (100, 50),  # Very slow, like watching someone type
}


def _check_ydotool_result(result, action: str) -> None:
    """Raise unless the ydotool run BOTH exited 0 and actually used the daemon.

    The client exits 0 even when it prints 'ydotoold backend unavailable' and
    falls back to a throwaway direct-uinput device. That direct path is
    unreliable (the transient device often vanishes before the compositor
    registers it — observed as "typed nothing" in the field) but it is NOT
    provably a no-op: delivery is UNCERTAIN. Raise with `uncertain=True` so
    callers surface the problem WITHOUT auto-retrying another injector —
    a retry after partial delivery would duplicate text (Codex review).
    """
    stderr = _to_text(result.stderr).strip()
    stdout = _to_text(result.stdout).strip()
    if result.returncode != 0:
        detail = stderr or stdout or "(no output)"
        raise InjectionError(f"ydotool {action} failed (exit {result.returncode}): {detail}")
    if "backend unavailable" in stderr.lower():
        err = InjectionError(
            f"ydotool {action}: ydotoold unreachable (client/daemon mismatch or dead "
            f"socket); the direct-uinput fallback makes delivery uncertain: {stderr}"
        )
        err.uncertain_delivery = True
        raise err


def _to_text(v) -> str:
    """stderr/stdout may be str or bytes depending on the caller's text= flag."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return str(v)


def check_ydotool_ready() -> tuple[bool, str]:
    """Check if ydotool is installed and the daemon is running.

    Returns:
        (ready, message) tuple
    """
    ydotool_bin = _get_ydotool_binary()
    if not ydotool_bin:
        from wayfinder.core.setup import _get_install_hint
        return False, f"ydotool not found on this system. Install with: {_get_install_hint('ydotool')}"

    env = _get_ydotool_env()
    socket_path = env.get("YDOTOOL_SOCKET")
    if not socket_path:
        restarted, restart_message = _restart_ydotool_user_service_once()
        if restarted:
            env = _get_ydotool_env()
            socket_path = env.get("YDOTOOL_SOCKET")
            if socket_path and _probe_unix_socket(socket_path):
                return True, f"ydotool ready after user-service recovery (socket: {socket_path})"
        # No socket found - daemon may not be running
        if shutil.which("systemctl"):
            hint = "Start the daemon: sudo systemctl enable --now ydotoold"
        else:
            hint = "Start the ydotoold daemon with your init system"
        if restart_message:
            hint = f"{hint} ({restart_message})"
        return False, f"ydotool daemon socket not found. {hint}"

    # A socket FILE can outlive a dead daemon (stale after a stop/crash), and a
    # process check can't tell WHICH socket a live ydotoold owns. Probe the
    # exact selected socket by CONNECTING (no input events, microseconds):
    # only a daemon actually listening there counts as ready (Codex review).
    if not _probe_unix_socket(socket_path):
        restarted, restart_message = _restart_ydotool_user_service_once()
        if restarted:
            env = _get_ydotool_env()
            recovered_path = env.get("YDOTOOL_SOCKET")
            if recovered_path and _probe_unix_socket(recovered_path):
                return True, (
                    "ydotool ready after user-service recovery "
                    f"(socket: {recovered_path})"
                )
        return False, (
            f"ydotool socket exists ({socket_path}) but nothing is listening "
            "on it (stale socket or dead daemon). Restart the daemon."
            + (f" ({restart_message})" if restart_message else "")
        )

    return True, f"ydotool ready (socket: {socket_path})"


def _restart_ydotool_user_service_once() -> tuple[bool, str]:
    """Recover a user ydotoold unit from the Bazzite /dev/uinput ACL race.

    This is deliberately narrow and safe: Wayland only, an already-writable
    uinput device, a host ydotoold binary, and an existing loaded user unit.
    It never invokes sudo, installs files, or touches a system service.
    """
    global _YDOTOOL_USER_SERVICE_RESTART_ATTEMPTED
    if _YDOTOOL_USER_SERVICE_RESTART_ATTEMPTED:
        return False, "user-service recovery was already attempted"
    _YDOTOOL_USER_SERVICE_RESTART_ATTEMPTED = True

    try:
        from wayfinder.utils.platform import is_wayland

        if not is_wayland():
            return False, "not a Wayland session"
    except Exception:
        return False, "could not identify the session type"

    if not shutil.which("systemctl") or not shutil.which("ydotoold"):
        return False, "systemctl or ydotoold is unavailable"
    if not os.path.exists("/dev/uinput") or not os.access("/dev/uinput", os.W_OK):
        return False, "/dev/uinput is not writable by this session"

    from wayfinder.utils.hostexec import host_env

    env = host_env()
    for unit in ("wayfinder-ydotoold.service", "ydotoold.service"):
        try:
            loaded = subprocess.run(
                ["systemctl", "--user", "show", "--property=LoadState", "--value", unit],
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
            )
            if loaded.returncode != 0 or loaded.stdout.strip() != "loaded":
                continue
            subprocess.run(
                ["systemctl", "--user", "reset-failed", unit],
                capture_output=True,
                text=True,
                timeout=3,
                env=env,
            )
            started = subprocess.run(
                ["systemctl", "--user", "start", unit],
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
            )
            if started.returncode != 0:
                detail = started.stderr.strip() or started.stdout.strip()
                return False, f"{unit} failed to start: {detail or 'unknown error'}"

            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                candidate_env = _get_ydotool_env()
                candidate = candidate_env.get("YDOTOOL_SOCKET")
                if candidate and _probe_unix_socket(candidate, timeout=0.1):
                    return True, f"restarted {unit}"
                time.sleep(0.05)
            return False, f"{unit} started but its socket did not become ready"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"could not restart {unit}: {exc}"
    return False, "no user ydotoold service is installed"


def _probe_unix_socket(path: str, timeout: float = 1.0) -> bool:
    """True if a live endpoint exists at the unix socket *path*.

    ydotoold binds a DATAGRAM socket (verified live: SOCK_STREAM connect gets
    EPROTOTYPE against a running daemon) — probe DGRAM first, STREAM second
    for generality. connect() on either type fails for stale files and dead
    sockets, which is exactly the liveness signal we need. No input events.
    """
    import socket as _socket
    for stype in (_socket.SOCK_DGRAM, _socket.SOCK_STREAM):
        try:
            s = _socket.socket(_socket.AF_UNIX, stype)
            try:
                s.settimeout(timeout)
                s.connect(path)
                return True
            finally:
                s.close()
        except OSError:
            continue
    return False


def _inject_text_macos(text: str) -> None:
    """
    Inject text on macOS by writing to clipboard then simulating Cmd+V.
    This is the most reliable method on macOS — works in any app.
    """
    import subprocess
    import time

    # Write text to clipboard using pbcopy
    proc = subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise InjectionError(f"pbcopy failed: {proc.stderr.decode()}")

    # Small delay to ensure clipboard is ready
    time.sleep(0.05)

    # Simulate Cmd+V using osascript
    script = 'tell application "System Events" to keystroke "v" using command down'
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        raise InjectionError(f"osascript paste failed: {proc.stderr.decode()}")


# Typing speed → pyautogui interval (seconds between keystrokes)
PYAUTOGUI_INTERVALS = {
    "instant": 0.0,
    "fast": 0.0,
    "normal": 0.012,
    "slow": 0.05,
    "very_slow": 0.1,
}


def _inject_text_pyautogui(text: str, typing_speed: str = "instant") -> None:
    """Inject text on macOS using clipboard paste (Cmd+V).

    pyautogui.typewrite() only handles ASCII and is slow. Instead, we copy
    the text to clipboard and simulate Cmd+V for instant, reliable paste
    that supports all Unicode characters.
    """
    try:
        import pyautogui
        import subprocess
    except ImportError:
        raise InjectionError(
            "pyautogui not installed. Install with: pip install pyautogui"
        )

    try:
        # Save current clipboard contents
        try:
            old_clipboard = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=5
            ).stdout
        except Exception:
            old_clipboard = None

        # Copy text to clipboard via pbcopy
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(text.encode("utf-8"))

        # Paste with Cmd+V (no sleep needed — proc.communicate() is synchronous)
        pyautogui.hotkey("command", "v")

        # Restore original clipboard after paste completes
        if old_clipboard is not None:
            import time
            time.sleep(0.1)
            # Smart restore: only restore if clipboard still contains our injected text
            # (user may have copied something new during the delay)
            try:
                current = subprocess.run(
                    ["pbpaste"], capture_output=True, text=True, timeout=2
                ).stdout
                if current == text:
                    proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
                    proc.communicate(old_clipboard.encode("utf-8"))
            except Exception:
                pass  # Best-effort restore

    except Exception as e:
        raise InjectionError(f"macOS text injection failed: {e}")


def warmup_clipboard() -> None:
    """Pre-warm the macOS pasteboard daemon (best-effort, non-blocking).

    The first pbcopy/pbpaste call can be slow (~100ms) due to pasteboard daemon
    startup. Calling this during app initialization eliminates the delay from
    the hot path.
    """
    if sys.platform != "darwin":
        return
    try:
        import subprocess
        subprocess.run(["pbpaste"], capture_output=True, timeout=2)
    except Exception:
        pass  # Best-effort — never block startup


def get_active_window() -> "str | None":
    """Return the active X11 window id, or None.

    Captured at record-start so injection can target the user's window even if focus drifts
    during transcription (global-hotkey dictation). Best-effort; None on any failure.
    """
    try:
        from wayfinder.utils.hostexec import host_env
        result = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=5,
            env=host_env(),
        )
        if result.returncode == 0:
            wid = result.stdout.strip()
            return wid or None
    except Exception:
        pass
    return None


# X core modifier-mask bits (X.h). Lock (CapsLock, 0x02) and Mod2 (NumLock,
# 0x10) are latched states, not keys the user is holding — excluded so
# caps-lock/numlock users never stall an injection.
_X_HELD_MODIFIER_BITS = 0x01 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80  # Shift Ctrl Mod1 Mod3 Mod4 Mod5


# Minimal xcb ABI (stable since libxcb 1.0). xcb — NOT Xlib — because injection
# runs off the Tk main thread and Tk already owns Xlib there without
# XInitThreads() (which is only safe as the very first Xlib call of the
# process, so it cannot be retrofitted). libxcb is thread-safe by design.
class _XcbScreen(ctypes.Structure):
    # Only the leading field is dereferenced; no iteration over the struct.
    _fields_ = [("root", ctypes.c_uint32)]


class _XcbScreenIterator(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.POINTER(_XcbScreen)),
        ("rem", ctypes.c_int),
        ("index", ctypes.c_int),
    ]


class _XcbQueryPointerCookie(ctypes.Structure):
    _fields_ = [("sequence", ctypes.c_uint)]


class _XcbQueryPointerReply(ctypes.Structure):
    _fields_ = [
        ("response_type", ctypes.c_uint8),
        ("same_screen", ctypes.c_uint8),
        ("sequence", ctypes.c_uint16),
        ("length", ctypes.c_uint32),
        ("root", ctypes.c_uint32),
        ("child", ctypes.c_uint32),
        ("root_x", ctypes.c_int16),
        ("root_y", ctypes.c_int16),
        ("win_x", ctypes.c_int16),
        ("win_y", ctypes.c_int16),
        ("mask", ctypes.c_uint16),
    ]


# Lazy singleton: (libxcb, libc) or None. Loaded at most once per process —
# repeated CDLL() would leak dlopen refs and find_library() shells out to
# ldconfig (~2ms) on every call (Codex review).
_XCB_HANDLES = None
_XCB_LOAD_ATTEMPTED = False


def _load_xcb():
    global _XCB_HANDLES, _XCB_LOAD_ATTEMPTED
    if _XCB_LOAD_ATTEMPTED:
        return _XCB_HANDLES
    _XCB_LOAD_ATTEMPTED = True
    try:
        try:
            lib = ctypes.CDLL("libxcb.so.1")
        except OSError:
            # NOT `import ctypes.util` — inside a function that statement
            # makes `ctypes` function-local and every use above it explodes.
            from ctypes.util import find_library
            name = find_library("xcb")
            if not name:
                return None
            lib = ctypes.CDLL(name)
        libc = ctypes.CDLL(None)
        lib.xcb_connect.restype = ctypes.c_void_p
        lib.xcb_connect.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        lib.xcb_connection_has_error.restype = ctypes.c_int
        lib.xcb_connection_has_error.argtypes = [ctypes.c_void_p]
        lib.xcb_disconnect.restype = None
        lib.xcb_disconnect.argtypes = [ctypes.c_void_p]
        lib.xcb_get_setup.restype = ctypes.c_void_p
        lib.xcb_get_setup.argtypes = [ctypes.c_void_p]
        lib.xcb_setup_roots_iterator.restype = _XcbScreenIterator
        lib.xcb_setup_roots_iterator.argtypes = [ctypes.c_void_p]
        lib.xcb_query_pointer.restype = _XcbQueryPointerCookie
        lib.xcb_query_pointer.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.xcb_query_pointer_reply.restype = ctypes.POINTER(_XcbQueryPointerReply)
        lib.xcb_query_pointer_reply.argtypes = [
            ctypes.c_void_p, _XcbQueryPointerCookie, ctypes.c_void_p,
        ]
        libc.free.restype = None
        libc.free.argtypes = [ctypes.c_void_p]
        _XCB_HANDLES = (lib, libc)
    except Exception:
        _XCB_HANDLES = None
    return _XCB_HANDLES


class _ModifierProbe:
    """One xcb connection reused across a wait's polls; close() is mandatory."""

    def __init__(self, lib, libc, conn, root):
        self._lib = lib
        self._libc = libc
        self._conn = conn
        self._root = root

    def query(self) -> "int | None":
        """Held-modifier bits right now, or None when the query fails."""
        try:
            cookie = self._lib.xcb_query_pointer(self._conn, self._root)
            reply = self._lib.xcb_query_pointer_reply(self._conn, cookie, None)
            if not reply:
                return None
            try:
                return reply.contents.mask & _X_HELD_MODIFIER_BITS
            finally:
                self._libc.free(reply)
        except Exception:
            return None

    def close(self) -> None:
        try:
            self._lib.xcb_disconnect(self._conn)
        except Exception:
            pass


def _open_modifier_probe() -> "_ModifierProbe | None":
    """Connect to the X server for modifier queries; None when unavailable.

    Works on native X11 and, via XWayland's mirrored seat state, on Wayland
    sessions too (where DISPLAY points at XWayland). No DISPLAY / no libxcb /
    connection failure → None, and callers skip the gate entirely.
    """
    if not os.environ.get("DISPLAY"):
        return None
    loaded = _load_xcb()
    if loaded is None:
        return None
    lib, libc = loaded
    conn = None
    try:
        conn = lib.xcb_connect(None, None)
        # xcb_connect never returns NULL — a failed connect is an error-state
        # object that must still be disconnected to free it.
        if not conn or lib.xcb_connection_has_error(conn):
            if conn:
                lib.xcb_disconnect(conn)
            return None
        it = lib.xcb_setup_roots_iterator(lib.xcb_get_setup(conn))
        if not it.data:
            lib.xcb_disconnect(conn)
            return None
        return _ModifierProbe(lib, libc, conn, it.data.contents.root)
    except Exception:
        if conn:
            try:
                lib.xcb_disconnect(conn)
            except Exception:
                pass
        return None


def _x11_held_modifiers() -> "int | None":
    """One-shot: modifier bits the X server considers held, or None."""
    probe = _open_modifier_probe()
    if probe is None:
        return None
    try:
        return probe.query()
    finally:
        probe.close()


def _wait_for_modifier_release(timeout: float = 2.0, poll: float = 0.1) -> bool:
    """Bounded wait until no modifier key is physically held. True when clear.

    Why: under XWayland the compositor keeps re-asserting the physical
    keyboard state, so xdotool's --clearmodifiers cannot neutralize a key the
    user is still holding — a dictation injected ~1s after hotkey release,
    with a hand back on Shift, lands fully shifted (field bug: `I"M ... NOW<`
    for "I'm ... now,"). Held Ctrl is worse: letters become shortcuts fired at
    the focused app.

    Runs once per injection immediately before synthetic keys — a transient
    bounded wait, not a repeating idle timer, and the 100ms poll honors the
    project's no-sub-100ms-polling rule. The common case returns on the first
    query (mask clear, or probe unavailable — which must never block).
    False means the user still held a modifier at *timeout*; callers proceed
    anyway with --clearmodifiers as the best-effort backstop.
    """
    probe = _open_modifier_probe()
    if probe is None:
        return True
    try:
        deadline = time.monotonic() + timeout
        while True:
            if not probe.query():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)
    finally:
        probe.close()


def _inject_text_xdotool(text: str, typing_speed: str = "instant", target_window: "str | None" = None) -> None:
    """Inject text on Linux/X11 using xdotool type.

    xdotool synthesizes keystrokes via X11 XTEST — no daemon, no uinput, no input-group
    membership required. Works in stock SteamOS where ydotool packages get wiped by updates.
    """
    if typing_speed in TYPING_SPEEDS:
        key_delay, _ = TYPING_SPEEDS[typing_speed]
    else:
        key_delay = 2

    # xdotool is a host binary — never feed it the bundle's library path.
    from wayfinder.utils.hostexec import host_env
    _env = host_env()

    # Refocus the record-start window ONLY if focus actually drifted off it. When it's already
    # focused (the common case) we must NOT windowactivate — a redundant activate immediately
    # before typing races the synthetic keys (window not input-ready for ~tens of ms) and
    # intermittently dropped the WHOLE injection into a focused terminal. So: check current
    # focus; if it's already the target, type straight away (the proven-reliable path); only on
    # real drift do we activate, then settle so the focus-in completes before typing.
    if target_window:
        try:
            active = subprocess.run(
                ["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=5,
                env=_env,
            )
            already_focused = active.returncode == 0 and active.stdout.strip() == str(target_window)
        except Exception:
            already_focused = False
        if not already_focused:
            try:
                subprocess.run(
                    ["xdotool", "windowactivate", "--sync", str(target_window)],
                    capture_output=True, text=True, timeout=5, env=_env,
                )
                import time as _t
                _t.sleep(0.06)  # let the WM/app finish the focus-in before synthetic keys
            except Exception:
                pass

    # Gate immediately before the synthetic keys (after refocus/settling, so
    # no later pre-work re-opens the race): the user's hand is often still on
    # the keyboard ~1s after hotkey release, and under XWayland
    # --clearmodifiers cannot neutralize a physically-held modifier.
    _wait_for_modifier_release()

    cmd = [
        "xdotool", "type",
        "--clearmodifiers",
        "--delay", str(key_delay),
        "--", text,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=_env,
        )
        if result.returncode != 0:
            error_detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"xdotool failed (exit {result.returncode}): {error_detail}")
    except subprocess.TimeoutExpired:
        raise InjectionError("xdotool timed out after 120s")
    except FileNotFoundError:
        raise InjectionError("xdotool not found in PATH")


# Belt-and-suspenders for the very first wtype injection after launch: even with the
# startup primer (prime_wayland_injection), if the KDE "allow input control" approval is
# still settling, a short pause lets focus return to the target window before we type — so
# the first dictation can't land as garble (the classic "recurring T's") in the wrong window.
_wtype_first_injection = True


def _inject_text_wtype(text: str) -> None:
    """Inject text on Linux/Wayland using wtype (virtual-keyboard protocol).

    Needs no uinput device and no daemon, so it works inside a Flatpak sandbox (unlike
    ydotool). No per-key delay control. The compositor must implement the virtual-keyboard
    protocol (KDE Plasma 6 does); if it doesn't, wtype errors and the caller can fall back.
    """
    global _wtype_first_injection
    if _wtype_first_injection:
        _wtype_first_injection = False
        time.sleep(0.35)
    # Gate immediately before the synthetic keys (after the first-injection
    # settle): a physically-held modifier corrupts virtual-keyboard typing the
    # same way it corrupts XTEST typing.
    _wait_for_modifier_release()
    try:
        result = subprocess.run(
            ["wtype", text],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            error_detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"wtype failed (exit {result.returncode}): {error_detail}")
    except subprocess.TimeoutExpired:
        raise InjectionError("wtype timed out after 120s")
    except FileNotFoundError:
        raise InjectionError("wtype not found in PATH")


def prime_wayland_injection() -> "tuple[bool, str]":
    """Surface the compositor's one-time "allow input control" approval BEFORE any dictation.

    On Wayland (KDE Plasma) the first wtype call after an install/rebuild triggers a per-app
    security prompt for the virtual-keyboard protocol. If that prompt races the first REAL
    injection, the keystrokes land in the wrong window — the "recurring T's" garble the user hit
    on a fresh Flatpak build. This sends a benign no-op (press+release Shift, which emits no
    character) so the approval dialog appears at startup, decoupled from real text. Once the user
    approves, KDE remembers it and every injection after is clean.

    Best-effort and only meaningful when wtype is the active injector (i.e. the Flatpak/Wayland
    path); a no-op everywhere else (e.g. the desktop's ydotool path, which needs no approval).
    Returns (ran, message) for logging.
    """
    from ..utils.platform import get_text_injector
    if get_text_injector() != "wtype":
        return False, ""
    if not shutil.which("wtype"):
        return False, "wtype not found — can't pre-arm Wayland injection approval"
    try:
        # -M presses a modifier, -m releases it: Shift down then up types nothing, but it
        # exercises the virtual-keyboard protocol and so triggers the KDE approval prompt.
        result = subprocess.run(
            ["wtype", "-M", "shift", "-m", "shift"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            # Learn compositor refusal at startup so the first REAL dictation
            # already routes to ydotool (with a clear enable-hint if it's down).
            _note_wtype_failure(detail)
            if _WTYPE_UNSUPPORTED:
                return False, (
                    f"this compositor refuses wtype ({detail}) — dictation will use "
                    "ydotool; enable its daemon: sudo systemctl enable --now ydotoold"
                )
            return False, f"injection primer failed (exit {result.returncode}): {detail}"
        return True, "Wayland injection pre-armed — approve KDE's 'allow input' prompt once if it appears"
    except subprocess.TimeoutExpired:
        return False, "injection primer timed out (an approval dialog may be waiting for you)"
    except FileNotFoundError:
        return False, "wtype not found — can't pre-arm Wayland injection approval"


def _clipboard_write_linux(text: str) -> None:
    """Write *text* to the session clipboard. Raises InjectionError on failure."""
    # Prefer Wayland tools when available; fall back to X11 clipboard utilities.
    for cmd in (
        ["wl-copy", "--"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            result = subprocess.run(
                cmd,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return
        except Exception:
            continue
    raise InjectionError(
        "No working clipboard writer found (need wl-copy, xclip, or xsel)"
    )


def _clipboard_read_linux() -> "str | None":
    """Best-effort read of the session clipboard (for restore after paste)."""
    for cmd in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=5)
            if result.returncode == 0:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception:
            continue
    return None


def _send_ctrl_v_linux(tool: str) -> None:
    """Synthesize Ctrl+V with the active injection tool."""
    # A physically-held Shift would turn this into Ctrl+Shift+V (app-dependent
    # behavior) — same XWayland --clearmodifiers gap as the type path.
    _wait_for_modifier_release()
    if tool == "xdotool":
        result = subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "ctrl+v"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"xdotool ctrl+v failed: {detail}")
        return
    if tool == "wtype":
        result = subprocess.run(
            ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"wtype ctrl+v failed: {detail}")
        return
    # ydotool (and any other Linux path that reached here)
    ydotool_bin = _get_ydotool_binary()
    if not ydotool_bin:
        raise InjectionError(
            "No host ydotool client found — install ydotool, or rely on wtype/xdotool"
        )
    env = _get_ydotool_env()
    # KEY_LEFTCTRL = 29, KEY_V = 47; 1=press, 0=release
    result = subprocess.run(
        [ydotool_bin, "key", "29:1", "47:1", "47:0", "29:0"],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    _check_ydotool_result(result, "ctrl+v")


def press_enter() -> None:
    """Synthesize a single Enter keypress with the active injection backend.

    Backs the opt-in "Auto press Enter after dictation" setting: dictate →
    text lands → Enter fires, so chat-style inputs submit hands-free. Uses the
    same tool dispatch as text injection so it works wherever typing works.
    """
    if sys.platform == "darwin":
        import pyautogui
        pyautogui.press("enter")
        return

    from wayfinder.utils.platform import get_text_injector
    # Held Shift would send Shift+Return — newline instead of submit in most
    # chat inputs, silently breaking the auto-Enter promise.
    _wait_for_modifier_release()
    tool = get_text_injector()
    if tool == "xdotool":
        result = subprocess.run(
            ["xdotool", "key", "--clearmodifiers", "Return"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
            raise InjectionError(f"xdotool Return failed: {detail}")
        return
    if tool == "wtype" and not _WTYPE_UNSUPPORTED:
        result = subprocess.run(
            ["wtype", "-k", "Return"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        _note_wtype_failure(detail)
        ready, _msg = check_ydotool_ready()
        if not ready:
            raise InjectionError(f"wtype Return failed: {detail}")
        # fall through to ydotool (same chain as text injection)
    # ydotool (and any other Linux path): KEY_ENTER = 28; 1=press, 0=release
    ydotool_bin = _get_ydotool_binary()
    if not ydotool_bin:
        raise InjectionError(
            "No host ydotool client found — install ydotool, or rely on wtype/xdotool"
        )
    env = _get_ydotool_env()
    result = subprocess.run(
        [ydotool_bin, "key", "28:1", "28:0"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    _check_ydotool_result(result, "Return")


def inject_text_clipboard_paste(text: str) -> None:
    """Inject *text* via clipboard write + Ctrl+V (Linux) or Cmd+V (macOS).

    Used as the Game Mode fallback when synthetic typing fails, and as the
    native macOS path. Best-effort clipboard restore after paste.
    """
    if not text:
        return
    text = text.strip()
    if not text:
        return

    if sys.platform == "darwin":
        _inject_text_pyautogui(text, "instant")
        return

    from ..utils.platform import get_text_injector

    tool = get_text_injector()
    if tool == "none":
        raise InjectionError(
            "No text injection tool available for clipboard paste on Linux."
        )

    old_clipboard = _clipboard_read_linux()
    _clipboard_write_linux(text)
    time.sleep(0.05)
    try:
        _send_ctrl_v_linux(tool if tool in ("xdotool", "wtype", "ydotool") else "ydotool")
    finally:
        # Best-effort restore: only if clipboard still holds our text.
        if old_clipboard is not None:
            try:
                time.sleep(0.08)
                current = _clipboard_read_linux()
                if current == text:
                    _clipboard_write_linux(old_clipboard)
            except Exception:
                pass


def _inject_text_type_linux(
    text: str,
    typing_speed: str = "instant",
    target_window: "str | None" = None,
) -> None:
    """Linux type-path only (xdotool / wtype / ydotool). Raises InjectionError."""
    from ..utils.platform import get_text_injector

    tool = get_text_injector()
    if tool == "xdotool":
        _inject_text_xdotool(text, typing_speed, target_window)
        return
    if tool == "wtype" and _WTYPE_UNSUPPORTED:
        # This compositor already refused wtype's protocol this session —
        # go straight to ydotool instead of failing the same way again.
        tool = "ydotool"
    if tool == "wtype":
        try:
            _inject_text_wtype(text)
            return
        except InjectionError as wtype_err:
            # Tool selection can't know the compositor refuses the
            # virtual-keyboard protocol until the first real attempt
            # (GNOME/Mutter never supports it; some KWin setups deny it).
            # Fall through the documented chain — ydotool if its daemon is
            # up — instead of dying with a working alternative bundled.
            _note_wtype_failure(str(wtype_err))
            ready, _msg = check_ydotool_ready()
            if not ready:
                raise InjectionError(
                    f"{wtype_err} — and the ydotool daemon is not running. "
                    "Enable it for this desktop: sudo systemctl enable --now ydotoold"
                ) from wtype_err
            # fall through to the ydotool path below
    if tool == "none":
        raise InjectionError(
            "No text injection tool available on Linux. "
            "Install xdotool (X11) or ydotool (Wayland)."
        )

    # Linux/ydotool path — pre-flight check
    ready, msg = check_ydotool_ready()
    if not ready:
        raise InjectionError(msg)

    if typing_speed in TYPING_SPEEDS:
        key_delay, key_hold = TYPING_SPEEDS[typing_speed]
    else:
        key_delay, key_hold = 2, 2  # Safe default

    try:
        ydotool_bin = _get_ydotool_binary()
        if not ydotool_bin:
            raise InjectionError(
                "No host ydotool client found — install ydotool, or rely on wtype/xdotool"
            )
        cmd = [
            ydotool_bin, "type",
            "--key-delay", str(key_delay),
            "--key-hold", str(key_hold),
            "--", text,
        ]
        env = _get_ydotool_env()
        # Gate immediately before the synthetic keys (after readiness checks):
        # uinput-level typing merges with physically-held modifiers in the
        # compositor exactly like the other backends.
        _wait_for_modifier_release()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        _check_ydotool_result(result, "type")
    except subprocess.TimeoutExpired:
        raise InjectionError("ydotool timed out after 120s")
    except FileNotFoundError:
        from wayfinder.core.setup import _get_install_hint
        raise InjectionError(
            f"ydotool not found. Install with: {_get_install_hint('ydotool')}"
        )


def inject_text(
    text: str,
    typing_speed: str = "instant",
    target_window: "str | None" = None,
    *,
    game_mode: bool = False,
    paste_fallback: bool = True,
) -> None:
    """
    Inject text into the active window.

    Dispatches to platform-specific backend:
    - Linux/X11: xdotool (preferred); ydotool as fallback
    - Linux/Wayland: ydotool when its daemon is live (compositor-proof), else
      wtype; a wtype refusal at injection time falls back to ydotool if ready
    - macOS: clipboard paste (pbcopy + Cmd-V)

    When *game_mode* is True and *paste_fallback* is True, a failed Linux type
    path is retried once via clipboard write + Ctrl+V (chat boxes that reject
    synthetic typing). Desktop Mode (game_mode=False) never takes that GM path.

    Args:
        text: Text to inject
        typing_speed: Speed preset (Linux only) - "instant", "fast", "normal", "slow", "very_slow"
        target_window: X11 window id (from get_active_window() at record-start) to refocus before
            typing, so a long dictation lands in the user's window even if focus drifted during
            transcription. xdotool backend only; ignored elsewhere.
        game_mode: Enable Game Mode type→paste fallback when the type path fails.
        paste_fallback: When game_mode is True, attempt clipboard paste after type failure.
    """
    if not text:
        return

    # Clean up the text - remove leading/trailing whitespace
    text = text.strip()

    if not text:
        return

    if sys.platform == "darwin":
        _inject_text_pyautogui(text, typing_speed)
        return

    # Linux type path first.
    try:
        _inject_text_type_linux(text, typing_speed, target_window)
        return
    except InjectionError as e:
        # Uncertain delivery (ydotool direct-uinput fallback engaged): the text
        # MAY have partially landed — a paste retry could duplicate it. Surface
        # the error instead of retrying (Codex review).
        if getattr(e, "uncertain_delivery", False):
            raise
        if not (game_mode and paste_fallback):
            raise
        # Game Mode only: type failed → clipboard + Ctrl+V.
        inject_text_clipboard_paste(text)

"""Typing through the RemoteDesktop portal: Aura's keyboard on Wayland desktops.

The Flatpak only has an X11 socket, so xdotool reaches X11/XWayland windows
and nothing else: a native Wayland window (most KDE and GNOME apps, Firefox on
Wayland) never saw a dictation. The desktop's RemoteDesktop portal is the one
input path a sandboxed app has into every window: the compositor types the
keys itself, as if from a keyboard, so they reach Wayland and XWayland windows
alike (Wine and Proton games included) and never cross XWayland's XTest bridge
that dropped keys.

    CreateSession → SelectDevices(keyboard, persist until revoked) → Start
    → NotifyKeyboardKeysym press/release for every character

Start shows the desktop's "allow remote control" dialog once. The restore
token from Start (stored next to the config) lets every later launch start
without a dialog; each Start returns a fresh token. The session is opened at
app launch, never mid-dictation, and kept for the app's lifetime. A declined
request is remembered so the dialog is not shown on every launch; the user can
ask again from Settings.

Not used in SteamOS Game Mode: gamescope has no RemoteDesktop portal, and
xdotool on gamescope's own X server reaches every game there. X11 sessions
keep xdotool too (the KDE portal refuses remote control on X11).

The session thread owns a private D-Bus connection on its own GLib context
(the same pattern as hotkeys/dbus.py); key calls come from the injection
thread with ``call_sync``, which is safe from any thread.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

_PORTAL_DEST = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_REQUEST_IFACE = "org.freedesktop.portal.Request"
_SESSION_IFACE = "org.freedesktop.portal.Session"
_REMOTE_IFACE = "org.freedesktop.portal.RemoteDesktop"

DEVICE_KEYBOARD = 1
PERSIST_UNTIL_REVOKED = 2
_RESPONSE_OK = 0
_RESPONSE_CANCELLED = 1

# CreateSession and SelectDevices answer at once; Start waits for the user.
_SETUP_TIMEOUT_S = 20
_CALL_TIMEOUT_MS = 2000

_STATE_FILE = "portal-keyboard.json"

# Keysyms for the characters and keys Aura sends.
KEYSYM_RETURN = 0xFF0D
_CHAR_KEYSYMS = {"\n": KEYSYM_RETURN, "\r": KEYSYM_RETURN, "\t": 0xFF09}
_NAMED_KEYSYMS = {
    "return": KEYSYM_RETURN, "enter": KEYSYM_RETURN, "tab": 0xFF09,
    "escape": 0xFF1B, "backspace": 0xFF08, "space": 0x20,
    "ctrl": 0xFFE3, "control": 0xFFE3, "shift": 0xFFE1, "alt": 0xFFE9,
    "super": 0xFFEB,
}


class PortalKeyboardError(Exception):
    """A key could not be sent through the portal."""


def keysym_for_char(ch: str) -> int:
    """The X keysym for one character: Latin-1 keysyms equal the code point,
    everything else uses the Unicode keysym range."""
    if ch in _CHAR_KEYSYMS:
        return _CHAR_KEYSYMS[ch]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


def keysym_for_name(name: str) -> int:
    """Keysym for a key name ("Return", "ctrl", "v")."""
    key = name.strip()
    if key.lower() in _NAMED_KEYSYMS:
        return _NAMED_KEYSYMS[key.lower()]
    if len(key) == 1:
        return keysym_for_char(key)
    raise PortalKeyboardError(f"unknown key name {name!r}")


def untypeable_chars(text: str, has_keysym: Callable[[int], bool]) -> set:
    """Characters the compositor's keymap has no key for.

    KWin and Mutter type a keysym only when the current layout has a key for
    it (pressing Shift/AltGr as needed); anything else is dropped. The caller
    pastes such text instead of losing letters.
    """
    missing = set()
    for ch in set(text):
        if ch in _CHAR_KEYSYMS or 0x20 <= ord(ch) <= 0x7E:
            continue
        if not has_keysym(keysym_for_char(ch)):
            missing.add(ch)
    return missing


def ascii_fold(text: str) -> str:
    """Accents stripped (café -> cafe); the last resort when nothing can paste."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


# --------------------------------------------------------------------------
# When to use it
# --------------------------------------------------------------------------

def _gi():
    try:
        import gi
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
        return Gio, GLib
    except Exception:
        return None


def gamescope_x_server() -> bool:
    """True when DISPLAY is gamescope's X server (SteamOS Game Mode or a
    gamescope window): its root window carries GAMESCOPE_* properties."""
    try:
        from Xlib import X, display
    except Exception:
        return False
    try:
        disp = display.Display()
    except Exception:
        return False
    try:
        root = disp.screen().root
        atom = disp.intern_atom("GAMESCOPE_FOCUSED_WINDOW", only_if_exists=True)
        if not atom:
            return False
        return root.get_full_property(atom, X.AnyPropertyType) is not None
    except Exception:
        return False
    finally:
        try:
            disp.close()
        except Exception:
            pass


def host_is_wayland_desktop() -> bool:
    """A Wayland desktop session (KDE Plasma, GNOME), not Game Mode and not X11.

    Inside the Flatpak XDG_SESSION_TYPE is forced to x11, so the X server is
    asked instead: XWayland means a Wayland compositor, and gamescope (also
    XWayland) is told apart by its root properties.
    """
    import os
    import sys

    if not sys.platform.startswith("linux"):
        return False
    try:
        from ..utils.platform import is_game_mode
        if is_game_mode():
            return False
    except Exception:
        pass
    try:
        from .injector import _running_under_xwayland
        xwayland = _running_under_xwayland()
    except Exception:
        xwayland = False
    if not xwayland and not (os.environ.get("WAYLAND_DISPLAY")
                             and os.environ.get("XDG_SESSION_TYPE") != "x11"):
        return False
    return not gamescope_x_server()


def portal_keyboard_offered(bus=None) -> bool:
    """The desktop's RemoteDesktop portal exists and offers a keyboard."""
    gi = _gi()
    if gi is None:
        return False
    Gio, GLib = gi
    try:
        if bus is None:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = bus.call_sync(
            _PORTAL_DEST, _PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (_REMOTE_IFACE, "AvailableDeviceTypes")),
            GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, _CALL_TIMEOUT_MS, None,
        )
        return bool(int(reply.unpack()[0]) & DEVICE_KEYBOARD)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Remembered state (restore token, declined)
# --------------------------------------------------------------------------

def _state_path() -> Path:
    from ..utils.platform import get_config_dir
    return get_config_dir() / _STATE_FILE


def load_state(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads((path or _state_path()).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(update: dict, path: Optional[Path] = None) -> None:
    target = path or _state_path()
    data = load_state(target)
    data.update(update)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(target)
    except Exception:
        pass


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------

class PortalKeyboard:
    """One keyboard-only RemoteDesktop session, kept open while the app runs."""

    IDLE, STARTING, WAITING, READY, DECLINED, FAILED, CLOSED = (
        "idle", "starting", "waiting for approval", "ready", "declined", "failed", "closed")

    def __init__(self, *, state_path: Optional[Path] = None, bus_factory=None):
        self._state_path = state_path
        self._bus_factory = bus_factory
        self._state = self.IDLE
        self._detail = ""
        self._bus = None
        self._session = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._changed = threading.Condition()
        self._type_lock = threading.Lock()
        self._listeners: list = []

    # -- status ---------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def detail(self) -> str:
        return self._detail

    def ready(self) -> bool:
        return self._state == self.READY and bool(self._session) and self._bus is not None

    def on_change(self, callback: Callable[[str, str], None]) -> None:
        """callback(state, detail) on every state change (any thread)."""
        self._listeners.append(callback)

    def _set(self, state: str, detail: str = "") -> None:
        with self._changed:
            self._state, self._detail = state, detail
            self._changed.notify_all()
        for callback in list(self._listeners):
            try:
                callback(state, detail)
            except Exception:
                pass

    def wait_until_settled(self, timeout: float) -> str:
        """Wait while starting (not while the dialog waits for the user)."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self._state in (self.IDLE, self.STARTING) and self._thread is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._changed.wait(left)
        return self._state

    # -- lifecycle ------------------------------------------------------------
    def start(self, *, parent_window: str = "", ask_again: bool = False,
              log: Optional[Callable[[str], None]] = None) -> bool:
        """Open the session in the background. False when nothing was started
        (already running, or a declined request and ``ask_again`` is False)."""
        if self._thread is not None and self._thread.is_alive():
            if not self._stop.is_set():
                return False
            self._thread.join(timeout=2.0)  # closing: let it finish first
            if self._thread.is_alive():
                return False
        remembered = load_state(self._state_path)
        if remembered.get("declined") and not ask_again:
            self._set(self.DECLINED, "declined earlier")
            return False
        self._stop.clear()
        self._set(self.STARTING)
        self._thread = threading.Thread(
            target=self._run, args=(parent_window, remembered.get("restore_token") or "", log),
            name="wayfinder-portal-keyboard", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        bus, session = self._bus, self._session
        if bus is not None and session:
            gi = _gi()
            if gi is not None:
                Gio, _GLib = gi
                try:
                    bus.call_sync(_PORTAL_DEST, session, _SESSION_IFACE, "Close", None, None,
                                  Gio.DBusCallFlags.NONE, _CALL_TIMEOUT_MS, None)
                except Exception:
                    pass

    def _open_bus(self, Gio):
        if self._bus_factory is not None:
            return self._bus_factory()
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        return Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)

    def _run(self, parent_window: str, restore_token: str,
             log: Optional[Callable[[str], None]]) -> None:
        def say(msg: str) -> None:
            if log:
                try:
                    log(msg)
                except Exception:
                    pass

        gi = _gi()
        if gi is None:
            self._set(self.FAILED, "PyGObject unavailable")
            return
        Gio, GLib = gi
        context = GLib.MainContext.new()
        context.push_thread_default()
        bus = None
        subs: list = []
        try:
            bus = self._open_bus(Gio)
            sender = (bus.get_unique_name() or "").lstrip(":").replace(".", "_")
            responses: dict = {}
            waiting = {"path": "", "loop": None, "closed": ""}

            def quit_loop() -> None:
                loop = waiting["loop"]
                if loop is not None:
                    loop.quit()

            def on_response(_c, _s, path, _i, _n, params):
                try:
                    code, results = params.unpack()
                except Exception:
                    code, results = 2, {}
                responses[path] = (int(code), results if isinstance(results, dict) else {})
                if path == waiting["path"]:
                    quit_loop()

            def on_closed(_c, _s, path, _i, _n, _p):
                if path and path == self._session:
                    waiting["closed"] = "the desktop ended the session"
                    quit_loop()

            def on_owner(_c, _s, _p, _i, _n, params):
                try:
                    _name, old, new = params.unpack()
                except Exception:
                    return
                if old:
                    waiting["closed"] = "the portal restarted" if new else "the portal exited"
                    quit_loop()

            subs.append(bus.signal_subscribe(_PORTAL_DEST, _REQUEST_IFACE, "Response", None,
                                             None, Gio.DBusSignalFlags.NONE, on_response))
            subs.append(bus.signal_subscribe(_PORTAL_DEST, _SESSION_IFACE, "Closed", None,
                                             None, Gio.DBusSignalFlags.NONE, on_closed))
            subs.append(bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus",
                                             "NameOwnerChanged", "/org/freedesktop/DBus",
                                             _PORTAL_DEST, Gio.DBusSignalFlags.NONE, on_owner))

            poll = GLib.timeout_source_new(500)

            def on_poll(*_a):
                if self._stop.is_set() or waiting["closed"] or waiting["path"] in responses:
                    quit_loop()
                return GLib.SOURCE_CONTINUE

            poll.set_callback(on_poll)
            poll.attach(context)

            def wait(path: str, timeout_s: Optional[int]):
                if path in responses:
                    return responses[path]
                waiting["path"] = path
                loop = GLib.MainLoop.new(context, False)
                waiting["loop"] = loop
                timer = None
                if timeout_s is not None:
                    timer = GLib.timeout_source_new_seconds(timeout_s)
                    timer.set_callback(lambda *_: (loop.quit(), GLib.SOURCE_REMOVE)[1])
                    timer.attach(context)
                try:
                    loop.run()
                finally:
                    if timer is not None:
                        timer.destroy()
                    waiting["loop"] = None
                    waiting["path"] = ""
                return responses.get(path)

            def request(method: str, signature: str, args: tuple, timeout_s: Optional[int]):
                token = f"wayfinder_{secrets.token_hex(12)}"
                predicted = f"{_PORTAL_PATH}/request/{sender}/{token}"
                options = dict(args[-1])
                options["handle_token"] = GLib.Variant("s", token)
                reply = bus.call_sync(_PORTAL_DEST, _PORTAL_PATH, _REMOTE_IFACE, method,
                                      GLib.Variant(signature, args[:-1] + (options,)),
                                      GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE,
                                      _SETUP_TIMEOUT_S * 1000, None)
                try:
                    path = reply.unpack()[0] or predicted
                except Exception:
                    path = predicted
                return wait(path, timeout_s)

            created = request("CreateSession", "(a{sv})", ({
                "session_handle_token": GLib.Variant("s", f"wayfinder_{secrets.token_hex(12)}"),
            },), _SETUP_TIMEOUT_S)
            if self._stop.is_set():
                return
            if created is None or created[0] != _RESPONSE_OK:
                self._set(self.FAILED, "the desktop did not create a session")
                return
            self._session = str(created[1].get("session_handle") or "")
            if not self._session:
                self._set(self.FAILED, "no session handle")
                return

            select_options = {
                "types": GLib.Variant("u", DEVICE_KEYBOARD),
                "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
            }
            if restore_token:
                select_options["restore_token"] = GLib.Variant("s", restore_token)
            selected = request("SelectDevices", "(oa{sv})", (self._session, select_options),
                               _SETUP_TIMEOUT_S)
            if self._stop.is_set():
                return
            if selected is None or selected[0] != _RESPONSE_OK:
                self._set(self.FAILED, "the desktop refused a keyboard")
                return

            self._set(self.WAITING)
            if not restore_token:
                say("⌨️ Asking your desktop to let Aura type in every app — choose "
                    "Allow (and keep 'remember' checked) in the dialog")
            # No timeout: the dialog waits for the user (maybe away at login).
            started = request("Start", "(osa{sv})", (self._session, parent_window or "", {}),
                              None)
            if self._stop.is_set():
                return
            if waiting["closed"]:
                self._set(self.CLOSED, waiting["closed"])
                return
            if started is None:
                self._set(self.FAILED, "no answer from the desktop")
                return
            code, results = started
            if code == _RESPONSE_CANCELLED:
                save_state({"declined": True, "restore_token": ""}, self._state_path)
                self._set(self.DECLINED, "you chose not to allow it")
                return
            if code != _RESPONSE_OK:
                self._set(self.FAILED, f"the desktop answered {code}")
                return
            devices = int(results.get("devices", 0) or 0)
            if not devices & DEVICE_KEYBOARD:
                self._set(self.FAILED, "the desktop granted no keyboard")
                return
            token = results.get("restore_token")
            save_state({"declined": False, "restore_token": str(token or "")}, self._state_path)
            self._bus = bus
            self._set(self.READY)
            # Stay alive to notice the session ending (revoked, portal restart).
            while not self._stop.is_set() and not waiting["closed"]:
                wait("", None)
            self._bus = None
            if waiting["closed"]:
                self._set(self.CLOSED, waiting["closed"])
        except Exception as exc:
            self._set(self.FAILED, f"{type(exc).__name__}: {exc}")
        finally:
            self._bus = None
            if self._stop.is_set() and self._state in (self.STARTING, self.WAITING, self.READY):
                self._set(self.IDLE, "stopped")
            if bus is not None:
                for sub in subs:
                    try:
                        bus.signal_unsubscribe(sub)
                    except Exception:
                        pass
            try:
                context.pop_thread_default()
            except Exception:
                pass

    # -- keys -----------------------------------------------------------------
    def _notify(self, keysym: int, pressed: bool) -> None:
        bus, session = self._bus, self._session
        if bus is None or not session:
            raise PortalKeyboardError("the portal keyboard is not ready")
        gi = _gi()
        if gi is None:
            raise PortalKeyboardError("PyGObject unavailable")
        Gio, GLib = gi
        try:
            bus.call_sync(_PORTAL_DEST, _PORTAL_PATH, _REMOTE_IFACE, "NotifyKeyboardKeysym",
                          GLib.Variant("(oa{sv}iu)", (session, {}, int(keysym), 1 if pressed else 0)),
                          None, Gio.DBusCallFlags.NONE, _CALL_TIMEOUT_MS, None)
        except Exception as exc:
            raise PortalKeyboardError(f"portal key failed: {exc}") from exc

    def _tap(self, keysym: int) -> None:
        self._notify(keysym, True)
        # Always release, retrying once: a key left down auto-repeats in the
        # target app until the next press.
        try:
            self._notify(keysym, False)
        except PortalKeyboardError:
            self._notify(keysym, False)

    def type_text(self, text: str, key_delay_ms: float = 0.0) -> None:
        """Type ``text`` as key presses; Enter for newlines."""
        delay = max(0.0, key_delay_ms) / 1000.0
        with self._type_lock:
            for typed, ch in enumerate(text):
                try:
                    self._tap(keysym_for_char(ch))
                except PortalKeyboardError as e:
                    e.typed = typed  # characters that already landed
                    raise
                if delay:
                    time.sleep(delay)

    def press_keys(self, combo: str, hold_s: float = 0.0) -> None:
        """Press "Return", "ctrl+v"...: modifiers first, released last."""
        keysyms = [keysym_for_name(part) for part in combo.split("+") if part.strip()]
        if not keysyms:
            return
        with self._type_lock:
            pressed: list = []
            try:
                for ks in keysyms:
                    self._notify(ks, True)
                    pressed.append(ks)
                if hold_s > 0:
                    time.sleep(hold_s)
            finally:
                for ks in reversed(pressed):
                    try:
                        self._notify(ks, False)
                    except PortalKeyboardError:
                        pass


_KEYBOARD: Optional[PortalKeyboard] = None
_KEYBOARD_LOCK = threading.Lock()


def keyboard() -> PortalKeyboard:
    """The app's single portal keyboard."""
    global _KEYBOARD
    with _KEYBOARD_LOCK:
        if _KEYBOARD is None:
            _KEYBOARD = PortalKeyboard()
        return _KEYBOARD


def ready() -> bool:
    """True when dictation should type through the portal."""
    return _KEYBOARD is not None and _KEYBOARD.ready()

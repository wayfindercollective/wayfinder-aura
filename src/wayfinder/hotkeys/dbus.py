"""GlobalShortcuts portal hotkeys, spoken over GDBus (Gio) — no dbus-python.

The portal is the only sandbox-viable global-hotkey mechanism: in a Flatpak,
evdev can't read /dev/input and pynput can't see Wayland global keys. The
lifecycle is the portal's request/response dance:

    CreateSession → Request.Response (real session_handle)
    BindShortcuts → Request.Response (the list actually bound)
    Activated signals on the session → events

Both Responses arrive asynchronously on per-call Request objects, so each
subscription is set up BEFORE its call is issued — the portal may answer before
the method call even returns, and a subscription set up afterwards would miss
it. The Response subscription watches every path and filters in the handler:
GDBus matches signals against subscriptions at receive time, and pre-0.9
portals ignore handle_token and mint their own request path, whose Response
can arrive while call_sync is still returning that path to us.

A cancelled bind is not a failure: the session stays valid, the user can bind
the shortcuts later in System Settings → Shortcuts, and Activated still fires
once they do. Returning False on cancel would make the hotkey supervisor
re-arm the listener every 10 seconds and re-pop the compositor's confirmation
dialog forever.

PyGObject is the module's only dependency; dbus-python is deliberately not
used (it drags a native libdbus + dbus-glib chain the Flatpak runtime does not
guarantee).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from queue import Queue
from threading import Event
from typing import Callable, Optional

from wayfinder.utils.platform import get_portal_app_id

from .types import EventType

_PORTAL_DEST = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_REQUEST_IFACE = "org.freedesktop.portal.Request"
_SESSION_IFACE = "org.freedesktop.portal.Session"
_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"

# org.freedesktop.portal.Request.Response codes.
_RESPONSE_OK = 0
_RESPONSE_CANCELLED = 1

# CreateSession answers promptly or not at all — no dialog is involved.
_CREATE_TIMEOUT_SECONDS = 20

# The gi probe is cached: the import is not free, and its outcome cannot
# change within a process. gi can raise non-ImportError at import time —
# PyGObject's override loader asserts against the GLib typelib and a version
# mismatch raises AssertionError — so the guard is broad on purpose.
_GI_PROBE = {"checked": False, "available": False, "detail": ""}


def portal_shortcuts_available() -> bool:
    """True when PyGObject (Gio/GLib) imports — the portal path's only dependency."""
    if not _GI_PROBE["checked"]:
        try:
            from gi.repository import Gio, GLib  # noqa: F401
            _GI_PROBE["available"] = True
        except Exception as exc:
            _GI_PROBE["detail"] = f"{type(exc).__name__}: {exc}"
        _GI_PROBE["checked"] = True
    return _GI_PROBE["available"]


def portal_unavailable_detail() -> str:
    """Why the probe failed ("" when it succeeded or hasn't run)."""
    portal_shortcuts_available()
    return _GI_PROBE["detail"]


# --- trigger encoding -------------------------------------------------------

# evdev keycode → XKB keysym name, for the shortcuts-spec trigger string.
# Covers exactly the chord-able keys config's KEY_CODES offers; mouse buttons
# (BTN_*) have no keysym and yield "" — the portal then lets the user pick a
# trigger in System Settings instead of us guessing a wrong one.
_KEYSYM_BY_CODE = {
    59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
    65: "F7", 66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
    57: "space", 28: "Return", 15: "Tab", 14: "BackSpace",
    70: "Scroll_Lock", 119: "Pause",
}

# Config modifier names → shortcuts-spec modifier tokens.
_MODIFIER_TOKENS = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "super": "LOGO"}


def encode_trigger(key_code: object, modifiers: object) -> str:
    """Encode config's evdev keycode + modifier names as a shortcuts-spec trigger.

    "CTRL+ALT+space", per the XDG shortcuts spec the portal's preferred_trigger
    expects (modifier tokens CTRL/SHIFT/ALT/LOGO plus an XKB keysym name). The
    UI's display label ("Ctrl+Alt+Space") is presentation, not protocol, and
    must never be sent here. Returns "" when the chord has no spec encoding —
    an omitted preferred_trigger just means the user picks one at bind time.
    """
    try:
        keysym = _KEYSYM_BY_CODE.get(int(key_code))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if not keysym:
        return ""
    # Config merges user JSON blindly, so modifiers can be any type. A string
    # is deliberately rejected too — iterating "ctrl" yields characters, not
    # modifier names (Codex review).
    if not isinstance(modifiers, (list, tuple, set, frozenset)) and modifiers is not None:
        return ""
    tokens = []
    for mod in modifiers or []:
        token = _MODIFIER_TOKENS.get(str(mod).lower())
        if token is None:
            return ""  # unknown modifier — no trigger beats a wrong chord
        if token not in tokens:  # "ctrl,ctrl" must not encode CTRL+CTRL
            tokens.append(token)
    return "+".join(tokens + [keysym])


@dataclass(frozen=True)
class ShortcutSpec:
    """One portal shortcut: identity, what to tell the compositor, what to emit."""

    shortcut_id: str
    description: str
    trigger: str  # shortcuts-spec encoding; "" = let the user pick at bind time
    event: EventType


def shortcut_specs_from_config(config: dict) -> list[ShortcutSpec]:
    """The app's two global shortcuts, triggers encoded from the live config."""
    return [
        ShortcutSpec(
            shortcut_id="record-toggle",
            description="Toggle voice recording",
            trigger=encode_trigger(
                config.get("hotkey_key", 57), config.get("hotkey_modifiers") or []
            ),
            event=EventType.HOTKEY_PRESSED,
        ),
        ShortcutSpec(
            shortcut_id="style-toggle",
            description="Cycle writing style",
            trigger=encode_trigger(
                config.get("style_toggle_key", 28),
                config.get("style_toggle_modifiers") or [],
            ),
            event=EventType.STYLE_TOGGLE,
        ),
    ]


# --- the listener -----------------------------------------------------------

def wayland_hotkey_listener(
    event_queue: Queue,
    shortcuts: list[ShortcutSpec],
    stop_event: Event,
    log_callback: Optional[Callable[[str], None]] = None,
) -> bool:
    """Register global shortcuts via the portal and emit events until stopped.

    Blocking — run in a dedicated thread. Returns False when setup failed
    (no PyGObject, no portal, session denied) OR when a live session died
    (Session.Closed, portal restart, connection loss) — either way the
    caller may retry to build a fresh session. True only for a stop_event
    exit. A cancelled bind is neither: the session stays valid and the
    listener keeps running (see module docstring).
    """
    def log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    if not portal_shortcuts_available():
        log(f"⚠️ PyGObject unavailable ({portal_unavailable_detail()}) — portal hotkeys disabled")
        return False

    from gi.repository import Gio, GLib

    specs = {spec.shortcut_id: spec for spec in shortcuts}
    app_id = get_portal_app_id()

    # Private connection on a thread-default context so signal dispatch happens
    # here rather than on whichever main context owns the shared session bus.
    context = GLib.MainContext.new()
    context.push_thread_default()
    bus = None
    response_sub = None
    activated_sub = None
    session_closed_sub = None
    owner_sub = None
    stop_source = None
    try:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        bus = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

        # Request paths are /request/<sender>/<token> and knowable pre-call.
        # The spec asks for unique, unguessable tokens — app-id-derived and
        # pid+thread-id tokens are neither.
        unique = bus.get_unique_name() or ""
        sender = unique.lstrip(":").replace(".", "_")
        create_token = f"wayfinder_{secrets.token_hex(16)}"
        create_path = f"{_PORTAL_PATH}/request/{sender}/{create_token}"

        responses: dict[str, tuple[int, dict]] = {}
        closed_paths: set = set()
        # Mutable cells shared with handlers across the setup and listen phases.
        target = {"path": create_path}
        state = {"session_handle": "", "loop": None, "waiting_path": "", "closed": ""}

        def _quit_loop() -> None:
            loop = state["loop"]
            if loop is not None:
                loop.quit()

        def _mark_closed(reason: str) -> None:
            if not state["closed"]:
                state["closed"] = reason
            _quit_loop()

        def _on_response(_conn, _sender, path, _iface, _signal, params):
            try:
                code, results = params.unpack()
            except Exception:
                code, results = -1, {}
            responses[path] = (code, results if isinstance(results, dict) else {})
            if path == target["path"]:
                _quit_loop()

        # One subscription, every path, filtered in the handler (see module
        # docstring for why a path-pinned subscription cannot be moved safely).
        # The connection is private and the sender is pinned to the portal's
        # well-known name, so nothing can forge a Response.
        response_sub = bus.signal_subscribe(
            _PORTAL_DEST,
            _REQUEST_IFACE,
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_response,
        )

        def _on_activated(_conn, _sender, _path, _iface, _signal, params):
            try:
                session_handle, shortcut_id = params.unpack()[:2]
            except Exception:
                return
            # Filter on our session UNCONDITIONALLY: the subscription exists
            # before CreateSession, so until our handle is known ("" matches
            # nothing) another session's record-toggle must not start a
            # recording here (Codex review).
            if session_handle != state["session_handle"]:
                return
            spec = specs.get(shortcut_id)
            if spec is not None:
                event_queue.put((spec.event, None))

        # Subscribed before the session exists: Activated can fire the moment
        # a bind lands, and the sender pin keeps impostors out.
        activated_sub = bus.signal_subscribe(
            _PORTAL_DEST,
            _SHORTCUTS_IFACE,
            "Activated",
            _PORTAL_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_activated,
        )

        # A dead session must not be listened to forever (Codex review): the
        # compositor can close it, and a portal restart silently voids it while
        # this loop would keep "listening" and the supervisor would believe the
        # hotkeys are healthy. Three watchers, all present BEFORE CreateSession
        # so no closure can slip between session birth and subscription:
        #   1. Session.Closed — the spec's own termination signal. All paths,
        #      sender-pinned; paths are recorded so a session closed before its
        #      handle is even known is still caught.
        #   2. NameOwnerChanged for the portal — a crashed/restarted portal
        #      never emits Closed for the sessions it took down with it.
        #   3. The private connection's "closed" GObject signal — a dead bus
        #      delivers nothing, including the two signals above.
        # On any of these the listener returns False so the supervisor can
        # build a fresh session.
        def _on_session_closed(_conn, _sender, path, _iface, _signal, _params):
            closed_paths.add(path)
            if path == state["session_handle"]:
                _mark_closed("session closed by the portal")

        session_closed_sub = bus.signal_subscribe(
            _PORTAL_DEST,
            _SESSION_IFACE,
            "Closed",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_session_closed,
        )

        def _on_portal_owner_changed(_conn, _sender, _path, _iface, _signal, params):
            try:
                _name, old_owner, new_owner = params.unpack()
            except Exception:
                return
            if old_owner:  # first-time ownership (old == "") is not a loss
                _mark_closed("portal restarted" if new_owner else "portal exited")

        owner_sub = bus.signal_subscribe(
            "org.freedesktop.DBus",
            "org.freedesktop.DBus",
            "NameOwnerChanged",
            "/org/freedesktop/DBus",
            _PORTAL_DEST,
            Gio.DBusSignalFlags.NONE,
            _on_portal_owner_changed,
        )

        def _on_bus_closed(_conn, _remote_peer_vanished, _error):
            if not stop_event.is_set():
                _mark_closed("bus connection closed")

        bus.connect("closed", _on_bus_closed)

        # A 500ms poll that stays attached across every phase (Rule #1: no
        # sub-100ms polling; this is the same cadence the old listener used).
        # It quits the current loop on stop, and also when the awaited Response
        # was recorded in the gap between re-targeting and loop.run() — the
        # handler quits only the loop that is running when the signal lands,
        # so without this recovery an untimed wait could sleep forever.
        # Never self-removes: quitting is idempotent, and removing on the
        # first stop tick could fire between loops and leave the next
        # loop.run() with nothing to quit it.
        stop_source = GLib.timeout_source_new(500)

        def _poll(*_args) -> bool:
            if (
                stop_event.is_set()
                or state["closed"]
                or state["waiting_path"] in responses
            ):
                _quit_loop()
            return GLib.SOURCE_CONTINUE

        stop_source.set_callback(_poll)
        stop_source.attach(context)

        def _wait_for(path: str, timeout_seconds: Optional[int]) -> Optional[tuple[int, dict]]:
            """Run the loop until Response[path], timeout, or stop. None = neither arrived."""
            if path in responses:
                return responses[path]
            target["path"] = path
            state["waiting_path"] = path
            loop = GLib.MainLoop.new(context, False)
            state["loop"] = loop
            timer = None
            if timeout_seconds is not None:
                timer = GLib.timeout_source_new_seconds(timeout_seconds)
                timer.set_callback(lambda *_: (loop.quit(), GLib.SOURCE_REMOVE)[1])
                timer.attach(context)
            try:
                loop.run()
            finally:
                if timer is not None:
                    timer.destroy()
                state["loop"] = None
                state["waiting_path"] = ""
            return responses.get(path)

        log(f"🔗 Requesting GlobalShortcuts portal session as '{app_id}'…")
        reply = bus.call_sync(
            _PORTAL_DEST,
            _PORTAL_PATH,
            _SHORTCUTS_IFACE,
            "CreateSession",
            GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", create_token),
                "session_handle_token": GLib.Variant("s", f"wayfinder_{secrets.token_hex(16)}"),
            },)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            _CREATE_TIMEOUT_SECONDS * 1000,
            None,
        )
        # Follow the returned handle when it differs from the prediction — the
        # handler is already watching every path, so the minted handle's
        # Response cannot have been missed.
        try:
            create_actual = reply.unpack()[0] or create_path
        except Exception:
            create_actual = create_path

        created = _wait_for(create_actual, _CREATE_TIMEOUT_SECONDS)
        if stop_event.is_set():
            return True
        if state["closed"]:
            log(f"⚠️ Portal hotkeys: {state['closed']} during setup")
            return False
        if created is None:
            log(f"⚠️ Portal CreateSession: no Response within {_CREATE_TIMEOUT_SECONDS}s")
            return False
        code, results = created
        if code != _RESPONSE_OK:
            log(f"⚠️ Portal CreateSession denied (code {code})")
            return False
        session_handle = str(results.get("session_handle") or "")
        if not session_handle:
            log("⚠️ Portal returned no session_handle")
            return False
        state["session_handle"] = session_handle
        if session_handle in closed_paths:
            # Closed before the handle was even known — the all-paths
            # subscription recorded it, so the race costs nothing.
            log("⚠️ Portal hotkeys: session closed by the portal during setup")
            return False
        log("✓ Portal session established")

        bind_token = f"wayfinder_{secrets.token_hex(16)}"
        bind_path = f"{_PORTAL_PATH}/request/{sender}/{bind_token}"
        shortcut_list = []
        for spec in shortcuts:
            props = {"description": GLib.Variant("s", spec.description)}
            if spec.trigger:
                props["preferred_trigger"] = GLib.Variant("s", spec.trigger)
            shortcut_list.append((spec.shortcut_id, props))

        reply = bus.call_sync(
            _PORTAL_DEST,
            _PORTAL_PATH,
            _SHORTCUTS_IFACE,
            "BindShortcuts",
            GLib.Variant(
                "(oa(sa{sv})sa{sv})",
                (
                    session_handle,
                    shortcut_list,
                    "",  # parent_window: no window handle for a background app
                    {"handle_token": GLib.Variant("s", bind_token)},
                ),
            ),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            _CREATE_TIMEOUT_SECONDS * 1000,
            None,
        )
        try:
            bind_actual = reply.unpack()[0] or bind_path
        except Exception:
            bind_actual = bind_path

        # The bind Response may wait on a compositor confirmation dialog, so
        # there is no timeout: the stop poll keeps the wait interruptible, and
        # a session whose bind is still pending can already listen.
        bound = _wait_for(bind_actual, None)
        if stop_event.is_set():
            return True
        if bound is None:
            if not state["closed"]:
                log("⚠️ Shortcut bind: no Response — listening anyway")
        else:
            bcode, bresults = bound
            if bcode == _RESPONSE_OK:
                # Code 0 alone is not registration: the spec lets the portal
                # return any subset. Verify OUR ids are in the bound list.
                returned = set()
                try:
                    for item in bresults.get("shortcuts") or []:
                        returned.add(str(item[0]))
                except Exception:
                    pass
                missing = [s.shortcut_id for s in shortcuts if s.shortcut_id not in returned]
                for spec in shortcuts:
                    if spec.shortcut_id in returned:
                        log(f"✓ Shortcut registered: {spec.shortcut_id}"
                            f" (trigger: {spec.trigger or 'choose in System Settings'})")
                if missing:
                    log(f"⚠️ Portal did not bind: {', '.join(missing)}"
                        " — set them in System Settings → Shortcuts")
            elif bcode == _RESPONSE_CANCELLED:
                log("⚠️ Shortcut bind cancelled — set them in System Settings → Shortcuts")
            else:
                log(f"⚠️ Shortcut bind failed (code {bcode})"
                    " — set them in System Settings → Shortcuts")

        # Listen until stopped or the session dies. Even after a cancelled
        # bind the session is valid: bindings made later in System Settings
        # deliver Activated here.
        if not state["closed"]:
            log("🎧 Listening for global shortcuts (portal)…")
            loop = GLib.MainLoop.new(context, False)
            state["loop"] = loop
            if not stop_event.is_set() and not state["closed"]:
                loop.run()
            state["loop"] = None
        if state["closed"] and not stop_event.is_set():
            # False → the supervisor rebuilds a fresh session (the dead one
            # can never deliver another Activated).
            log(f"⚠️ Portal hotkeys: {state['closed']} — restarting listener")
            return False
        return True

    except Exception as exc:
        log(f"⚠️ Portal hotkey setup failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        try:
            if stop_source is not None:
                stop_source.destroy()
            if bus is not None:
                for sub in (response_sub, activated_sub, session_closed_sub, owner_sub):
                    if sub is not None:
                        bus.signal_unsubscribe(sub)
                bus.close_sync(None)
        except Exception:
            pass
        context.pop_thread_default()

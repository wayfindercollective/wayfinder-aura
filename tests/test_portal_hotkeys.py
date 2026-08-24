"""GlobalShortcuts portal listener — the full lifecycle against a fake bus.

Mirrors tests/test_open_url.py's approach: a fake session bus that speaks
exactly the portal's request-reply shape, asserts the wire contract inside
``call_sync`` (so structural drift fails even where PyGObject is absent), and
models GDBus receive-time signal matching. A further test runs the listener
with the REAL GLib.Variant machinery where PyGObject is installed.

The regression this suite pins hardest: a cancelled bind must NOT read as
failure — returning False would make the hotkey supervisor re-arm the listener
every 10 seconds and re-pop the compositor's confirmation dialog forever.
"""

import sys
import types
from queue import Queue
from threading import Event

import pytest

import wayfinder.hotkeys.dbus as portal
from wayfinder.hotkeys.dbus import (
    ShortcutSpec,
    encode_trigger,
    shortcut_specs_from_config,
)
from wayfinder.hotkeys.types import EventType

SESSION_HANDLE = "/org/freedesktop/portal/desktop/session/1_42/wayfinder_s1"


@pytest.fixture(autouse=True)
def fresh_probe(monkeypatch):
    """The gi probe caches per process; tests must not inherit each other's."""
    monkeypatch.setattr(
        portal, "_GI_PROBE", {"checked": False, "available": False, "detail": ""}
    )


def _specs(record_trigger="CTRL+ALT+space", style_trigger="CTRL+ALT+Return"):
    return [
        ShortcutSpec("record-toggle", "Toggle voice recording",
                     record_trigger, EventType.HOTKEY_PRESSED),
        ShortcutSpec("style-toggle", "Cycle writing style",
                     style_trigger, EventType.STYLE_TOGGLE),
    ]


# ── trigger encoding: config is protocol input, the display label is not ─────

@pytest.mark.parametrize("code,mods,expected", [
    (57, ["ctrl", "alt"], "CTRL+ALT+space"),
    (28, ["ctrl", "alt"], "CTRL+ALT+Return"),
    (67, [], "F9"),
    (61, ["super"], "LOGO+F3"),
    (15, ["shift"], "SHIFT+Tab"),
    (14, ["ctrl", "shift"], "CTRL+SHIFT+BackSpace"),
    (70, [], "Scroll_Lock"),
    (119, [], "Pause"),
])
def test_encode_trigger_known_chords(code, mods, expected):
    assert encode_trigger(code, mods) == expected


@pytest.mark.parametrize("code,mods", [
    (272, ["ctrl"]),      # BTN_LEFT — mouse buttons have no keysym
    (276, []),            # BTN_EXTRA
    (999, []),            # unmapped keycode
    (57, ["hyper"]),      # unknown modifier: no trigger beats a wrong chord
    ("x", ["ctrl"]),      # garbage keycode
    (None, []),
    (57, 7),              # config merges user JSON blindly — must not raise
    (57, "ctrl"),         # a string iterates characters, not modifier names
    (57, {"ctrl": True}),  # a dict iterates keys — reject the type outright
])
def test_encode_trigger_unencodable_yields_empty(code, mods):
    assert encode_trigger(code, mods) == ""


def test_encode_trigger_deduplicates_modifiers():
    # "ctrl,ctrl" in a hand-edited config must not encode CTRL+CTRL+space.
    assert encode_trigger(57, ["ctrl", "ctrl", "alt"]) == "CTRL+ALT+space"


def test_specs_from_default_config():
    config = {
        "hotkey_key": 57, "hotkey_modifiers": ["ctrl", "alt"],
        "style_toggle_key": 28, "style_toggle_modifiers": ["ctrl", "alt"],
    }
    specs = shortcut_specs_from_config(config)
    assert [s.shortcut_id for s in specs] == ["record-toggle", "style-toggle"]
    assert [s.event for s in specs] == [EventType.HOTKEY_PRESSED, EventType.STYLE_TOGGLE]
    assert specs[0].trigger == "CTRL+ALT+space"
    assert specs[1].trigger == "CTRL+ALT+Return"


def test_specs_with_mouse_hotkey_omit_the_trigger():
    config = {
        "hotkey_key": 275, "hotkey_modifiers": [],  # BTN_SIDE
        "style_toggle_key": 28, "style_toggle_modifiers": ["ctrl", "alt"],
    }
    specs = shortcut_specs_from_config(config)
    assert specs[0].trigger == ""       # user picks one at bind time
    assert specs[1].trigger == "CTRL+ALT+Return"


# ── the fake bus ─────────────────────────────────────────────────────────────

class _Reply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeMainContext:
    def push_thread_default(self):
        pass

    def pop_thread_default(self):
        pass


class _FakeTimer:
    def set_callback(self, cb):
        pass

    def attach(self, context):
        pass

    def destroy(self):
        pass


def _token_from_options(options):
    value = options["handle_token"]
    return value[1] if isinstance(value, tuple) else value


class _FakeBus:
    """A session bus speaking the GlobalShortcuts request-reply lifecycle.

    ``create_code``/``bind_code`` are the two Responses' codes;
    ``bound_ids`` is what the bind Response reports as actually bound
    (None = echo everything requested); ``answers`` controls whether
    Responses arrive at all (``bind_answers`` narrows that to the bind
    Response alone); ``mismatch`` makes CreateSession mint its own
    request handle the way pre-0.9 portals do; ``activations`` are
    (session_handle, shortcut_id) pairs delivered once the listener reaches
    its listening loop. Responses are matched at RECEIVE time, like GDBus.
    """

    def __init__(self, create_code=0, bind_code=0, bound_ids=None,
                 answers=True, bind_answers=True, mismatch=False, activations=(),
                 close_session=False, portal_owner_change=None):
        self._create_code = create_code
        self._bind_code = bind_code
        self._bound_ids = bound_ids
        self._answers = answers
        self._bind_answers = bind_answers
        self._mismatch = mismatch
        self._activations = list(activations)
        self._close_session = close_session
        self._portal_owner_change = portal_owner_change  # (old, new) owners
        self.subscriptions = {}
        self.events = []
        self.bind_shortcut_args = None
        self._next_id = 1
        self._pending = None

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, sender, iface, signal, path, arg0, flags, cb):
        sid = self._next_id
        self._next_id += 1
        self.subscriptions[sid] = (sender, iface, signal, path, cb)
        self.events.append(("subscribe", sender, signal, path))
        return sid

    def signal_unsubscribe(self, sid):
        self.subscriptions.pop(sid, None)
        self.events.append(("unsubscribe", sid))

    def _response_would_match(self, path):
        return any(
            sig == "Response" and (sp is None or sp == path)
            for _s, _i, sig, sp, _cb in self.subscriptions.values()
        )

    def call_sync(self, dest, path, iface, method, params, reply_type, flags,
                  timeout, cancellable):
        assert dest == "org.freedesktop.portal.Desktop"
        assert path == "/org/freedesktop/portal/desktop"
        assert iface == "org.freedesktop.portal.GlobalShortcuts"
        rt = reply_type if isinstance(reply_type, str) else reply_type.dup_string()
        assert rt == "(o)"
        if hasattr(params, "get_type_string"):  # real GLib.Variant
            fmt, body = params.get_type_string(), params.unpack()
        else:
            fmt, body = params
        self.events.append(("call", method))
        sender = self.get_unique_name().lstrip(":").replace(".", "_")

        if method == "CreateSession":
            assert fmt == "(a{sv})"
            options = body[0]
            assert isinstance(options, dict), \
                "a{sv} takes a plain dict of Variants; a pre-wrapped Variant raises in PyGObject"
            token = _token_from_options(options)
            session_token = options["session_handle_token"]
            session_token = session_token[1] if isinstance(session_token, tuple) else session_token
            assert isinstance(token, str) and token
            assert isinstance(session_token, str) and session_token
            predicted = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
            actual = predicted + "_minted" if self._mismatch else predicted
            results = {"session_handle": SESSION_HANDLE}
            if self._answers and self._response_would_match(actual):
                self._pending = (actual, self._create_code, results)
            return _Reply((actual,))

        assert method == "BindShortcuts"
        assert fmt == "(oa(sa{sv})sa{sv})"
        session_handle, shortcut_list, parent_window, options = body
        assert session_handle == SESSION_HANDLE
        assert parent_window == ""
        assert isinstance(options, dict)
        token = _token_from_options(options)
        assert isinstance(token, str) and token
        self.bind_shortcut_args = shortcut_list
        requested_ids = [item[0] for item in shortcut_list]
        bound = self._bound_ids if self._bound_ids is not None else requested_ids
        results = {"shortcuts": [(sid, {}) for sid in bound]}
        predicted = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        if self._answers and self._bind_answers and self._response_would_match(predicted):
            self._pending = (predicted, self._bind_code, results)
        return _Reply((predicted,))

    def dispatch(self):
        """What loop.run() does: deliver the pending Response, or — once the
        listener is in its listening loop — the queued Activated signals."""
        if self._pending is not None:
            path, code, results = self._pending
            self._pending = None
            for sender, _iface, sig, spath, cb in list(self.subscriptions.values()):
                if sig == "Response" and (spath is None or spath == path):
                    cb(None, sender, path, "org.freedesktop.portal.Request",
                       "Response", _Reply((code, results)))
            return
        activations, self._activations = self._activations, []
        for session_handle, shortcut_id in activations:
            for sender, _iface, sig, spath, cb in list(self.subscriptions.values()):
                if sig == "Activated":
                    cb(None, sender, spath, "org.freedesktop.portal.GlobalShortcuts",
                       "Activated", _Reply((session_handle, shortcut_id, 12345, {})))
        if self._close_session:
            self._close_session = False
            for sender, _iface, sig, _spath, cb in list(self.subscriptions.values()):
                if sig == "Closed":
                    cb(None, sender, SESSION_HANDLE,
                       "org.freedesktop.portal.Session", "Closed", _Reply(({},)))
        if self._portal_owner_change is not None:
            old, new = self._portal_owner_change
            self._portal_owner_change = None
            for sender, _iface, sig, spath, cb in list(self.subscriptions.values()):
                if sig == "NameOwnerChanged":
                    cb(None, sender, spath, "org.freedesktop.DBus", "NameOwnerChanged",
                       _Reply(("org.freedesktop.portal.Desktop", old, new)))

    def connect(self, signal_name, cb):
        """GObject-style signal connect (the module watches "closed")."""
        self.gobject_handlers = getattr(self, "gobject_handlers", {})
        self.gobject_handlers[signal_name] = cb
        return 1

    def close_sync(self, cancellable):
        self.events.append(("close",))


class _FakeLoop:
    def __init__(self, bus):
        self._bus = bus

    def run(self):
        # A matching Response quits via the callback; no pending Response
        # models either the timeout firing or — in the listening phase —
        # activations arriving and the stop poll ending the loop.
        self._bus.dispatch()

    def quit(self):
        pass


def _install_fake_gi(monkeypatch, bus, real_glib=None):
    """Wire the fakes; with ``real_glib`` the variants are built by the real
    GLib machinery while the connection and main loop stay fake."""
    glib = types.SimpleNamespace(
        MainContext=types.SimpleNamespace(new=_FakeMainContext),
        MainLoop=types.SimpleNamespace(new=lambda ctx, running: _FakeLoop(bus)),
        Variant=real_glib.Variant if real_glib else (lambda fmt, value: (fmt, value)),
        VariantType=real_glib.VariantType if real_glib else (lambda s: s),
        timeout_source_new=lambda ms: _FakeTimer(),
        timeout_source_new_seconds=lambda s: _FakeTimer(),
        SOURCE_REMOVE=False,
        SOURCE_CONTINUE=True,
    )
    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SESSION=2),
        DBusConnectionFlags=types.SimpleNamespace(
            AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2
        ),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        dbus_address_get_for_bus_sync=lambda bus_type, cancellable: "unix:fake",
        DBusConnection=types.SimpleNamespace(new_for_address_sync=lambda *a, **k: bus),
    )
    repo = types.ModuleType("gi.repository")
    repo.Gio, repo.GLib = gio, glib
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    return bus


def _run(bus, specs=None, logs=None):
    queue = Queue()
    result = portal.wayland_hotkey_listener(
        queue, _specs() if specs is None else specs, Event(),
        (logs.append if logs is not None else None),
    )
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return result, events


# ── availability ─────────────────────────────────────────────────────────────

def test_listener_fails_cleanly_without_pygobject(monkeypatch):
    monkeypatch.setattr(
        portal, "_GI_PROBE",
        {"checked": True, "available": False, "detail": "ImportError: nope"},
    )
    logs = []
    result, events = _run(_FakeBus(), logs=logs)  # bus never reached
    assert result is False
    assert events == []
    assert any("PyGObject unavailable" in msg for msg in logs)


# ── protocol ordering and hygiene ────────────────────────────────────────────

def test_subscribes_before_creating_the_session(monkeypatch):
    """The portal may answer before CreateSession returns; a subscription set
    up afterwards silently misses that Response."""
    bus = _install_fake_gi(monkeypatch, _FakeBus())
    _run(bus)
    kinds = [e[0] for e in bus.events]
    assert kinds.index("subscribe") < kinds.index("call")


def test_every_subscription_pins_its_sender(monkeypatch):
    """Unpinned subscriptions let any connection forge a Response, an
    Activated that toggles recording, or a fake session Closed. Portal
    signals pin the portal; NameOwnerChanged pins the bus daemon that
    actually emits it."""
    bus = _install_fake_gi(monkeypatch, _FakeBus())
    _run(bus)
    pins = {(e[2], e[1]) for e in bus.events if e[0] == "subscribe"}
    assert pins == {
        ("Response", "org.freedesktop.portal.Desktop"),
        ("Activated", "org.freedesktop.portal.Desktop"),
        ("ShortcutsChanged", "org.freedesktop.portal.Desktop"),
        ("Closed", "org.freedesktop.portal.Desktop"),
        ("NameOwnerChanged", "org.freedesktop.DBus"),
    }


def test_unique_tokens_across_runs(monkeypatch):
    """The spec asks for unique, unguessable tokens; the old implementation
    derived them from the app id, identical on every run."""
    seen = set()

    class TokenBus(_FakeBus):
        def call_sync(self, *args, **kwargs):
            params = args[4]
            fmt, body = params
            if args[3] == "CreateSession":
                seen.add(_token_from_options(body[0]))
            return super().call_sync(*args, **kwargs)

    _run(_install_fake_gi(monkeypatch, TokenBus()))
    _run(_install_fake_gi(monkeypatch, TokenBus()))
    assert len(seen) == 2


def test_follows_a_mismatched_create_handle(monkeypatch):
    """Pre-0.9 portals mint their own request handle; the spec says follow it.
    Only an all-paths subscription that exists before the call survives the
    receive-time race this models."""
    bus = _install_fake_gi(monkeypatch, _FakeBus(mismatch=True))
    result, _events = _run(bus)
    assert result is True
    assert ("call", "BindShortcuts") in bus.events  # session was established


# ── session establishment failures ───────────────────────────────────────────

def test_session_denied_returns_false(monkeypatch):
    logs = []
    result, _events = _run(_install_fake_gi(monkeypatch, _FakeBus(create_code=1)), logs=logs)
    assert result is False
    assert any("CreateSession denied" in msg for msg in logs)


def test_no_create_response_returns_false(monkeypatch):
    """CreateSession acknowledged, Response never came — must fail, not hang."""
    logs = []
    result, _events = _run(_install_fake_gi(monkeypatch, _FakeBus(answers=False)), logs=logs)
    assert result is False
    assert any("no Response" in msg for msg in logs)


# ── binding: cancellation is not failure ─────────────────────────────────────

def test_cancelled_bind_keeps_listening(monkeypatch):
    """THE dialog-spam regression: False here would make the supervisor re-arm
    the listener every 10s and re-pop the compositor's confirmation dialog.
    And the session must stay LIVE — a binding made later in System Settings
    delivers Activated on it, so an activation after the cancel must still
    reach the event queue (Codex review)."""
    logs = []
    bus = _FakeBus(bind_code=1, activations=[(SESSION_HANDLE, "record-toggle")])
    result, events = _run(_install_fake_gi(monkeypatch, bus), logs=logs)
    assert result is True
    assert any("bind cancelled" in msg for msg in logs)
    assert events == [(EventType.HOTKEY_PRESSED, None)]


def test_unanswered_bind_keeps_listening(monkeypatch):
    """A bind Response that never arrives (dialog left open, portal wedged)
    must not kill the listener either — the wait is untimed and interruptible,
    and activations still route once the compositor delivers them."""
    logs = []
    bus = _FakeBus(bind_answers=False, activations=[(SESSION_HANDLE, "style-toggle")])
    result, events = _run(_install_fake_gi(monkeypatch, bus), logs=logs)
    assert result is True
    assert any("no Response — listening anyway" in msg for msg in logs)
    assert events == [(EventType.STYLE_TOGGLE, None)]


def test_bind_code_zero_alone_is_not_registration(monkeypatch):
    """The spec lets the portal bind any subset; code 0 with our ids missing
    must be reported, not logged as success."""
    logs = []
    bus = _install_fake_gi(monkeypatch, _FakeBus(bound_ids=["record-toggle"]))
    result, _events = _run(bus, logs=logs)
    assert result is True
    assert any("did not bind: style-toggle" in msg for msg in logs)
    assert not any("registered: style-toggle" in msg for msg in logs)


def test_full_bind_reports_both_shortcuts(monkeypatch):
    logs = []
    result, _events = _run(_install_fake_gi(monkeypatch, _FakeBus()), logs=logs)
    assert result is True
    assert any("registered: record-toggle" in msg for msg in logs)
    assert any("registered: style-toggle" in msg for msg in logs)


def test_both_shortcuts_are_requested_with_triggers(monkeypatch):
    bus = _install_fake_gi(monkeypatch, _FakeBus())
    _run(bus)
    ids = [item[0] for item in bus.bind_shortcut_args]
    assert ids == ["record-toggle", "style-toggle"]
    record_props = dict(bus.bind_shortcut_args)[
        "record-toggle"
    ]
    trigger = record_props["preferred_trigger"]
    trigger = trigger[1] if isinstance(trigger, tuple) else trigger
    assert trigger == "CTRL+ALT+space"


def test_empty_trigger_omits_preferred_trigger(monkeypatch):
    """A mouse-bound hotkey has no spec encoding; sending "" would ask the
    compositor to bind an empty chord instead of letting the user pick."""
    specs = [
        ShortcutSpec("record-toggle", "Toggle voice recording", "",
                     EventType.HOTKEY_PRESSED),
    ]
    bus = _install_fake_gi(monkeypatch, _FakeBus())
    _run(bus, specs=specs)
    props = dict(bus.bind_shortcut_args)["record-toggle"]
    assert "preferred_trigger" not in props
    assert "description" in props


# ── activation routing ───────────────────────────────────────────────────────

def test_record_activation_emits_hotkey_pressed(monkeypatch):
    bus = _install_fake_gi(
        monkeypatch, _FakeBus(activations=[(SESSION_HANDLE, "record-toggle")])
    )
    result, events = _run(bus)
    assert result is True
    assert events == [(EventType.HOTKEY_PRESSED, None)]


def test_style_activation_emits_style_toggle(monkeypatch):
    bus = _install_fake_gi(
        monkeypatch, _FakeBus(activations=[(SESSION_HANDLE, "style-toggle")])
    )
    _result, events = _run(bus)
    assert events == [(EventType.STYLE_TOGGLE, None)]


def test_foreign_session_activation_is_ignored(monkeypatch):
    """Another portal client's shortcut must not toggle recording here."""
    bus = _install_fake_gi(
        monkeypatch,
        _FakeBus(activations=[
            ("/org/freedesktop/portal/desktop/session/9_9/other", "record-toggle"),
        ]),
    )
    _result, events = _run(bus)
    assert events == []


def test_unknown_shortcut_id_is_ignored(monkeypatch):
    bus = _install_fake_gi(
        monkeypatch, _FakeBus(activations=[(SESSION_HANDLE, "self-destruct")])
    )
    _result, events = _run(bus)
    assert events == []


# ── session death: dead sessions must not be listened to forever ─────────────

def test_session_closed_returns_false_for_restart(monkeypatch):
    """Session.Closed is the spec's termination signal. The listener must exit
    False so the supervisor can build a fresh session — a dead session can
    never deliver another Activated, and 'still listening' would leave the
    hotkeys silently broken until app restart (Codex review)."""
    logs = []
    bus = _FakeBus(activations=[(SESSION_HANDLE, "record-toggle")], close_session=True)
    result, events = _run(_install_fake_gi(monkeypatch, bus), logs=logs)
    assert result is False
    assert events == [(EventType.HOTKEY_PRESSED, None)]  # pre-close events kept
    assert any("session closed" in msg for msg in logs)


def test_portal_exit_returns_false_for_restart(monkeypatch):
    """A crashed portal never emits Closed for the sessions it took down —
    NameOwnerChanged is the only tell."""
    logs = []
    bus = _FakeBus(portal_owner_change=(":1.5", ""))
    result, _events = _run(_install_fake_gi(monkeypatch, bus), logs=logs)
    assert result is False
    assert any("portal exited" in msg for msg in logs)


def test_portal_restart_returns_false_for_restart(monkeypatch):
    logs = []
    bus = _FakeBus(portal_owner_change=(":1.5", ":1.99"))
    result, _events = _run(_install_fake_gi(monkeypatch, bus), logs=logs)
    assert result is False
    assert any("portal restarted" in msg for msg in logs)


def test_portal_first_ownership_is_not_a_loss(monkeypatch):
    """old_owner == "" is the name being claimed, not lost — must keep
    listening and exit True on stop, not False."""
    bus = _FakeBus(portal_owner_change=("", ":1.5"))
    result, _events = _run(_install_fake_gi(monkeypatch, bus))
    assert result is True


def test_bus_connection_closed_returns_false_for_restart(monkeypatch):
    """A dead private connection delivers nothing — including Closed and
    NameOwnerChanged — so its own "closed" GObject signal must end the
    listener with False (Codex review: this handler had no direct test)."""

    class _ClosingConnBus(_FakeBus):
        def dispatch(self):
            if self._pending is not None:
                super().dispatch()
                return
            handler = getattr(self, "gobject_handlers", {}).get("closed")
            if handler is not None:
                self.gobject_handlers = {}
                handler(self, True, None)  # remote peer vanished

    logs = []
    result, _events = _run(_install_fake_gi(monkeypatch, _ClosingConnBus()), logs=logs)
    assert result is False
    assert any("bus connection closed" in msg for msg in logs)


# ── teardown ─────────────────────────────────────────────────────────────────

def test_unsubscribes_and_closes_on_exit(monkeypatch):
    bus = _install_fake_gi(monkeypatch, _FakeBus())
    _run(bus)
    kinds = [e[0] for e in bus.events]
    # Response, Activated, ShortcutsChanged, Session.Closed,
    # NameOwnerChanged — all released.
    assert kinds.count("unsubscribe") == 5
    assert kinds[-1] == "close"


def test_stop_during_setup_is_a_clean_exit(monkeypatch):
    """A stop_event set before the session phase completes is a clean exit,
    not a failure the supervisor should retry."""
    bus = _install_fake_gi(monkeypatch, _FakeBus())
    stop = Event()
    stop.set()
    result = portal.wayland_hotkey_listener(Queue(), _specs(), stop, None)
    assert result is True


# ── the real GLib machinery, where installed ─────────────────────────────────

def test_poll_ends_the_untimed_bind_wait(monkeypatch):
    """REAL GLib contexts, loops and timers; fake bus. The bind Response never
    arrives, so the untimed bind wait can only end via the 500ms poll — this
    proves the poll actually fires on the listener's private context and keeps
    the untimed wait interruptible (Codex review). The create Response is
    delivered through the real context, exercising genuine source dispatch.
    """
    import threading
    import time

    real_glib = pytest.importorskip("gi.repository.GLib")

    class _RealLoopBus(_FakeBus):
        context = None  # captured by the MainContext factory below

        def call_sync(self, *args, **kwargs):
            reply = super().call_sync(*args, **kwargs)
            method = args[3]
            if method == "BindShortcuts":
                self._pending = None  # the dialog never closes
            elif self._pending is not None:
                pending, self._pending = self._pending, None
                source = real_glib.timeout_source_new(10)

                def _deliver(*_a, _pending=pending):
                    path, code, results = _pending
                    for sender, _i, sig, spath, cb in list(self.subscriptions.values()):
                        if sig == "Response" and (spath is None or spath == path):
                            cb(None, sender, path, "org.freedesktop.portal.Request",
                               "Response", _Reply((code, results)))
                    return real_glib.SOURCE_REMOVE

                source.set_callback(_deliver)
                source.attach(_RealLoopBus.context)
            return reply

    bus = _RealLoopBus()

    class _ContextFactory:
        @staticmethod
        def new():
            context = real_glib.MainContext.new()
            _RealLoopBus.context = context
            return context

    class _GLibProxy:
        """Real GLib with only MainContext.new intercepted (to capture the
        listener's private context). PyGObject's MainLoop.run override lazily
        imports gi.repository.GLib for its signal-wakeup helper — that import
        resolves to THIS object via the patched sys.modules, so everything
        not overridden must fall through to the real module (IOChannel etc.)."""

        MainContext = _ContextFactory

        def __getattr__(self, name):
            return getattr(real_glib, name)

    glib = _GLibProxy()
    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SESSION=2),
        DBusConnectionFlags=types.SimpleNamespace(
            AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2
        ),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        dbus_address_get_for_bus_sync=lambda bus_type, cancellable: "unix:fake",
        DBusConnection=types.SimpleNamespace(new_for_address_sync=lambda *a, **k: bus),
    )
    repo = types.ModuleType("gi.repository")
    repo.Gio, repo.GLib = gio, glib
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)

    stop = Event()
    threading.Timer(0.8, stop.set).start()
    started = time.monotonic()
    result = portal.wayland_hotkey_listener(Queue(), _specs(), stop, None)
    elapsed = time.monotonic() - started

    assert result is True
    assert elapsed < 10, f"poll failed to end the untimed wait ({elapsed:.1f}s)"


def test_session_closed_ends_real_blocking_listen_loop(monkeypatch):
    """REAL GLib loops: the steady-state listen loop genuinely blocks, and only
    a Closed delivery may end it here (no stop_event). Proves the liveness fix
    end-to-end under real dispatch semantics — a fake loop that returns after
    one dispatch cannot (Codex review)."""
    import time

    real_glib = pytest.importorskip("gi.repository.GLib")

    class _ClosingBus(_FakeBus):
        context = None

        def _schedule(self, delay_ms, fn):
            source = real_glib.timeout_source_new(delay_ms)

            def _cb(*_a):
                fn()
                return real_glib.SOURCE_REMOVE

            source.set_callback(_cb)
            source.attach(_ClosingBus.context)

        def call_sync(self, *args, **kwargs):
            reply = super().call_sync(*args, **kwargs)
            if self._pending is not None:
                pending, self._pending = self._pending, None

                def deliver(_p=pending):
                    path, code, results = _p
                    for sender, _i, sig, spath, cb in list(self.subscriptions.values()):
                        if sig == "Response" and (spath is None or spath == path):
                            cb(None, sender, path, "org.freedesktop.portal.Request",
                               "Response", _Reply((code, results)))

                self._schedule(10, deliver)
            if args[3] == "BindShortcuts":
                def close_session():
                    for sender, _i, sig, _sp, cb in list(self.subscriptions.values()):
                        if sig == "Closed":
                            cb(None, sender, SESSION_HANDLE,
                               "org.freedesktop.portal.Session", "Closed", _Reply(({},)))

                self._schedule(300, close_session)
            return reply

    bus = _ClosingBus()

    class _ContextFactory:
        @staticmethod
        def new():
            context = real_glib.MainContext.new()
            _ClosingBus.context = context
            return context

    class _GLibProxy:
        MainContext = _ContextFactory

        def __getattr__(self, name):
            return getattr(real_glib, name)

    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SESSION=2),
        DBusConnectionFlags=types.SimpleNamespace(
            AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2
        ),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        dbus_address_get_for_bus_sync=lambda bus_type, cancellable: "unix:fake",
        DBusConnection=types.SimpleNamespace(new_for_address_sync=lambda *a, **k: bus),
    )
    repo = types.ModuleType("gi.repository")
    repo.Gio, repo.GLib = gio, _GLibProxy()
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)

    logs = []
    started = time.monotonic()
    result = portal.wayland_hotkey_listener(Queue(), _specs(), Event(), logs.append)
    elapsed = time.monotonic() - started

    assert result is False, "a closed session must exit False for a restart"
    assert elapsed < 10, f"listen loop failed to notice the closed session ({elapsed:.1f}s)"
    assert any("session closed" in msg for msg in logs)


def test_builds_variants_the_real_glib_accepts(monkeypatch):
    """Runs the listener with the REAL GLib variant machinery (fake bus/loop).

    The field bug this guards: a pre-wrapped Variant in an a{sv} slot makes
    PyGObject raise — a signature error no tuple-based fake can see. Skips
    where PyGObject is not installed (the uv test env); runs on the
    distrobox, the host, and the Flatpak SDK.
    """
    real_glib = pytest.importorskip("gi.repository.GLib")
    bus = _install_fake_gi(
        monkeypatch,
        _FakeBus(activations=[(SESSION_HANDLE, "record-toggle")]),
        real_glib=real_glib,
    )
    result, events = _run(bus)
    assert result is True, "listener failed with real GLib variants"
    assert events == [(EventType.HOTKEY_PRESSED, None)]

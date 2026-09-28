"""RemoteDesktop-portal typing: the session lifecycle against a fake bus, the
keysym mapping, and how the injector and Linux Gamer mode route through it.

The live path was verified on KWin 6.7.5 in an isolated nested session
(2026-09-28): Wayland and XWayland windows both received typed text, the
restore token skipped the dialog, Escape on the dialog read as declined, and
characters without a key in the layout were pasted instead.
"""
import threading
import time
import types

import pytest

from wayfinder.core import portal_keyboard as pk

SESSION = "/org/freedesktop/portal/desktop/session/1_42/wayfinder_s"


# ── keysyms ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ch,keysym", [
    ("a", 0x61), ("Z", 0x5A), (" ", 0x20), ("~", 0x7E), ("?", 0x3F),
    ("é", 0xE9), ("ñ", 0xF1),                      # Latin-1 keysym = code point
    ("—", 0x01002014), ("😀", 0x0101F600),        # Unicode keysym range
    ("\n", 0xFF0D), ("\t", 0xFF09),
])
def test_keysym_for_char(ch, keysym):
    assert pk.keysym_for_char(ch) == keysym


def test_keysym_for_name():
    assert pk.keysym_for_name("Return") == 0xFF0D
    assert pk.keysym_for_name("ctrl") == 0xFFE3
    assert pk.keysym_for_name("v") == 0x76
    with pytest.raises(pk.PortalKeyboardError):
        pk.keysym_for_name("hyperdrive")


def test_untypeable_chars_asks_the_layout_only_for_non_ascii():
    asked = []

    def has_keysym(ks):
        asked.append(ks)
        return ks == 0xE9  # this layout has é, nothing else

    missing = pk.untypeable_chars("Hello café naïve 😀\n", has_keysym)
    assert missing == {"ï", "😀"}
    assert all(ks > 0x7E for ks in asked)


def test_ascii_fold_keeps_the_letters():
    assert pk.ascii_fold("café naïve") == "cafe naive"


def test_state_round_trip_is_private(tmp_path):
    path = tmp_path / "pk.json"
    pk.save_state({"restore_token": "abc", "declined": False}, path)
    pk.save_state({"declined": True}, path)
    assert pk.load_state(path) == {"restore_token": "abc", "declined": True}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert pk.load_state(tmp_path / "missing.json") == {}


# ── the session against a fake bus ───────────────────────────────────────────

class _Reply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeBus:
    """Speaks the RemoteDesktop request/response lifecycle."""

    def __init__(self, start_code=0, devices=1, token="tok-new", fail_after=None,
                 close_after_ready=False):
        self.start_code = start_code
        self.devices = devices
        self.token = token
        self.fail_after = fail_after          # NotifyKeyboardKeysym calls that succeed
        self.close_after_ready = close_after_ready
        self.subs = {}
        self.calls = []
        self.keys = []
        self._pending = []
        self._lock = threading.Lock()
        self._n = 0

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, sender, iface, signal, path, arg0, flags, cb):
        self._n += 1
        self.subs[self._n] = (sender, iface, signal, path, cb)
        return self._n

    def signal_unsubscribe(self, sid):
        self.subs.pop(sid, None)

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancel):
        fmt, body = params if params is not None else (None, None)
        self.calls.append((iface, method, fmt, body))
        if method == "NotifyKeyboardKeysym":
            assert fmt == "(oa{sv}iu)" and body[0] == SESSION
            if self.fail_after is not None and len(self.keys) >= self.fail_after:
                raise RuntimeError("session gone")
            self.keys.append((body[2], body[3]))
            return None
        if method == "Close":
            return None
        options = body[-1]
        token = options["handle_token"][1]
        request = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
        if method == "CreateSession":
            assert fmt == "(a{sv})"
            self._queue(request, 0, {"session_handle": SESSION})
        elif method == "SelectDevices":
            assert fmt == "(oa{sv})" and body[0] == SESSION
            self._queue(request, 0, {})
        elif method == "Start":
            assert fmt == "(osa{sv})" and body[0] == SESSION
            results = {"devices": self.devices, "restore_token": self.token}
            self._queue(request, self.start_code, results)
        return _Reply((request,))

    def _queue(self, path, code, results):
        with self._lock:
            self._pending.append(("Response", path, (code, results)))

    def emit(self, signal, path, args):
        for sender, _iface, sig, spath, cb in list(self.subs.values()):
            if sig == signal and (spath is None or spath == path):
                cb(None, sender, path, None, signal, _Reply(args))

    def dispatch(self):
        with self._lock:
            pending, self._pending = self._pending, []
        for signal, path, args in pending:
            self.emit(signal, path, args)
        if not pending:
            time.sleep(0.002)

    def selected_options(self):
        return next(body[1] for _i, m, _f, body in self.calls if m == "SelectDevices")


class _Loop:
    def __init__(self, bus):
        self.bus = bus

    def run(self):
        self.bus.dispatch()

    def quit(self):
        pass


class _Source:
    def set_callback(self, cb):
        pass

    def attach(self, ctx):
        pass

    def destroy(self):
        pass


def _fake_gi(bus):
    glib = types.SimpleNamespace(
        MainContext=types.SimpleNamespace(new=lambda: types.SimpleNamespace(
            push_thread_default=lambda: None, pop_thread_default=lambda: None)),
        MainLoop=types.SimpleNamespace(new=lambda ctx, running: _Loop(bus)),
        Variant=lambda fmt, value: (fmt, value),
        VariantType=lambda s: s,
        timeout_source_new=lambda ms: _Source(),
        timeout_source_new_seconds=lambda s: _Source(),
        SOURCE_REMOVE=False, SOURCE_CONTINUE=True,
    )
    gio = types.SimpleNamespace(
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
    )
    return gio, glib


def _session(monkeypatch, tmp_path, bus, state=None):
    monkeypatch.setattr(pk, "_gi", lambda: _fake_gi(bus))
    path = tmp_path / "pk.json"
    if state is not None:
        pk.save_state(state, path)
    return pk.PortalKeyboard(state_path=path, bus_factory=lambda: bus), path


def _settle(kb, *states, timeout=3.0):
    deadline = time.monotonic() + timeout
    while kb.state not in states and time.monotonic() < deadline:
        time.sleep(0.005)
    return kb.state


def test_approved_session_types_every_character(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, path = _session(monkeypatch, tmp_path, bus)
    try:
        assert kb.start()
        assert _settle(kb, kb.READY) == kb.READY and kb.ready()
        options = bus.selected_options()
        assert options["types"] == ("u", pk.DEVICE_KEYBOARD)
        assert options["persist_mode"] == ("u", pk.PERSIST_UNTIL_REVOKED)
        assert "restore_token" not in options          # first run: the dialog
        assert pk.load_state(path) == {"declined": False, "restore_token": "tok-new"}

        kb.type_text("Hi!\n")
        assert bus.keys == [(0x48, 1), (0x48, 0), (0x69, 1), (0x69, 0),
                            (0x21, 1), (0x21, 0), (0xFF0D, 1), (0xFF0D, 0)]
    finally:
        kb.close()
    assert _settle(kb, kb.IDLE) == kb.IDLE and not kb.ready()


def test_combo_presses_modifiers_first_and_releases_them_last(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    try:
        kb.start()
        _settle(kb, kb.READY)
        kb.press_keys("ctrl+v")
        assert bus.keys == [(0xFFE3, 1), (0x76, 1), (0x76, 0), (0xFFE3, 0)]
    finally:
        kb.close()


def test_restore_token_is_sent_and_replaced(monkeypatch, tmp_path):
    bus = _FakeBus(token="tok-2")
    kb, path = _session(monkeypatch, tmp_path, bus, {"restore_token": "tok-1"})
    try:
        kb.start()
        assert _settle(kb, kb.READY) == kb.READY
        assert bus.selected_options()["restore_token"] == ("s", "tok-1")
        assert pk.load_state(path)["restore_token"] == "tok-2"   # tokens are single-use
    finally:
        kb.close()


def test_declined_is_remembered_and_not_asked_again(monkeypatch, tmp_path):
    bus = _FakeBus(start_code=1)
    kb, path = _session(monkeypatch, tmp_path, bus)
    kb.start()
    assert _settle(kb, kb.DECLINED) == kb.DECLINED and not kb.ready()
    assert pk.load_state(path)["declined"] is True

    calls = len(bus.calls)
    assert kb.start() is False                 # next launch: no dialog
    assert kb.state == kb.DECLINED and len(bus.calls) == calls

    bus.start_code = 0                         # Settings switch: ask again
    try:
        assert kb.start(ask_again=True)
        assert _settle(kb, kb.READY) == kb.READY
        assert pk.load_state(path)["declined"] is False
    finally:
        kb.close()


def test_a_grant_without_keyboard_fails(monkeypatch, tmp_path):
    bus = _FakeBus(devices=2)                  # pointer only
    kb, _path = _session(monkeypatch, tmp_path, bus)
    kb.start()
    assert _settle(kb, kb.FAILED) == kb.FAILED and not kb.ready()


def test_session_closed_by_the_desktop(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    kb.start()
    _settle(kb, kb.READY)
    bus.emit("Closed", SESSION, ({},))
    assert _settle(kb, kb.CLOSED) == kb.CLOSED and not kb.ready()
    with pytest.raises(pk.PortalKeyboardError):
        kb.type_text("x")


def test_partial_typing_reports_what_landed(monkeypatch, tmp_path):
    bus = _FakeBus(fail_after=4)               # two characters (press+release each)
    kb, _path = _session(monkeypatch, tmp_path, bus)
    try:
        kb.start()
        _settle(kb, kb.READY)
        with pytest.raises(pk.PortalKeyboardError) as err:
            kb.type_text("abcd")
        assert err.value.typed == 2
    finally:
        kb.close()


def test_state_changes_reach_listeners(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    seen = []
    kb.on_change(lambda state, detail: seen.append(state))
    try:
        kb.start()
        _settle(kb, kb.READY)
    finally:
        kb.close()
    _settle(kb, kb.IDLE)
    assert seen[:3] == [kb.STARTING, kb.WAITING, kb.READY] and seen[-1] == kb.IDLE


# ── when to use it ───────────────────────────────────────────────────────────

def test_wayland_desktop_detection(monkeypatch):
    import wayfinder.core.injector as injector
    import wayfinder.utils.platform as platform

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(platform, "is_game_mode", lambda: False)
    monkeypatch.setattr(injector, "_running_under_xwayland", lambda: True)
    monkeypatch.setattr(pk, "gamescope_x_server", lambda: False)
    assert pk.host_is_wayland_desktop()

    monkeypatch.setattr(pk, "gamescope_x_server", lambda: True)   # gamescope is XWayland too
    assert not pk.host_is_wayland_desktop()

    monkeypatch.setattr(pk, "gamescope_x_server", lambda: False)
    monkeypatch.setattr(platform, "is_game_mode", lambda: True)
    assert not pk.host_is_wayland_desktop()

    monkeypatch.setattr(platform, "is_game_mode", lambda: False)
    monkeypatch.setattr(injector, "_running_under_xwayland", lambda: False)
    assert not pk.host_is_wayland_desktop()                        # X11 session


# ── the injector routes through it ───────────────────────────────────────────

class _Keyboard:
    def __init__(self, fail_at=None):
        self.typed = []
        self.pressed = []
        self.fail_at = fail_at

    def type_text(self, text, delay):
        if self.fail_at is not None:
            err = pk.PortalKeyboardError("gone")
            err.typed = self.fail_at
            raise err
        self.typed.append((text, delay))

    def press_keys(self, combo, hold_s=0.0):
        self.pressed.append(combo)


@pytest.fixture
def portal_ready(monkeypatch):
    import wayfinder.core.injector as injector

    kb = _Keyboard()
    monkeypatch.setattr(pk, "ready", lambda: True)
    monkeypatch.setattr(pk, "keyboard", lambda: kb)
    monkeypatch.setattr(injector, "_require_modifier_release", lambda: None)
    monkeypatch.setattr(injector, "_X11Keymap", lambda: _Layout(set()))
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE", None)
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_READ", None)
    monkeypatch.setattr("sys.platform", "linux")
    return kb


class _Layout:
    """has_keysym for a layout that has exactly ``keysyms`` beyond ASCII."""

    def __init__(self, keysyms):
        self.keysyms = keysyms

    def __call__(self, keysym):
        return keysym in self.keysyms

    def close(self):
        pass


def test_portal_is_the_injector_when_ready(portal_ready):
    from wayfinder.utils.platform import get_text_injector
    assert get_text_injector() == "portal"


def test_type_path_uses_the_portal(portal_ready):
    import wayfinder.core.injector as injector
    injector._inject_text_type_linux("Hello “world”", "normal")
    assert portal_ready.typed == [('Hello "world"', 12)]   # typography folded first


def test_characters_without_a_key_are_pasted(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    written = []
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE", lambda t: written.append(t) or True)
    monkeypatch.setattr(injector, "_PORTAL_PASTE_SETTLE_S", 0)
    injector._inject_text_type_linux("café 😀")
    assert written == ["café 😀"] and portal_ready.pressed == ["ctrl+v"]
    assert portal_ready.typed == []


def test_without_a_clipboard_accents_are_folded(portal_ready):
    import wayfinder.core.injector as injector
    injector._inject_text_type_linux("café")
    assert portal_ready.typed == [("cafe", 2)]


def test_failure_before_any_key_falls_back_to_xdotool(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    portal_ready.fail_at = 0
    used = []
    monkeypatch.setattr(injector, "_portal_fallback_tool", lambda: "xdotool")
    monkeypatch.setattr(injector, "_inject_text_xdotool",
                        lambda text, speed, target: used.append(text))
    injector._inject_text_type_linux("hello")
    assert used == ["hello"]


def test_partial_failure_is_never_retyped(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    portal_ready.fail_at = 3
    monkeypatch.setattr(injector, "_inject_text_xdotool",
                        lambda *a: pytest.fail("would type the text twice"))
    with pytest.raises(injector.InjectionError) as err:
        injector.inject_text("hello", game_mode=True)
    assert err.value.uncertain_delivery


def test_enter_goes_through_the_portal(portal_ready):
    import wayfinder.core.injector as injector
    injector.press_enter()
    assert portal_ready.pressed == ["Return"]


# ── Linux Gamer mode ─────────────────────────────────────────────────────────

def test_game_keys_use_the_portal_when_ready(portal_ready, monkeypatch):
    from wayfinder.core import linux_game_chat as gc
    monkeypatch.setattr(gc, "_xdotool", lambda *a: pytest.fail("xdotool used"))
    gc.press_return()
    gc.type_text("inc left")
    assert portal_ready.pressed == ["Return"]
    assert portal_ready.typed == [("inc left", gc.TYPE_DELAY_MS)]


def test_game_keys_use_xdotool_without_the_portal(monkeypatch):
    import wayfinder.core.injector as injector
    from wayfinder.core import linux_game_chat as gc
    monkeypatch.setattr(pk, "ready", lambda: False)
    monkeypatch.setattr(injector, "_require_modifier_release", lambda: None)
    sent = []
    monkeypatch.setattr(gc, "_xdotool", lambda *a: sent.append(a))
    gc.type_text("inc")
    assert sent == [("type", "--clearmodifiers", "--delay", str(gc.TYPE_DELAY_MS), "--", "inc")]


class _Win:
    def __init__(self, wid, parent=None):
        self.id = wid
        self._parent = parent
        self.focused = False

    def query_tree(self):
        return types.SimpleNamespace(parent=self._parent)

    def set_input_focus(self, revert, when):
        self.focused = True


class _Display:
    def __init__(self, root, focus, windows):
        self._root, self._focus, self._windows = root, focus, windows
        self.synced = False

    def screen(self):
        return types.SimpleNamespace(root=self._root)

    def get_input_focus(self):
        return types.SimpleNamespace(focus=self._focus)

    def create_resource_object(self, kind, wid):
        return self._windows[wid]

    def sync(self):
        self.synced = True

    def close(self):
        pass


def _xlib(monkeypatch, disp):
    import sys
    X = types.SimpleNamespace(NONE=0, PointerRoot=1, RevertToParent=2, CurrentTime=0)
    xlib = types.ModuleType("Xlib")
    xlib.X = X
    xlib.display = types.SimpleNamespace(Display=lambda: disp)
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setattr("sys.platform", "linux")


def test_focus_left_on_the_wine_launcher_is_given_back(monkeypatch):
    from wayfinder.core import linux_game_chat as gc
    root = _Win(1)
    game, launcher = _Win(0x6200001, root), _Win(0x580000E, root)
    disp = _Display(root, launcher, {0x6200001: game})
    _xlib(monkeypatch, disp)
    assert gc.ensure_game_focus(0x6200001) is True
    assert game.focused and disp.synced


def test_focus_inside_the_game_window_is_left_alone(monkeypatch):
    from wayfinder.core import linux_game_chat as gc
    root = _Win(1)
    game = _Win(0x6200001, root)
    child = _Win(0x6200005, game)
    disp = _Display(root, child, {0x6200001: game})
    _xlib(monkeypatch, disp)
    assert gc.ensure_game_focus(0x6200001) is False
    assert not game.focused

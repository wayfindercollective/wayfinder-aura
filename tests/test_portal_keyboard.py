"""RemoteDesktop-portal typing: the session lifecycle against a fake bus, the
keysym mapping, and how the injector and Linux Gamer mode route through it.

The live path was verified on KWin 6.7.5 in an isolated nested session
(2026-09-28): Wayland and XWayland windows both received typed text, the
restore token skipped the dialog, Escape on the dialog read as declined, and
characters without a key in the layout were pasted instead.
"""
import os
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


class _Disp:
    """keysym_to_keycodes for a US layout: (X keycode, index) pairs."""
    US = {0x61: 38, 0x62: 56, 0x68: 43, 0x69: 31, 0x31: 10, 0x3B: 47, 0x76: 55,
          0x20: 65, 0xFF0D: 36, 0xFFE1: 50, 0xFFE3: 37}
    SHIFTED = {0x41: 38, 0x48: 43, 0x21: 10, 0x3A: 47, 0x28: 18}
    EXOTIC = {0x28: 187}   # "(" also sits unshifted on KEY_KPLEFTPAREN

    def keysym_to_keycodes(self, keysym):
        if keysym in self.EXOTIC:
            yield self.EXOTIC[keysym], 0
        if keysym in self.US:
            yield self.US[keysym], 0
        if keysym in self.SHIFTED:
            yield self.SHIFTED[keysym], 1
        if keysym == 0xE9:
            yield 26, 4          # é on an AltGr level only: not typeable

    def close(self):
        pass


def test_layout_gives_evdev_codes_and_the_shift_level():
    layout = pk.X11Layout(display_factory=_Disp)
    assert layout.available
    assert layout.key_for(0x61) == (30, False)       # a  (X 38 - 8)
    assert layout.key_for(0x41) == (30, True)        # A: same key, Shift
    assert layout.key_for(0x21) == (2, True)         # ! is Shift+1
    assert layout.key_for(0xE9) is None              # AltGr level: pasted instead
    assert layout.key_for(0x28) == (10, True)        # Shift+9, not the keypad "(" Wine drops
    assert pk.untypeable_chars("Hi! é", layout) == {"é"}


def test_no_x_server_means_no_layout():
    def boom():
        raise OSError("no display")
    layout = pk.X11Layout(display_factory=boom)
    assert not layout.available and layout.key_for(0x61) is None


def test_ascii_fold_keeps_the_letters():
    assert pk.ascii_fold("café naïve") == "cafe naive"


def test_state_round_trip_is_private(tmp_path):
    path = tmp_path / "pk.json"
    pk.save_state({"restore_token": "abc", "declined": False}, path)
    pk.save_state({"declined": True}, path)
    assert pk.load_state(path) == {"restore_token": "abc", "declined": True}
    if os.name == "posix":  # Windows has no owner-only mode bits
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert pk.load_state(tmp_path / "missing.json") == {}


def test_a_hand_edited_token_that_is_not_text_is_ignored(tmp_path):
    path = tmp_path / "pk.json"
    path.write_text('{"restore_token": 123, "declined": false}')
    assert pk.load_state(path) == {"restore_token": "", "declined": False}


def test_state_is_written_by_the_owner_only_writer(tmp_path, monkeypatch):
    # The restore token lets Aura type without asking: the file must never
    # exist with group/other bits, which the shared writer guarantees (mkstemp).
    from wayfinder.utils import fs_security
    calls = []
    monkeypatch.setattr(fs_security, "atomic_write_json",
                        lambda path, data, mode=0o600: calls.append((path, data, mode)))
    pk.save_state({"restore_token": "abc"}, tmp_path / "pk.json")
    assert calls == [(tmp_path / "pk.json", {"restore_token": "abc"}, 0o600)]


# ── the session against a fake bus ───────────────────────────────────────────

class _Reply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeBus:
    """Speaks the RemoteDesktop request/response lifecycle."""

    def __init__(self, start_code=0, devices=1, token="tok-new", fail_after=None,
                 close_after_ready=False, registry=True):
        self.start_code = start_code
        self.registry = registry  # the portal has org.freedesktop.host.portal.Registry
        self.devices = devices
        self.token = token
        self.fail_after = fail_after          # NotifyKeyboardKeysym calls that succeed
        self.close_after_ready = close_after_ready
        self.subs = {}
        self.calls = []
        self.keys = []
        self.codes = []
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
        self.timeouts = getattr(self, "timeouts", {})
        self.timeouts[method] = timeout
        if method in ("NotifyKeyboardKeysym", "NotifyKeyboardKeycode"):
            assert fmt == "(oa{sv}iu)" and body[0] == SESSION
            if self.fail_after is not None and len(self.keys) + len(self.codes) >= self.fail_after:
                raise RuntimeError("session gone")
            if method == "NotifyKeyboardKeycode":
                self.codes.append((body[2], body[3]))
            else:
                self.keys.append((body[2], body[3]))
            return None
        if method == "Close":
            return None
        if method == "Register":
            assert fmt == "(sa{sv})"
            if not self.registry:
                raise RuntimeError("org.freedesktop.DBus.Error.UnknownInterface")
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


def test_layout_typing_sends_keys_with_shift_around_shifted_runs(monkeypatch, tmp_path):
    """KWin 6.4 drops Shift for keysyms typed into XWayland windows; keys
    with an explicit Shift type the same everywhere."""
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    try:
        kb.start()
        _settle(kb, kb.READY)
        kb.type_text("HA! a\n", 0, pk.X11Layout(display_factory=_Disp))
        shift = 42
        assert bus.codes == [
            (shift, 1), (35, 1), (35, 0), (30, 1), (30, 0), (2, 1), (2, 0),   # "HA!" under Shift
            (shift, 0), (57, 1), (57, 0), (30, 1), (30, 0), (28, 1), (28, 0),  # " a\n"
        ]
        assert bus.keys == []
    finally:
        kb.close()


def test_layout_typing_reports_what_landed_when_a_key_fails(monkeypatch, tmp_path):
    bus = _FakeBus(fail_after=3)          # Shift down, H down, H up, then gone
    kb, _path = _session(monkeypatch, tmp_path, bus)
    try:
        kb.start()
        _settle(kb, kb.READY)
        with pytest.raises(pk.PortalKeyboardError) as err:
            kb.type_text("HA", 0, pk.X11Layout(display_factory=_Disp))
        assert err.value.typed == 1
    finally:
        kb.close()


def test_combo_presses_modifiers_first_and_releases_them_last(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    try:
        kb.start()
        _settle(kb, kb.READY)
        kb.press_keys("ctrl+v")
        assert bus.keys == [(0xFFE3, 1), (0x76, 1), (0x76, 0), (0xFFE3, 0)]
        kb.press_keys("ctrl+v", 0, pk.X11Layout(display_factory=_Disp))
        assert bus.codes == [(29, 1), (47, 1), (47, 0), (29, 0)]
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


def test_the_appimage_registers_its_app_id_before_any_other_portal_call(monkeypatch, tmp_path):
    # Without an app ID the desktop cannot name Aura or keep its approval.
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    monkeypatch.setattr(pk, "host_app_id", lambda: "io.wayfindercollective.WayfinderAura")
    try:
        assert kb.start()
        assert _settle(kb, kb.READY) == kb.READY
        iface, method, _fmt, body = bus.calls[0]
        assert (iface, method) == (pk._REGISTRY_IFACE, "Register")
        assert body == ("io.wayfindercollective.WayfinderAura", {})
        assert bus.calls[1][1] == "CreateSession"
        # A portal still starting at login answers slowly: the setup timeout.
        assert bus.timeouts["Register"] == pk._SETUP_TIMEOUT_S * 1000
    finally:
        kb.close()


def test_a_portal_without_the_registry_still_types(monkeypatch, tmp_path):
    bus = _FakeBus(registry=False)  # xdg-desktop-portal before 1.19
    kb, _path = _session(monkeypatch, tmp_path, bus)
    monkeypatch.setattr(pk, "host_app_id", lambda: "io.wayfindercollective.WayfinderAura")
    try:
        assert kb.start()
        assert _settle(kb, kb.READY, kb.FAILED) == kb.READY
    finally:
        kb.close()


def test_flatpak_and_source_runs_do_not_register(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    monkeypatch.setattr(pk, "host_app_id", lambda: "")
    try:
        assert kb.start()
        assert _settle(kb, kb.READY) == kb.READY
        assert "Register" not in [method for _i, method, _f, _b in bus.calls]
    finally:
        kb.close()


def test_host_app_id_is_the_appimage_menu_entry(monkeypatch, tmp_path):
    from wayfinder.utils import platform as plat

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(plat, "is_flatpak", lambda: False)
    monkeypatch.setattr(plat, "is_appimage", lambda: True)
    assert pk.host_app_id() == ""  # no menu entry under that ID yet
    entry = tmp_path / "applications" / "io.wayfindercollective.WayfinderAura.desktop"
    entry.parent.mkdir()
    entry.write_text("[Desktop Entry]\n")
    assert pk.host_app_id() == "io.wayfindercollective.WayfinderAura"
    monkeypatch.setattr(plat, "is_flatpak", lambda: True)  # the sandbox names it
    assert pk.host_app_id() == ""
    monkeypatch.setattr(plat, "is_flatpak", lambda: False)
    monkeypatch.setattr(plat, "is_appimage", lambda: False)  # a source run
    assert pk.host_app_id() == ""


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
        self.press_fails = False
        self.failed = None

    def mark_failed(self, detail):
        self.failed = detail

    def type_text(self, text, delay, layout=None):
        if self.fail_at is not None:
            err = pk.PortalKeyboardError("gone")
            err.typed = self.fail_at
            raise err
        self.typed.append((text, delay))

    def press_keys(self, combo, hold_s=0.0, layout=None):
        if self.press_fails:
            raise pk.PortalKeyboardError("gone")
        self.pressed.append(combo)


@pytest.fixture
def portal_ready(monkeypatch):
    import wayfinder.core.injector as injector

    kb = _Keyboard()
    monkeypatch.setattr(pk, "ready", lambda: True)
    monkeypatch.setattr(pk, "keyboard", lambda: kb)
    monkeypatch.setattr(injector, "_require_modifier_release", lambda: None)
    monkeypatch.setattr(pk, "X11Layout", lambda: _Layout(set()))
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE", None)
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_READ", None)
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_RESTORE", None)
    monkeypatch.setattr("sys.platform", "linux")
    return kb


class _Layout:
    """A layout with every ASCII key plus exactly ``extra`` keysyms."""

    available = True

    def __init__(self, extra):
        self.extra = extra

    def key_for(self, keysym):
        return (30, False) if keysym <= 0x7E or keysym in self.extra else None

    __call__ = key_for

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


def test_the_portal_paste_puts_the_old_clipboard_back(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    clipboard = {"text": "user's url"}
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_READ", lambda: clipboard["text"])
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE",
                        lambda t: clipboard.update(text=t) or True)
    monkeypatch.setattr(injector, "_PORTAL_PASTE_SETTLE_S", 0)
    monkeypatch.setattr(injector, "_PORTAL_RESTORE_AFTER_S", 0)
    injector._inject_text_type_linux("café")
    assert portal_ready.pressed == ["ctrl+v"] and clipboard["text"] == "user's url"


def test_the_portal_paste_never_overwrites_a_newer_copy(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    clipboard = {"text": "user's url"}
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_READ", lambda: clipboard["text"])
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE",
                        lambda t: clipboard.update(text=t) or True)
    monkeypatch.setattr(injector, "_PORTAL_PASTE_SETTLE_S", 0)
    # The user copies something else while Aura waits to restore.
    monkeypatch.setattr(injector.time, "sleep",
                        lambda s: clipboard.update(text="copied meanwhile") if s else None)
    monkeypatch.setattr(injector, "_PORTAL_RESTORE_AFTER_S", 0.4)
    injector._inject_text_type_linux("café")
    assert clipboard["text"] == "copied meanwhile"


def test_without_a_clipboard_accents_are_folded(portal_ready):
    import wayfinder.core.injector as injector
    injector._inject_text_type_linux("café")
    assert portal_ready.typed == [("cafe", 2)]


def _selection_without_a_failed_portal(monkeypatch, kb, fallback="xdotool"):
    from wayfinder.utils import platform as plat
    monkeypatch.setattr(plat, "get_text_injector",
                        lambda: fallback if kb.failed else "portal")


def test_failure_before_any_key_marks_the_portal_failed_and_falls_back(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    portal_ready.fail_at = 0
    _selection_without_a_failed_portal(monkeypatch, portal_ready)
    used = []
    monkeypatch.setattr(injector, "_inject_text_xdotool",
                        lambda text, speed, target: used.append(text))
    injector._inject_text_type_linux("hello")
    assert used == ["hello"]
    assert "did not reach the desktop" in portal_ready.failed   # the app logs it


def _no_real_tools(monkeypatch):
    """Record subprocess calls instead of running them (ydotoold may be live)."""
    import wayfinder.core.injector as injector
    from types import SimpleNamespace
    runs = []
    monkeypatch.setattr(injector.subprocess, "run",
                        lambda argv, **k: runs.append(list(argv)) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    return runs


def test_after_a_portal_failure_the_normal_selection_decides(portal_ready, monkeypatch):
    # A non-Flatpak Wayland install with ydotool but no xdotool must not
    # hard-fail with "no tool" once the portal stops taking keys.
    import wayfinder.core.injector as injector
    runs = _no_real_tools(monkeypatch)
    portal_ready.fail_at = 0
    _selection_without_a_failed_portal(monkeypatch, portal_ready, fallback="ydotool")
    monkeypatch.setattr(injector, "_portal_fallback_tool", lambda: "none")
    monkeypatch.setattr(injector, "check_ydotool_ready", lambda: (True, ""))
    monkeypatch.setattr(injector, "_get_ydotool_binary", lambda: "/fake/ydotool")
    monkeypatch.setattr(injector, "_get_ydotool_env", lambda: {})
    injector._inject_text_type_linux("hello")
    assert runs and runs[-1][0] == "/fake/ydotool" and runs[-1][-1] == "hello"


def test_a_clipboard_error_does_not_disable_the_portal(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE", lambda text: False)
    monkeypatch.setattr(injector, "_portal_fallback_tool", lambda: "xdotool")
    used = []
    monkeypatch.setattr(injector, "_inject_text_xdotool",
                        lambda text, speed, target: used.append(text))
    injector._inject_text_type_linux("café")
    assert used == ["café"] and portal_ready.failed is None


def test_enter_after_a_portal_failure_marks_it_failed(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    runs = _no_real_tools(monkeypatch)
    portal_ready.press_fails = True
    _selection_without_a_failed_portal(monkeypatch, portal_ready, fallback="none")
    monkeypatch.setattr(injector, "_get_ydotool_binary", lambda: None)
    with pytest.raises(injector.InjectionError, match="ydotool"):
        injector.press_enter()
    assert portal_ready.failed and runs == []


def test_mark_failed_only_leaves_a_ready_session(tmp_path):
    kb = pk.PortalKeyboard(state_path=tmp_path / "state.json")
    seen = []
    kb.on_change(lambda state, detail: seen.append(state))
    kb._state = kb.DECLINED
    kb.mark_failed("x")
    assert kb.state == kb.DECLINED and seen == []
    kb._state = kb.READY
    kb.mark_failed("keys did not reach the desktop")
    assert kb.state == kb.FAILED and seen == [kb.FAILED]


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
    monkeypatch.setattr(injector, "_running_under_xwayland", lambda: False)
    sent = []
    monkeypatch.setattr(gc, "_xdotool", lambda *a, **k: sent.append(a))
    gc.type_text("inc")
    assert sent == [tuple(injector.build_xdotool_type_command("inc", gc.TYPE_DELAY_MS)[1:])]


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


# ── the app wiring ───────────────────────────────────────────────────────────

class _TkLike:
    def __init__(self, clipboard=None, error=None):
        self._clipboard, self._error = clipboard, error

    def after(self, _ms, fn):
        fn()

    def clipboard_get(self):
        if self._error:
            raise self._error
        return self._clipboard

    def clipboard_clear(self):
        self._clipboard = ""

    def clipboard_append(self, text):
        self._clipboard += text

    def update_idletasks(self):
        pass


def test_app_clipboard_reader_runs_on_the_tk_side():
    import wayfinder_main as wm
    read = wm.WayfinderApp._get_clipboard_from_worker
    assert read(_TkLike("https://example.org")) == "https://example.org"
    assert read(_TkLike(error=RuntimeError("CLIPBOARD selection doesn't exist"))) is None


def _start_portal(monkeypatch, offered):
    """Run _start_portal_keyboard with the start thread inline and no waits."""
    from types import SimpleNamespace
    import wayfinder_main as wm
    import wayfinder.core.injector as injector

    hooks, starts, logs = [], [], []
    kb = SimpleNamespace(on_change=lambda cb: None,
                         start=lambda **k: starts.append(k))
    answers = iter(offered)
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    monkeypatch.setattr(wm, "IS_MACOS", False)
    monkeypatch.setattr(wm, "IS_WINDOWS", False)
    monkeypatch.setattr(pk, "host_is_wayland_desktop", lambda: True)
    monkeypatch.setattr(pk, "keyboard", lambda: kb)
    monkeypatch.setattr(pk, "portal_keyboard_offered", lambda: next(answers))
    monkeypatch.setattr(injector, "set_portal_clipboard_hooks",
                        lambda write, read=None, restore=None: hooks.append((write, read, restore)))
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    class _Inline:
        def __init__(self, target, **_k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(wm.threading, "Thread", _Inline)
    from queue import Queue
    app = SimpleNamespace(config={"linux_portal_typing": True}, log=logs.append,
                          wm_frame=lambda: "0x1", _portal_listener_added=True,
                          _set_clipboard_from_worker=lambda t: True,
                          _get_clipboard_from_worker=lambda: "old",
                          _restore_clipboard_from_worker=lambda pasted, previous: True,
                          event_queue=Queue(), app_state=wm.AppState.IDLE,
                          after=lambda ms, fn: None)
    app._start_portal_session_when_idle = (
        lambda *a: wm.WayfinderApp._start_portal_session_when_idle(app, *a))
    wm.WayfinderApp._start_portal_keyboard(app)
    while not app.event_queue.empty():          # the app's Tk-thread drain
        app.event_queue.get_nowait()[1]()
    return hooks, starts, logs


def test_app_wires_both_clipboard_hooks_so_the_paste_restores(monkeypatch):
    hooks, starts, _ = _start_portal(monkeypatch, [True])
    assert len(hooks) == 1 and hooks[0][1] is not None and hooks[0][2] is not None
    assert len(starts) == 1


def test_a_portal_not_up_yet_at_login_is_asked_again(monkeypatch):
    _, starts, logs = _start_portal(monkeypatch, [False, False, True])
    assert len(starts) == 1 and not logs


def test_a_desktop_without_the_portal_settles_for_xdotool(monkeypatch):
    _, starts, logs = _start_portal(monkeypatch, [False] * 4)
    assert starts == [] and "no remote-control keyboard" in logs[0]


# ── Setup and the startup primer leave typing to the portal ─────────────────

def _setup_on_a_portal_desktop(monkeypatch, state=pk.PortalKeyboard.WAITING, offered=True,
                               typing_switch=True):
    """Setup on a faked portal desktop. ``typing_switch`` stubs the saved
    "Type into every app" switch (None: read the real CONFIG_FILE the test set)."""
    from types import SimpleNamespace
    from wayfinder.core import setup
    monkeypatch.setattr(setup, "_PORTAL_OFFERED", {})
    if typing_switch is not None:
        monkeypatch.setattr(setup, "_portal_typing_enabled", lambda: typing_switch)
    monkeypatch.setattr(pk, "host_is_wayland_desktop", lambda: True)
    monkeypatch.setattr(pk, "keyboard", lambda: SimpleNamespace(state=state))
    monkeypatch.setattr(pk, "ready", lambda: state == pk.PortalKeyboard.READY)
    monkeypatch.setattr(pk, "portal_keyboard_offered", lambda *a: offered)
    monkeypatch.setattr(setup, "IS_APPIMAGE", False)
    monkeypatch.setattr(setup, "IS_FLATPAK", False)
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    monkeypatch.setattr(setup.sys, "platform", "linux")
    return setup


def test_setup_installs_no_ydotool_while_the_portal_will_type(monkeypatch):
    setup = _setup_on_a_portal_desktop(monkeypatch)
    monkeypatch.setattr("wayfinder.utils.platform.get_text_injector", lambda: "wtype")
    assert "ydotool" not in setup.get_missing_system_packages()
    status = setup.check_text_injection()
    assert status.installed and "portal" in status.detail


def test_setup_offers_ydotool_after_a_declined_portal(monkeypatch):
    setup = _setup_on_a_portal_desktop(monkeypatch, state=pk.PortalKeyboard.DECLINED)
    assert "ydotool" in setup.get_missing_system_packages()


def test_setup_offers_ydotool_where_no_portal_keyboard_exists(monkeypatch):
    setup = _setup_on_a_portal_desktop(monkeypatch, offered=False)
    assert "ydotool" in setup.get_missing_system_packages()


def test_setup_offers_ydotool_when_typing_into_every_app_is_off(monkeypatch, tmp_path):
    import json
    import wayfinder.config as config_module
    setup = _setup_on_a_portal_desktop(monkeypatch, state=pk.PortalKeyboard.IDLE,
                                       typing_switch=None)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    (tmp_path / "config.json").write_text(json.dumps({"linux_portal_typing": False}))
    assert "ydotool" in setup.get_missing_system_packages()
    (tmp_path / "config.json").write_text(json.dumps({"linux_portal_typing": True}))
    assert "ydotool" not in setup.get_missing_system_packages()


def test_wtype_primer_waits_while_the_portal_is_in_play(monkeypatch):
    from types import SimpleNamespace
    import wayfinder_main as wm
    monkeypatch.setattr(wm, "_IS_LINUX", True)
    monkeypatch.setattr(wm, "IS_MACOS", False)
    monkeypatch.setattr(wm, "IS_WINDOWS", False)
    monkeypatch.setattr(wm, "_portal_desktop", lambda: True)
    app = SimpleNamespace(config={"linux_portal_typing": True})
    for state, in_play in ((pk.PortalKeyboard.WAITING, True), (pk.PortalKeyboard.READY, True),
                           (pk.PortalKeyboard.DECLINED, False), (pk.PortalKeyboard.FAILED, False)):
        monkeypatch.setattr(pk, "keyboard", lambda s=state: SimpleNamespace(state=s))
        assert wm.WayfinderApp._portal_typing_in_play(app) is in_play, state
    app._portal_not_offered = True
    monkeypatch.setattr(pk, "keyboard", lambda: SimpleNamespace(state=pk.PortalKeyboard.IDLE))
    assert wm.WayfinderApp._portal_typing_in_play(app) is False


def test_a_decline_is_shown_and_the_switch_turns_off(monkeypatch, tmp_path):
    """Esc on the desktop's dialog counts as a decline: the Dictate tab says so
    and the switch goes off, so turning it on asks again."""
    from queue import Queue
    from types import SimpleNamespace
    import wayfinder_main as wm

    saved, banners, logs = [], [], []
    monkeypatch.setattr(wm, "save_config", lambda cfg: saved.append(dict(cfg)))
    var = SimpleNamespace(value=True)
    var.set = lambda v: setattr(var, "value", v)
    app = SimpleNamespace(config={"linux_portal_typing": True}, event_queue=Queue(),
                          log=logs.append, _portal_typing_var=var,
                          _show_error_banner=banners.append)
    app._after_portal_declined = lambda: wm.WayfinderApp._after_portal_declined(app)
    wm.WayfinderApp._on_portal_keyboard_state(app, pk.PortalKeyboard.DECLINED, "declined")
    kind, callback = app.event_queue.get_nowait()
    assert kind == wm.EventType.UI_CALLBACK
    callback()                                   # runs on the Tk thread in the app
    assert app.config["linux_portal_typing"] is False and saved
    assert var.value is False
    assert "declined" in banners[0] and "Type into every app" in banners[0]


def _state_app(**extra):
    """A stand-in app for the portal state handler, retry plumbing included."""
    from queue import Queue
    from types import SimpleNamespace
    import wayfinder_main as wm

    W = wm.WayfinderApp
    app = SimpleNamespace(config={}, event_queue=Queue(), logs=[], banners=[], timers=[],
                          starts=[], _PORTAL_RETRY_DELAYS_S=W._PORTAL_RETRY_DELAYS_S,
                          _portal_failure_retryable=W._portal_failure_retryable)
    app.log = app.logs.append
    app._show_error_banner = app.banners.append
    app.after = lambda ms, fn: app.timers.append((ms, fn))
    app._start_portal_keyboard = lambda **k: app.starts.append(k)
    for name in ("_portal_session_lost", "_reset_portal_retries", "_schedule_portal_retry",
                 "_retry_portal_keyboard", "_after_portal_declined"):
        setattr(app, name, getattr(W, name).__get__(app))
    app.state = lambda state, detail: W._on_portal_keyboard_state(app, state, detail)
    for key, value in extra.items():
        setattr(app, key, value)
    return app


def _drain(app):
    while not app.event_queue.empty():
        app.event_queue.get_nowait()[1]()


def _failed_keyboard(monkeypatch):
    from types import SimpleNamespace

    kb = SimpleNamespace(state=pk.PortalKeyboard.FAILED, closes=0)
    kb.close = lambda: setattr(kb, "closes", kb.closes + 1)
    monkeypatch.setattr(pk, "keyboard", lambda: kb)
    return kb


def test_a_failed_or_closed_session_is_shown(monkeypatch):
    # Retries used up (FAILED) and a session the desktop ended (CLOSED).
    app = _state_app(_portal_retries=3)
    app.state(pk.PortalKeyboard.FAILED, "gone")
    app.state(pk.PortalKeyboard.CLOSED, "the desktop ended the session")
    _drain(app)
    assert len(app.banners) == 2 and all("Type into every app" in b for b in app.banners)
    assert app.timers == []


def test_a_session_that_fails_to_start_is_tried_again_before_the_banner(monkeypatch):
    _failed_keyboard(monkeypatch)
    app = _state_app()
    for attempt, delay in enumerate((5, 30, 120)):
        app.state(pk.PortalKeyboard.FAILED, "the desktop did not create a session")
        _drain(app)
        assert app.banners == [] and app.timers[attempt][0] == delay * 1000
        assert f"trying again in {delay} s" in app.logs[-1]
        app.timers[attempt][1]()                      # the retry fires
        assert app.starts[-1] == {"retry": True}
    app.state(pk.PortalKeyboard.FAILED, "the desktop did not create a session")
    _drain(app)
    assert len(app.banners) == 1 and len(app.timers) == 3


def test_one_retry_waits_at_a_time():
    # A key failure and then the portal exiting: one timer, not two.
    app = _state_app()
    app.state(pk.PortalKeyboard.FAILED, "keys stopped reaching the desktop")
    app.state(pk.PortalKeyboard.CLOSED, "the portal exited")
    _drain(app)
    assert len(app.timers) == 1 and app.banners == []


def test_a_stale_retry_cannot_reopen_a_revoked_session(monkeypatch):
    kb = _failed_keyboard(monkeypatch)
    app = _state_app()
    app.state(pk.PortalKeyboard.FAILED, "keys stopped reaching the desktop")
    _drain(app)
    stale = app.timers[0][1]
    app.state(pk.PortalKeyboard.READY, "")             # recovered meanwhile
    kb.state = pk.PortalKeyboard.CLOSED
    app.state(pk.PortalKeyboard.CLOSED, "the desktop ended the session")  # revoked
    _drain(app)
    stale()
    assert app.starts == [] and kb.closes == 0 and len(app.banners) == 1


def test_a_decline_voids_a_waiting_retry(monkeypatch):
    _failed_keyboard(monkeypatch)
    monkeypatch.setattr("wayfinder_main.save_config", lambda cfg: None)
    app = _state_app(_portal_typing_var=None)
    app.state(pk.PortalKeyboard.FAILED, "the desktop did not create a session")
    _drain(app)
    app._after_portal_declined()
    app.timers[0][1]()
    assert app.starts == []


def test_a_ready_session_earns_fresh_retries():
    app = _state_app(_portal_retries=3)
    app.state(pk.PortalKeyboard.READY, "")
    _drain(app)
    app.state(pk.PortalKeyboard.FAILED, "keys stopped reaching the desktop")
    _drain(app)
    assert app.banners == [] and app.timers[0][0] == 5000


def test_failures_a_retry_cannot_fix_go_straight_to_the_banner():
    for detail in ("PyGObject unavailable", "the desktop granted no keyboard",
                   "the desktop answered 2"):
        app = _state_app()
        app.state(pk.PortalKeyboard.FAILED, detail)
        _drain(app)
        assert len(app.banners) == 1 and app.timers == [], detail


def test_a_restarted_portal_is_rejoined_but_a_revoked_session_is_not():
    app = _state_app()
    app.state(pk.PortalKeyboard.CLOSED, "the portal restarted")
    _drain(app)
    assert app.banners == [] and app.timers[0][0] == 5000
    app = _state_app()
    app.state(pk.PortalKeyboard.CLOSED, "the desktop ended the session")
    _drain(app)
    assert len(app.banners) == 1 and app.timers == []


def test_no_retry_once_typing_is_switched_off():
    app = _state_app(config={"linux_portal_typing": False})
    app.state(pk.PortalKeyboard.FAILED, "the desktop did not create a session")
    _drain(app)
    assert app.timers == []


def test_the_retry_closes_the_failed_session_and_starts_again(monkeypatch):
    kb = _failed_keyboard(monkeypatch)
    app = _state_app()
    app._retry_portal_keyboard(0)
    assert kb.closes == 1 and app.starts == [{"retry": True}]
    # Running again (switched off and on meanwhile), or switched off: no retry.
    kb.state = pk.PortalKeyboard.READY
    app._retry_portal_keyboard(0)
    kb.state = pk.PortalKeyboard.FAILED
    app.config["linux_portal_typing"] = False
    app._retry_portal_keyboard(0)
    assert kb.closes == 1 and app.starts == [{"retry": True}]


def test_a_retry_that_finds_no_portal_keeps_counting_down(monkeypatch):
    """The portal answered before; if it does not answer on a retry, that is a
    failure for the bounded retries and, at the end, the banner. It used to
    end silently with neither."""
    from types import SimpleNamespace
    import wayfinder.core.injector as injector
    import wayfinder_main as wm

    class _Inline:
        def __init__(self, target, **_k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(wm, "_IS_LINUX", True)
    monkeypatch.setattr(wm, "IS_MACOS", False)
    monkeypatch.setattr(wm, "IS_WINDOWS", False)
    monkeypatch.setattr(wm.threading, "Thread", _Inline)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)
    monkeypatch.setattr(pk, "host_is_wayland_desktop", lambda: True)
    monkeypatch.setattr(pk, "portal_keyboard_offered", lambda: False)
    monkeypatch.setattr(pk, "keyboard", lambda: SimpleNamespace(on_change=lambda cb: None))
    monkeypatch.setattr(injector, "set_portal_clipboard_hooks", lambda *a, **k: None)
    app = _state_app(_portal_retries=1, _portal_listener_added=True, wm_frame=lambda: "0x1",
                     _set_clipboard_from_worker=None, _get_clipboard_from_worker=None,
                     _restore_clipboard_from_worker=None)
    wm.WayfinderApp._start_portal_keyboard(app, retry=True)
    _drain(app)
    assert app.timers and app.timers[0][0] == 30_000      # the next retry, not silence
    assert not getattr(app, "_portal_not_offered", False)
    app._portal_retries = 3
    app._portal_retry_pending = False
    wm.WayfinderApp._start_portal_keyboard(app, retry=True)
    _drain(app)
    assert len(app.banners) == 1                           # retries used up: the banner


def test_warm_mic_time_keeps_an_explicit_zero():
    import wayfinder_main as wm

    default = 5.0 if wm.IS_MACOS else 30.0
    assert wm._warm_mic_idle_secs({}) == default
    assert wm._warm_mic_idle_secs({"mic_warm_idle_secs": None}) == default
    assert wm._warm_mic_idle_secs({"mic_warm_idle_secs": 0}) == 0   # never closes
    assert wm._warm_mic_idle_secs({"mic_warm_idle_secs": 12.5}) == 12.5


def test_the_approval_dialog_never_opens_mid_dictation(monkeypatch):
    """The retried probe can succeed seconds after launch; the session (and
    the desktop's dialog) waits until no dictation is running."""
    from types import SimpleNamespace
    import wayfinder_main as wm

    class _Inline:
        def __init__(self, target, **_k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(wm.threading, "Thread", _Inline)
    starts, retries = [], []
    kb = SimpleNamespace(start=lambda **k: starts.append(k))
    app = SimpleNamespace(config={"linux_portal_typing": True}, log=lambda m: None,
                          app_state=wm.AppState.RECORDING,
                          after=lambda ms, fn: retries.append((ms, fn)))
    wm.WayfinderApp._start_portal_session_when_idle(app, kb, "x11:1", False)
    assert starts == [] and retries and retries[0][0] >= 100
    app.app_state = wm.AppState.IDLE
    wm.WayfinderApp._start_portal_session_when_idle(app, kb, "x11:1", False)
    assert len(starts) == 1
    # The worker gets the same guard for its Start request.
    may_ask = starts[0]["may_ask"]
    assert may_ask() is True
    app.app_state = wm.AppState.RECORDING
    assert may_ask() is False


def test_the_start_request_waits_until_the_app_may_ask(monkeypatch, tmp_path):
    """Between the app's idle check and the worker's Start call a dictation can
    begin; the worker holds the request (and the dialog) until it ends."""
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    busy = {"now": True}
    try:
        assert kb.start(may_ask=lambda: not busy["now"])
        time.sleep(0.3)
        assert not any(m == "Start" for _i, m, _f, _b in bus.calls)
        busy["now"] = False
        assert _settle(kb, kb.READY) == kb.READY
        assert any(m == "Start" for _i, m, _f, _b in bus.calls)
    finally:
        kb.close()



def test_app_restores_the_clipboard_only_while_it_holds_the_dictation():
    import wayfinder_main as wm
    restore = wm.WayfinderApp._restore_clipboard_from_worker
    tk = _TkLike("café")
    assert restore(tk, "café", "user's url") is True and tk._clipboard == "user's url"
    tk = _TkLike("copied meanwhile")
    assert restore(tk, "café", "user's url") is False and tk._clipboard == "copied meanwhile"


def test_the_portal_paste_uses_the_one_step_restorer(portal_ready, monkeypatch):
    import wayfinder.core.injector as injector
    restored = []
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_READ", lambda: "user's url")
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_WRITE", lambda t: True)
    monkeypatch.setattr(injector, "_PORTAL_CLIPBOARD_RESTORE",
                        lambda pasted, previous: restored.append((pasted, previous)))
    monkeypatch.setattr(injector, "_PORTAL_PASTE_SETTLE_S", 0)
    monkeypatch.setattr(injector, "_PORTAL_RESTORE_AFTER_S", 0)
    injector._inject_text_type_linux("café")
    assert restored == [("café", "user's url")]


def test_a_start_asked_for_before_a_switch_off_does_nothing(monkeypatch, tmp_path):
    """A delayed starter must not reopen a session the user just turned off."""
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    seen = kb.close_count
    kb.close()                                   # the switch went off
    assert kb.start(after_closes=seen) is False
    assert not any(m == "CreateSession" for _i, m, _f, _b in bus.calls)


def test_concurrent_starters_launch_one_worker(monkeypatch, tmp_path):
    """Startup and a switch retry can both reach start(); only one worker runs.
    The first starter is held inside start() (reading the state file) while the
    second one tries: without serialization both would launch."""
    import threading as _threading
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    real_load = pk.load_state
    entered, second_read, proceed = _threading.Event(), _threading.Event(), _threading.Event()
    calls = {"n": 0}

    def slow_first_load(path=None):
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            proceed.wait(2)
        else:
            second_read.set()     # only reachable while the first holds start()
        return real_load(path)

    monkeypatch.setattr(pk, "load_state", slow_first_load)
    results = []
    first = _threading.Thread(target=lambda: results.append(kb.start(may_ask=lambda: False)))
    second = _threading.Thread(target=lambda: results.append(kb.start(may_ask=lambda: False)))
    try:
        first.start()
        assert entered.wait(2)
        second.start()
        # Unserialized, the second starter reaches the state read while the
        # first is held there; serialized, it waits on the lock instead.
        assert not second_read.wait(0.5)
        proceed.set()
        first.join(3)
        second.join(3)
        assert sorted(results) == [False, True]
    finally:
        kb.close()


def test_a_close_during_start_never_reaches_the_dialog(monkeypatch, tmp_path):
    """A switch-off that lands while start() reads its state cancels the new
    worker before its Start request (the one that can show the dialog)."""
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    real_load = pk.load_state

    def load_then_close(path=None):
        state = real_load(path)
        kb.close()                 # the user switched typing off right now
        return state

    monkeypatch.setattr(pk, "load_state", load_then_close)
    kb.start()
    kb._thread.join(2)             # the worker ran to its end (or its cancellation)
    assert not any(m == "Start" for _i, m, _f, _b in bus.calls)


def test_a_stale_start_leaves_the_running_session_alone(monkeypatch, tmp_path):
    bus = _FakeBus()
    kb, _path = _session(monkeypatch, tmp_path, bus)
    try:
        stale = kb.close_count - 1           # asked for before a close
        assert kb.start(may_ask=lambda: False)
        assert kb.start(after_closes=stale) is False
        assert not kb._stop.is_set()          # the running worker was not cancelled
    finally:
        kb.close()

# ── one desktop dialog at a time ─────────────────────────────────────────────

def _may_ask(monkeypatch, bind):
    from types import SimpleNamespace
    import wayfinder_main as wm

    class _Inline:
        def __init__(self, target, **_k):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(wm.threading, "Thread", _Inline)
    starts = []
    kb = SimpleNamespace(start=lambda **k: starts.append(k))
    app = SimpleNamespace(config={"linux_portal_typing": True}, log=lambda m: None,
                          app_state=wm.AppState.IDLE, after=lambda ms, fn: None,
                          _PORTAL_BIND_WAIT_S=wm.WayfinderApp._PORTAL_BIND_WAIT_S)
    if bind is not None:
        app._portal_bind_settled = bind
    wm.WayfinderApp._start_portal_session_when_idle(app, kb, "x11:1", False)
    return starts[0]["may_ask"], app


def test_the_typing_dialog_waits_for_the_shortcut_bind(monkeypatch):
    import threading
    import wayfinder_main as wm

    bind = threading.Event()
    may_ask, app = _may_ask(monkeypatch, bind)
    assert may_ask() is False             # the shortcut dialog may be up
    bind.set()
    assert may_ask() is True
    app.app_state = wm.AppState.RECORDING
    assert may_ask() is False             # still never mid-dictation


def test_an_unanswered_bind_holds_the_typing_dialog_only_so_long(monkeypatch):
    import threading
    import wayfinder_main as wm

    now = [1000.0]
    monkeypatch.setattr(wm.time, "monotonic", lambda: now[0])
    may_ask, app = _may_ask(monkeypatch, threading.Event())
    now[0] += app._PORTAL_BIND_WAIT_S - 1
    assert may_ask() is False
    now[0] += 2
    assert may_ask() is True


def test_no_shortcut_portal_means_nothing_to_wait_for(monkeypatch):
    may_ask, _app = _may_ask(monkeypatch, None)
    assert may_ask() is True


def test_a_replacement_shortcut_listener_holds_the_typing_dialog_again(monkeypatch):
    """After a portal restart or a shortcut change the listener binds again and
    may show its dialog again; the typing dialog waits for that answer too."""
    import threading
    from types import SimpleNamespace
    import wayfinder_main as wm

    bind = threading.Event()
    bind.set()                                     # the first bind was answered
    launched = []

    class _NoStart:
        def __init__(self, target, **_k):
            launched.append(target)

        def start(self):
            pass

    monkeypatch.setattr(wm.threading, "Thread", _NoStart)
    app = SimpleNamespace(config={}, _portal_bind_settled=bind, _portal_listener_started=False)
    wm.WayfinderApp._start_portal_listener(app)
    assert len(launched) == 1 and not bind.is_set()
    bind.set()
    wm.WayfinderApp._start_portal_listener(app)   # still running: nothing to wait for
    assert len(launched) == 1 and bind.is_set()


def test_listeners_without_a_portal_bind_release_the_typing_dialog(monkeypatch):
    """AppImage and source installs (evdev) and X11 (pynput) bind nothing:
    their typing dialog must not sit out the bind wait."""
    from types import SimpleNamespace
    import wayfinder_main as wm

    for backend, settled in (("evdev", True), ("pynput", True), ("unavailable", True),
                             ("portal", False)):
        monkeypatch.setattr(wm, "resolve_hotkey_backend", lambda *a, b=backend: b)
        app = SimpleNamespace(
            config={}, log=lambda m: None, _game_mode=False,
            _ensure_socket_listener=lambda: None, _warm_up_transcription=lambda: None,
            _ensure_gamemode_listener=lambda: None, _flatpak_portal_hotkeys=lambda: True,
            _hotkey_session_type=lambda: "wayland", _start_portal_listener=lambda: None,
            _start_pynput_listener=lambda: None, _start_evdev_listener=lambda: None,
        )
        wm.WayfinderApp.start_hotkey_listener(app)
        assert app._portal_bind_settled.is_set() is settled, backend


# ── the tour names the dialogs ───────────────────────────────────────────────

def _note(monkeypatch, *, backend="portal", bind_set=False, in_play=True,
          state=pk.PortalKeyboard.IDLE, token=""):
    import threading
    from types import SimpleNamespace
    import wayfinder_main as wm

    monkeypatch.setattr(wm, "_IS_LINUX", True)
    monkeypatch.setattr(wm, "IS_MACOS", False)
    monkeypatch.setattr(wm, "IS_WINDOWS", False)
    monkeypatch.setattr(pk, "keyboard", lambda: SimpleNamespace(state=state))
    monkeypatch.setattr(pk, "load_state", lambda *a: {"restore_token": token})
    bind = threading.Event()
    if bind_set:
        bind.set()
    app = SimpleNamespace(_hotkey_backend=backend, _portal_bind_settled=bind,
                          _portal_typing_in_play=lambda: in_play)
    return wm.WayfinderApp._desktop_approval_note(app)


def test_the_tour_names_both_desktop_dialogs_on_a_first_flatpak_run(monkeypatch):
    assert "asks twice" in _note(monkeypatch)


def test_the_tour_names_only_the_dialog_still_to_come(monkeypatch):
    assert "may type in every app" in _note(monkeypatch, bind_set=True)
    assert "may type in every app" in _note(monkeypatch, backend="evdev")  # AppImage
    assert "shortcuts" in _note(monkeypatch, token="tok")      # typing already allowed
    assert "twice" not in _note(monkeypatch, token="tok")


def test_the_tour_says_nothing_once_no_dialog_is_pending(monkeypatch):
    assert _note(monkeypatch, bind_set=True, token="tok") == ""
    assert _note(monkeypatch, bind_set=True, state=pk.PortalKeyboard.READY) == ""
    assert _note(monkeypatch, backend="pynput", in_play=False) == ""  # X11

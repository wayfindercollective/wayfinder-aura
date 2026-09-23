from queue import Queue
from threading import Event
from types import ModuleType

import pytest

from wayfinder_main import _modifier_display, _wheel_event_notches

from wayfinder.core import injector
from wayfinder.hotkeys import pynput_listener
from wayfinder.hotkeys.types import EventType


def test_macos_trackpad_deltas_are_not_divided_by_120():
    assert _wheel_event_notches(-1, "darwin") == 0.25
    assert _wheel_event_notches(-4, "darwin") == 1.0
    assert _wheel_event_notches(-120, "darwin") == 1.0


def test_windows_and_linux_keep_120_delta_contract():
    assert _wheel_event_notches(-120, "win32") == 1.0
    assert _wheel_event_notches(-120, "linux") == 1.0


def test_macos_modifier_names_match_the_keyboard():
    assert _modifier_display("alt", "darwin") == "Option"
    assert _modifier_display("ctrl", "darwin") == "Control"
    assert _modifier_display("super", "darwin") == "Command"
    assert _modifier_display("fn", "darwin") == "Fn"


def test_fn_state_comes_from_quartz_flags(monkeypatch):
    quartz = ModuleType("Quartz")
    quartz.kCGEventFlagMaskSecondaryFn = 0x800000
    quartz.kCGEventFlagMaskAlternate = 0x080000
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGEventFlagMaskControl = 0x040000
    quartz.kCGEventFlagMaskShift = 0x020000
    quartz.kCGEventSourceStateCombinedSessionState = 0
    quartz.CGEventSourceFlagsState = lambda _state: 0x800000
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")

    assert pynput_listener._darwin_fn_pressed() is True


def test_fn_state_is_false_off_macos(monkeypatch):
    monkeypatch.setattr(pynput_listener.sys, "platform", "linux")

    assert pynput_listener._darwin_fn_pressed() is False


def test_physical_key_state_uses_quartz_virtual_keycode(monkeypatch):
    quartz = ModuleType("Quartz")
    quartz.kCGEventSourceStateCombinedSessionState = 0
    quartz.CGEventSourceKeyState = lambda state, keycode: state == 0 and keycode == 49
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")

    assert pynput_listener._darwin_key_pressed(57) is True
    assert pynput_listener._darwin_key_pressed(28) is False


def test_fn_space_is_tracked_and_suppressed_by_darwin_listener(monkeypatch):
    captured = {}

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

    quartz = ModuleType("Quartz")
    quartz.kCGEventFlagMaskSecondaryFn = 0x800000
    quartz.kCGEventFlagMaskAlternate = 0x080000
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGEventFlagMaskControl = 0x040000
    quartz.kCGEventFlagMaskShift = 0x020000
    quartz.kCGEventKeyDown = 10
    quartz.kCGEventKeyUp = 11
    quartz.kCGKeyboardEventKeycode = 9
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, _field: event["keycode"]
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)

    stop = Event()
    stop.set()
    pynput_listener.pynput_hotkey_listener(
        Queue(),
        hotkey_key=57,
        hotkey_modifiers=["fn"],
        stop_event=stop,
    )

    intercept = captured["darwin_intercept"]
    down = {"flags": 0x800000, "keycode": 0x31}
    up = {"flags": 0, "keycode": 0x31}
    other = {"flags": 0, "keycode": 0x24}
    assert intercept(10, down) is None
    assert intercept(11, up) is None
    assert intercept(10, other) is other


def test_fn_enter_style_chord_is_also_suppressed(monkeypatch):
    captured = {}

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

    quartz = ModuleType("Quartz")
    quartz.kCGEventFlagMaskSecondaryFn = 0x800000
    quartz.kCGEventFlagMaskAlternate = 0x080000
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGEventFlagMaskControl = 0x040000
    quartz.kCGEventFlagMaskShift = 0x020000
    quartz.kCGEventKeyDown = 10
    quartz.kCGEventKeyUp = 11
    quartz.kCGKeyboardEventKeycode = 9
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, _field: event["keycode"]
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)
    stop = Event()
    stop.set()
    pynput_listener.pynput_hotkey_listener(
        Queue(), 57, ["fn"], stop,
        style_toggle_key=28, style_toggle_modifiers=["fn"],
    )

    intercept = captured["darwin_intercept"]
    assert intercept(10, {"flags": 0x800000, "keycode": 0x24}) is None
    assert intercept(11, {"flags": 0, "keycode": 0x24}) is None


def test_unsupported_macos_key_falls_back_to_fn_space_not_bare_space(monkeypatch):
    captured = {}

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)
    fn = {"value": False}
    monkeypatch.setattr(pynput_listener, "_darwin_fn_pressed", lambda: fn["value"])
    stop = Event()
    stop.set()
    events = Queue()
    pynput_listener.pynput_hotkey_listener(events, 275, [], stop)

    captured["on_press"](pynput_listener.Key.space)
    assert events.empty()
    fn["value"] = True
    captured["on_release"](pynput_listener.Key.space)
    captured["on_press"](pynput_listener.Key.space)
    assert events.get_nowait() == (EventType.HOTKEY_PRESSED, None)


def test_escape_queues_global_recording_cancel_on_macos(monkeypatch):
    captured = {}

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)
    monkeypatch.setattr(pynput_listener, "_darwin_fn_pressed", lambda: False)
    stop = Event()
    stop.set()
    events = Queue()

    pynput_listener.pynput_hotkey_listener(
        events,
        hotkey_key=57,
        hotkey_modifiers=["fn"],
        stop_event=stop,
    )
    captured["on_press"](pynput_listener.Key.esc)

    assert events.get_nowait() == (EventType.CANCEL_RECORDING, None)


def test_macos_injection_restores_full_snapshot_when_paste_fails(monkeypatch):
    class Pasteboard:
        @staticmethod
        def changeCount():
            return 7

    pasteboard = Pasteboard()
    snapshot = [[("public.png", b"image"), ("public.rtf", b"rich")]]
    restored = []
    pyautogui = ModuleType("pyautogui")
    pyautogui.hotkey = lambda *_args: (_ for _ in ()).throw(RuntimeError("denied"))
    monkeypatch.setitem(injector.sys.modules, "pyautogui", pyautogui)
    monkeypatch.setattr(
        injector, "_snapshot_macos_pasteboard", lambda: (pasteboard, snapshot)
    )
    monkeypatch.setattr(
        injector, "_write_macos_pasteboard_text", lambda _pb, _text: 7
    )
    monkeypatch.setattr(
        injector,
        "_restore_macos_pasteboard",
        lambda _pb, items: restored.append(items) or True,
    )

    import wayfinder.core.macos_paste as macos_paste

    def quartz_denied():
        raise RuntimeError("quartz denied")

    # Quartz post fails, then the PyAutoGUI fallback fails too.
    monkeypatch.setattr(macos_paste, "post_command_v", quartz_denied)

    with pytest.raises(injector.InjectionError, match="denied"):
        injector._inject_text_pyautogui("hello")

    # A failed paste restores immediately (no deferred restore pending).
    assert restored == [snapshot]


def test_macos_injection_defers_restore_and_keeps_text_without_accessibility(monkeypatch):
    import wayfinder.core.macos_paste as macos_paste

    class Pasteboard:
        @staticmethod
        def changeCount():
            return 7

    pasteboard = Pasteboard()
    restored, written, posted = [], [], []
    monkeypatch.setattr(injector, "_snapshot_macos_pasteboard", lambda: (pasteboard, ["old"]))
    monkeypatch.setattr(injector, "_write_macos_pasteboard_text", lambda _pb, text: written.append(text) or 7)
    monkeypatch.setattr(injector, "_restore_macos_pasteboard", lambda _pb, items: restored.append(items))
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", lambda: True)
    monkeypatch.setattr(macos_paste, "post_command_v", lambda: posted.append(True))

    # Successful paste: the restore is deferred, then lands on flush.
    injector._inject_text_pyautogui("hello")
    assert posted == [True] and restored == []
    macos_paste.pending_restore.flush()
    assert restored == [["old"]]

    # No Accessibility: refuse, leave the text on the clipboard, never restore.
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: False)
    restored.clear()
    with pytest.raises(injector.InjectionError, match="Accessibility"):
        injector._inject_text_pyautogui("second")
    assert written[-1] == "second" and restored == []


def test_aqua_wheel_click_scrolls_a_full_notch_but_trackpad_stream_stays_smooth():
    from wayfinder_main import _aqua_wheel_notches

    assert _aqua_wheel_notches(-1, 0.5) == 1.0      # lone mouse-wheel click
    assert _aqua_wheel_notches(-3, 0.5) == 3.0      # accelerated click
    assert _aqua_wheel_notches(-1, 0.016) == 0.25   # trackpad stream (~60 Hz)
    assert _aqua_wheel_notches(-120, 0.016) == 1.0  # notch-native delta
    assert _aqua_wheel_notches("bad", 0.5) == 0.0


def test_post_command_v_releases_command_so_it_cannot_stick(monkeypatch):
    """Command down, V down, V up, Command up with cleared flags (real posts stubbed)."""
    import importlib
    import types

    import wayfinder.core.macos_paste as macos_paste

    real = importlib.reload(macos_paste)  # undo the autouse stub for this module object
    posted = []
    quartz = types.ModuleType("Quartz")
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGHIDEventTap = 0
    quartz.CGEventCreateKeyboardEvent = lambda _src, code, down: {"code": code, "down": down}
    quartz.CGEventSetFlags = lambda event, flags: event.__setitem__("flags", flags)
    quartz.CGEventPost = lambda _tap, event: posted.append(event)
    monkeypatch.setitem(__import__("sys").modules, "Quartz", quartz)
    monkeypatch.setattr(real, "keycode_for_character", lambda _c: 0x09)

    real.post_command_v()

    assert [(e["code"], e["down"], e["flags"]) for e in posted] == [
        (0x37, True, 0x100000),
        (0x09, True, 0x100000),
        (0x09, False, 0x100000),
        (0x37, False, 0),
    ]


def test_press_enter_posts_a_bare_return_through_quartz(monkeypatch):
    import sys as _sys
    from wayfinder.core import injector, macos_paste
    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", lambda *a, **k: True)
    sent = []
    monkeypatch.setattr(macos_paste, "post_return", lambda: sent.append("return"))
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: True)
    injector.press_enter()
    assert sent == ["return"]


def test_press_enter_refuses_without_accessibility(monkeypatch):
    import pytest as _pytest
    from wayfinder.core import injector, macos_paste
    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", lambda *a, **k: True)
    monkeypatch.setattr(macos_paste, "post_return", lambda: None)
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: False)
    with _pytest.raises(injector.InjectionError):
        injector.press_enter()


def test_active_window_is_reported_on_macos(monkeypatch):
    from wayfinder.core import injector, macos_paste
    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(macos_paste, "frontmost_window_id", lambda: "123:456")
    assert injector.get_active_window() == "123:456"


def test_frontmost_window_id_live():
    import sys as _sys
    import pytest as _pytest
    if _sys.platform != "darwin":
        _pytest.skip("macOS only")
    from wayfinder.core.macos_paste import frontmost_window_id
    value = frontmost_window_id()
    assert value is None or (":" in value and value.split(":")[0].isdigit())


class _FakePasteboard:
    """Stands in for NSPasteboard (AppKit calls only; never the real clipboard)."""

    def __init__(self, behavior=0, items=()):
        self.behavior = behavior
        self._items = list(items)
        self.data = {}
        self.options = []
        self.change = 0
        self.written = None

    def accessBehavior(self):
        return self.behavior

    def pasteboardItems(self):
        return self._items

    def prepareForNewContentsWithOptions_(self, options):
        self.options.append(options)
        self.data = {}
        self.change += 1
        return self.change

    def clearContents(self):
        self.data = {}
        self.change += 1

    def setString_forType_(self, text, type_name):
        self.data[str(type_name)] = text
        return True

    def setData_forType_(self, data, type_name):
        self.data[str(type_name)] = data
        return True

    def writeObjects_(self, items):
        self.written = items
        return True

    def changeCount(self):
        return self.change


def _darwin_only():
    import sys as _sys
    import pytest as _pytest
    if _sys.platform != "darwin":
        _pytest.skip("AppKit only")


def test_dictation_write_is_transient_and_this_mac_only():
    _darwin_only()
    from wayfinder.core import injector
    pb = _FakePasteboard()
    injector._write_macos_pasteboard_text(pb, "hello")
    assert pb.options == [1]  # NSPasteboardContentsCurrentHostOnly
    assert "org.nspasteboard.TransientType" in pb.data
    assert "org.nspasteboard.AutoGeneratedType" in pb.data
    assert "hello" in pb.data.values()


def test_clipboard_is_not_read_when_macos_would_prompt_or_deny(monkeypatch):
    _darwin_only()
    import types as _types
    import AppKit
    from wayfinder.core import injector

    class _Items:
        def __iter__(self):
            raise AssertionError("the clipboard must not be read")

    for behavior in (1, 3):  # Ask, AlwaysDeny
        pb = _FakePasteboard(behavior=behavior)
        pb.pasteboardItems = lambda: _Items()
        monkeypatch.setattr(AppKit, "NSPasteboard", _types.SimpleNamespace(generalPasteboard=lambda: pb))
        assert injector._snapshot_macos_pasteboard() == (pb, injector._NO_SNAPSHOT)
    assert injector._macos_pasteboard_readable(_FakePasteboard(behavior=2)) is True


def test_oversized_clipboard_is_not_snapshotted(monkeypatch):
    _darwin_only()
    import types as _types
    import AppKit
    from wayfinder.core import injector

    class _Item:
        def types(self):
            return ["public.tiff"]

        def dataForType_(self, _t):
            return b"x" * 1024

    pb = _FakePasteboard(behavior=2, items=[_Item()])
    monkeypatch.setattr(AppKit, "NSPasteboard", _types.SimpleNamespace(generalPasteboard=lambda: pb))
    monkeypatch.setattr(injector, "_MACOS_SNAPSHOT_MAX_BYTES", 100)
    assert injector._snapshot_macos_pasteboard() == (pb, injector._NO_SNAPSHOT)


def test_no_snapshot_means_no_restore():
    _darwin_only()
    from wayfinder.core import injector
    pb = _FakePasteboard()
    pb.data = {"public.utf8-plain-text": "dictation"}
    assert injector._restore_macos_pasteboard(pb, injector._NO_SNAPSHOT) is True
    assert pb.data == {"public.utf8-plain-text": "dictation"} and pb.change == 0


def test_restored_clipboard_is_marked_transient():
    _darwin_only()
    from wayfinder.core import injector
    pb = _FakePasteboard()
    assert injector._restore_macos_pasteboard(pb, [[("public.utf8-plain-text", b"mine")]])
    types = {str(t) for t in pb.written[0].types()}
    assert {"public.utf8-plain-text", "org.nspasteboard.TransientType"} <= types
    assert pb.options == [1]

from queue import Queue
from threading import Event
from types import ModuleType

import pytest

from wayfinder_main import _modifier_display, _wheel_event_notches

from wayfinder.core import injector
from wayfinder.hotkeys import pynput_listener
from wayfinder.hotkeys.types import EventType

# pynput cannot import on a headless Linux runner (no X display).
needs_pynput = pytest.mark.skipif(pynput_listener.keyboard is None,
                                  reason="pynput unavailable here")


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


@pytest.mark.macos_only
def test_physical_key_state_uses_quartz_virtual_keycode(monkeypatch):
    quartz = ModuleType("Quartz")
    quartz.kCGEventSourceStateCombinedSessionState = 0
    quartz.CGEventSourceKeyState = lambda state, keycode: state == 0 and keycode == 49
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")

    assert pynput_listener._darwin_key_pressed(57) is True
    assert pynput_listener._darwin_key_pressed(28) is False


@pytest.mark.macos_only
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
    quartz.kCGEventSourceUnixProcessID = 12
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, field: (
        event.get("source_pid", 0) if field == 12 else event["keycode"]
    )
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


@pytest.mark.macos_only
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
    quartz.kCGEventSourceUnixProcessID = 12
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, field: (
        event.get("source_pid", 0) if field == 12 else event["keycode"]
    )
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)
    stop = Event()
    stop.set()
    events = Queue()
    pynput_listener.pynput_hotkey_listener(
        events, 57, ["fn"], stop,
        style_toggle_key=28, style_toggle_modifiers=["fn"],
    )

    intercept = captured["darwin_intercept"]
    captured["on_press"](pynput_listener.Key.enter, False)
    assert intercept(10, {"flags": 0x800000, "keycode": 0x24}) is None
    assert events.get_nowait() == (EventType.STYLE_TOGGLE, None)
    captured["on_release"](pynput_listener.Key.enter, False)
    assert intercept(11, {"flags": 0, "keycode": 0x24}) is None


@pytest.mark.macos_only
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
    quartz = ModuleType("Quartz")
    quartz.kCGEventFlagMaskSecondaryFn = 0x800000
    quartz.kCGEventFlagMaskAlternate = 0x080000
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGEventFlagMaskControl = 0x040000
    quartz.kCGEventFlagMaskShift = 0x020000
    quartz.kCGEventKeyDown = 10
    quartz.kCGEventKeyUp = 11
    quartz.kCGKeyboardEventKeycode = 9
    quartz.kCGEventSourceUnixProcessID = 12
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, field: (
        event.get("source_pid", 0) if field == 12 else event["keycode"]
    )
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    stop = Event()
    stop.set()
    events = Queue()
    pynput_listener.pynput_hotkey_listener(events, 275, [], stop)

    captured["on_press"](pynput_listener.Key.space)
    assert captured["darwin_intercept"](10, {"flags": 0, "keycode": 49}) is not None
    assert events.empty()
    captured["on_press"](pynput_listener.Key.space)
    assert captured["darwin_intercept"](10, {"flags": 0x800000, "keycode": 49}) is None
    assert events.get_nowait() == (EventType.HOTKEY_PRESSED, None)


@pytest.mark.macos_only
def test_macos_record_requires_physical_event_with_exact_fn_chord(monkeypatch):
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
    quartz.kCGEventSourceUnixProcessID = 12
    quartz.CGEventGetFlags = lambda event: event["flags"]
    quartz.CGEventGetIntegerValueField = lambda event, field: (
        event.get("source_pid", 0) if field == 12 else event["keycode"]
    )
    monkeypatch.setitem(pynput_listener.sys.modules, "Quartz", quartz)
    monkeypatch.setattr(pynput_listener.sys, "platform", "darwin")
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)
    # The old listener trusted this separate global poll, even when the Space
    # event itself lacked Fn. Reproducing that disagreement must not record.
    monkeypatch.setattr(pynput_listener, "_darwin_fn_pressed", lambda: True)
    stop = Event()
    stop.set()
    events = Queue()
    pynput_listener.pynput_hotkey_listener(events, 57, ["fn"], stop)
    press = captured["on_press"]
    intercept = captured["darwin_intercept"]

    press(pynput_listener.Key.space, False)
    assert intercept(10, {"flags": 0, "keycode": 49}) is not None
    assert events.empty()

    press(pynput_listener.Key.space, True)
    assert intercept(10, {"flags": 0x800000, "keycode": 49, "source_pid": 42}) is not None
    assert events.empty()

    press(pynput_listener.Key.space, False)
    assert intercept(10, {"flags": 0x800000 | 0x020000, "keycode": 49}) is not None
    assert events.empty()

    press(pynput_listener.Key.space, False)
    assert intercept(10, {"flags": 0x800000, "keycode": 49}) is None
    assert events.get_nowait() == (EventType.HOTKEY_PRESSED, None)
    # Key repeat stays latched until the physical key-up, then can fire again.
    press(pynput_listener.Key.space, False)
    assert intercept(10, {"flags": 0x800000, "keycode": 49}) is None
    assert events.empty()
    captured["on_release"](pynput_listener.Key.space, False)
    assert intercept(11, {"flags": 0, "keycode": 49}) is None


@needs_pynput
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
    pyautogui.keyDown = lambda *_args: (_ for _ in ()).throw(RuntimeError("denied"))
    pyautogui.keyUp = lambda *_args: None
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
    # Another app is frontmost (the suite's own Tk windows can make pytest
    # frontmost, which the self-target guard would rightly refuse).
    monkeypatch.setattr(macos_paste, "frontmost_window_id", lambda: "1:1")

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
    monkeypatch.setattr(macos_paste, "frontmost_window_id", lambda: "1:1")  # another app

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


def test_macos_injection_refuses_auras_own_window(monkeypatch):
    import os
    from wayfinder.core import macos_paste

    written, posted = [], []
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_paste, "frontmost_window_id",
                        lambda: f"{os.getpid()}:123")
    monkeypatch.setattr(macos_paste, "post_command_v", lambda: posted.append(True))
    monkeypatch.setattr(injector, "_snapshot_macos_pasteboard",
                        lambda: (object(), []))
    monkeypatch.setattr(injector, "_write_macos_pasteboard_text",
                        lambda _pb, text: written.append(text) or 1)

    with pytest.raises(injector.InjectionError, match="frontmost"):
        injector._inject_text_pyautogui("hello")
    assert written == ["hello"]
    assert posted == []


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
    monkeypatch.setattr(real, "_v_keycode", 0x09)

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


def test_blocked_microphone_gets_a_permission_message_not_a_mute_hint(monkeypatch):
    import queue as _queue
    import wayfinder_main as wm
    from wayfinder.utils import macos_permissions as mp

    class _App:
        config = {"audio_device_name": "DualSense Wireless Controller"}
        event_queue = _queue.Queue()

        def _refresh_macos_permission_banner(self):
            pass

    monkeypatch.setattr(wm, "IS_MACOS", True)
    monkeypatch.setattr(mp, "microphone_authorization", lambda: mp.MIC_DENIED)
    message = wm.WayfinderApp._silence_error_message(_App())
    assert "Privacy & Security → Microphone" in message and "mute" not in message
    assert not _App.event_queue.empty()  # banner refresh queued

    monkeypatch.setattr(mp, "microphone_authorization", lambda: mp.MIC_AUTHORIZED)
    message = wm.WayfinderApp._silence_error_message(_App())
    assert "DualSense" in message and "mute" in message


def test_microphone_authorization_reads_tcc():
    import sys as _sys
    import pytest as _pytest
    if _sys.platform != "darwin":
        _pytest.skip("macOS only")
    from wayfinder.utils.macos_permissions import microphone_authorization
    assert microphone_authorization() in (0, 1, 2, 3)


@pytest.mark.parametrize("at", ["modifier-wait", "pasteboard-write"])
def test_macos_injection_retired_during_its_waits_pastes_nothing(monkeypatch, at):
    # Sol mac r11: a reset while the injector waited for the hotkey's
    # modifiers still pasted the retired text.
    import wayfinder.core.macos_paste as macos_paste

    class Pasteboard:
        @staticmethod
        def changeCount():
            return 7

    retired = [False]
    restored, written, posted = [], [], []

    def wait():
        retired[0] = at == "modifier-wait"
        return True

    def write(_pb, text):
        written.append(text)
        retired[0] = True
        return 7

    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_paste, "frontmost_window_id", lambda: "1:1")  # another app
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", wait)
    monkeypatch.setattr(injector, "_snapshot_macos_pasteboard", lambda: (Pasteboard(), ["old"]))
    monkeypatch.setattr(injector, "_write_macos_pasteboard_text", write)
    monkeypatch.setattr(injector, "_restore_macos_pasteboard", lambda _pb, items: restored.append(items))
    monkeypatch.setattr(macos_paste, "post_command_v", lambda: posted.append(True))
    with pytest.raises(injector.InjectionCancelled):
        injector._inject_text_pyautogui("retired words", cancelled=lambda: retired[0])
    assert posted == []
    if at == "modifier-wait":
        assert written == []                                   # the clipboard untouched
    else:
        assert written == ["retired words"] and restored == [["old"]]   # put back now


def test_macos_auto_enter_retired_during_its_wait_sends_nothing(monkeypatch):
    import wayfinder.core.macos_paste as macos_paste
    retired, sent = [False], []

    def wait():
        retired[0] = True
        return True

    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", wait)
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_paste, "post_return", lambda: sent.append(True))
    with pytest.raises(injector.InjectionCancelled):
        injector.press_enter(cancelled=lambda: retired[0])
    assert sent == []


def test_macos_paste_asks_the_threads_cancellable(monkeypatch):
    # A path that passes no cancellation on (a game chat's paste) is asked
    # through the scope.
    import wayfinder.core.macos_paste as macos_paste
    posted = []
    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: True)
    monkeypatch.setattr(macos_paste, "frontmost_window_id", lambda: "1:1")
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", lambda: True)
    monkeypatch.setattr(injector, "_snapshot_macos_pasteboard",
                        lambda: pytest.fail("the pasteboard was touched"))
    monkeypatch.setattr(macos_paste, "post_command_v", lambda: posted.append(True))
    with injector.cancellable(lambda: True):
        with pytest.raises(injector.InjectionCancelled):
            injector.inject_text("retired words")
    assert posted == []


def _mac_paste_rig(monkeypatch):
    """The Mac paste with a fake pasteboard; *rig.retired* is the dictation's."""
    import os
    from types import SimpleNamespace
    import wayfinder.core.macos_paste as macos_paste

    class Pasteboard:
        @staticmethod
        def changeCount():
            return 7

    rig = SimpleNamespace(retired=False, written=[], restored=[], posted=[], hotkeys=[],
                          trusted=True, front="1:1", own=f"{os.getpid()}:1")
    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(macos_paste, "accessibility_trusted", lambda: rig.trusted)
    monkeypatch.setattr(macos_paste, "frontmost_window_id", lambda: rig.front)
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", lambda: True)
    monkeypatch.setattr(injector, "_snapshot_macos_pasteboard", lambda: (Pasteboard(), ["old"]))
    monkeypatch.setattr(injector, "_write_macos_pasteboard_text",
                        lambda _pb, text: rig.written.append(text) or 7)
    monkeypatch.setattr(injector, "_restore_macos_pasteboard",
                        lambda _pb, items: rig.restored.append(items))
    monkeypatch.setattr(macos_paste, "post_command_v", lambda: rig.posted.append(True))
    return rig, macos_paste


@pytest.mark.parametrize("retire", [True, False])
@pytest.mark.parametrize("refusal", ["accessibility-off", "aura-frontmost"])
def test_a_refused_mac_paste_leaves_no_retired_text_on_the_clipboard(monkeypatch, refusal, retire):
    # Sol mac r12: a refused paste leaves the transcript on the clipboard for
    # a manual paste; a retired dictation's too, during the permission or the
    # focus query.
    rig, macos_paste = _mac_paste_rig(monkeypatch)
    if refusal == "accessibility-off":
        def trusted():
            rig.retired = retire
            return False
        monkeypatch.setattr(macos_paste, "accessibility_trusted", trusted)
    else:
        def front():
            rig.retired = retire
            return rig.own
        monkeypatch.setattr(macos_paste, "frontmost_window_id", front)
    if retire:
        with pytest.raises(injector.InjectionCancelled):
            injector.inject_text("retired words", cancelled=lambda: rig.retired)
        assert rig.written == []
    else:
        with pytest.raises(injector.InjectionError, match="on the clipboard"):
            injector.inject_text("live words", cancelled=lambda: rig.retired)
        assert rig.written == ["live words"]
    assert rig.posted == []


@pytest.mark.parametrize("retire", [True, False])
def test_a_mac_paste_retired_while_quartz_failed_sends_no_fallback(monkeypatch, retire):
    # Sol mac r12: when Quartz fails, PyAutoGUI's Cmd+V is the fallback; a
    # retirement during the failure or its import still pasted.
    import sys as _sys
    rig, macos_paste = _mac_paste_rig(monkeypatch)

    def quartz_fails():
        rig.retired = retire
        raise RuntimeError("CGEventPost failed")

    fake = ModuleType("pyautogui")
    fake.keyDown = lambda key: rig.hotkeys.append(("down", key))
    fake.keyUp = lambda key: rig.hotkeys.append(("up", key))
    monkeypatch.setitem(_sys.modules, "pyautogui", fake)
    monkeypatch.setattr(macos_paste, "post_command_v", quartz_fails)
    monkeypatch.setattr(macos_paste.pending_restore, "schedule", lambda restore: None)
    if retire:
        with pytest.raises(injector.InjectionCancelled):
            injector.inject_text("retired words", cancelled=lambda: rig.retired)
        assert rig.hotkeys == [] and rig.restored == [["old"]]   # put back now
    else:
        injector.inject_text("live words", cancelled=lambda: rig.retired)
        assert rig.hotkeys == [("down", "command"), ("down", "v"), ("up", "v"),
                               ("up", "command")]


@pytest.mark.parametrize("stop", ["retired-at-command", "v-fails", "release-fails"])
def test_the_pyautogui_cmd_v_asks_before_v_and_always_releases(monkeypatch, stop):
    # Sol mac r13: hotkey() sleeps after Command's press and then pressed V
    # for a dictation retired meanwhile. Now V asks first, and Command goes
    # back up however the sequence ends.
    import sys as _sys
    rig, macos_paste = _mac_paste_rig(monkeypatch)

    def key_down(key):
        if key == "v" and stop == "v-fails":
            raise RuntimeError("PyAutoGUI could not post V")
        rig.hotkeys.append(("down", key))
        if key == "command" and stop == "retired-at-command":
            rig.retired = True

    def key_up(key):
        rig.hotkeys.append(("up", key))
        if stop == "release-fails":
            raise RuntimeError("PyAutoGUI could not release")

    fake = ModuleType("pyautogui")
    fake.keyDown, fake.keyUp = key_down, key_up
    monkeypatch.setitem(_sys.modules, "pyautogui", fake)
    monkeypatch.setattr(macos_paste, "post_command_v",
                        lambda: (_ for _ in ()).throw(RuntimeError("CGEventPost failed")))
    monkeypatch.setattr(macos_paste.pending_restore, "schedule", lambda restore: None)
    if stop == "retired-at-command":
        with pytest.raises(injector.InjectionCancelled):
            injector.inject_text("retired words", cancelled=lambda: rig.retired)
        assert rig.hotkeys == [("down", "command"), ("up", "command")]
        assert rig.restored == [["old"]]
    elif stop == "v-fails":
        with pytest.raises(injector.InjectionError, match="could not post V"):
            injector.inject_text("words", cancelled=lambda: rig.retired)
        assert rig.hotkeys == [("down", "command"), ("up", "v"), ("up", "command")]
    else:   # the paste went out; failed releases are not a second failure
        injector.inject_text("words", cancelled=lambda: rig.retired)
        assert rig.hotkeys == [("down", "command"), ("down", "v"), ("up", "v"),
                               ("up", "command")]


def test_the_pyautogui_cmd_v_releases_past_the_fail_safe(monkeypatch):
    # Sol mac r14: PyAutoGUI's public keyUp raises its fail-safe while the
    # pointer sits in a screen corner, so Command stayed down. Its own sender
    # releases it.
    import sys as _sys
    rig, macos_paste = _mac_paste_rig(monkeypatch)

    class FailSafeException(Exception):
        pass

    corner = {"now": False}
    native = ModuleType("platformModule")
    native._keyDown = lambda key: rig.hotkeys.append(("down", key))
    native._keyUp = lambda key: rig.hotkeys.append(("up", key))

    def checked(send):
        def public(key):
            if corner["now"]:
                raise FailSafeException("fail-safe: the pointer is in a corner")
            send(key)
            corner["now"] = True    # the user's pointer reaches a corner
        return public

    fake = ModuleType("pyautogui")
    fake.platformModule = native
    fake.keyDown, fake.keyUp = checked(native._keyDown), checked(native._keyUp)
    monkeypatch.setitem(_sys.modules, "pyautogui", fake)
    monkeypatch.setattr(macos_paste, "post_command_v",
                        lambda: (_ for _ in ()).throw(RuntimeError("CGEventPost failed")))
    monkeypatch.setattr(macos_paste.pending_restore, "schedule", lambda restore: None)
    with pytest.raises(injector.InjectionError, match="fail-safe"):
        injector.inject_text("words", cancelled=lambda: rig.retired)
    assert rig.hotkeys == [("down", "command"), ("up", "v"), ("up", "command")]


def test_a_failed_native_release_falls_back_to_the_public_one(monkeypatch):
    sent = []
    native = ModuleType("platformModule")
    native._keyUp = lambda key: (_ for _ in ()).throw(RuntimeError("native sender gone"))
    fake = ModuleType("pyautogui")
    fake.platformModule = native
    fake.keyDown = lambda key: sent.append(("down", key))
    fake.keyUp = lambda key: sent.append(("up", key))
    injector._pyautogui_command_v(fake)
    assert sent == [("down", "command"), ("down", "v"), ("up", "v"), ("up", "command")]


def test_the_real_pyautogui_wrappers_release_past_the_fail_safe(monkeypatch):
    pyautogui = pytest.importorskip("pyautogui")
    sent, where = [], iter([(500, 500)])
    monkeypatch.setattr(pyautogui, "PAUSE", 0)
    monkeypatch.setattr(pyautogui.platformModule, "_position", lambda: next(where, (0, 0)))
    monkeypatch.setattr(pyautogui.platformModule, "_keyDown", lambda key: sent.append(("down", key)))
    monkeypatch.setattr(pyautogui.platformModule, "_keyUp", lambda key: sent.append(("up", key)))
    with pytest.raises(pyautogui.FailSafeException):
        injector._pyautogui_command_v(pyautogui)
    assert sent == [("down", "command"), ("up", "v"), ("up", "command")]


def test_a_mac_paste_retired_during_the_snapshot_writes_nothing(monkeypatch):
    # Sol mac r13: the snapshot reads every pasteboard item; a retirement
    # during that read still cleared and wrote the pasteboard.
    rig, macos_paste = _mac_paste_rig(monkeypatch)
    snapshot = injector._snapshot_macos_pasteboard

    def slow_snapshot():
        rig.retired = True
        return snapshot()

    monkeypatch.setattr(injector, "_snapshot_macos_pasteboard", slow_snapshot)
    with pytest.raises(injector.InjectionCancelled):
        injector.inject_text("retired words", cancelled=lambda: rig.retired)
    assert rig.written == [] and rig.posted == [] and rig.restored == []


def _real_quartz(monkeypatch, *, fail=None, retire_after=None):
    """The real post_command_v / post_return over a fake Quartz. *fail* makes
    one (code, down) event fail to post; *retire_after* retires the scope
    after that event posts."""
    import importlib
    import types

    import wayfinder.core.macos_paste as macos_paste

    real = importlib.reload(macos_paste)   # undo the autouse stub
    posted, retired = [], [False]

    def post(_tap, event):
        key = (event["code"], event["down"])
        if key == fail:
            raise RuntimeError("CGEventPost failed")
        posted.append(key)
        if key == retire_after:
            retired[0] = True

    quartz = types.ModuleType("Quartz")
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGHIDEventTap = 0
    quartz.CGEventCreateKeyboardEvent = lambda _src, code, down: {"code": code, "down": down}
    quartz.CGEventSetFlags = lambda event, flags: None
    quartz.CGEventPost = post
    monkeypatch.setitem(__import__("sys").modules, "Quartz", quartz)
    monkeypatch.setattr(real, "_v_keycode", 0x09)
    return real, posted, retired


def test_quartz_cmd_v_retired_after_command_sends_no_v_and_releases_command(monkeypatch):
    # Sol mac r13: each press asks the dictation's scope, so a retirement
    # while Command goes down sends no V, and Command comes back up.
    real, posted, retired = _real_quartz(monkeypatch, retire_after=(0x37, True))
    with injector.cancellable(lambda: retired[0]):
        with pytest.raises(injector.InjectionCancelled):
            real.post_command_v()
    assert posted == [(0x37, True), (0x37, False)]


@pytest.mark.parametrize("fail", [(0x09, True), (0x37, True)])
def test_quartz_cmd_v_releases_what_it_pressed_when_a_post_fails(monkeypatch, fail):
    # Sol mac r13: a failed V left Command held for the user's next clicks.
    real, posted, _retired = _real_quartz(monkeypatch, fail=fail)
    with pytest.raises(RuntimeError, match="CGEventPost failed"):
        real.post_command_v()
    if fail == (0x09, True):
        assert posted == [(0x37, True), (0x09, False), (0x37, False)]
    else:
        assert posted == [(0x37, False)]


def test_quartz_releases_that_fail_do_not_fail_the_paste(monkeypatch):
    # A release that fails is not a paste failure: the injector would
    # otherwise paste again through PyAutoGUI.
    real, posted, _retired = _real_quartz(monkeypatch, fail=(0x09, False))
    real.post_command_v()
    assert posted == [(0x37, True), (0x09, True), (0x37, False)]


def test_quartz_return_asks_before_its_press(monkeypatch):
    real, posted, retired = _real_quartz(monkeypatch)
    retired[0] = True
    with injector.cancellable(lambda: retired[0]):
        with pytest.raises(injector.InjectionCancelled):
            real.post_return()
    assert posted == []
    retired[0] = False
    with injector.cancellable(lambda: retired[0]):
        real.post_return()
    assert posted == [(0x24, True), (0x24, False)]


def test_quartz_return_is_released_when_its_hold_fails(monkeypatch):
    real, posted, _retired = _real_quartz(monkeypatch)
    monkeypatch.setattr(real, "_hold", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        real.post_return()
    assert posted == [(0x24, True), (0x24, False)]


def test_a_mac_enter_retired_during_the_permission_query_sends_nothing(monkeypatch):
    # Sol mac r12: the check sat before the Accessibility query, not right
    # before the key.
    import wayfinder.core.macos_paste as macos_paste
    retired, sent = [False], []

    def trusted():
        retired[0] = True
        return True

    monkeypatch.setattr(injector.sys, "platform", "darwin")
    monkeypatch.setattr(injector, "_wait_for_macos_modifier_release", lambda: True)
    monkeypatch.setattr(macos_paste, "accessibility_trusted", trusted)
    monkeypatch.setattr(macos_paste, "post_return", lambda: sent.append(True))
    with pytest.raises(injector.InjectionCancelled):
        injector.press_enter(cancelled=lambda: retired[0])
    assert sent == []


def test_a_quartz_press_refused_for_a_retired_dictation_tries_no_pyautogui(monkeypatch):
    # A press refused for a retired dictation is not a Quartz failure: no
    # fallback loads PyAutoGUI to send it another way.
    import sys as _sys
    rig, macos_paste = _mac_paste_rig(monkeypatch)

    def refused():
        rig.retired = True
        raise injector.InjectionCancelled("the dictation was retired: nothing injected")

    monkeypatch.setattr(macos_paste, "post_command_v", refused)
    monkeypatch.setitem(_sys.modules, "pyautogui", None)     # importing it fails
    monkeypatch.setattr(macos_paste.pending_restore, "schedule", lambda restore: None)
    with pytest.raises(injector.InjectionCancelled):
        injector.inject_text("retired words", cancelled=lambda: rig.retired)
    assert rig.restored == [["old"]]

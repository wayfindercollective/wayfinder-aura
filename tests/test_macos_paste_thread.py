"""The paste thread never reads the keyboard layout, and a hung paste recovers.

Field bug (macOS 27): the paste thread called the Text Input Sources API to
find the layout's V key; off the main queue that API asserts and the thread
hung forever. Aura sat on "Typing..." and nothing was pasted.
"""

import threading
from types import ModuleType, SimpleNamespace

import pytest

import wayfinder_main
from wayfinder.core import macos_paste


def _in_thread(fn):
    out = []
    t = threading.Thread(target=lambda: out.append(fn()))
    t.start()
    t.join(2)
    assert not t.is_alive(), "call blocked off the main thread"
    return out[0]


def test_layout_lookup_refuses_to_run_off_the_main_thread():
    assert _in_thread(lambda: macos_paste.keycode_for_character("v")) is None


def test_refresh_off_the_main_thread_keeps_the_cache(monkeypatch):
    monkeypatch.setattr(macos_paste, "_v_keycode", 0x2F)
    _in_thread(macos_paste.refresh_layout_cache)
    assert macos_paste.paste_keycode() == 0x2F


def test_refresh_on_the_main_thread_updates_the_cache(monkeypatch):
    monkeypatch.setattr(macos_paste, "_v_keycode", None)
    monkeypatch.setattr(macos_paste, "keycode_for_character", lambda _c: 0x2F)
    macos_paste.refresh_layout_cache()
    assert macos_paste.paste_keycode() == 0x2F


def test_paste_falls_back_to_the_ansi_v_position(monkeypatch):
    monkeypatch.setattr(macos_paste, "_v_keycode", None)
    assert macos_paste.paste_keycode() == 0x09


def test_post_command_v_never_reads_the_layout(monkeypatch):
    import importlib

    # The autouse conftest stub replaces post_command_v; reload for the real
    # one. Quartz is faked below, so no real key event can leave the process.
    real = importlib.reload(macos_paste)
    posted = []
    quartz = ModuleType("Quartz")
    quartz.kCGEventFlagMaskCommand = 0x100000
    quartz.kCGHIDEventTap = 0
    quartz.CGEventCreateKeyboardEvent = lambda _src, code, down: {"code": code, "down": down}
    quartz.CGEventSetFlags = lambda event, flags: event.__setitem__("flags", flags)
    quartz.CGEventPost = lambda _tap, event: posted.append(event)
    monkeypatch.setitem(__import__("sys").modules, "Quartz", quartz)
    monkeypatch.setattr(real, "_v_keycode", 0x2F)

    def _boom(_c):
        raise AssertionError("layout read from the paste path")

    monkeypatch.setattr(real, "keycode_for_character", _boom)
    _in_thread(real.post_command_v)
    assert [e["code"] for e in posted] == [0x37, 0x2F, 0x2F, 0x37]


def _app(state, gen=7, text="hello world"):
    errors, clip = [], []
    app = SimpleNamespace(
        app_state=state,
        session_generation=gen,
        _pasting_text=text,
        _paste_watchdog_job="job",
        executor="stuck-executor",
        clipboard_clear=lambda: clip.clear(),
        clipboard_append=lambda t: clip.append(t),
        on_error=lambda msg, g: errors.append((msg, g)),
    )
    return app, errors, clip


@pytest.mark.parametrize("is_windows, keys", [(False, "⌘V"), (True, "Ctrl+V")])
def test_hung_paste_hands_the_text_over_and_resets(monkeypatch, is_windows, keys):
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", is_windows)
    app, errors, clip = _app(wayfinder_main.AppState.PASTING)
    wayfinder_main.WayfinderApp._on_paste_timeout(app, 7)
    assert clip == ["hello world"]
    assert app.session_generation == 8  # the stuck worker's late result is dropped
    assert app.executor != "stuck-executor"  # later pastes don't queue behind it
    assert errors == [(f"The paste didn't finish. Your text is on the clipboard: press {keys}.", 8)]


@pytest.mark.parametrize("state,gen", [
    (wayfinder_main.AppState.IDLE, 7),      # the paste finished in time
    (wayfinder_main.AppState.PASTING, 6),   # a newer dictation took over
])
def test_watchdog_is_a_no_op_once_the_paste_moved_on(state, gen):
    app, errors, clip = _app(state)
    wayfinder_main.WayfinderApp._on_paste_timeout(app, gen)
    assert errors == [] and clip == [] and app.executor == "stuck-executor"

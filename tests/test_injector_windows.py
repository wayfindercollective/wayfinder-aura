"""Unit tests for the Windows SendInput text-injection adapter.

Windows-only: the adapter loads user32/kernel32 at import, so every test imports
it lazily and the whole module is skipped off Windows. All OS calls are mocked —
no test sends a real keystroke or touches the real clipboard.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows-only SendInput adapter"
)


def _load():
    from wayfinder.core import injector_windows as w

    return w


# ---------------------------------------------------------------------------
# Character -> INPUT conversion
# ---------------------------------------------------------------------------


def test_ascii_char_is_unicode_down_up_pair():
    w = _load()
    inputs = w._char_inputs("a")
    assert len(inputs) == 2
    down, up = inputs
    assert down.u.ki.dwFlags & w.KEYEVENTF_UNICODE
    assert not (down.u.ki.dwFlags & w.KEYEVENTF_KEYUP)
    assert down.u.ki.wScan == ord("a")
    assert up.u.ki.dwFlags & w.KEYEVENTF_KEYUP


def test_newline_becomes_enter_keypress_not_unicode():
    w = _load()
    inputs = w._char_inputs("\n")
    assert len(inputs) == 2
    assert inputs[0].u.ki.wVk == w.VK_RETURN
    assert not (inputs[0].u.ki.dwFlags & w.KEYEVENTF_UNICODE)


def test_carriage_return_is_dropped_so_crlf_is_one_enter():
    w = _load()
    assert w._char_inputs("\r") == []
    total = sum(len(w._char_inputs(ch)) for ch in "\r\n")
    assert total == 2  # CRLF -> a single Enter


def test_tab_becomes_tab_keypress():
    w = _load()
    inputs = w._char_inputs("\t")
    assert len(inputs) == 2
    assert inputs[0].u.ki.wVk == w.VK_TAB


def test_astral_codepoint_uses_surrogate_pair():
    w = _load()
    # U+1F600 splits into two UTF-16 code units -> two down/up pairs.
    assert len(w._char_inputs("\U0001F600")) == 4


def test_injected_events_carry_signature_tag():
    w = _load()
    down = w._char_inputs("a")[0]
    assert down.u.ki.dwExtraInfo == w._WAYFINDER_INJECT_SIGNATURE


# ---------------------------------------------------------------------------
# inject_text_windows dispatch + safety gates
# ---------------------------------------------------------------------------


def test_inject_text_sends_expected_event_count(monkeypatch):
    w = _load()
    captured = {"count": 0, "calls": 0}

    def fake_send_input(n, arr, cb):
        captured["count"] += n
        captured["calls"] += 1
        return n  # success

    monkeypatch.setattr(w._user32, "SendInput", fake_send_input)
    monkeypatch.setattr(w._user32, "GetForegroundWindow", lambda: 1)
    monkeypatch.setattr(w, "modifiers_held_windows", lambda: False)

    w.inject_text_windows("hi", typing_speed="instant")
    # "hi" -> 2 chars * (down+up) = 4 events, one batched call.
    assert captured["count"] == 4
    assert captured["calls"] == 1


def test_empty_text_sends_nothing(monkeypatch):
    w = _load()
    calls = []
    monkeypatch.setattr(w._user32, "SendInput", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(w._user32, "GetForegroundWindow", lambda: 1)
    monkeypatch.setattr(w, "modifiers_held_windows", lambda: False)
    w.inject_text_windows("", typing_speed="instant")
    assert calls == []


def test_inject_fails_closed_without_foreground_window(monkeypatch):
    w = _load()
    monkeypatch.setattr(w._user32, "GetForegroundWindow", lambda: 0)
    monkeypatch.setattr(w, "modifiers_held_windows", lambda: False)
    with pytest.raises(w.InjectionError):
        w.inject_text_windows("hello", typing_speed="instant")


def test_require_modifier_release_raises_when_still_held(monkeypatch):
    w = _load()
    monkeypatch.setattr(w, "wait_for_modifier_release_windows", lambda *a, **k: False)
    with pytest.raises(w.InjectionError):
        w.require_modifier_release_windows()


def test_inject_aborts_when_modifier_held(monkeypatch):
    w = _load()
    sent = []
    monkeypatch.setattr(w._user32, "SendInput", lambda *a: sent.append(a) or 0)
    monkeypatch.setattr(w._user32, "GetForegroundWindow", lambda: 1)
    # Modifier never releases; the bounded wait reports failure immediately.
    monkeypatch.setattr(w, "wait_for_modifier_release_windows", lambda *a, **k: False)
    with pytest.raises(w.InjectionError):
        w.inject_text_windows("hello", typing_speed="instant")
    assert sent == []  # nothing typed


def test_send_raises_on_partial_injection(monkeypatch):
    w = _load()
    # SendInput reports fewer events accepted than submitted (blocked input).
    monkeypatch.setattr(w._user32, "SendInput", lambda n, arr, cb: 0)
    with pytest.raises(w.InjectionError):
        w._send(w._char_inputs("a"))


def test_press_enter_sends_return_pair(monkeypatch):
    w = _load()
    captured = {"count": 0}
    monkeypatch.setattr(
        w._user32, "SendInput", lambda n, arr, cb: captured.__setitem__("count", n) or n
    )
    monkeypatch.setattr(w._user32, "GetForegroundWindow", lambda: 1)
    monkeypatch.setattr(w, "modifiers_held_windows", lambda: False)
    w.press_enter_windows()
    assert captured["count"] == 2  # Enter down + up

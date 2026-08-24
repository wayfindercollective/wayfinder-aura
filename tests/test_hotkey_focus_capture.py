"""Settings → Detect in environments with no global listener.

The Wayland Flatpak grants no /dev/input (evdev) and pynput needs X11, so the
old gate refused to arm — with its explanation sent only to the activity log.
Result: a silently dead Detect button in exactly the packaging Flathub ships,
found live on the first Flatpak install test. Detect never needed a global
listener: the user just clicked the button, so the window has focus and the
next key arrives to Tk itself.
"""

from pathlib import Path

import pytest

from wayfinder.hotkeys.dbus import _KEYSYM_BY_CODE, encode_trigger
from wayfinder.hotkeys.tk_capture import (
    KEYSYM_TO_KEYCODE,
    TK_MODIFIER_KEYSYMS,
    captured_payload,
)

MAIN_SRC = Path(__file__).resolve().parent.parent / "wayfinder_main.py"


class TestCapturedPayload:
    @pytest.mark.parametrize("keysym,code", [
        ("F1", 59), ("F3", 61), ("F12", 88),
        ("space", 57), ("Return", 28), ("Pause", 119),
    ])
    def test_mapped_keys_produce_evdev_codes(self, keysym, code):
        assert captured_payload(keysym, 0)["code"] == code

    def test_modifier_state_bits_become_names(self):
        assert captured_payload("F5", 0x1)["modifiers"] == ["shift"]
        assert captured_payload("F5", 0x4)["modifiers"] == ["ctrl"]
        assert captured_payload("F5", 0x8)["modifiers"] == ["alt"]
        assert captured_payload("F5", 0xD)["modifiers"] == ["shift", "ctrl", "alt"]

    @pytest.mark.parametrize("keysym", sorted(TK_MODIFIER_KEYSYMS))
    def test_a_bare_modifier_keeps_waiting(self, keysym):
        assert captured_payload(keysym, 0) is None

    def test_unmapped_keys_keep_waiting(self):
        # Letters have no evdev encoding here — same rule as the pynput path;
        # the 8s Detect timeout is the backstop.
        assert captured_payload("a", 0) is None
        assert captured_payload("XF86AudioPlay", 0) is None
        assert captured_payload("", 0) is None

    def test_map_is_the_portal_encoders_inverse(self):
        """A key Detect-via-focus can capture must be exactly a key the portal
        can express — the map is derived, but pin the property, not the
        implementation."""
        assert KEYSYM_TO_KEYCODE == {v: k for k, v in _KEYSYM_BY_CODE.items()}
        for keysym, code in KEYSYM_TO_KEYCODE.items():
            assert encode_trigger(code, []) == keysym


@pytest.fixture(scope="module")
def src():
    return MAIN_SRC.read_text()


class TestDetectWiring:
    """Structural pins on wayfinder_main.py — the UI cannot run headless, but
    the wiring that resurrects the dead button can be pinned in source."""

    def test_gate_refusal_no_longer_kills_detect(self, src):
        body = src.split("def _start_hotkey_detect", 1)[1]
        body = body.split("\n    def ", 1)[0]
        assert "use_focus_capture = not can_detect" in body
        assert "self.log(gate_message)" not in body

    def test_focus_capture_binding_is_installed_once(self, src):
        body = src.split("def _start_hotkey_detect", 1)[1].split("\n    def ", 1)[0]
        assert 'self.bind("<KeyPress>", self._on_tk_detect_key, add="+")' in body
        # Guarded: Tk unbind(seq) clears EVERY binding on the sequence, so the
        # binding must be persistent, not re-added per Detect.
        assert "_tk_capture_bound" in body

    def test_handler_is_inert_outside_an_armed_session(self, src):
        body = src.split("def _on_tk_detect_key", 1)[1].split("\n    def ", 1)[0]
        assert '_tk_capture_active' in body
        assert 'return None' in body

    def test_handler_stamps_the_generation(self, src):
        body = src.split("def _on_tk_detect_key", 1)[1].split("\n    def ", 1)[0]
        assert 'payload["gen"] = _HOTKEY_CAPTURE.get("gen")' in body

    def test_cancel_disarms_focus_capture(self, src):
        body = src.split("def _cancel_hotkey_detect", 1)[1].split("\n    def ", 1)[0]
        assert "self._tk_capture_active = False" in body

    def test_portal_mode_names_the_real_owner_of_the_binding(self, src):
        body = src.split("def _apply_captured_hotkey", 1)[1].split("\n    def ", 1)[0]
        assert "resolve_hotkey_backend" in body
        assert "System Settings" in body

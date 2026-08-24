"""The compositor's answer is the truth about portal shortcut bindings.

Found live on the first Flatpak test: Settings said the hotkey was Space
while F3 (the binding KDE actually stores) kept working — "super disconnected
and weird" (Peter). The BindShortcuts response and the ShortcutsChanged
signal both carry each shortcut's trigger_description, and the listener was
discarding it, even logging the trigger WE requested as though it were the
binding. These tests pin the repair: parse the compositor's description back
into config's vocabulary, follow it, and never guess when it doesn't parse.
"""

from pathlib import Path

import pytest

from wayfinder.hotkeys.dbus import (
    _KEYSYM_BY_CODE,
    encode_trigger,
    parse_trigger_description,
    triggers_from_bound_shortcuts,
)
from wayfinder.hotkeys.types import EventType

MAIN_SRC = Path(__file__).resolve().parent.parent / "wayfinder_main.py"
DBUS_SRC = Path(__file__).resolve().parent.parent / "src/wayfinder/hotkeys/dbus.py"


class TestParseTriggerDescription:
    @pytest.mark.parametrize("desc,expected", [
        ("F3", (61, [])),
        ("F2", (60, [])),
        ("Space", (57, [])),
        ("Ctrl+Alt+Return", (28, ["ctrl", "alt"])),
        ("Meta+F5", (63, ["super"])),          # KDE writes Meta for Super
        ("Logo+F5", (63, ["super"])),          # the spec's own token
        ("SHIFT+SPACE", (57, ["shift"])),      # case-insensitive
        ("Ctrl+Ctrl+F1", (59, ["ctrl"])),      # deduped
    ])
    def test_descriptions_parse_to_config_vocabulary(self, desc, expected):
        assert parse_trigger_description(desc) == expected

    @pytest.mark.parametrize("desc", [
        "F13",              # key with no config encoding
        "Hyper+F3",         # unknown modifier word
        "F3+",              # trailing separator
        "+F3",              # leading separator
        "", "   ", None, 61,
    ])
    def test_unknown_input_returns_none_never_a_guess(self, desc):
        # A wrong chord displayed confidently is the exact bug this fixes.
        assert parse_trigger_description(desc) is None

    def test_round_trips_the_encoder(self):
        """Anything we can ask the portal for, we can read back."""
        for code in _KEYSYM_BY_CODE:
            for mods in ([], ["ctrl"], ["ctrl", "shift"], ["super"]):
                enc = encode_trigger(code, mods)
                assert parse_trigger_description(enc) == (code, mods), enc


class TestTriggersFromBoundShortcuts:
    def test_extracts_id_to_description(self):
        shortcuts = [
            ("record-toggle", {"trigger_description": "F3"}),
            ("style-toggle", {"trigger_description": "F2"}),
        ]
        assert triggers_from_bound_shortcuts(shortcuts) == {
            "record-toggle": "F3", "style-toggle": "F2"}

    def test_unbound_and_malformed_entries_are_skipped(self):
        shortcuts = [
            ("no-desc", {}),
            ("empty-desc", {"trigger_description": "  "}),
            ("wrong-type", {"trigger_description": 7}),
            "not-a-tuple",
            ("good", {"trigger_description": "F1"}),
        ]
        assert triggers_from_bound_shortcuts(shortcuts) == {"good": "F1"}

    def test_hostile_input_yields_empty_never_raises(self):
        for bad in (None, 42, "string", object()):
            assert triggers_from_bound_shortcuts(bad) == {}


@pytest.fixture(scope="module")
def dbus_src():
    return DBUS_SRC.read_text()


@pytest.fixture(scope="module")
def main_src():
    return MAIN_SRC.read_text()


class TestListenerWiring:

    def test_bind_response_emits_portal_triggers(self, dbus_src):
        assert "EventType.PORTAL_TRIGGERS" in dbus_src
        body = dbus_src.split("bcode, bresults = bound", 1)[1]
        assert "triggers_from_bound_shortcuts(bresults" in body.split("elif bcode", 1)[0]

    def test_registered_log_reports_the_actual_trigger(self, dbus_src):
        # Logging the REQUESTED trigger as though it were the binding was
        # half of the lie.
        body = dbus_src.split("Shortcut registered", 1)[1][:200]
        assert "actual or spec.trigger" in body

    def test_shortcuts_changed_is_subscribed_and_session_filtered(self, dbus_src):
        assert '"ShortcutsChanged"' in dbus_src
        body = dbus_src.split("def _on_shortcuts_changed", 1)[1].split("\n        shortcuts_changed_sub", 1)[0]
        assert 'session_handle != state["session_handle"]' in body

    def test_new_subscription_is_cleaned_up(self, dbus_src):
        cleanup = dbus_src.split("signal_unsubscribe", 1)[0].rsplit("for sub in", 1)[1]
        assert "shortcuts_changed_sub" in cleanup


class TestUIWiring:
    def test_portal_triggers_event_is_dispatched(self, main_src):
        assert "elif event_type == EventType.PORTAL_TRIGGERS:" in main_src
        assert "self._on_portal_triggers(data)" in main_src

    def test_sync_updates_config_and_saves(self, main_src):
        body = main_src.split("def _on_portal_triggers", 1)[1].split("\n    def ", 1)[0]
        for needle in ('"record-toggle"', '"style-toggle"',
                       "parse_trigger_description", "save_config(self.config)"):
            assert needle in body

    def test_unparsed_descriptions_never_touch_config(self, main_src):
        body = main_src.split("def _on_portal_triggers", 1)[1].split("\n    def ", 1)[0]
        assert "if parsed is None:" in body
        assert body.index("if parsed is None:") < body.index("save_config")

    def test_captions_exist_for_both_hotkeys(self, main_src):
        assert "self._portal_caption_record = ctk.CTkLabel(" in main_src
        assert "self._portal_caption_style = ctk.CTkLabel(" in main_src

    def test_a_desktop_owned_press_during_detect_explains_itself(self, main_src):
        # The silent `return` while armed left the user pressing their hotkey
        # at a listening button with no reaction at all.
        body = main_src.split('if _HOTKEY_CAPTURE.get("armed"):', 1)[1]
        body = body.split("if time.time() < float", 1)[0]
        assert "System Settings" in body
        assert "self._reset_detect_button(target)" in body

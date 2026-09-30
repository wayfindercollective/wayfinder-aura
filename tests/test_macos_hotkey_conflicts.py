"""macOS hotkey conflict captions (read-only checks, no system changes)."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.utils import macos_hotkey_conflicts as hc  # noqa: E402


def _conflict(code, mods, hotkeys=None, installed=()):
    with patch.object(hc.sys, "platform", "darwin"):
        return hc.conflict_for(code, mods, hotkeys=hotkeys or {}, installed=list(installed))


def test_spotlight():
    assert "Spotlight" in _conflict(57, ["super"])


def test_spotlight_turned_off_is_clear():
    assert _conflict(57, ["super"], hotkeys={"64": {"enabled": False}}) is None


def test_input_sources_follow_the_system_setting():
    assert "input sources" in _conflict(57, ["ctrl"])
    assert _conflict(57, ["ctrl"], hotkeys={"60": {"enabled": False}}) is None


def test_option_space_only_warns_when_a_launcher_is_installed():
    assert _conflict(57, ["alt"]) is None
    assert "Raycast" in _conflict(57, ["alt"], installed=["Raycast"])


def test_fn_explains_keyboard_support():
    assert "Right Option" in _conflict(57, ["fn"])


def test_voiceover_modifier():
    assert "VoiceOver" in _conflict(67, ["ctrl", "alt"])


def test_right_option_and_plain_keys_are_clear():
    assert _conflict(100, []) is None
    assert _conflict(67, []) is None


def test_never_answers_off_macos():
    with patch.object(hc.sys, "platform", "linux"):
        assert hc.conflict_for(57, ["super"], hotkeys={}, installed=[]) is None

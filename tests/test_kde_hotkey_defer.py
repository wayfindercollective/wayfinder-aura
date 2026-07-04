"""Unit tests for kde_record_shortcut_active — the check that makes the app
defer its record hotkey to KDE.

When KDE's kglobalshortcutsrc binds our record action to a real key, KWin grabs
that key at the compositor level. The app must then NOT also read it via the
passive evdev listener: evdev can't consume the key, so a shared binding leaks
F3 into the focused window (the browser's Find bar, which eats the dictation).
kde_record_shortcut_active parses KDE's own config to detect this. Reading KDE's
file — never rewritten by the app's save_config — is what makes the deferral
immune to the in-app hotkey setting being reset back to F3.

Importing wayfinder_main pulls in customtkinter/numpy, so the file is skipped
where those deps are absent (matching tests/test_dropdown_geometry.py).
"""

import pytest

pytest.importorskip("customtkinter")

from wayfinder_main import kde_record_shortcut_active  # noqa: E402


BOUND = (
    "[wayfinder-aura.desktop]\n"
    "_k_friendly_name=Wayfinder Aura\n"
    "cycle-style=F2,none,Cycle Output Style\n"
    "toggle-recording=F3,none,Toggle Recording\n"
)


def test_active_when_bound_to_a_real_key():
    # F3 is the active (first) field → KDE owns it → app should defer.
    assert kde_record_shortcut_active(BOUND) is True


def test_inactive_when_unbound_none():
    # System Settings "unbind" writes `none` as the active field.
    text = "[wayfinder-aura.desktop]\ntoggle-recording=none,none,Toggle Recording\n"
    assert kde_record_shortcut_active(text) is False


def test_inactive_when_active_field_empty():
    text = "[wayfinder-aura.desktop]\ntoggle-recording=,none,Toggle Recording\n"
    assert kde_record_shortcut_active(text) is False


def test_inactive_when_group_absent():
    # No wayfinder group at all → nothing to defer to → evdev stays in charge.
    text = "[org.kde.spectacle.desktop]\n_launch=Print,Print,Launch Spectacle\n"
    assert kde_record_shortcut_active(text) is False


def test_inactive_when_action_absent_in_group():
    text = "[wayfinder-aura.desktop]\ncycle-style=F2,none,Cycle Output Style\n"
    assert kde_record_shortcut_active(text) is False


def test_only_matches_within_the_target_group():
    # A toggle-recording line under a DIFFERENT app's group must not count.
    text = (
        "[some-other-app.desktop]\n"
        "toggle-recording=F3,none,Toggle Recording\n"
        "[wayfinder-aura.desktop]\n"
        "toggle-recording=none,none,Toggle Recording\n"
    )
    assert kde_record_shortcut_active(text) is False


def test_handles_a_non_f_key_binding():
    # e.g. the user rebinds to Meta+R — still a real active shortcut.
    text = "[wayfinder-aura.desktop]\ntoggle-recording=Meta+R,none,Toggle Recording\n"
    assert kde_record_shortcut_active(text) is True


def test_empty_file_is_inactive():
    assert kde_record_shortcut_active("") is False

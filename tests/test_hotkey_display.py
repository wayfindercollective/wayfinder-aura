"""What the UI shows as the global hotkey, when the compositor owns it.

Regression this pins: a Steam Deck Flatpak whose portal bind registered
NOTHING kept advertising "Ctrl+Alt+Space" in the header, in Settings and in the
first-run tour's "your hotkey" step. The chord was a request the compositor had
declined, so nothing happened when the user pressed it.
"""

import pytest

from wayfinder.hotkeys.display import UNBOUND_LABEL, hotkey_label, is_unbound

CONFIG = "Ctrl+Alt+Space"


class TestWithoutThePortal:
    """evdev / pynput: the app owns the binding, so config IS the hotkey."""

    @pytest.mark.parametrize("known,trigger", [
        (False, None), (True, None), (True, ""), (True, "Meta+I"),
    ])
    def test_config_wins_whatever_the_portal_state_says(self, known, trigger):
        assert hotkey_label(
            CONFIG, portal_active=False, portal_known=known, portal_trigger=trigger
        ) == CONFIG


class TestUnderThePortal:
    def test_unreported_shortcut_falls_back_to_config(self):
        """At cold start the portal has not answered yet. Flashing "not set"
        on every launch would be its own lie."""
        assert hotkey_label(
            CONFIG, portal_active=True, portal_known=False, portal_trigger=None
        ) == CONFIG

    def test_not_bound_says_so_and_names_the_fix(self):
        label = hotkey_label(
            CONFIG, portal_active=True, portal_known=True, portal_trigger=None
        )
        assert label == UNBOUND_LABEL
        assert is_unbound(label)
        assert CONFIG not in label
        assert "System Settings" in label

    def test_bound_shows_the_compositors_chord_not_ours(self):
        """The user can edit the chord in the picker; ours was only a request."""
        assert hotkey_label(
            CONFIG, portal_active=True, portal_known=True, portal_trigger="Meta+Insert"
        ) == "Meta+Insert"

    def test_bound_without_a_label_falls_back_to_config(self):
        """Something IS bound and our requested chord is the likeliest thing
        it is — the one case where the old behaviour was right."""
        label = hotkey_label(
            CONFIG, portal_active=True, portal_known=True, portal_trigger=""
        )
        assert label == CONFIG
        assert not is_unbound(label)


def test_is_unbound_only_matches_the_sentinel():
    assert not is_unbound(CONFIG)
    assert not is_unbound("")
    assert not is_unbound("not set")
    assert is_unbound(UNBOUND_LABEL)


class TestAnyEvent:
    """The portal listener's stop event, widened so a user-triggered rebind can
    end one session without signalling app shutdown."""

    def test_set_when_either_is_set(self):
        from threading import Event

        from wayfinder.hotkeys.types import AnyEvent

        app, local = Event(), Event()
        either = AnyEvent(app, local)
        assert either.is_set() is False
        local.set()
        assert either.is_set() is True
        local.clear()
        app.set()
        assert either.is_set() is True

    def test_no_events_is_never_set(self):
        from wayfinder.hotkeys.types import AnyEvent

        assert AnyEvent().is_set() is False

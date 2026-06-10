"""Tests for the Feral GameMode integration."""

import threading
from unittest.mock import patch

import pytest

from wayfinder.integrations import gamemode


@pytest.fixture(autouse=True)
def _reset_pause_state():
    """Each test starts from a clean global pause counter."""
    gamemode._set_active_count(0)
    yield
    gamemode._set_active_count(0)


class TestPauseFlag:
    def test_initial_state_is_unpaused(self):
        assert gamemode.is_hotkeys_paused() is False

    def test_set_active_count_pauses(self):
        gamemode._set_active_count(1)
        assert gamemode.is_hotkeys_paused() is True

    def test_set_active_count_zero_unpauses(self):
        gamemode._set_active_count(3)
        gamemode._set_active_count(0)
        assert gamemode.is_hotkeys_paused() is False

    def test_bump_increments_and_pauses(self):
        assert gamemode._bump(+1) == 1
        assert gamemode.is_hotkeys_paused() is True

    def test_bump_clamps_at_zero(self):
        # Out-of-order GameUnregistered (or daemon restart) must not pin the
        # counter at -1, which would never re-pause on the next GameRegistered.
        assert gamemode._bump(-5) == 0
        assert gamemode.is_hotkeys_paused() is False

    def test_bump_negative_after_positive(self):
        gamemode._bump(+2)
        assert gamemode.is_hotkeys_paused() is True
        gamemode._bump(-1)
        assert gamemode.is_hotkeys_paused() is True  # still 1 active
        gamemode._bump(-1)
        assert gamemode.is_hotkeys_paused() is False

    def test_multi_thread_increment_is_lock_safe(self):
        # 20 threads each bump +5 — final count must be exactly 100.
        threads = [threading.Thread(target=lambda: [gamemode._bump(+1) for _ in range(5)])
                   for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No public getter for the raw count; pause is True iff > 0.
        assert gamemode.is_hotkeys_paused() is True
        # Drain — 100 decrements should land exactly at 0.
        for _ in range(100):
            gamemode._bump(-1)
        assert gamemode.is_hotkeys_paused() is False


class TestListenerGuards:
    def test_returns_silently_when_deps_missing(self, caplog):
        # Simulate the no-dbus environment by patching the module-level flag.
        stop = threading.Event()
        stop.set()  # listener should never enter the mainloop anyway
        with patch.object(gamemode, "_GAMEMODE_DEPS_OK", False):
            # Should return cleanly without raising — gracefully degraded.
            gamemode.gamemode_pause_listener(stop, log_callback=lambda m: None)
        # Pause flag stays False — gracefully degraded.
        assert gamemode.is_hotkeys_paused() is False

    def test_is_gamemode_available_reflects_deps(self):
        with patch.object(gamemode, "_GAMEMODE_DEPS_OK", True):
            assert gamemode.is_gamemode_available() is True
        with patch.object(gamemode, "_GAMEMODE_DEPS_OK", False):
            assert gamemode.is_gamemode_available() is False

    def test_log_callback_failure_does_not_crash(self):
        # A misbehaving log callback (e.g. UI thread already shut down) must
        # never propagate out of the listener and kill the daemon thread.
        stop = threading.Event()
        stop.set()
        with patch.object(gamemode, "_GAMEMODE_DEPS_OK", False):
            def bad_log(_msg):
                raise RuntimeError("UI gone")
            # Must not raise.
            gamemode.gamemode_pause_listener(stop, log_callback=bad_log)


class TestDefaultsAreSuperFKey:
    """Smoke check: DEFAULT_CONFIG ships modifiered F-keys, not bare F-keys.

    Bare F-keys collide with countless game keybinds. New users get Super+F2 /
    Super+F3 by default — almost no game binds Super+combos because compositors
    reserve them.
    """

    def test_defaults(self):
        from wayfinder.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["hotkey_key"] == 60  # F2
        assert DEFAULT_CONFIG["hotkey_modifiers"] == ["super"]
        assert DEFAULT_CONFIG["style_toggle_key"] == 61  # F3
        assert DEFAULT_CONFIG["style_toggle_modifiers"] == ["super"]

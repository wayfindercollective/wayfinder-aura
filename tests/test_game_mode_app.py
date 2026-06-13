"""Tests for the app-side Game Mode pieces: mode detection, the toggle/mode
markers (platform.py), and the audio cue module (feedback/audio.py).

These are headless and don't touch Tk or any audio device — the audio tests
assert the silent-no-op contract rather than actually playing sound.
"""

import sys
import time

import pytest

from wayfinder.utils import platform as p


# =============================================================================
# Mode marker + is_game_mode
# =============================================================================

@pytest.fixture
def runtime_dir(temp_dir, monkeypatch):
    """Point the app's runtime dir at a temp dir and force a clean Linux env."""
    rt = temp_dir / "run"
    rt.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    monkeypatch.setattr(sys, "platform", "linux")
    # Neutralize the env fallback so marker behavior is tested in isolation.
    monkeypatch.delenv("GAMESCOPE_WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    return p.get_runtime_dir()  # <rt>/wayfinder-aura


def _write_mode(runtime_dir, value):
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "mode").write_text(value)


def test_read_mode_marker_game(runtime_dir):
    _write_mode(runtime_dir, "game")
    assert p.read_mode_marker() == "game"


def test_read_mode_marker_desktop(runtime_dir):
    _write_mode(runtime_dir, "desktop\n")  # tolerate trailing newline
    assert p.read_mode_marker() == "desktop"


def test_read_mode_marker_absent(runtime_dir):
    assert p.read_mode_marker() is None


def test_read_mode_marker_garbage(runtime_dir):
    _write_mode(runtime_dir, "banana")
    assert p.read_mode_marker() is None


def test_is_game_mode_marker_game(runtime_dir):
    _write_mode(runtime_dir, "game")
    assert p.is_game_mode() is True


def test_is_game_mode_marker_desktop_overrides_env(runtime_dir, monkeypatch):
    # Even with a gamescope-looking env, an explicit 'desktop' marker wins.
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    _write_mode(runtime_dir, "desktop")
    assert p.is_game_mode() is False


def test_is_game_mode_env_fallback_xdg(runtime_dir, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "gamescope")
    assert p.is_game_mode() is True


def test_is_game_mode_env_fallback_gamescope_display(runtime_dir, monkeypatch):
    monkeypatch.setenv("GAMESCOPE_WAYLAND_DISPLAY", "gamescope-0")
    assert p.is_game_mode() is True


def test_is_game_mode_desktop_default(runtime_dir):
    # No marker, KDE desktop, no gamescope env -> Desktop.
    assert p.is_game_mode() is False


def test_is_game_mode_non_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert p.is_game_mode() is False


def test_is_game_mode_wait_for_marker(runtime_dir):
    """wait_secs should pick up a marker that appears slightly after the call starts."""
    import threading

    def _delayed_write():
        time.sleep(0.25)
        _write_mode(runtime_dir, "game")

    threading.Thread(target=_delayed_write, daemon=True).start()
    assert p.is_game_mode(wait_secs=2.0) is True


# =============================================================================
# Toggle marker write
# =============================================================================

@pytest.fixture
def config_dir(temp_dir, monkeypatch):
    cfg = temp_dir / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setattr(sys, "platform", "linux")
    return p.get_config_dir()  # <cfg>/wayfinder-aura


def test_write_game_mode_marker_true(config_dir):
    p.write_game_mode_marker(True)
    assert (config_dir / "game-mode-dictation").read_text() == "1"


def test_write_game_mode_marker_false(config_dir):
    p.write_game_mode_marker(False)
    assert (config_dir / "game-mode-dictation").read_text() == "0"


def test_write_game_mode_marker_atomic_no_temp_left(config_dir):
    p.write_game_mode_marker(True)
    # The temp file used for the atomic replace must not linger.
    assert not (config_dir / ".game-mode-dictation.tmp").exists()


def test_write_game_mode_marker_never_raises(monkeypatch):
    # Unwritable config dir must not raise (UI/startup must survive).
    monkeypatch.setattr(p, "get_config_dir", lambda: __import__("pathlib").Path("/proc/nonexistent/wayfinder"))
    p.write_game_mode_marker(True)  # should swallow the error


# =============================================================================
# Audio cues — silent no-op contract
# =============================================================================

def test_play_cue_unknown_name_is_noop():
    from wayfinder.feedback import audio
    # Should return immediately without spawning anything / raising.
    audio.play_cue("not-a-cue")


def test_play_cue_returns_immediately_and_silently(monkeypatch):
    from wayfinder.feedback import audio

    # Force the playback backend to be unavailable; the cue must stay silent.
    fake_sd = type(sys)("sounddevice")
    def _boom(*a, **k):
        raise RuntimeError("no audio device")
    fake_sd.OutputStream = _boom
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)

    audio.play_cue("start")  # spawns a daemon thread that will hit _boom and swallow it
    # Give the worker a moment; the test passes as long as nothing propagates.
    time.sleep(0.1)


def test_audio_load_missing_returns_none():
    from wayfinder.feedback import audio
    assert audio._load("definitely-missing-cue") is None


def test_audio_load_real_cue_decodes():
    """The shipped cue WAVs should decode to (samples, samplerate)."""
    pytest.importorskip("numpy")
    from wayfinder.feedback import audio
    result = audio._load("start")
    assert result is not None
    samples, sr = result
    assert sr > 0 and len(samples) > 0


def test_prewarm_never_raises():
    from wayfinder.feedback import audio
    audio.prewarm()

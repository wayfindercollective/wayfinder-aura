"""Unit tests for the three lightweight Game Mode fixes:

1. Rumble pulse helper (silent no-op without FF device)
2. inject_text type→paste fallback under Game Mode only
3. effective_asr_config light profile (runtime only, never mutates saved config)

These drive the *shipped* entry points — no reimplementation in the tests.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.core.gm_asr import effective_asr_config
from wayfinder.core.injector import (
    InjectionError,
    inject_text,
    inject_text_clipboard_paste,
)
from wayfinder.feedback import play_game_mode_cue, pulse_for_cue
from wayfinder.feedback import rumble as rumble_mod


# =============================================================================
# 1. Rumble — silent no-op contract (real shipped API)
# =============================================================================


class TestRumbleNoop:
    def test_unknown_cue_is_noop(self):
        pulse_for_cue("not-a-cue")  # must not raise / not spawn useful work

    def test_pulse_for_cue_returns_immediately_without_device(self, monkeypatch):
        """With no FF device, the worker thread swallows failure silently."""
        monkeypatch.setattr(rumble_mod, "_find_ff_device", lambda: None)
        # Also force import path failures inside worker to stay silent.
        pulse_for_cue("start")
        pulse_for_cue("stop")
        pulse_for_cue("done")
        pulse_for_cue("error")
        time.sleep(0.15)  # let daemon threads finish

    def test_pulse_blocking_swallows_upload_errors(self, monkeypatch):
        class BoomDev:
            name = "Fake Pad"
            def capabilities(self):
                return {}
            def upload_effect(self, *a, **k):
                raise OSError("no FF")
            def write(self, *a, **k):
                raise OSError("no write")
            def erase_effect(self, *a, **k):
                pass
            def close(self):
                pass

        monkeypatch.setattr(rumble_mod, "_find_ff_device", lambda: BoomDev())
        rumble_mod._pulse_blocking("start")  # must not raise

    def test_play_game_mode_cue_calls_audio_and_rumble(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "wayfinder.feedback.play_cue",
            lambda name: calls.append(("audio", name)),
        )
        monkeypatch.setattr(
            "wayfinder.feedback.pulse_for_cue",
            lambda name: calls.append(("rumble", name)),
        )
        # Import after patch targets are set — play_game_mode_cue uses the
        # names bound in the feedback package.
        from wayfinder import feedback as fb

        monkeypatch.setattr(fb, "play_cue", lambda name: calls.append(("audio", name)))
        monkeypatch.setattr(fb, "pulse_for_cue", lambda name: calls.append(("rumble", name)))
        fb.play_game_mode_cue("done")
        assert ("audio", "done") in calls
        assert ("rumble", "done") in calls

    def test_all_lifecycle_cues_accepted(self, monkeypatch):
        monkeypatch.setattr(rumble_mod, "_find_ff_device", lambda: None)
        for name in ("start", "stop", "done", "error"):
            pulse_for_cue(name)
        time.sleep(0.1)


# =============================================================================
# 2. Injection — Game Mode type→paste fallback
# =============================================================================


class TestInjectGameModePasteFallback:
    def test_desktop_type_failure_does_not_paste(self):
        """game_mode=False keeps historical behavior: type failure propagates."""
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.core.injector._inject_text_type_linux",
                   side_effect=InjectionError("type failed")) as mock_type, \
             patch("wayfinder.core.injector.inject_text_clipboard_paste") as mock_paste:
            mock_sys.platform = "linux"
            with pytest.raises(InjectionError, match="type failed"):
                inject_text("hello", game_mode=False, paste_fallback=True)
            mock_type.assert_called_once()
            mock_paste.assert_not_called()

    def test_game_mode_type_failure_uses_paste(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.core.injector._inject_text_type_linux",
                   side_effect=InjectionError("type failed")) as mock_type, \
             patch("wayfinder.core.injector.inject_text_clipboard_paste") as mock_paste:
            mock_sys.platform = "linux"
            inject_text("hello world", game_mode=True, paste_fallback=True)
            mock_type.assert_called_once()
            mock_paste.assert_called_once_with("hello world")

    def test_game_mode_paste_fallback_disabled_raises(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.core.injector._inject_text_type_linux",
                   side_effect=InjectionError("type failed")), \
             patch("wayfinder.core.injector.inject_text_clipboard_paste") as mock_paste:
            mock_sys.platform = "linux"
            with pytest.raises(InjectionError, match="type failed"):
                inject_text("hello", game_mode=True, paste_fallback=False)
            mock_paste.assert_not_called()

    def test_game_mode_happy_type_skips_paste(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.core.injector._inject_text_type_linux") as mock_type, \
             patch("wayfinder.core.injector.inject_text_clipboard_paste") as mock_paste:
            mock_sys.platform = "linux"
            inject_text("ok", game_mode=True, paste_fallback=True)
            mock_type.assert_called_once()
            mock_paste.assert_not_called()

    def test_clipboard_paste_linux_writes_and_sends_ctrl_v(self):
        """Drive the real clipboard-paste entry with mocked I/O tools."""
        write_cmds = []
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.utils.platform.get_text_injector", return_value="xdotool"), \
             patch("wayfinder.core.injector.shutil.which", return_value="/usr/bin/tool"), \
             patch("wayfinder.core.injector._clipboard_read_linux", side_effect=["old", "hi"]), \
             patch("wayfinder.core.injector._clipboard_write_linux") as mock_write, \
             patch("wayfinder.core.injector._send_ctrl_v_linux") as mock_v, \
             patch("wayfinder.core.injector.time.sleep"):
            mock_sys.platform = "linux"
            inject_text_clipboard_paste("hi")
            assert mock_write.call_args_list[0][0][0] == "hi"
            mock_v.assert_called_once_with("xdotool")
            # restore path should write old clipboard back
            assert mock_write.call_args_list[-1][0][0] == "old"

    def test_send_ctrl_v_xdotool_command_shape(self):
        from wayfinder.core.injector import _send_ctrl_v_linux

        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.core.injector.subprocess.run", return_value=result) as mock_run:
            _send_ctrl_v_linux("xdotool")
        cmd = mock_run.call_args[0][0]
        assert cmd[:2] == ["xdotool", "key"]
        assert "ctrl+v" in cmd


# =============================================================================
# 3. Light ASR profile — pure effective_asr_config
# =============================================================================


class TestEffectiveAsrConfig:
    def _desktop_cfg(self, **overrides):
        cfg = {
            "model_path": "/models/ggml-large-v3-turbo.bin",
            "accuracy_mode": "high",
            "use_gpu": True,
            "threads": 8,
            "transcription_backend": "whisper_cpp",
            "game_mode_light_asr": True,
            "game_mode_accuracy_mode": "fast",
            "game_mode_use_gpu": False,
            "game_mode_model_path": "",
            "game_mode_faster_whisper_model": "base",
        }
        cfg.update(overrides)
        return cfg

    def test_inactive_game_mode_returns_user_settings(self):
        cfg = self._desktop_cfg()
        out = effective_asr_config(cfg, game_mode_active=False)
        assert out["model_path"] == cfg["model_path"]
        assert out["accuracy_mode"] == "high"
        assert out["use_gpu"] is True
        assert out["threads"] == 8
        # Input not mutated
        assert cfg["accuracy_mode"] == "high"

    def test_active_gm_applies_fast_and_cpu(self, tmp_path):
        tiny = tmp_path / "ggml-tiny.en.bin"
        tiny.write_bytes(b"fake")
        cfg = self._desktop_cfg(model_path=str(tmp_path / "ggml-large-v3-turbo.bin"))
        out = effective_asr_config(cfg, game_mode_active=True)
        assert out["accuracy_mode"] == "fast"
        assert out["use_gpu"] is False
        assert out["threads"] == 2  # capped
        assert out["model_path"] == str(tiny)
        # Original config untouched
        assert cfg["model_path"].endswith("ggml-large-v3-turbo.bin")
        assert cfg["accuracy_mode"] == "high"
        assert cfg["use_gpu"] is True

    def test_light_asr_disabled_keeps_desktop(self, tmp_path):
        tiny = tmp_path / "ggml-tiny.en.bin"
        tiny.write_bytes(b"fake")
        cfg = self._desktop_cfg(
            model_path=str(tmp_path / "ggml-large-v3-turbo.bin"),
            game_mode_light_asr=False,
        )
        out = effective_asr_config(cfg, game_mode_active=True)
        assert out["model_path"] == cfg["model_path"]
        assert out["accuracy_mode"] == "high"
        assert out["use_gpu"] is True

    def test_explicit_game_mode_model_path_wins(self, tmp_path):
        explicit = tmp_path / "custom-light.bin"
        explicit.write_bytes(b"x")
        (tmp_path / "ggml-tiny.en.bin").write_bytes(b"y")
        cfg = self._desktop_cfg(
            model_path=str(tmp_path / "ggml-large-v3-turbo.bin"),
            game_mode_model_path=str(explicit),
        )
        out = effective_asr_config(cfg, game_mode_active=True)
        assert out["model_path"] == str(explicit)

    def test_faster_whisper_uses_light_size(self):
        cfg = self._desktop_cfg(
            transcription_backend="faster_whisper",
            faster_whisper_model="large-v3-turbo",
            faster_whisper_compute_type="float16",
        )
        out = effective_asr_config(cfg, game_mode_active=True)
        assert out["faster_whisper_model"] == "base"
        assert out["use_gpu"] is False
        assert out["faster_whisper_compute_type"] == "int8"
        assert cfg["faster_whisper_model"] == "large-v3-turbo"  # not mutated

    def test_cloud_backend_not_forced_to_local_model(self):
        cfg = self._desktop_cfg(
            transcription_backend="groq_whisper",
            model_path="/models/ggml-large-v3-turbo.bin",
        )
        out = effective_asr_config(cfg, game_mode_active=True)
        # Still light accuracy/threads, but model_path left for local path unused
        assert out["accuracy_mode"] == "fast"
        assert out["transcription_backend"] == "groq_whisper"

    def test_no_light_file_keeps_heavy_path_with_fast_overrides(self, tmp_path, monkeypatch):
        heavy = tmp_path / "ggml-large-v3-turbo.bin"
        heavy.write_bytes(b"heavy")
        # Isolate from host model dirs so "no light weight on disk" is real.
        monkeypatch.setattr(
            "wayfinder.core.gm_asr.IS_FLATPAK",
            False,
        )
        # Point HOME at empty dir so ~/whisper.cpp and ~/.local/share are empty.
        empty_home = tmp_path / "home"
        empty_home.mkdir()
        monkeypatch.setenv("HOME", str(empty_home))
        # Also expanduser uses HOME
        cfg = self._desktop_cfg(model_path=str(heavy))
        out = effective_asr_config(cfg, game_mode_active=True)
        assert out["model_path"] == str(heavy)
        assert out["accuracy_mode"] == "fast"
        assert out["use_gpu"] is False

    def test_default_config_has_gm_keys(self):
        from wayfinder.config import DEFAULT_CONFIG

        for key in (
            "game_mode_dictation",
            "game_mode_light_asr",
            "game_mode_accuracy_mode",
            "game_mode_use_gpu",
            "game_mode_model_path",
            "game_mode_faster_whisper_model",
            "game_mode_paste_fallback",
        ):
            assert key in DEFAULT_CONFIG, f"missing DEFAULT_CONFIG key: {key}"

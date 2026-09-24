"""A model the user downloads with Get/Download becomes the active model for its
role right away — no Installed tab, no Save & Apply — but never mid-dictation."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import wayfinder_main as wm

WApp = wm.WayfinderApp
TURBO = "ggml-large-v3-turbo-q5_0.bin"
GEMMA = "google_gemma-3-1b-it-Q4_K_M.gguf"


class _Var:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ActivationApp:
    """Stub self carrying exactly the surface the activation helpers touch."""

    _activate_downloaded_model = WApp._activate_downloaded_model
    _apply_downloaded_model = WApp._apply_downloaded_model
    _flush_pending_model_activations = WApp._flush_pending_model_activations
    _set_active_whisper_model = WApp._set_active_whisper_model
    _cleanup_model_is_unlocked = WApp._cleanup_model_is_unlocked
    get_model_display = WApp.get_model_display

    def __init__(self, *, ultra: bool = True, state=wm.AppState.IDLE, config=None):
        self.app_state = state
        self.feature_gate = SimpleNamespace(has_feature=lambda _feature: ultra)
        self.config = dict(config or {})
        self.logs: list[str] = []
        self.model_btn = MagicMock()
        self.gpu_var = _Var(True)
        self._maybe_show_setup_cue = MagicMock()
        self._update_compatibility_banner = MagicMock()

    def log(self, message):
        self.logs.append(message)


@pytest.fixture
def env(tmp_path, monkeypatch):
    saved: list[dict] = []
    released: list[bool] = []
    monkeypatch.setattr(wm, "save_config", lambda cfg: saved.append(dict(cfg)))
    monkeypatch.setattr(wm, "_release_cleanup_residency", lambda: released.append(True))
    home = tmp_path / "home"
    whisper_dir = home / ".var" / "app" / "x" / "data" / "wayfinder-aura" / "whisper-models"
    llm_dir = whisper_dir.parent / "llm-models"
    whisper_dir.mkdir(parents=True)
    llm_dir.mkdir(parents=True)
    # Path.home()/expanduser read HOME on POSIX but USERPROFILE on Windows.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    turbo = whisper_dir / TURBO
    turbo.write_bytes(b"turbo")
    gemma = llm_dir / GEMMA
    gemma.write_bytes(b"gemma")
    return SimpleNamespace(saved=saved, released=released, home=home, turbo=turbo, gemma=gemma)


# --- speech model --------------------------------------------------------------

def test_downloaded_whisper_model_becomes_active_when_idle(env):
    app = ActivationApp()

    outcome = app._activate_downloaded_model("whisper", str(env.turbo))

    assert outcome == "active"
    # Stored like Save & Apply stores it ('~' for the home dir) and persisted.
    assert app.config["model_path"] == "~" + os.sep + str(env.turbo.relative_to(env.home))
    assert Path(os.path.expanduser(app.config["model_path"])) == env.turbo
    assert env.saved and env.saved[-1]["model_path"] == app.config["model_path"]
    app.model_btn.configure.assert_called_with(text="Turbo Q5 (Balanced)")
    assert "✓ Large v3 Turbo Q5 downloaded — now active" in app.logs
    app._maybe_show_setup_cue.assert_called_once()


@pytest.mark.parametrize(
    "busy", [wm.AppState.RECORDING, wm.AppState.PROCESSING, wm.AppState.PASTING]
)
def test_whisper_switch_waits_while_dictating(env, busy):
    app = ActivationApp(state=busy, config={"model_path": "~/old/ggml-base.en.bin"})

    outcome = app._activate_downloaded_model("whisper", str(env.turbo))

    assert outcome == "deferred"
    assert app.config["model_path"] == "~/old/ggml-base.en.bin"  # untouched mid-dictation
    assert env.saved == []
    assert any("when this dictation finishes" in line for line in app.logs)

    # Still busy: flushing early changes nothing.
    app._flush_pending_model_activations()
    assert app.config["model_path"] == "~/old/ggml-base.en.bin"

    refresh = MagicMock()
    app._model_panel_refreshers = {"whisper": refresh}
    app.app_state = wm.AppState.IDLE
    app._flush_pending_model_activations()

    assert app.config["model_path"].endswith(TURBO)
    assert "✓ Large v3 Turbo Q5 is now active" in app.logs
    refresh.assert_called_once()
    # Applied once, not again on the next idle transition.
    app._flush_pending_model_activations()
    assert len(env.saved) == 1


def test_latest_download_per_role_wins_while_busy(env):
    small = env.turbo.parent / "ggml-small.en.bin"
    small.write_bytes(b"small")
    app = ActivationApp(state=wm.AppState.RECORDING)

    app._activate_downloaded_model("whisper", str(small))
    app._activate_downloaded_model("whisper", str(env.turbo))
    app.app_state = wm.AppState.IDLE
    app._flush_pending_model_activations()

    assert app.config["model_path"].endswith(TURBO)
    assert len(env.saved) == 1


def test_locked_whisper_model_is_not_activated(env):
    app = ActivationApp(ultra=False, config={"model_path": "/m/ggml-base.en.bin"})

    assert app._activate_downloaded_model("whisper", str(env.turbo)) == "skipped"
    assert app.config["model_path"] == "/m/ggml-base.en.bin"
    assert env.saved == []


def test_missing_file_is_not_activated(env):
    app = ActivationApp(config={"model_path": "/m/ggml-base.en.bin"})

    outcome = app._activate_downloaded_model("whisper", str(env.turbo.parent / "ggml-gone.bin"))

    assert outcome == "skipped"
    assert app.config["model_path"] == "/m/ggml-base.en.bin"


def test_free_user_base_download_keeps_cpu_note(env):
    base = env.turbo.parent / "ggml-base.en.bin"
    base.write_bytes(b"base")
    app = ActivationApp(ultra=False)

    assert app._activate_downloaded_model("whisper", str(base)) == "active"
    assert app.config["model_path"].endswith("ggml-base.en.bin")
    assert app.gpu_var.get() is False  # same as Save & Apply for Free


# --- cleanup (LLM) model --------------------------------------------------------

def test_downloaded_cleanup_model_becomes_active_when_idle(env):
    app = ActivationApp(config={"llama_cpp_model_path": "/m/old.gguf"})
    info = {"name": "Gemma 3 1B ⭐", "filename": GEMMA, "requires_feature": None}

    outcome = app._activate_downloaded_model("llm", str(env.gemma), info)

    assert outcome == "active"
    assert app.config["llama_cpp_model_path"] == str(env.gemma)
    assert app.config["llama_cpp_model_requires_feature"] is None
    assert env.saved[-1]["llama_cpp_model_path"] == str(env.gemma)
    assert env.released == [True]  # old resident model dropped, as on manual select
    assert "✓ Gemma 3 1B downloaded — now active" in app.logs
    # The speech model is a different role and stays put.
    assert "model_path" not in app.config


def test_cleanup_switch_never_releases_model_mid_dictation(env):
    app = ActivationApp(state=wm.AppState.PROCESSING, config={"llama_cpp_model_path": "/m/old.gguf"})

    assert app._activate_downloaded_model("llm", str(env.gemma), {"name": "Gemma"}) == "deferred"
    assert env.released == []  # the running cleanup keeps its model
    assert app.config["llama_cpp_model_path"] == "/m/old.gguf"

    app.app_state = wm.AppState.IDLE
    app._flush_pending_model_activations()

    assert app.config["llama_cpp_model_path"] == str(env.gemma)
    assert env.released == [True]


def test_both_roles_queue_independently(env):
    app = ActivationApp(state=wm.AppState.RECORDING)
    app._activate_downloaded_model("whisper", str(env.turbo))
    app._activate_downloaded_model("llm", str(env.gemma), {"name": "Gemma"})

    app.app_state = wm.AppState.IDLE
    app._flush_pending_model_activations()

    assert app.config["model_path"].endswith(TURBO)
    assert app.config["llama_cpp_model_path"] == str(env.gemma)


def test_locked_cleanup_model_is_not_activated(env):
    app = ActivationApp(ultra=False, config={"llama_cpp_model_path": "/m/old.gguf"})
    info = {"name": "Qwen3 4B", "requires_feature": "large_cleanup_models"}

    assert app._activate_downloaded_model("llm", str(env.gemma), info) == "skipped"
    assert app.config["llama_cpp_model_path"] == "/m/old.gguf"
    assert env.released == []


# --- state machine hook ---------------------------------------------------------

def _state_app(pending):
    scheduled: list = []
    app = SimpleNamespace(
        app_state=wm.AppState.PASTING,
        config={"audio_ducking_enabled": False},
        _write_status_breadcrumb=lambda: None,
        update_tray=lambda _state: None,
        log=lambda _m: None,
        after=lambda delay, fn: scheduled.append((delay, fn)),
        _flush_pending_model_activations=MagicMock(),
        _pending_model_activations=pending,
    )
    return app, scheduled


def test_returning_to_idle_applies_queued_downloads():
    app, scheduled = _state_app({"whisper": ("/m/x.bin", None)})

    WApp.update_state(app, wm.AppState.IDLE)

    assert scheduled == [(0, app._flush_pending_model_activations)]


def test_idle_transition_without_queued_downloads_schedules_nothing():
    app, scheduled = _state_app({})

    WApp.update_state(app, wm.AppState.IDLE)

    assert scheduled == []


def test_model_downloader_itself_never_changes_config():
    """Only the user's Get handler activates; the downloader just downloads."""
    import inspect

    source = inspect.getsource(wm.ModelDownloader)
    assert "config[" not in source
    assert "_activate_downloaded_model" not in source

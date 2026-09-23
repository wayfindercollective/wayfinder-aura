"""Flatpak model storage: downloads must land in the persistent data dir, and a
selected model that vanished (or was moved there) must be reported to the user,
never swapped for Base.en silently."""

from __future__ import annotations

from types import SimpleNamespace

import wayfinder_main as wm

TURBO_OLD = "~/.local/share/wayfinder-aura/whisper-models/ggml-large-v3-turbo-q5_0.bin"
BASE_BUNDLED = "/app/share/whisper-models/ggml-base.en.bin"
GEMMA_OLD = "/home/u/.local/share/wayfinder-aura/llm-models/google_gemma-3-1b-it-Q4_K_M.gguf"


def _app():
    logs: list[str] = []
    banners: list[str] = []
    app = SimpleNamespace(log=logs.append, _show_error_banner=banners.append)
    return app, logs, banners


def test_missing_speech_model_is_logged_and_shown_on_dictate_tab(monkeypatch):
    app, logs, banners = _app()
    monkeypatch.setattr(
        "wayfinder.config.consume_model_path_notices",
        lambda: [{
            "kind": "missing", "key": "model_path",
            "path": TURBO_OLD, "replacement": BASE_BUNDLED,
        }],
    )

    wm.WayfinderApp._surface_model_path_notices(app)

    assert len(logs) == 1
    assert "Large v3 Turbo Q5" in logs[0]
    assert "Base (English)" in logs[0]
    assert "Whisper Models" in logs[0]
    assert len(banners) == 1 and "Large v3 Turbo Q5" in banners[0]
    assert not banners[0].startswith("⚠")


def test_missing_cleanup_model_points_at_llm_setting(monkeypatch):
    app, logs, banners = _app()
    monkeypatch.setattr(
        "wayfinder.config.consume_model_path_notices",
        lambda: [{
            "kind": "missing", "key": "llama_cpp_model_path",
            "path": GEMMA_OLD, "replacement": "",
        }],
    )

    wm.WayfinderApp._surface_model_path_notices(app)

    assert "Gemma 3 1B" in logs[0] and "LLM Model" in logs[0]
    assert banners and "Gemma 3 1B" in banners[0]


def test_migration_is_logged_without_an_error_banner(monkeypatch):
    app, logs, banners = _app()
    new = "/home/u/.var/app/x/data/wayfinder-aura/whisper-models/ggml-large-v3-turbo-q5_0.bin"
    monkeypatch.setattr(
        "wayfinder.config.consume_model_path_notices",
        lambda: [{"kind": "migrated", "key": "model_path", "path": TURBO_OLD, "replacement": new}],
    )

    wm.WayfinderApp._surface_model_path_notices(app)

    assert len(logs) == 1 and "persistent" in logs[0] and "Large v3 Turbo Q5" in logs[0]
    assert banners == []


def test_no_notices_is_silent(monkeypatch):
    app, logs, banners = _app()
    monkeypatch.setattr("wayfinder.config.consume_model_path_notices", lambda: [])

    wm.WayfinderApp._surface_model_path_notices(app)

    assert logs == [] and banners == []


def test_notices_are_consumed_once(monkeypatch):
    from wayfinder import config as cfg

    cfg.consume_model_path_notices()
    cfg._add_model_path_notice({"kind": "missing", "key": "model_path", "path": "a", "replacement": ""})
    cfg._add_model_path_notice({"kind": "missing", "key": "model_path", "path": "a", "replacement": ""})
    assert len(cfg.consume_model_path_notices()) == 1  # deduplicated across both loads
    assert cfg.consume_model_path_notices() == []


def test_app_model_dirs_follow_the_platform_helpers(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(wm, "IS_FLATPAK", True)

    assert wm._get_whisper_models_dir() == data / "wayfinder-aura" / "whisper-models"
    assert wm._get_llm_models_dir() == data / "wayfinder-aura" / "llm-models"
    assert wm._whisper_model_search_dirs()[0] == data / "wayfinder-aura" / "whisper-models"


def test_setup_wizard_downloads_into_the_persistent_flatpak_dir(tmp_path, monkeypatch):
    from wayfinder.core import setup

    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(setup, "IS_FLATPAK", True)
    targets: list = []
    monkeypatch.setattr(
        setup, "_download_model_file",
        lambda url, target, *a, **k: targets.append(target),
    )

    class _SyncThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(setup.threading, "Thread", _SyncThread)

    setup.download_whisper_model("base.en", lambda _m: None, lambda _ok, _d: None)

    assert targets == [data / "wayfinder-aura" / "whisper-models" / "ggml-base.en.bin"]


def test_setup_check_finds_model_in_persistent_flatpak_dir(tmp_path, monkeypatch):
    from wayfinder.core import setup

    data = tmp_path / "data"
    base = data / "wayfinder-aura" / "whisper-models" / "ggml-base.en.bin"
    base.parent.mkdir(parents=True)
    base.write_bytes(b"x" * 10)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(setup, "IS_FLATPAK", True)

    status = setup.check_whisper_model({"model_path": str(tmp_path / "gone" / "ggml-base.en.bin")})

    assert status.installed
    assert "ggml-base.en.bin" in status.detail


def test_game_mode_light_model_found_in_persistent_flatpak_dir(tmp_path, monkeypatch):
    from wayfinder.core import gm_asr

    data = tmp_path / "data"
    tiny = data / "wayfinder-aura" / "whisper-models" / "ggml-tiny.en.bin"
    tiny.parent.mkdir(parents=True)
    tiny.write_bytes(b"t")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(gm_asr, "IS_FLATPAK", True)

    # The desktop model lives somewhere else (e.g. a Browse… pick).
    assert gm_asr._pick_light_model_path(str(tmp_path / "other" / "ggml-large-v3.bin"), "") == str(tiny)

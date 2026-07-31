"""Headless checks for UI entitlement boundaries (real unbound methods)."""

from types import SimpleNamespace
from unittest.mock import patch

import wayfinder_main


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _free_app():
    prompts = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        config={"output_tone": "minimal", "use_gpu": False},
        active_tab="dictate",
        gpu_var=_Var(True),
        _show_premium_prompt=lambda feature: prompts.append(feature),
    )
    return app, prompts


def test_free_style_tab_click_opens_ultra_prompt_without_switching():
    app, prompts = _free_app()

    wayfinder_main.WayfinderApp._switch_tab(app, "style")

    assert prompts == ["tone_system"]
    assert app.active_tab == "dictate"


def test_free_style_hotkey_opens_ultra_prompt_without_changing_tone():
    app, prompts = _free_app()

    wayfinder_main.WayfinderApp.on_style_toggle(app)

    assert prompts == ["tone_system"]
    assert app.config["output_tone"] == "minimal"


def test_ultra_style_selection_persists_and_updates_ui(monkeypatch):
    applied = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda feature: feature == "tone_system"),
        config={"output_tone": "minimal", "prompt": "old"},
        log=lambda _message: None,
        _apply_style_to_ui=applied.append,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)

    wayfinder_main.WayfinderApp._set_output_style(app, "professional")

    assert app.config["output_tone"] == "professional"
    assert app.config["prompt"] == wayfinder_main.TONE_PROMPTS["professional"]
    assert applied == ["professional"]


def test_free_strong_and_caricature_toggles_snap_off():
    prompts = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        config={"strong_mode": False, "caricature_mode": False},
        strong_mode_var=_Var(True),
        caricature_mode_var=_Var(True),
        _show_premium_prompt=prompts.append,
    )

    wayfinder_main.WayfinderApp._on_strong_mode_toggled(app)
    wayfinder_main.WayfinderApp._set_caricature_mode(app, True)

    assert app.strong_mode_var.get() is False
    assert app.caricature_mode_var.get() is False
    assert app.config["strong_mode"] is False
    assert app.config["caricature_mode"] is False
    assert prompts == ["tone_system", "tone_system"]


def test_direct_style_set_is_also_gated():
    app, prompts = _free_app()

    wayfinder_main.WayfinderApp._set_output_style(app, "professional")

    assert prompts == ["tone_system"]
    assert app.config["output_tone"] == "minimal"


def test_free_gpu_toggle_snaps_off_and_opens_ultra_prompt():
    app, prompts = _free_app()

    wayfinder_main.WayfinderApp.toggle_gpu(app)

    assert app.gpu_var.get() is False
    assert prompts == ["gpu_acceleration"]
    assert app.config["use_gpu"] is False


def test_ultra_gpu_toggle_on_persists_and_restarts_runtime(monkeypatch):
    saved = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda feature: feature == "gpu_acceleration"),
        config={"use_gpu": False},
        gpu_var=_Var(True),
        log=lambda _message: None,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda cfg: saved.append(dict(cfg)))

    with patch("wayfinder.core.transcriber.WhisperServerBackend.shutdown") as shutdown:
        wayfinder_main.WayfinderApp.toggle_gpu(app)

    assert app.config["use_gpu"] is True
    assert saved[-1]["use_gpu"] is True
    shutdown.assert_called_once()


def test_ultra_gpu_toggle_off_persists_cpu_and_restarts_runtime(monkeypatch):
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda feature: feature == "gpu_acceleration"),
        config={"use_gpu": True},
        gpu_var=_Var(False),
        log=lambda _message: None,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)

    with patch("wayfinder.core.transcriber.WhisperServerBackend.shutdown") as shutdown:
        wayfinder_main.WayfinderApp.toggle_gpu(app)

    assert app.config["use_gpu"] is False
    shutdown.assert_called_once()


def test_free_chunk_toggle_snaps_off_and_prompts():
    prompts = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        config={"chunked_mode": False},
        chunked_var=_Var(True),
        _show_premium_prompt=prompts.append,
    )

    wayfinder_main.WayfinderApp.toggle_chunked_mode(app)

    assert app.chunked_var.get() is False
    assert app.config["chunked_mode"] is False
    assert prompts == ["chunked_recording"]


def test_ultra_chunk_toggle_persists(monkeypatch):
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(
            has_feature=lambda feature: feature == "chunked_recording"
        ),
        config={"chunked_mode": False},
        chunked_var=_Var(True),
        log=lambda _message: None,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)

    wayfinder_main.WayfinderApp.toggle_chunked_mode(app)

    assert app.config["chunked_mode"] is True


def test_free_remote_mode_resets_selector_and_prompts():
    prompts = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        config={"processing_mode": "local"},
        mode_selector=_Var("remote"),
        _show_premium_prompt=prompts.append,
    )

    wayfinder_main.WayfinderApp._on_processing_mode_changed(app, "remote")

    assert app.mode_selector.get() == "local"
    assert app.config["processing_mode"] == "local"
    assert prompts == ["cloud_backends"]


def test_ultra_remote_mode_selects_cloud_backend(monkeypatch):
    rebuilt = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(
            has_feature=lambda feature: feature == "cloud_backends"
        ),
        config={
            "processing_mode": "local",
            "transcription_backend": "whisper_cpp",
        },
        log=lambda _message: None,
        _build_mode_settings=rebuilt.append,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)

    wayfinder_main.WayfinderApp._on_processing_mode_changed(app, "remote")

    assert app.config["processing_mode"] == "remote"
    assert app.config["transcription_backend"] == "groq_whisper"
    assert rebuilt == ["remote"]


def test_free_faster_whisper_selection_restores_previous_backend():
    prompts = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        config={
            "transcription_backend": "whisper_cpp",
            "transcription_backend_auto": True,
        },
        backend_var=_Var("Faster-Whisper (experimental)"),
        _backend_display_map={"Faster-Whisper (experimental)": "faster_whisper"},
        _show_premium_prompt=prompts.append,
    )

    wayfinder_main.WayfinderApp.on_backend_changed(
        app, "Faster-Whisper (experimental)"
    )

    assert app.backend_var.get() == "Auto (whisper.cpp)"
    assert app.config["transcription_backend"] == "whisper_cpp"
    assert prompts == ["faster_whisper"]


def test_ultra_faster_whisper_selection_persists(monkeypatch):
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(
            has_feature=lambda feature: feature == "faster_whisper"
        ),
        config={
            "transcription_backend": "whisper_cpp",
            "transcription_backend_auto": True,
        },
        _backend_display_map={"Faster-Whisper (experimental)": "faster_whisper"},
        log=lambda _message: None,
        _refresh_backend_tooltip=lambda: None,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)
    monkeypatch.setattr(
        wayfinder_main,
        "get_gpu_info",
        lambda: SimpleNamespace(is_nvidia=True),
    )
    monkeypatch.setattr(
        "wayfinder.utils.gpu.ctranslate2_cuda_available", lambda: True
    )

    wayfinder_main.WayfinderApp.on_backend_changed(
        app, "Faster-Whisper (experimental)"
    )

    assert app.config["transcription_backend"] == "faster_whisper"
    assert app.config["transcription_backend_auto"] is False


def test_free_large_cleanup_selection_restores_previous_and_prompts():
    prompts = []
    app = SimpleNamespace(
        config={},
        _llamacpp_model_var=_Var("Qwen 4B  ·  Ultra"),
        _llamacpp_current_display="Gemma 1B",
        _llamacpp_model_data={
            "Qwen 4B  ·  Ultra": {
                "unlocked": False,
                "installed": True,
                "path": "/models/qwen-4b.gguf",
                "info": {"requires_feature": "large_cleanup_models"},
            }
        },
        _show_premium_prompt=prompts.append,
    )

    wayfinder_main.WayfinderApp._on_llamacpp_model_selected(
        app, "Qwen 4B  ·  Ultra"
    )

    assert app._llamacpp_model_var.get() == "Gemma 1B"
    assert "llama_cpp_model_path" not in app.config
    assert prompts == ["large_cleanup_models"]


def test_ultra_installed_cleanup_selection_persists(monkeypatch):
    app = SimpleNamespace(
        config={},
        _llamacpp_model_var=_Var("Qwen 4B"),
        _llamacpp_current_display="Gemma 1B",
        _llamacpp_model_data={
            "Qwen 4B": {
                "unlocked": True,
                "installed": True,
                "path": "/models/qwen-4b.gguf",
                "info": {
                    "name": "Qwen 4B",
                    "requires_feature": "large_cleanup_models",
                },
            }
        },
        log=lambda _message: None,
        _update_llamacpp_download_button=lambda: None,
        _update_llamacpp_info_panel=lambda: None,
        _update_compatibility_banner=lambda: None,
    )
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)

    wayfinder_main.WayfinderApp._on_llamacpp_model_selected(app, "Qwen 4B")

    assert app.config["llama_cpp_model_path"] == "/models/qwen-4b.gguf"
    assert app.config["llama_cpp_model_requires_feature"] == "large_cleanup_models"


def test_free_voice_profile_dialog_and_learning_are_blocked():
    prompts = []
    app = SimpleNamespace(
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        _show_premium_prompt=prompts.append,
    )

    wayfinder_main.WayfinderApp.open_voice_profile_dialog(app)
    wayfinder_main.WayfinderApp._add_to_voice_learning(app, "secret history")

    assert prompts == ["voice_profiles"]


def test_base_tooltip_is_candid_about_free_default_accuracy():
    tip = wayfinder_main.get_dynamic_tooltip(
        "whisper_model", {"model_path": "/models/ggml-base.en.bin"}
    ).lower()

    assert "free default" in tip
    assert "fast" in tip
    assert "inaccurate" in tip


def test_benchmark_can_compare_gpu_while_free_pipeline_remains_cpu():
    summary = wayfinder_main.BenchmarkRunner.build_pipeline_summary(
        asr_model_id="base.en",
        asr_model_name="Base",
        gpu_time=1.0,
        cpu_time=4.0,
        use_gpu=False,
        pp_results={},
        pp_enabled=False,
    )

    assert summary["asr_mode"] == "cpu"
    assert summary["asr_time"] == 4.0

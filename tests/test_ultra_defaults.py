"""The one-time recommended Ultra setup (wayfinder.core.ultra_defaults)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import wayfinder_main
from wayfinder.config import DEFAULT_CONFIG, enforce_license_config
from wayfinder.core.ultra_defaults import (
    RECOMMENDED_SPEECH_MODEL,
    ULTRA_DEFAULTS_KEY,
    apply_ultra_defaults,
    describe_changes,
    gpu_is_default_capable,
)

REPO = Path(__file__).resolve().parent.parent
TURBO = f"~/models/{RECOMMENDED_SPEECH_MODEL}"


def _gate(premium=True, features=None):
    """features: granted Ultra feature ids (None = all)."""
    return SimpleNamespace(
        is_premium=premium,
        has_feature=lambda f: premium and (features is None or f in features),
    )


def _free_config(**overrides):
    """What a Free install has on disk: Ultra switches forced off."""
    config = {
        "use_gpu": False,
        "chunked_mode": "off",
        "post_processing_enabled": False,
        "model_path": "~/models/ggml-base.en.bin",
        ULTRA_DEFAULTS_KEY: False,
    }
    config.update(overrides)
    return config


class TestWhenItRuns:
    def test_free_install_records_that_it_saw_free_and_changes_nothing_else(self):
        config = _free_config(**{ULTRA_DEFAULTS_KEY: None})
        assert apply_ultra_defaults(config, _gate(premium=False), gpu_capable=True) == [
            ULTRA_DEFAULTS_KEY
        ]
        assert config == _free_config()

    def test_first_switch_to_ultra_turns_on_gpu_and_auto_chunking(self):
        config = _free_config()
        changed = apply_ultra_defaults(config, _gate(), gpu_capable=True)
        assert changed == ["use_gpu", "chunked_mode", "post_processing_enabled", ULTRA_DEFAULTS_KEY]
        assert config["use_gpu"] is True
        assert config["chunked_mode"] == "auto"
        # Normal then removes um/uh with no model; styles need it on.
        assert config["post_processing_enabled"] is True
        assert config[ULTRA_DEFAULTS_KEY] is True

    def test_activation_in_the_app_applies_even_without_a_record(self):
        config = _free_config(**{ULTRA_DEFAULTS_KEY: None})
        apply_ultra_defaults(config, _gate(), gpu_capable=True, activated_now=True)
        assert config["use_gpu"] is True and config["chunked_mode"] == "auto"

    def test_an_install_that_was_already_ultra_keeps_its_settings(self):
        # A config from before this feature, on a license that is already Ultra:
        # CPU and chunking off were that user's choices.
        config = _free_config(**{ULTRA_DEFAULTS_KEY: None})
        assert apply_ultra_defaults(config, _gate(), gpu_capable=True) == [ULTRA_DEFAULTS_KEY]
        assert config["use_gpu"] is False and config["chunked_mode"] == "off"
        assert config["post_processing_enabled"] is False
        assert config[ULTRA_DEFAULTS_KEY] is True

    def test_it_runs_once_and_later_choices_stick(self):
        config = _free_config()
        apply_ultra_defaults(config, _gate(), gpu_capable=True)
        config["use_gpu"] = False
        config["chunked_mode"] = "off"
        config["post_processing_enabled"] = False
        for activated_now in (False, True):
            assert apply_ultra_defaults(
                config, _gate(), gpu_capable=True, activated_now=activated_now
            ) == []
        assert config["use_gpu"] is False and config["chunked_mode"] == "off"

    def test_a_broken_gate_changes_nothing(self):
        class _Broken:
            @property
            def is_premium(self):
                raise RuntimeError("license store unreadable")

        config = _free_config()
        assert apply_ultra_defaults(config, _Broken(), gpu_capable=True) == []
        assert config == _free_config()


class TestWhatItChanges:
    def test_gpu_stays_off_on_hardware_that_does_not_suit_it(self):
        config = _free_config()
        apply_ultra_defaults(config, _gate(), gpu_capable=False)
        assert config["use_gpu"] is False
        assert config["chunked_mode"] == "auto"

    def test_only_granted_features_are_switched_on(self):
        config = _free_config()
        apply_ultra_defaults(config, _gate(features={"large_models"}), gpu_capable=True)
        assert config["use_gpu"] is False and config["chunked_mode"] == "off"
        assert config[ULTRA_DEFAULTS_KEY] is True

    @pytest.mark.parametrize("mode", ["on", "auto"])
    def test_a_chunking_choice_already_made_is_kept(self, mode):
        config = _free_config(chunked_mode=mode)
        apply_ultra_defaults(config, _gate(), gpu_capable=True)
        assert config["chunked_mode"] == mode

    def test_turbo_q5_replaces_base_when_downloaded_and_on_the_gpu(self):
        config = _free_config()
        changed = apply_ultra_defaults(
            config, _gate(), gpu_capable=True, recommended_model_path=TURBO
        )
        assert "model_path" in changed and config["model_path"] == TURBO

    def test_turbo_q5_is_not_chosen_for_a_cpu_only_setup(self):
        # About 9x slower than Base on CPU (docs/EVAL-2026-09-24.md).
        config = _free_config()
        apply_ultra_defaults(config, _gate(), gpu_capable=False, recommended_model_path=TURBO)
        assert config["model_path"] == "~/models/ggml-base.en.bin"

    def test_a_non_free_model_the_user_picked_is_kept(self):
        config = _free_config(model_path="~/models/ggml-small.en.bin")
        apply_ultra_defaults(config, _gate(), gpu_capable=True, recommended_model_path=TURBO)
        assert config["model_path"] == "~/models/ggml-small.en.bin"

    def test_no_download_means_no_model_change(self):
        config = _free_config()
        changed = apply_ultra_defaults(config, _gate(), gpu_capable=True)
        assert "model_path" not in changed


class TestFromAFreshInstall:
    def test_free_then_activation_through_the_real_license_repair(self):
        config = dict(DEFAULT_CONFIG)
        free = _gate(premium=False)
        enforce_license_config(config, free)  # Free forces chunking off
        apply_ultra_defaults(config, free, gpu_capable=True)
        assert config["chunked_mode"] == "off" and config["use_gpu"] is False
        assert config[ULTRA_DEFAULTS_KEY] is False

        ultra = _gate()
        enforce_license_config(config, ultra)
        apply_ultra_defaults(config, ultra, gpu_capable=True, activated_now=True)
        assert config["use_gpu"] is True and config["chunked_mode"] == "auto"

    def test_the_record_starts_undecided(self):
        assert DEFAULT_CONFIG[ULTRA_DEFAULTS_KEY] is None
        assert DEFAULT_CONFIG["use_gpu"] is False


@pytest.mark.parametrize(
    "vendor, platform_name, expected",
    [
        ("apple", "darwin", True),
        ("intel", "darwin", False),  # Intel Mac: Metal whisper.cpp is not the better default
        ("nvidia", "linux", True),
        ("amd", "linux", True),  # Steam Deck, Radeon
        ("intel", "linux", False),  # integrated graphics can lose to the CPU
        ("unknown", "linux", False),
        ("apple", "linux", False),
        ("nvidia", "win32", True),
        ("amd", "win32", True),
        ("intel", "win32", False),
        ("", "win32", False),
    ],
)
def test_gpu_default_hardware(vendor, platform_name, expected):
    assert gpu_is_default_capable(vendor, platform_name) is expected


def test_the_log_line_names_what_changed():
    assert describe_changes([]) == ""
    assert describe_changes([ULTRA_DEFAULTS_KEY]) == ""
    text = describe_changes(
        ["use_gpu", "chunked_mode", "post_processing_enabled", "model_path", ULTRA_DEFAULTS_KEY]
    )
    assert "GPU acceleration on" in text
    assert "text cleanup on" in text
    assert "Chunk Processing on Auto" in text
    assert "Large v3 Turbo Q5" in text
    assert "Settings" in text


# --- The app's hooks (real unbound WayfinderApp methods on a stub) ----------


def _app(gate, config, state=None):
    events = []
    app = SimpleNamespace(
        feature_gate=gate,
        config=config,
        app_state=state or wayfinder_main.AppState.IDLE,
        log=lambda message: events.append(("log", message)),
        _gpu_default_capable=lambda: True,
        _recommended_speech_model_on_disk=lambda: None,
        _apply_transcription_hardware_change=lambda on: events.append(("hardware", on)),
    )
    return app, events


def test_app_applies_saves_restarts_and_logs(monkeypatch):
    saved = []
    monkeypatch.setattr(wayfinder_main, "save_config", lambda cfg: saved.append(dict(cfg)))
    gate = _gate()
    gate.refresh_pending = False
    app, events = _app(gate, _free_config())

    changed = wayfinder_main.WayfinderApp._apply_ultra_defaults(app, activated_now=True)

    assert "use_gpu" in changed and saved[-1]["use_gpu"] is True
    assert ("hardware", True) in events
    assert any(kind == "log" and "Ultra setup" in text for kind, text in events)


def test_app_waits_for_a_pending_license_refresh(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)
    gate = _gate()
    gate.refresh_pending = True
    app, _events = _app(gate, _free_config())
    assert wayfinder_main.WayfinderApp._apply_ultra_defaults(app) == []
    assert app.config[ULTRA_DEFAULTS_KEY] is False


def test_app_never_switches_under_a_running_dictation(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)
    gate = _gate()
    gate.refresh_pending = False
    app, events = _app(gate, _free_config(), state=wayfinder_main.AppState.PROCESSING)
    assert wayfinder_main.WayfinderApp._apply_ultra_defaults(app, activated_now=True) == []
    assert app.config["use_gpu"] is False and events == []


def test_activation_runs_the_setup_before_settings_are_rebuilt():
    events = []
    gate = SimpleNamespace(is_premium=True)
    result = SimpleNamespace(is_valid=True, error_message=None)
    app = SimpleNamespace(
        _license_key_entry=SimpleNamespace(get=lambda: "wv-test-key"),
        _license_feedback=SimpleNamespace(configure=lambda **_kw: None),
        update_idletasks=lambda: None,
        log=lambda _message: None,
        _apply_ultra_defaults=lambda **kw: events.append(("ultra", kw)),
        _rebuild_header=lambda: events.append("header"),
        _refresh_entitlement_ui=lambda: events.append("entitlements"),
        _render_license_tile=lambda: None,
        _show_ultra_banner=lambda: None,
        _write_status_breadcrumb=lambda: None,
        after=lambda _ms, _fn: None,
        show_permissions_setup=lambda: None,
    )
    with patch("wayfinder.license.store_license", return_value=result), patch(
        "wayfinder.license.get_feature_gate", return_value=gate
    ):
        wayfinder_main.WayfinderApp._activate_license(app)

    assert events[0] == ("ultra", {"activated_now": True})
    assert events.index("entitlements") > 0


def test_launch_and_background_refresh_both_settle_the_record():
    source = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")
    init = source[source.index("_entitlement_repairs = ("):]
    assert init.index("self._apply_ultra_defaults()") < init.index("resolve_audio_device(self.config)")
    refresh = source[source.index("def _refresh_license_background"):]
    refresh = refresh[: refresh.index("threading.Thread(")]
    assert refresh.index("enforce_license_config") < refresh.index("self._apply_ultra_defaults()")
    assert refresh.index("self._apply_ultra_defaults()") < refresh.index("self._refresh_entitlement_ui()")

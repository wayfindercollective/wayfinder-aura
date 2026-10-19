"""Simple/advanced views share one config and the existing gated operations."""

from copy import deepcopy
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wayfinder.config import DEFAULT_CONFIG
from wayfinder.state import AppState
from wayfinder.ui.basic_mode import (
    basic_mode_enabled,
    basic_tabs,
    indicator_choice,
    indicator_updates,
    language_choice,
    premium_model_selected,
    speech_choice,
)
from wayfinder.ui.basic_settings import BasicSettingsController


def test_basic_is_the_default_for_new_and_existing_installs():
    assert DEFAULT_CONFIG["advanced_mode"] is False
    assert basic_mode_enabled({})
    assert basic_mode_enabled(DEFAULT_CONFIG)
    assert not basic_mode_enabled({"advanced_mode": True})
    assert DEFAULT_CONFIG["raw_transcription"] is False


def test_the_app_and_modular_ui_share_the_same_state_enum():
    import wayfinder_main as wm

    assert wm.AppState is AppState


def test_failed_staged_build_keeps_the_foreground_loader_retryable():
    import wayfinder_main as wm

    partial = Mock()
    loader = SimpleNamespace(frame=Mock(), failed=Mock())

    def fail():
        raise RuntimeError("test build failure")

    app = SimpleNamespace(
        config={"advanced_mode": True}, _workspace=object(),
        _settings_build_complete=False, _settings_build_iterator=(fail() for _ in range(1)),
        _settings_loader=loader, _advanced_settings_frame=partial,
        tab_frames={"settings": partial}, app_state=AppState.IDLE,
        winfo_viewable=lambda: True, log=Mock(), _schedule_settings_preload=Mock(),
    )
    wm.WayfinderApp._preload_settings_slice(app)
    partial.destroy.assert_called_once()
    loader.frame.destroy.assert_not_called()
    loader.failed.assert_called_once()
    assert app.tab_frames["settings"] is loader.frame
    assert app._advanced_settings_frame is None
    assert app._settings_build_iterator is None
    app._schedule_settings_preload.assert_not_called()


def test_model_manager_waits_for_staged_settings_instead_of_using_missing_controls():
    import wayfinder_main as wm

    loader = SimpleNamespace(when_ready=Mock())
    app = SimpleNamespace(
        config={"advanced_mode": True}, _workspace=object(),
        _settings_loader=loader, open_model_settings=Mock(),
    )
    wm.WayfinderApp.open_model_settings(app)
    loader.when_ready.assert_called_once_with(app.open_model_settings)


@pytest.mark.parametrize("unlocked,raw,expected", [
    (False, False, ("dictate", "settings")),
    (False, True, ("dictate", "settings")),
    (True, False, ("dictate", "settings", "style")),
    (True, True, ("dictate", "settings")),
])
def test_style_is_a_third_tab_only_for_ultra_with_cleanup_enabled(unlocked, raw, expected):
    config = {"raw_transcription": raw}
    assert basic_tabs(config, style_unlocked=unlocked) == expected
    assert config == {"raw_transcription": raw}


@pytest.mark.parametrize("label,premium,model,language", [
    ("English", False, "base.en", "en"),
    ("Multilingual", False, "base", "auto"),
    ("English", True, "large-v3-turbo-q5_0", "en"),
    ("Multilingual", True, "large-v3-turbo-q5_0", "auto"),
])
def test_plain_language_choices_use_the_shipped_models(label, premium, model, language):
    result = speech_choice(label, premium)
    assert result.model_id == model
    assert result.language == language


@pytest.mark.parametrize("config,expected", [
    ({}, "Always on"),
    ({"overlay_type": "disappearing"}, "Disappearing"),
    ({"overlay_type": "always_on", "overlay_enabled": False}, "Off"),
    ({"overlay_type": "disappearing", "overlay_enabled": False}, "Off"),
])
def test_indicator_projection(config, expected):
    assert indicator_choice(config) == expected


@pytest.mark.parametrize("choice", ["Always on", "Disappearing", "Off"])
def test_indicator_round_trip_preserves_all_tuning(choice):
    config = {
        "overlay_type": "disappearing", "overlay_enabled": True,
        "overlay_scale": 1.25, "overlay_y_offset": 52,
        "enable_tray_icon": False, "overlay_quality": "high",
    }
    original = deepcopy(config)
    config.update(indicator_updates(choice))
    assert indicator_choice(config) == choice
    for key in original.keys() - {"overlay_type", "overlay_enabled"}:
        assert config[key] == original[key]
    if choice == "Off":
        assert config["overlay_type"] == "disappearing"


@pytest.mark.parametrize("function,value", [(indicator_updates, "Unknown"), (lambda x: speech_choice(x, False), "Spanish")])
def test_invalid_ui_choices_do_not_rewrite_settings(function, value):
    with pytest.raises(ValueError):
        function(value)


def test_reading_basic_settings_preserves_custom_advanced_preferences():
    config = deepcopy(DEFAULT_CONFIG)
    config.update(model_path="~/models/ggml-medium.bin", language="fr", beam_size=7,
                  overlay_scale=1.4, gpu_device="2", output_tone="dev", raw_transcription=True)
    original = deepcopy(config)
    assert premium_model_selected(config)
    assert language_choice(config) == "Multilingual"
    basic_mode_enabled(config)
    indicator_choice(config)
    assert config == original


@pytest.mark.parametrize("filename,premium", [
    ("ggml-base.en.bin", False), ("ggml-base.bin", False),
    ("ggml-small.en.bin", True), ("ggml-large-v3-turbo-q5_0.bin", True),
    ("", False),
])
def test_premium_projection_uses_the_license_model_classification(filename, premium):
    assert premium_model_selected({"model_path": filename}) is premium


class Downloader:
    def __init__(self, existing=None):
        self.existing = existing
        self.get_model_path = Mock(side_effect=lambda _model: existing)
        self.download_model = Mock(side_effect=self.start)
        self.callbacks = {}

    def start(self, model_id, **callbacks):
        self.model_id = model_id
        self.callbacks = callbacks


@pytest.fixture
def controller():
    app = SimpleNamespace(
        config={"model_path": "ggml-base.en.bin", "language": "en", "processing_mode": "local"},
        app_state=AppState.IDLE, event_queue=Queue(),
        feature_gate=SimpleNamespace(has_feature=lambda feature: True),
        _show_premium_prompt=Mock(),
    )

    def activate(_role, path, _info):
        app.config["model_path"] = path
        return "active"

    app._activate_downloaded_model = Mock(side_effect=activate)
    app.on_language_changed = Mock(side_effect=lambda value: app.config.update(language=value))
    downloader = Downloader()
    instance = BasicSettingsController(app, lambda: downloader, {})
    instance.on_change = Mock()
    return instance, app, downloader


def drain(app):
    while not app.event_queue.empty():
        _kind, callback = app.event_queue.get_nowait()
        callback()


def test_free_upgrade_click_uses_the_existing_gate_without_downloading(controller):
    ctl, app, downloader = controller
    app.feature_gate.has_feature = lambda feature: False
    before = deepcopy(app.config)
    ctl.choose_model("English", True)
    app._show_premium_prompt.assert_called_once_with("large_models")
    downloader.download_model.assert_not_called()
    app._activate_downloaded_model.assert_not_called()
    assert app.config == before


@pytest.mark.parametrize("state", [AppState.RECORDING, AppState.PROCESSING, AppState.PASTING])
def test_no_model_or_language_changes_mid_dictation(controller, state):
    ctl, app, downloader = controller
    app.app_state = state
    ctl.choose_model("Multilingual", False)
    downloader.download_model.assert_not_called()
    assert app.config["language"] == "en"


def test_downloads_use_the_shared_downloader_and_ui_queue(controller):
    ctl, app, downloader = controller
    before = deepcopy(app.config)
    ctl.choose_model("Multilingual", False)
    assert downloader.model_id == "base"
    assert ctl.downloading
    assert app.config == before
    downloader.callbacks["complete_callback"]("/models/ggml-base.bin")
    app._activate_downloaded_model.assert_not_called()
    drain(app)
    app._activate_downloaded_model.assert_called_once_with("whisper", "/models/ggml-base.bin", None)
    assert app.config["language"] == "auto"
    assert not ctl.downloading


def test_an_installed_model_activates_without_a_download(controller):
    ctl, app, downloader = controller
    downloader.get_model_path.return_value = Path("/models/ggml-base.bin")
    downloader.get_model_path.side_effect = None
    ctl.choose_model("Multilingual", False)
    downloader.download_model.assert_not_called()
    assert app.config["model_path"] == "/models/ggml-base.bin"
    assert app.config["language"] == "auto"


def test_a_failed_download_does_not_change_the_working_selection(controller):
    ctl, app, downloader = controller
    before = deepcopy(app.config)
    ctl.choose_model("Multilingual", False)
    downloader.callbacks["error_callback"]("Connection interrupted")
    drain(app)
    assert app.config == before
    assert not ctl.downloading
    assert "Connection interrupted" in ctl.status


def test_progress_is_coalesced_to_whole_percentages_and_runs_on_ui_thread(controller):
    ctl, app, downloader = controller
    ctl.choose_model("Multilingual", False)
    callback = downloader.callbacks["progress_callback"]
    callback(0.250, 25, 100)
    callback(0.251, 25, 100)
    assert app.event_queue.qsize() == 1
    drain(app)
    assert "25%" in ctl.status


def test_download_that_finishes_mid_recording_waits_for_idle(controller):
    ctl, app, downloader = controller
    ctl.choose_model("Multilingual", False)
    app.app_state = AppState.RECORDING
    downloader.callbacks["complete_callback"]("/models/ggml-base.bin")
    drain(app)
    app._activate_downloaded_model.assert_not_called()
    assert app.config["language"] == "en"
    app.app_state = AppState.IDLE
    ctl.apply_pending()
    assert app.config["language"] == "auto"
    assert app.config["model_path"] == "/models/ggml-base.bin"


def test_download_cannot_overwrite_an_intervening_advanced_selection(controller):
    ctl, app, downloader = controller
    ctl.choose_model("Multilingual", False)
    app.config.update(model_path="/models/ggml-medium.bin", language="fr")
    downloader.callbacks["complete_callback"]("/models/ggml-base.bin")
    drain(app)
    app._activate_downloaded_model.assert_not_called()
    assert app.config["language"] == "fr"
    assert "Keeping" in ctl.status


def test_an_expired_entitlement_is_still_checked_by_the_shared_activation(controller):
    ctl, app, downloader = controller
    ctl.choose_model("English", True)
    app._activate_downloaded_model.side_effect = lambda *_: "skipped"
    downloader.callbacks["complete_callback"]("/models/ggml-large-v3-turbo-q5_0.bin")
    drain(app)
    assert app.config["model_path"] == "ggml-base.en.bin"
    assert "unchanged" in ctl.status


def test_remote_settings_are_not_silently_replaced_by_local_choices(controller):
    ctl, app, downloader = controller
    app.config["processing_mode"] = "remote"
    before = deepcopy(app.config)
    ctl.choose_model("English", False)
    assert app.config == before
    downloader.download_model.assert_not_called()


def test_one_time_ultra_download_is_not_started_a_second_time(controller):
    ctl, app, downloader = controller
    app._ultra_model_downloading = True
    ctl.choose_model("English", True)
    downloader.download_model.assert_not_called()


@pytest.mark.parametrize("choice,enabled,style,calls", [
    ("Off", False, "always_on", 1),
    ("Disappearing", True, "disappearing", 1),
    ("Always on", True, "always_on", 0),
])
def test_indicator_uses_the_existing_apply_once_and_preserves_tuning(choice, enabled, style, calls):
    import wayfinder_main as wm

    class Variable:
        def __init__(self, value):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = SimpleNamespace(
        config={"overlay_enabled": True, "overlay_type": "always_on", "overlay_scale": 1.25, "enable_tray_icon": False},
        overlay_enabled_var=Variable(True), overlay_type_var=Variable("always_on"),
        _on_overlay_enabled_toggled=Mock(),
    )
    wm.WayfinderApp.set_indicator_mode(app, choice)
    assert app.config["overlay_enabled"] is enabled
    assert app.config["overlay_type"] == style
    assert app.config["overlay_scale"] == 1.25
    assert app.config["enable_tray_icon"] is False
    assert app._on_overlay_enabled_toggled.call_count == calls


def test_advanced_indicator_style_does_not_enable_a_disabled_indicator(monkeypatch):
    import wayfinder_main as wm

    monkeypatch.setattr(wm, "save_config", Mock())
    app = SimpleNamespace(
        config={"overlay_enabled": False, "overlay_type": "always_on"},
        set_indicator_mode=Mock(),
    )
    wm.WayfinderApp.on_overlay_type_changed(app, "disappearing")
    assert app.config == {"overlay_enabled": False, "overlay_type": "disappearing"}
    app.set_indicator_mode.assert_not_called()


def test_basic_mode_never_preloads_hidden_advanced_widgets():
    import wayfinder_main as wm

    app = SimpleNamespace(config={"advanced_mode": False}, _workspace=object(), after=Mock())
    wm.WayfinderApp._schedule_settings_preload(app)
    app.after.assert_not_called()


def test_basic_language_changes_also_update_the_cached_advanced_control(monkeypatch):
    import wayfinder_main as wm

    monkeypatch.setattr(wm, "save_config", Mock())
    app = SimpleNamespace(config={"language": "en"}, language_var=Mock(), log=Mock())
    wm.WayfinderApp.on_language_changed(app, "auto")
    assert app.config["language"] == "auto"
    app.language_var.set.assert_called_once_with("auto")


def test_view_mode_changes_only_the_presentation_preference(monkeypatch):
    import wayfinder_main as wm

    monkeypatch.setattr(wm, "save_config", Mock())
    original = {**DEFAULT_CONFIG, "raw_transcription": True, "use_gpu": True}
    app = SimpleNamespace(config=deepcopy(original), _switch_tab=Mock())
    wm.WayfinderApp._set_advanced_mode(app, True)
    assert app.config == {**original, "advanced_mode": True}
    app._switch_tab.assert_called_once_with("settings")

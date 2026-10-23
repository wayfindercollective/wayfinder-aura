"""Real-widget checks for the default workspace; never open audio or hotkeys."""

import os
from copy import deepcopy
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.ui
ctk = pytest.importorskip("customtkinter")


def _hero_layout(app):
    return [
        (widget.winfo_rooty() - app.winfo_rooty(), widget.winfo_height())
        for widget in (
            app.hero_canvas, app._hero_mic_container, app.status_label,
            app._hero_hotkey_chip, app._workspace.nav,
        )
    ]


def _assert_native_hero_is_aligned(app):
    mic = getattr(app, "_macos_mic_button", None)
    if mic is not None:
        from Quartz import CGImageGetWidth

        frame = mic.layer.frame()
        actual_top = mic.ns_window.contentView().bounds().size.height - frame.origin.y - frame.size.height
        assert actual_top == pytest.approx(app.mic_button_canvas.winfo_rooty() - app.winfo_rooty(), abs=1)
        assert frame.size.width == app.mic_button_canvas.winfo_width()
        assert CGImageGetWidth(mic.layer.contents()) == round(frame.size.width * mic.ns_window.backingScaleFactor())
    native = getattr(app, "_macos_hero_layer", None)
    if native is None:
        return
    layers = [native.layer] if native.layer is not None else [
        layer for layer in native.ns_window.contentView().layer().sublayers()
        if str(layer.name()) in ("WayfinderHeroWaveformMetalNative", "WayfinderHeroWaveformVector")
    ]
    assert layers
    for layer in layers:
        frame = layer.frame()
        actual_top = native.ns_window.contentView().bounds().size.height - frame.origin.y - frame.size.height
        expected_top = app.hero_canvas.winfo_rooty() - app.winfo_rooty()
        assert actual_top == pytest.approx(expected_top, abs=1)
        assert frame.size.height == app.hero_canvas.winfo_height()
        assert actual_top + frame.size.height <= app._hero_mic_container.winfo_rooty() - app.winfo_rooty()


def _finish_staged_settings(app):
    # Drive finite batches without wall-clock sleeps. update() must get an
    # event turn between each one, just as the production preload chain does.
    for _step in range(10):
        if getattr(app, "_settings_build_complete", False):
            break
        job = getattr(app, "_settings_preload_job", None)
        if job is not None:
            app.after_cancel(job)
        app._preload_settings_slice()
        app.update()
    if not getattr(app, "_settings_build_complete", False):
        pytest.fail(f"Settings did not finish; recent diagnostics: {app.log.call_args_list[-3:]!r}")
    assert app._settings_loader is None


def _assert_children_do_not_cover_bottom_border(card):
    border = card._apply_widget_scaling(card.cget("border_width"))
    bottom = card.winfo_rooty() + card.winfo_height()
    for child in card.winfo_children():
        if child.winfo_ismapped():
            assert child.winfo_rooty() + child.winfo_height() <= bottom - border, (
                f"{child} paints over {card}'s bottom border"
            )


def _snapshot_own_test_window(app, name):
    """Optional QA render of this test's own NSView, never the desktop/mic."""
    target = os.environ.get("AURA_UI_SNAPSHOTS")
    if not target:
        return
    import sys
    if sys.platform != "darwin":
        return
    from AppKit import NSApp

    from wayfinder.ui.ui_inspect import write_inspection

    app._draw_hero_waveform(force_canvas=True)
    app.update_idletasks()
    window = next(w for w in NSApp.windows() if str(w.title()) == app.title())
    view = window.contentView()
    image = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), image)
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    write_inspection(app._workspace.frame, app, target / f"{name}.json")
    assert image.representationUsingType_properties_(4, {}).writeToFile_atomically_(str(target / f"{name}.png"), True)


@pytest.fixture
def view(monkeypatch):
    import tkinter as tk

    import wayfinder_main as wm

    from wayfinder.config import DEFAULT_CONFIG
    from wayfinder.state import AppState
    from wayfinder.ui import icons
    from wayfinder.ui.basic_settings import BasicSettingsController, BasicSettingsView
    from wayfinder.ui.theme import FONT_SIZES

    icons._CACHE.clear()

    try:
        root = ctk.CTk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open here: {exc}")
    root.geometry("820x1000+3000+3000")
    monkeypatch.setattr(wm, "save_config", lambda config: None)
    app = SimpleNamespace(
        config=deepcopy(DEFAULT_CONFIG), app_state=AppState.IDLE,
        feature_gate=SimpleNamespace(has_feature=lambda _feature: False),
        event_queue=Queue(), font_body=("Helvetica",), font_header=("Helvetica",),
        font_sizes=FONT_SIZES, _mic_device_map={"Test microphone": 2},
        _mic_display_to_name={"Test microphone": "Test microphone"},
        _get_microphone_dropdown_options=lambda: (["Auto-detect", "Test microphone"], "Auto-detect"),
        get_hotkey_display=lambda: "Right Option", log=Mock(),
        _apply_transcription_hardware_change=Mock(), _show_premium_prompt=Mock(),
        _start_hotkey_detect=Mock(), update_audio_device=Mock(),
        _on_overlay_enabled_toggled=Mock(),
    )
    for name in ("toggle_gpu", "_on_raw_transcription_changed", "_on_microphone_selected"):
        setattr(app, name, getattr(wm.WayfinderApp, name).__get__(app))
    app.set_indicator_mode = Mock()
    scroll = ctk.CTkScrollableFrame(root)
    scroll.pack(fill="both", expand=True)
    downloader = SimpleNamespace(get_model_path=Mock(return_value=None), download_model=Mock())
    controller = BasicSettingsController(app, lambda: downloader, {})
    result = BasicSettingsView(app, scroll, controller)
    root.update()
    try:
        yield result, app, root, downloader
    finally:
        root.destroy()


def test_every_basic_control_is_available_and_no_advanced_fields_are_built(view):
    result, _app, _root, _downloader = view
    assert len(result._rows) == 7
    assert result.language.cget("values") == ["English", "Multilingual"]
    assert result.indicator.cget("values") == ["Always on", "Disappearing", "Off"]
    assert result.premium_switch.cget("state") == "normal"
    assert result.gpu_switch.cget("state") == "normal"


def test_only_non_obvious_options_have_visible_descriptions(view):
    result, _app, _root, _downloader = view
    assert [hint.cget("text") for _row, _labels, hint, _control in result._rows if hint is not None] == [
        "Better recognition with Whisper Turbo.",
        "Removes ums and ahs; applies corrections and writing style.",
    ]


def test_free_premium_switch_opens_the_existing_upgrade_flow(view):
    result, app, _root, downloader = view
    result.premium_switch.toggle()
    app._show_premium_prompt.assert_called_once_with("large_models")
    downloader.download_model.assert_not_called()
    assert result.premium_var.get() is False


def test_free_gpu_switch_never_enables_gpu(view):
    result, app, _root, _downloader = view
    result.gpu_switch.toggle()
    app._show_premium_prompt.assert_called_once_with("gpu_acceleration")
    assert not app.config["use_gpu"]
    assert not result.gpu_var.get()
    app._apply_transcription_hardware_change.assert_not_called()


def test_raw_switch_is_the_shared_setting_not_a_second_preference(view):
    result, app, _root, _downloader = view
    assert result.cleanup_var.get() is True
    result.cleanup_switch.toggle()
    assert result.cleanup_var.get() is False
    assert app.config["raw_transcription"] is True
    result.cleanup_switch.toggle()
    assert result.cleanup_var.get() is True
    assert app.config["raw_transcription"] is False


def test_changes_in_advanced_settings_project_back_into_basic_widgets(view):
    result, app, _root, _downloader = view
    app.feature_gate.has_feature = lambda _feature: True
    app.config.update(
        raw_transcription=True, overlay_type="disappearing", overlay_enabled=True,
        use_gpu=True, model_path="/models/ggml-large-v3-turbo-q5_0.bin", language="auto",
    )
    before = deepcopy(app.config)
    result.refresh()
    assert result.cleanup_var.get() is False
    assert result.raw_var.get() is True
    assert result.gpu_var.get() is True
    assert result.premium_var.get() is True
    assert result.language_var.get() == "Multilingual"
    assert result.indicator_var.get() == "Disappearing"
    assert app.config == before


def test_controls_stack_instead_of_clipping_in_a_narrow_window(view):
    result, _app, root, _downloader = view
    root.geometry("460x900+3000+3000")
    root.update()
    assert result._stacked is True
    for _row, _labels, _hint, control in result._rows:
        assert control.grid_info()["column"] == 0
        assert control.grid_info()["row"] == 1
    root.geometry("820x1000+3000+3000")
    root.update()
    assert result._stacked is False


def test_model_and_microphone_controls_disable_during_capture(view):
    from wayfinder.state import AppState

    result, app, _root, _downloader = view
    app.app_state = AppState.RECORDING
    result.refresh()
    assert result.microphone.cget("state") == "disabled"
    assert result.language._state == "disabled"
    assert result.gpu_switch.cget("state") == "disabled"
    assert result.raw_switch.cget("state") == "normal"  # applies to next dictation


def test_selecting_microphone_uses_the_normal_audio_device_handler(view):
    result, app, _root, _downloader = view
    result._choose_microphone("Test microphone")
    assert app.config["audio_device"] == 2
    assert app.config["audio_device_name"] == "Test microphone"
    app.update_audio_device.assert_called_once()


@pytest.mark.parametrize("layout", ["native", "shared", "shared_glass"])
def test_real_workspace_can_switch_views_without_resetting_config(monkeypatch, layout):
    """Run the actual shell/page builders, not a second mock UI implementation."""
    import tkinter as tk

    import wayfinder_main as wm

    from wayfinder.config import DEFAULT_CONFIG
    from wayfinder.state import AppState
    from wayfinder.ui import icons

    if layout != "native":
        # Exercise Linux's packed-page branch as well as Aqua's placed stack.
        # This is a layout check, not a substitute for native platform CI.
        monkeypatch.setattr(wm, "IS_MACOS", False)
        monkeypatch.setattr(wm, "MAC_LOOK", layout == "shared_glass")

    icons._CACHE.clear()  # Each test root owns a distinct Tcl image interpreter.

    try:
        app = wm.WayfinderApp.__new__(wm.WayfinderApp)
        ctk.CTk.__init__(app)
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open here: {exc}")
    app.geometry("800x780+3000+3000")
    app.title("Aura design preview")
    from wayfinder.utils.macos_fonts import register_bundled_macos_fonts
    from wayfinder.utils.tk_dpi import normalize_tk_font_dpi

    normalize_tk_font_dpi(app)
    register_bundled_macos_fonts()
    app.config = deepcopy(DEFAULT_CONFIG)
    app.config.update(setup_completed=True, welcome_completed=True, raw_transcription=True)
    app.app_state = AppState.IDLE
    app.ui_scale = 1.0
    app.dynamic_tooltips = {}
    app.event_queue = Queue()
    app.feature_gate = SimpleNamespace(is_premium=False, license_info=None, has_feature=lambda _feature: False)
    app.log = Mock()
    app._write_status_breadcrumb = Mock()
    app._start_idle_breath = Mock()
    app._maybe_show_setup_cue = Mock()
    app._maybe_show_ultra_tips = Mock()
    app._surface_model_path_notices = Mock()
    app._show_premium_prompt = Mock()
    app._sync_cleanup_residency = Mock()  # Never load/release models in UI tests.
    app._get_microphone_dropdown_options = lambda: (["Auto-detect"], "Auto-detect")
    monkeypatch.setattr(wm, "save_config", lambda config: None)
    try:
        app.setup_ui()
        app.update()
        if wm.IS_MACOS:
            from wayfinder.ui.macos_hero import MacOSHeroLayer

            app._macos_hero_layer = MacOSHeroLayer.try_create(app, app.hero_canvas)
            assert app._macos_hero_layer is not None
            assert app._macos_mic_button is not None
        initial_hero = _hero_layout(app)
        _assert_native_hero_is_aligned(app)
        assert app.mic_button_canvas.cget("cursor") == ("pointinghand" if wm.IS_MACOS else "hand2")
        if wm.IS_MACOS:
            assert app.mic_button_canvas.cget("cursor") == app.copy_btn.cget("cursor")
        app.on_record_button = Mock()  # Never open a microphone in UI checks.
        app.mic_button_canvas.event_generate("<Button-1>", x=40, y=40)
        app.update()
        app.on_record_button.assert_called_once_with()
        _snapshot_own_test_window(app, "dictate")
        assert not app._sidebar.winfo_manager()
        assert app._workspace.nav.winfo_manager() == "pack"
        assert app._workspace.footer.winfo_ismapped()
        assert app._workspace.advanced_switch.master is app._workspace.footer
        assert app._workspace.version.cget("text").endswith(" · Free")
        assert app._hero_hotkey_hint.winfo_manager() == ""
        assert app._hero_hotkey_chip.winfo_manager() == "pack"
        assert app._header_upgrade_button.cget("text") == "Upgrade to Ultra"
        assert app._header_upgrade_button.winfo_ismapped()
        assert app._header_right_controls.winfo_height() < 60
        app._header_upgrade_button.invoke()
        app._show_premium_prompt.assert_called_once_with("large_models")
        prompt_spy = app._show_premium_prompt
        app._show_premium_prompt = wm.WayfinderApp._show_premium_prompt.__get__(app)
        app.feature_gate.get_upgrade_message = lambda _feature: "Premium speech models and GPU acceleration."
        app._header_upgrade_button.invoke()
        app.update()
        assert app._premium_key_entry.winfo_ismapped()
        if wm.IS_MACOS:
            assert app._macos_mic_button.layer.isHidden()
        assert app._premium_dismiss_button.winfo_ismapped()
        app._premium_dismiss_button.invoke()
        app.update()
        assert app._premium_banner is None
        if wm.IS_MACOS:
            assert not app._macos_mic_button.layer.isHidden()
        app._show_premium_prompt = prompt_spy
        app._switch_tab("settings")
        app.update()
        _snapshot_own_test_window(app, "settings")
        assert app._hero_mic_container.winfo_manager() == "pack"
        assert app._hero_hotkey_chip.winfo_manager() == "pack"
        assert _hero_layout(app) == initial_hero
        _assert_native_hero_is_aligned(app)
        assert app._basic_settings_view.frame.winfo_ismapped()
        assert app._basic_settings_view.raw_var.get() is True
        basic_scroll = app._basic_settings_view.frame.master
        # CTk 5.2's scrollbar cget does not expose its button colors correctly;
        # inspect the stored paint values in both supported CTk versions.
        assert basic_scroll._scrollbar._button_color == wm.COLORS["scrollbar_thumb"]
        assert basic_scroll._scrollbar._button_hover_color == wm.COLORS["scrollbar_thumb_hover"]
        from wayfinder.ui.theme import COLORS

        for token in ("scrollbar_thumb", "scrollbar_thumb_hover"):
            assert wm.COLORS[token] == COLORS[token]
        assert app._basic_settings_view._rows[0][0].winfo_rootx() - app._tab_pane.winfo_rootx() == 16
        for width in (360, 460, 800):
            app.geometry(f"{width}x780+3000+3000")
            app.update()
            upgrade = app._header_upgrade_button
            assert upgrade.winfo_ismapped()
            assert upgrade.winfo_width() >= upgrade.winfo_reqwidth()
            indicator = app._basic_settings_view.indicator
            assert indicator.winfo_width() <= indicator.master.winfo_width()
            assert app._workspace.footer.winfo_ismapped()
            assert not hasattr(app._basic_settings_view, "advanced_switch")
            assert app._workspace.version.winfo_width() >= app._workspace.version.winfo_reqwidth()
            _assert_native_hero_is_aligned(app)
        for height in (1020, 780):
            app.geometry(f"800x{height}+3000+3000")
            app.update()
            _assert_native_hero_is_aligned(app)
        before = deepcopy(app.config)

        # The pre-existing long-dictation nudge still has a visible place and
        # reaches the same gated purchase/key-entry flow in the simple view.
        app._gpu_nudge_shown = False
        with monkeypatch.context() as gpu_patch:
            gpu_patch.setattr(wm, "get_gpu_info", lambda: SimpleNamespace(has_gpu=True))
            app._maybe_show_gpu_nudge(wm._GPU_NUDGE_MIN_DURATION_S + 1)
        app._switch_tab("dictate")
        app.update()
        assert app.gpu_nudge_banner.winfo_ismapped()
        app._upgrade_from_gpu_nudge()
        app._show_premium_prompt.assert_called_with("gpu_acceleration")
        assert not app.gpu_nudge_banner.winfo_manager()

        # Live activation adds Style without constructing advanced Settings.
        # Raw still hides it; turning Raw off reveals the actual existing page.
        app.feature_gate.is_premium = True
        app.feature_gate.has_feature = lambda _feature: True
        app._rebuild_header()
        app._refresh_entitlement_ui()
        app.update()
        assert app._header_upgrade_button is None
        assert app._workspace.version.cget("text").endswith(" · Ultra")
        assert app._workspace.tabs.cget("values") == ["Dictate", "Settings"]
        app._switch_tab("settings")
        app._basic_settings_view.raw_switch.toggle()
        app.update()
        assert app.config["raw_transcription"] is False
        assert app._workspace.tabs.cget("values") == ["Dictate", "Settings", "Style"]
        assert not getattr(app, "_settings_build_complete", False)
        paid_hero = _hero_layout(app)
        app._switch_tab("style")
        app.update()
        assert app.active_tab == "style"
        assert app.config["advanced_mode"] is False
        assert not app._sidebar.winfo_manager()
        assert app._workspace.tabs.get() == "Style"
        assert _hero_layout(app) == paid_hero
        _assert_native_hero_is_aligned(app)
        assert set(app.tone_buttons) == set(wm.STYLE_CYCLE)
        _snapshot_own_test_window(app, "style")

        # Raw/entitlement changes remove Style immediately without discarding
        # its saved tone or leaving a selected, inaccessible page on screen.
        app.config["raw_transcription"] = True
        app._refresh_raw_settings_visibility()
        app.update()
        assert app.active_tab == "settings"
        assert app._workspace.tabs.cget("values") == ["Dictate", "Settings"]
        app.feature_gate.is_premium = False
        app.feature_gate.has_feature = lambda _feature: False
        app._rebuild_header()
        app._refresh_entitlement_ui()
        app.update()
        assert app._header_upgrade_button.winfo_ismapped()
        assert app.config == before

        app.app_state = AppState.IDLE
        assert (
            app._basic_settings_view.advanced_section is None
            or app._basic_settings_view.advanced_section.winfo_manager() == ""
        )
        app._workspace.advanced_switch.toggle()
        app.update()
        _assert_native_hero_is_aligned(app)
        assert app.config["advanced_mode"] is True
        assert not app._sidebar.winfo_manager()
        assert app._workspace.nav.winfo_manager() == "pack"
        assert app._workspace.footer.winfo_ismapped()
        assert app._workspace.version.winfo_ismapped()
        assert app.tab_frames["settings"] is app._basic_settings_frame
        assert app._basic_settings_view._advanced_built is True
        assert app._basic_settings_view.advanced_section.winfo_manager() == "pack"
        assert len(app._basic_settings_view._advanced_rows) >= 15
        _snapshot_own_test_window(app, "advanced")
        app._switch_tab("dictate")
        app.update()
        _assert_children_do_not_cover_bottom_border(app._dictate_mic_card)
        _snapshot_own_test_window(app, "dictate_advanced")
        app._switch_tab("settings")
        app.update()
        app._workspace.advanced_switch.toggle()
        app.update()
        assert app.config["advanced_mode"] is False
        assert app.tab_frames["settings"] is app._basic_settings_frame
        assert app._basic_settings_view.advanced_section.winfo_manager() == ""
        assert not app._sidebar.winfo_manager()
        assert app._workspace.footer.winfo_ismapped()
        app._set_advanced_mode(True)
        app.update()
        assert app._basic_settings_view.advanced_section.winfo_manager() == "pack"
        app._set_advanced_mode(before["advanced_mode"])
        app.update()
        assert app.config == before
    finally:
        native = getattr(app, "_macos_hero_layer", None)
        if native is not None:
            native.close()
        app.destroy()


def test_benchmark_hardware_readout_fills_in(view, monkeypatch):
    # 1.3.0 imported a module that never existed, so this stayed "Detecting…".
    from wayfinder.hotkeys.types import EventType
    from wayfinder.utils import system_info

    result, app, root, _downloader = view
    monkeypatch.setattr(
        system_info, "get_system_info",
        lambda: {"cpu": "Test CPU", "gpu": "Test GPU", "ram": "8 GB"},
    )
    app._update_benchmark_results_display = Mock()
    parent = ctk.CTkFrame(root)
    result._build_advanced_benchmark_section(parent)

    kind, callback = app.event_queue.get(timeout=5)
    assert kind == EventType.UI_CALLBACK
    callback()
    texts = [
        widget.cget("text")
        for widget in _all_widgets(parent)
        if isinstance(widget, ctk.CTkLabel)
    ]
    assert "CPU: Test CPU\nGPU: Test GPU\nRAM: 8 GB" in texts


def _all_widgets(widget):
    for child in widget.winfo_children():
        yield child
        yield from _all_widgets(child)

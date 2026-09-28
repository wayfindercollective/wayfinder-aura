"""macOS UI-stack and event-budget contracts."""

from __future__ import annotations

import sys
import importlib.util
from pathlib import Path

import pytest

import wayfinder_main


def test_aqua_idle_matches_linux_while_active_uses_thirty_fps():
    assert wayfinder_main._hero_idle_interval_ms("darwin") == 33
    assert wayfinder_main._hero_active_interval_ms("darwin") == 33
    assert wayfinder_main._tray_pulse_interval_ms("darwin") == 50


def test_aqua_active_hero_uses_thirty_fps_for_fast_audio_motion():
    assert wayfinder_main._hero_active_interval_ms("darwin") == 33


def test_aqua_raw_hero_canvas_tracks_ui_scale_without_changing_linux(monkeypatch):
    assert wayfinder_main._hero_visual_scale(2.0, "darwin") == 2.0
    assert wayfinder_main._hero_visual_scale(1.25, "darwin") == 1.25
    assert wayfinder_main._hero_visual_scale(1.25, "linux") == 1.0
    assert wayfinder_main._hero_canvas_pady("darwin") == (10, 0)
    # Linux draws the same shader ribbon, placed like the Mac's...
    assert wayfinder_main._hero_canvas_pady("linux") == (10, 0)
    # ...unless the old look is restored.
    monkeypatch.setenv("WAYFINDER_LINUX_MAC_LOOK", "0")
    assert wayfinder_main._hero_canvas_pady("linux") == (0, 8)


def test_staged_ctk_scaling_jobs_preserve_dpi_arguments():
    calls = []
    window = object()

    class Tracker:
        widget_scaling = 1.0
        window_scaling = 1.1
        deactivate_automatic_dpi_awareness = False
        window_widgets_dict = {window: [calls.append]}
        window_dpi_scaling_dict = {window: 2.0}

    jobs = wayfinder_main._ctk_scaling_callback_jobs(Tracker, 1.25)

    assert Tracker.widget_scaling == 1.25
    assert jobs == [(calls.append, 2.5, 2.2)]
    assert wayfinder_main._settings_preload_interval_ms("darwin") >= 100


def test_macos_hide_uses_native_application_hide(monkeypatch):
    calls = []

    class NativeApp:
        @staticmethod
        def hide_(sender):
            calls.append(sender)

    class NSApplication:
        @staticmethod
        def sharedApplication():
            return NativeApp()

    appkit = type("AppKit", (), {"NSApplication": NSApplication})
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(wayfinder_main.sys, "platform", "darwin")
    app = type("App", (), {"iconify": lambda self: pytest.fail("fallback used")})()

    wayfinder_main.WayfinderApp.hide_to_tray(app)

    assert calls == [None]


def test_macos_status_menu_defers_until_native_menu_unwinds(monkeypatch):
    import queue

    ran = []
    monkeypatch.setattr(wayfinder_main.sys, "platform", "darwin")
    action_queue = queue.Queue()
    app = type("App", (), {"_tray_action_queue": action_queue})()

    wayfinder_main.WayfinderApp._dispatch_tray_action(
        app, lambda: ran.append(True)
    )

    assert ran == []
    action_queue.get_nowait()()
    assert ran == [True]


def test_macos_lifecycle_notifications_are_marshaled_out_of_appkit_callbacks():
    """PyObjC notification callbacks must never enter Tcl re-entrantly."""
    source = Path(wayfinder_main.__file__).read_text()
    lifecycle_setup = source.split(
        "self._macos_lifecycle_observer = MacOSLifecycleObserver.start(", 1
    )[1].split(
        "if IS_MACOS:", 1
    )[0]

    assert lifecycle_setup.count("EventType.UI_CALLBACK") == 4
    assert "self.after(" not in lifecycle_setup


def test_macos_status_menu_window_action_is_contextual(monkeypatch):
    class NativeApp:
        hidden = False

        @classmethod
        def isHidden(cls):
            return cls.hidden

    class NSApplication:
        @staticmethod
        def sharedApplication():
            return NativeApp

    monkeypatch.setitem(
        sys.modules, "AppKit", type("AppKit", (), {"NSApplication": NSApplication})
    )
    app = object()

    assert (
        wayfinder_main.WayfinderApp._tray_window_action_text(app)
        == "Hide Wayfinder Aura"
    )
    NativeApp.hidden = True
    assert (
        wayfinder_main.WayfinderApp._tray_window_action_text(app)
        == "Show Wayfinder Aura"
    )


def test_macos_idle_status_icon_uses_native_template_rendering(monkeypatch):
    calls = []
    native = type("NativeImage", (), {"setTemplate_": lambda self, value: calls.append(value)})()
    app = type(
        "App", (), {"tray_icon": type("Tray", (), {"_icon_image": native})()}
    )()
    monkeypatch.setattr(wayfinder_main.sys, "platform", "darwin")

    wayfinder_main.WayfinderApp._set_macos_tray_template(app, True)
    wayfinder_main.WayfinderApp._set_macos_tray_template(app, False)

    assert calls == [True, False]


def test_macos_native_hero_is_suspended_below_tabs_and_ultra_scrim():
    hidden = []
    layer = type("Layer", (), {"set_hidden": lambda self, value: hidden.append(value)})()
    app = type(
        "App",
        (),
        {
            "active_tab": "dictate",
            "_premium_banner": None,
            "_macos_hero_layer": layer,
        },
    )()

    assert wayfinder_main.WayfinderApp._macos_hero_is_occluded(app) is False
    app._premium_banner = object()
    assert wayfinder_main.WayfinderApp._macos_hero_is_occluded(app) is True
    wayfinder_main.WayfinderApp._sync_macos_hero_visibility(app)
    assert hidden[-1] is True

    app._premium_banner = None
    app.active_tab = "settings"
    assert wayfinder_main.WayfinderApp._macos_hero_is_occluded(app) is True


def test_settings_wave_animates_on_canvas_while_native_layer_is_hidden(monkeypatch):
    """The always-visible hero must not become a still image on Settings."""
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    _stub_appkit(monkeypatch)
    drawn = []
    scheduled = []
    hidden = []

    class Native:
        native_renderer = object()

        def set_hidden(self, value):
            hidden.append(value)

    class Canvas:
        @staticmethod
        def winfo_viewable():
            return True

    app = type("App", (), {
        "active_tab": "settings",
        "app_state": wayfinder_main.AppState.IDLE,
        "hero_canvas": Canvas(),
        "_macos_hero_layer": Native(),
        "_hero_last_frame_ts": wayfinder_main.time.monotonic(),
        "_hero_morph": 0.0,
        "_hero_wave_time": 0.0,
        "_idle_breath_job": None,
        "state": lambda self: "normal",
        "after": lambda self, ms, fn: scheduled.append(ms) or "job",
        "_animate_idle_breath": lambda self: None,
        "_draw_hero_waveform": lambda self, **kwargs: drawn.append(kwargs),
    })()

    wayfinder_main.WayfinderApp._sync_macos_hero_visibility(app)
    wayfinder_main.WayfinderApp._animate_idle_breath(app)
    assert hidden == [True]
    assert drawn == [{"force_canvas": True}, {}]
    assert scheduled == [66]

    app.active_tab = "dictate"
    wayfinder_main.WayfinderApp._animate_idle_breath(app)
    assert app._idle_breath_job is None
    assert scheduled == [66]


def test_settings_wave_does_not_reactivate_native_layer(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(wayfinder_main, "get_hero_caches", lambda *args: None)
    frame = object()
    monkeypatch.setattr(wayfinder_main, "render_hero_wave", lambda *args, **kwargs: frame)
    pasted = []

    class Photo:
        def width(self):
            return 240

        def height(self):
            return 64

        def paste(self, image):
            pasted.append(image)

    class Canvas:
        def winfo_width(self):
            return 240

        def winfo_height(self):
            return 64

        def itemconfigure(self, *args, **kwargs):
            pass

    class Native:
        def render_wave(self, **kwargs):
            pytest.fail("Native renderer covered Settings")

        def set_image(self, image):
            pytest.fail("Native layer covered Settings")

    app = type("App", (), {
        "active_tab": "settings",
        "hero_canvas": Canvas(),
        "_hero_wave_items_created": True,
        "_hero_wave_image_id": 1,
        "_hero_wave_photo": Photo(),
        "_hero_wave_time": 1.0,
        "_hero_audio_level": 0.0,
        "_hero_morph": 0.0,
        "_macos_hero_layer": Native(),
        "app_state": wayfinder_main.AppState.IDLE,
        "ui_scale": 1.0,
    })()

    wayfinder_main.WayfinderApp._draw_hero_waveform(app)
    assert pasted == [frame]


def test_switching_to_settings_restarts_idle_wave_after_native_handoff(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    events = []

    class Frame:
        def winfo_manager(self):
            return "place"

        def lift(self):
            events.append("lift")

    app = type("App", (), {
        "active_tab": "dictate",
        "app_state": wayfinder_main.AppState.IDLE,
        "_macos_hero_layer": None,
        "tab_buttons": {},
        "tab_colors": {},
        "tab_frames": {"settings": Frame()},
        "_ensure_tab_created": lambda self, tab: events.append("build"),
        "_write_status_breadcrumb": lambda self: None,
        "_start_idle_breath": lambda self: events.append("idle"),
    })()

    wayfinder_main.WayfinderApp._switch_tab(app, "settings")
    assert app.active_tab == "settings"
    assert events == ["build", "lift", "idle"]


def test_tab_switch_raises_persistent_opaque_pages_instead_of_unmapping_them():
    import inspect

    switch_source = inspect.getsource(wayfinder_main.WayfinderApp._switch_tab)
    setup_source = inspect.getsource(wayfinder_main.WayfinderApp.setup_ui)

    assert 'place_in_macos_content_pane(frame)' in switch_source
    assert 'self.tab_frames[tab_id].lift()' in switch_source
    # The content pane is the rounded glass pane (or the opaque Aqua surface
    # without glass); pages inside keep the opaque _tab_surface_kwargs().
    assert '**self._content_pane_kwargs()' in setup_source
    assert wayfinder_main._tab_surface_kwargs("darwin") == {
        "fg_color": wayfinder_main.COLORS["bg_base"],
        "bg_color": wayfinder_main.COLORS["bg_base"],
        "corner_radius": 0,
    }
    assert wayfinder_main._tab_surface_kwargs("linux") == {
        "fg_color": "transparent"
    }


def test_linux_animation_cadence():
    assert wayfinder_main._hero_idle_interval_ms("linux") == 33
    # Recording: 30 fps on desktops like macOS/Windows; Steam hardware keeps
    # 15 fps so the ribbon doesn't compete with transcription for the CPU.
    assert wayfinder_main._hero_active_interval_ms("linux", steam_platform=None) == 33
    assert wayfinder_main._hero_active_interval_ms("linux", steam_platform="deck") == 66
    assert wayfinder_main._tray_pulse_interval_ms("linux") == 50
    assert wayfinder_main._settings_preload_interval_ms("linux") == 25


def test_hero_updates_one_persistent_tk_photo_handle():
    source = Path(wayfinder_main.__file__).read_text()
    assert "photo.paste(img)" in source


def test_pointer_release_containment_uses_geometry_not_hover_state():
    class Widget:
        _mouse_inside = False

        @staticmethod
        def winfo_rootx():
            return 100

        @staticmethod
        def winfo_rooty():
            return 200

        @staticmethod
        def winfo_width():
            return 80

        @staticmethod
        def winfo_height():
            return 40

    inside = type("Event", (), {"x_root": 140, "y_root": 220})()
    outside = type("Event", (), {"x_root": 90, "y_root": 220})()
    assert wayfinder_main._pointer_event_inside_widget(Widget(), inside) is True
    assert wayfinder_main._pointer_event_inside_widget(Widget(), outside) is False


def test_self_focus_injection_error_keeps_its_actionable_guidance(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    message = ("Injection: Wayfinder Aura was frontmost, so there was no external "
               "paste target. Your text is on the clipboard.")
    guidance = wayfinder_main.WayfinderApp._error_guidance(None, message)
    assert "frontmost" in guidance
    assert "clipboard" in guidance
    assert "Accessibility" not in guidance


def test_missing_input_monitoring_stays_visible_with_manual_add_guidance(monkeypatch):
    class Widget:
        def __init__(self, managed=""):
            self.managed = managed
            self.options = {}

        def configure(self, **kwargs):
            self.options.update(kwargs)

        def winfo_manager(self):
            return self.managed

        def pack(self, **kwargs):
            self.managed = "pack"
            self.options["pack"] = kwargs

        def pack_forget(self):
            self.managed = ""

    banner, label, button = Widget(), Widget(), Widget()
    app = type(
        "App",
        (),
        {
            "macos_permission_banner": banner,
            "macos_permission_label": label,
            "macos_permission_open_btn": button,
            "_dictate_banner_anchor": Widget("pack"),
            "_macos_permission_state": lambda self: (True, False),
        },
    )()
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)

    wayfinder_main.WayfinderApp._refresh_macos_permission_banner(app)

    assert app._missing_macos_permission == "input_monitoring"
    assert "click +" in label.options["text"]
    assert "/Applications/Wayfinder Aura.app" in label.options["text"]
    assert button.options["text"] == "Permissions…"
    assert banner.managed == "pack"


def test_macos_build_rejects_non_312_interpreter(monkeypatch):
    path = Path(__file__).parents[1] / "packaging" / "macos" / "build.py"
    spec = importlib.util.spec_from_file_location("wayfinder_macos_build", path)
    assert spec is not None and spec.loader is not None
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)

    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build.platform, "machine", lambda: "arm64")
    if sys.version_info[:2] == build.REQUIRED_PYTHON:
        pytest.skip("test runner already uses the required macOS build interpreter")
    with pytest.raises(SystemExit, match="pinned to Python 3.12"):
        build.require_macos()


def test_macos_docs_do_not_claim_python_311_is_supported():
    text = (Path(__file__).parents[1] / "packaging/macos/README.md").read_text()
    assert "3.11 also works" not in text
    assert "pinned to the 3.12 line" in text


@pytest.mark.parametrize("is_macos, is_windows, expected", [
    (True, False, False), (False, True, False), (False, False, True)])
def test_gated_developer_model_does_not_count_as_usable_on_macos(
        monkeypatch, is_macos, is_windows, expected):
    """A Free Mac/PC with only small.en (Ultra) on disk must still be offered Base."""
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.setattr(wayfinder_main, "IS_MACOS", is_macos)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", is_windows)
    monkeypatch.setattr(
        wayfinder_main,
        "_resolve_whisper_model",
        lambda name: Path("/dev-models") / name if name == "ggml-small.en.bin" else None,
    )
    free_gate = SimpleNamespace(has_feature=lambda _feature: False)
    app = SimpleNamespace(
        config={"transcription_backend": "whisper_cpp", "model_path": "~/Library/x/ggml-base.en.bin"},
        feature_gate=free_gate,
    )

    assert wayfinder_main.WayfinderApp._has_usable_whisper_model(app) is expected


def test_blocked_microphone_banner_opens_the_microphone_pane(monkeypatch):
    from wayfinder.utils import macos_permissions as mp

    class Widget:
        def __init__(self, managed=""):
            self.managed = managed
            self.options = {}

        def configure(self, **kwargs):
            self.options.update(kwargs)

        def winfo_manager(self):
            return self.managed

        def pack(self, **kwargs):
            self.managed = "pack"

        def pack_forget(self):
            self.managed = ""

    banner, label, button = Widget(), Widget(), Widget()
    app = type("App", (), {
        "macos_permission_banner": banner,
        "macos_permission_label": label,
        "macos_permission_open_btn": button,
        "_dictate_banner_anchor": Widget("pack"),
        "_macos_permission_state": lambda self: (True, True),
        "get_hotkey_display": lambda self: "Right Option",
        # Checklist unavailable → falls back to opening the Settings pane.
        "show_permissions_setup": lambda self, force=False: False,
    })()
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(mp, "microphone_authorization", lambda: mp.MIC_DENIED)
    monkeypatch.setattr(mp, "macos_install_location_ready", lambda: True)
    wayfinder_main.WayfinderApp._refresh_macos_permission_banner(app)
    assert app._missing_macos_permission == "microphone"
    assert button.options["text"] == "Open Microphone"

    opened = []
    monkeypatch.setattr(mp, "open_macos_privacy_settings", lambda perm: opened.append(perm) or True)
    wayfinder_main.WayfinderApp._open_missing_macos_permission(app)
    assert opened == ["microphone"]


def _stub_appkit(monkeypatch, hidden=False):
    """An AppKit stand-in (not hidden), so these run where PyObjC is absent."""
    from types import ModuleType, SimpleNamespace

    appkit = ModuleType("AppKit")
    app = SimpleNamespace(isHidden=lambda: hidden)
    appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: app)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)


def test_hidden_window_with_native_hero_does_not_poll(monkeypatch):
    """A withdrawn/hidden window used to re-poll every 250 ms (a Core Animation
    commit each tick) even though the native layer was stopped."""
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    _stub_appkit(monkeypatch)
    hidden_calls = []
    scheduled = []

    class Native:
        native_renderer = object()

        def set_hidden(self, hidden):
            hidden_calls.append(hidden)

    app = type("App", (), {
        "app_state": wayfinder_main.AppState.IDLE,
        "_macos_hero_layer": Native(),
        "_idle_breath_job": "pending",
        "state": lambda self: "withdrawn",
        "after": lambda self, ms, fn: scheduled.append(ms) or "job",
    })()
    wayfinder_main.WayfinderApp._animate_idle_breath(app)
    assert hidden_calls == [True]
    assert scheduled == [] and app._idle_breath_job is None


def test_unhide_restores_the_native_hero(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    _stub_appkit(monkeypatch)
    events = []

    class Native:
        def update_geometry(self):
            events.append("geometry")

    app = type("App", (), {
        "app_state": wayfinder_main.AppState.IDLE,
        "_macos_hero_layer": Native(),
        "_refresh_tray_menu": lambda self: events.append("menu"),
        "_sync_macos_hero_visibility": lambda self: events.append("sync"),
        "_start_idle_breath": lambda self: events.append("breath"),
    })()
    wayfinder_main.WayfinderApp._on_macos_app_visibility_changed(app)
    # NSApplication reports not hidden, so this is the unhide path.
    assert events == ["menu", "geometry", "sync", "breath"]


def test_ctk_dpi_poll_is_idle_on_macos():
    import sys as _sys
    import customtkinter as _ctk
    if _sys.platform == "darwin":
        assert _ctk.ScalingTracker.update_loop_interval >= 60_000
        # The check it would run is a constant on macOS, so nothing is lost.
        assert _ctk.ScalingTracker.get_window_dpi_scaling(None) == 1


def test_settings_footer_names_the_platform():
    assert wayfinder_main._footer_tagline(is_macos=True) == "handcrafted for Mac"
    # Linux keeps its original line; Windows names itself.
    assert wayfinder_main._footer_tagline(is_macos=False, is_windows=False) == "handcrafted for Linux"
    assert wayfinder_main._footer_tagline(is_macos=False, is_windows=True) == "handcrafted for Windows"


def test_paste_failure_guidance_is_platform_specific(monkeypatch):
    ns = object()
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    mac = wayfinder_main.WayfinderApp._error_guidance(ns, "inject failed")
    assert "Accessibility" in mac and "ydotool" not in mac
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    windows = wayfinder_main.WayfinderApp._error_guidance(ns, "inject failed")
    assert "administrator" in windows and "ydotool" not in windows
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    linux = wayfinder_main.WayfinderApp._error_guidance(ns, "inject failed")
    assert linux == ("Couldn't type the text — check input permissions (Settings) "
                     "or install ydotool.")

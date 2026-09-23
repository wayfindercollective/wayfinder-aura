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


def test_aqua_raw_hero_canvas_tracks_ui_scale_without_changing_linux():
    assert wayfinder_main._hero_visual_scale(1.25, "darwin") == 1.25
    assert wayfinder_main._hero_visual_scale(2.0, "darwin") == 2.0
    assert wayfinder_main._hero_visual_scale(1.25, "linux") == 1.0
    assert wayfinder_main._hero_canvas_pady("darwin") == (10, 0)
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


def test_linux_animation_cadence_is_unchanged():
    assert wayfinder_main._hero_idle_interval_ms("linux") == 33
    assert wayfinder_main._hero_active_interval_ms("linux") == 66
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
    assert button.options["text"] == "Open Input Monitoring"
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

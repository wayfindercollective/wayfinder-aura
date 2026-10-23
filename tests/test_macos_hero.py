"""Native Core Animation presentation for the macOS hero waveform."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from wayfinder.ui.macos_hero import MacOSHeroLayer
from wayfinder.ui.macos_overlay_metal import _expanded_wave_frame


@pytest.mark.skipif(sys.platform == "darwin", reason="non-macOS fallback only")
def test_non_macos_attach_is_a_noop():
    assert MacOSHeroLayer.try_create(object(), object()) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="Core Animation is macOS-only")
def test_native_layer_preserves_exact_frame_dimensions():
    from Quartz import (
        CALayer,
        CGColorSpaceCreateWithName,
        CGImageGetHeight,
        CGImageGetWidth,
        kCGColorSpaceSRGB,
    )

    class Canvas:
        @staticmethod
        def winfo_width():
            return 123

        @staticmethod
        def winfo_height():
            return 47

    native = MacOSHeroLayer(
        owner=None,
        canvas=Canvas(),
        ns_window=None,
        layer=CALayer.layer(),
        color_space=CGColorSpaceCreateWithName(kCGColorSpaceSRGB),
    )
    native._size = (123, 47)
    frame = Image.new("RGB", (123, 47), (22, 27, 34))

    assert native.set_image(frame) is True
    assert CGImageGetWidth(native._last_contents) == 123
    assert CGImageGetHeight(native._last_contents) == 47


@pytest.mark.skipif(sys.platform != "darwin", reason="Metal is macOS-only")
def test_metal_hero_shader_compiles_on_the_release_device():
    from wayfinder.ui.macos_hero_metal import MetalHeroRenderer

    renderer = MetalHeroRenderer.try_create(2.0)
    assert renderer is not None
    try:
        assert renderer.pipeline is not None
        assert renderer.command_queue is not None
    finally:
        renderer.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Metal is macOS-only")
def test_metal_overlay_shader_compiles_on_the_release_device():
    from wayfinder.ui.macos_hero_metal import _default_device
    from wayfinder.ui.macos_overlay_metal import _SHADER

    device = _default_device()
    result = device.newLibraryWithSource_options_error_(_SHADER, None, None)
    library, error = result if isinstance(result, tuple) else (result, None)
    assert library is not None, error
    assert library.newFunctionWithName_("overlay_wave") is not None


def test_metal_overlay_frame_preserves_qt_wave_overflow():
    # Qt's QContainerLayer is geometry-flipped: its sublayers use a top-left
    # origin, like the Qt wave box. A standalone unflipped layer uses AppKit's
    # bottom-left origin. Metal still expands four pixels for the soft glow.
    assert _expanded_wave_frame(
        10, 20, 80, 24, 100, geometry_flipped=True
    ) == (6.0, 16.0, 88, 32)
    assert _expanded_wave_frame(
        10, 20, 80, 24, 100, geometry_flipped=False
    ) == (6.0, 52.0, 88, 32)
    # Reserving 15px above the pill for the cancel hint moves both Qt and
    # Metal wave frames down 15px inside the enlarged window. With the old
    # unflipped formula, Metal stayed at y=18 and floated above the pill.
    plain = _expanded_wave_frame(10, 20, 80, 16, 60, geometry_flipped=True)
    hinted = _expanded_wave_frame(10, 35, 80, 16, 75, geometry_flipped=True)
    assert hinted[1] - plain[1] == 15


def test_layout_observer_coalesces_only_anchor_events_and_cleans_up():
    owner = SimpleNamespace(
        bind=Mock(return_value="binding"), after_idle=Mock(return_value="job"),
        after_cancel=Mock(), unbind=Mock(),
    )
    parent = SimpleNamespace(master=owner)
    canvas = SimpleNamespace(master=parent)
    renderer = Mock()
    native = MacOSHeroLayer(owner, canvas, None, None, None, native_renderer=renderer)
    native.update_geometry = Mock()
    native._watch_layout()
    native._on_layout_configure(SimpleNamespace(widget=object()))
    owner.after_idle.assert_not_called()
    for widget in (canvas, parent, owner):
        native._on_layout_configure(SimpleNamespace(widget=widget))
    owner.after_idle.assert_called_once()
    owner.after_idle.call_args.args[0]()
    native.update_geometry.assert_called_once()
    native._on_layout_configure(SimpleNamespace(widget=parent))
    native.close()
    owner.after_cancel.assert_called_once_with("job")
    owner.unbind.assert_called_once_with("<Configure>", "binding")
    renderer.close.assert_called_once()
    native._on_layout_configure(SimpleNamespace(widget=owner))
    assert owner.after_idle.call_count == 2


@pytest.mark.ui
@pytest.mark.skipif(sys.platform != "darwin", reason="Aqua native-layer placement")
@pytest.mark.parametrize("change", ["window_height", "ancestor_position"])
def test_native_hero_tracks_layout_even_when_its_canvas_size_does_not_change(change):
    """Native layers must follow Tk ancestors, not only Canvas resize events."""
    import tkinter as tk

    import customtkinter as ctk

    try:
        root = ctk.CTk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open here: {exc}")
    root.title("Aura native hero placement QA")
    root.geometry("800x780+3000+3000")
    spacer = ctk.CTkFrame(root, height=80)
    spacer.pack(fill="x")
    parent = ctk.CTkFrame(root)
    parent.pack(fill="x")
    canvas = ctk.CTkCanvas(parent, height=64, highlightthickness=0, bg="#151B25")
    canvas.pack(fill="x")
    ctk.CTkButton(root, text="Record", height=40).pack()
    native = None
    try:
        root.update()
        native = MacOSHeroLayer.try_create(root, canvas)
        assert native is not None
        layer = native.layer
        if layer is None:
            layer = next(
                layer for layer in native.ns_window.contentView().layer().sublayers()
                if str(layer.name()) == "WayfinderHeroWaveformMetalNative"
            )
        before_size = (canvas.winfo_width(), canvas.winfo_height())
        if change == "window_height":
            root.geometry("800x960+3000+3000")
        else:
            spacer.configure(height=20)
        root.update()
        assert (canvas.winfo_width(), canvas.winfo_height()) == before_size
        actual_top = (
            native.ns_window.contentView().bounds().size.height
            - layer.frame().origin.y - layer.frame().size.height
        )
        assert actual_top == pytest.approx(canvas.winfo_rooty() - root.winfo_rooty(), abs=1)
        assert layer.frame().size.width == canvas.winfo_width()
        assert layer.frame().size.height == canvas.winfo_height()
    finally:
        if native is not None:
            native.close()
        root.destroy()

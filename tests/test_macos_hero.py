"""Native Core Animation presentation for the macOS hero waveform."""

from __future__ import annotations

import sys

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
    # Qt's (10, 20, 80, 24) wave box maps to y=56 in a 100px-tall native
    # content view. Metal expands four pixels on every edge, so the glow can
    # breathe outside that layout box exactly as the QPainter pen did.
    assert _expanded_wave_frame(10, 20, 80, 24, 100) == (6.0, 52.0, 88, 32)

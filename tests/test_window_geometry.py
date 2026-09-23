"""Default window geometry: right half on Linux, content-sized on macOS."""

from wayfinder.ui.window_geometry import (
    MACOS_DEFAULT_H,
    MACOS_DEFAULT_W,
    MACOS_TITLEBAR_H,
    MIN_H,
    default_window_geometry,
)


def test_linux_keeps_right_half_layout():
    assert default_window_geometry(1920, 1080, platform="linux") == (960, 992, 960, 32)


def test_macos_is_content_sized_and_centred_in_visible_frame():
    # 1920x1080 screen with a 30pt menu bar and a 78pt Dock.
    width, height, x, y = default_window_geometry(
        1920, 1080, platform="darwin", visible_frame=(0, 30, 1920, 972)
    )
    assert (width, height) == (MACOS_DEFAULT_W, MACOS_DEFAULT_H)
    assert x == (1920 - width) // 2
    assert y == 30 + (972 - height - MACOS_TITLEBAR_H) // 2
    # Far from the old right-half, full-height default.
    assert width < 1920 // 2 and height < 972


def test_macos_small_screen_stays_inside_visible_frame():
    width, height, x, y = default_window_geometry(
        1280, 720, platform="darwin", visible_frame=(0, 25, 1280, 620)
    )
    assert x >= 0 and x + width <= 1280
    assert y >= 25 and y + height + MACOS_TITLEBAR_H <= 25 + 620
    assert height >= MIN_H

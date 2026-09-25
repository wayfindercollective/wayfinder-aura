"""Default (first-run / rescue) geometry for the main Aura window.

Linux keeps the original layout: the right half of the screen at full usable
height. On macOS that reads as oversized, so the window is sized to its
content -- the 188pt sidebar beside the 520x360 welcome card, under the header
and hero -- and centred in the screen's visible frame (menu bar and Dock
excluded). Windows opens the same content-sized window, centred in the work
area (taskbar excluded), so both ports look and open alike.
"""

from __future__ import annotations

import sys

# Linux panels the right-half layout reserves.
LINUX_TASKBAR_H = 56
LINUX_TOP_PANEL_H = 32

# macOS content-sized default (Tk geometry: content size, title bar excluded).
MACOS_DEFAULT_W = 800
MACOS_DEFAULT_H = 780
MACOS_TITLEBAR_H = 28
MACOS_EDGE_MARGIN = 24
MACOS_MENU_BAR_H = 25  # fallback when AppKit is unavailable

# Windows: the Mac's content size, in the same logical pixels (the Tk process
# is not per-monitor DPI aware, so Windows scales it like macOS points).
WINDOWS_TITLEBAR_H = 32
WINDOWS_EDGE_MARGIN = 24

# Matches the window's minsize().
MIN_W = 360
MIN_H = 500


def macos_visible_frame(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Primary screen's visible frame as (x, y, w, h) with a top-left origin."""
    try:
        import AppKit

        screen = AppKit.NSScreen.screens()[0]
        full = screen.frame()
        visible = screen.visibleFrame()
        x = int(visible.origin.x - full.origin.x)
        # Cocoa's origin is bottom-left; Tk's is top-left.
        y = int((full.origin.y + full.size.height) - (visible.origin.y + visible.size.height))
        return x, y, int(visible.size.width), int(visible.size.height)
    except Exception:
        return 0, MACOS_MENU_BAR_H, screen_w, max(0, screen_h - MACOS_MENU_BAR_H)


def windows_work_area(screen_w: int, screen_h: int) -> tuple[int, int, int, int]:
    """Primary monitor's work area (taskbar excluded) as (x, y, w, h)."""
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, screen_w, max(0, screen_h - 48)


def default_window_geometry(
    screen_w: int,
    screen_h: int,
    platform: str | None = None,
    visible_frame: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """Return (width, height, x, y) for a window with no usable saved geometry."""
    platform = platform or sys.platform
    if platform == "darwin":
        vx, vy, vw, vh = visible_frame or macos_visible_frame(screen_w, screen_h)
        width = max(MIN_W, min(MACOS_DEFAULT_W, vw - 2 * MACOS_EDGE_MARGIN))
        height = max(MIN_H, min(MACOS_DEFAULT_H, vh - 2 * MACOS_EDGE_MARGIN - MACOS_TITLEBAR_H))
        x = vx + max(0, (vw - width) // 2)
        y = vy + max(0, (vh - height - MACOS_TITLEBAR_H) // 2)
        return width, height, x, y
    if platform == "win32":
        vx, vy, vw, vh = visible_frame or windows_work_area(screen_w, screen_h)
        width = max(MIN_W, min(MACOS_DEFAULT_W, vw - 2 * WINDOWS_EDGE_MARGIN))
        height = max(MIN_H, min(MACOS_DEFAULT_H, vh - 2 * WINDOWS_EDGE_MARGIN - WINDOWS_TITLEBAR_H))
        x = vx + max(0, (vw - width) // 2)
        y = vy + max(0, (vh - height - WINDOWS_TITLEBAR_H) // 2)
        return width, height, x, y

    usable_h = screen_h - LINUX_TASKBAR_H - LINUX_TOP_PANEL_H
    return screen_w // 2, usable_h, screen_w // 2, LINUX_TOP_PANEL_H

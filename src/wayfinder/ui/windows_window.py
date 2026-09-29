"""Native window chrome for the main Aura window on Windows.

The Windows counterpart of ``macos_window``'s unified title bar: the caption
bar takes the app's own deep-ink colour and a dark theme (white caption
buttons and title), and the window border matches the panes' rim, so the
title bar reads as part of the app instead of a light system strip. Uses
``DwmSetWindowAttribute``; Windows 10 1809+ honours the dark mode flag,
Windows 11 also the caption/border colours and rounded corners. Every call
is best effort and does nothing elsewhere.
"""

from __future__ import annotations

import ctypes
import sys

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1 = 19
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWCP_ROUND = 2


def colorref(hex_color: str) -> int:
    """'#RRGGBB' -> COLORREF (0x00BBGGRR)."""
    value = hex_color.lstrip("#")
    r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return (b << 16) | (g << 8) | r


def _hwnd(root) -> int:
    """The top-level frame window Tk wraps around the root widget."""
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]
    return user32.GetParent(root.winfo_id()) or root.winfo_id()


def _set(hwnd: int, attribute: int, value: int) -> bool:
    data = ctypes.c_int(value)
    dwmapi = ctypes.windll.dwmapi
    dwmapi.DwmSetWindowAttribute.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32]
    return dwmapi.DwmSetWindowAttribute(
        hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data)) == 0


def apply_window_chrome(root, caption: str, border: str, text: str = "#E6EDF3") -> bool:
    """Dark caption bar in *caption*, *border* edge, rounded corners. True if applied."""
    if sys.platform != "win32":
        return False
    try:
        root.update_idletasks()
        hwnd = _hwnd(root)
        dark = _set(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1) or _set(
            hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1, 1)
        _set(hwnd, _DWMWA_CAPTION_COLOR, colorref(caption))
        _set(hwnd, _DWMWA_BORDER_COLOR, colorref(border))
        _set(hwnd, _DWMWA_TEXT_COLOR, colorref(text))
        _set(hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND)
        return dark
    except Exception:
        return False


_RDW_INVALIDATE = 0x0001
_RDW_ERASE = 0x0004
_RDW_ALLCHILDREN = 0x0080
_RDW_UPDATENOW = 0x0100
_RDW_FRAME = 0x0400

# One-shot, and above the app's 100 ms timer floor.
REPAINT_DELAY_MS = 120


def repaint(root) -> bool:
    """Invalidate and repaint the whole window, every child widget included."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.RedrawWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        flags = (_RDW_INVALIDATE | _RDW_ERASE | _RDW_FRAME
                 | _RDW_ALLCHILDREN | _RDW_UPDATENOW)
        return bool(user32.RedrawWindow(_hwnd(root), None, None, flags))
    except Exception:
        return False


def repaint_after_activation(root, delay_ms: int = REPAINT_DELAY_MS) -> bool:
    """Repaint the window once after it gains/loses focus or (re)maps a page.

    On some GPU setups (hardware GPU scheduling, two adapters) Windows hands
    back parts of the window's image black after an activation change and
    never asks Tk to repaint them, so small black rectangles stay where CTk
    widgets are until each happens to redraw. One full repaint shortly after
    restores them. Focus and Map events arrive once per child widget, so the
    repaint is coalesced into a single pending ``after``. True if installed.
    """
    if sys.platform != "win32":
        return False
    pending = {"id": None}

    def _run():
        pending["id"] = None
        repaint(root)

    def _schedule(_event=None):
        if pending["id"] is None:
            try:
                pending["id"] = root.after(delay_ms, _run)
            except Exception:
                pending["id"] = None

    try:
        for sequence in ("<FocusIn>", "<FocusOut>", "<Map>"):
            root.bind(sequence, _schedule, add="+")  # CTk binds <FocusIn> itself
        return True
    except Exception:
        return False


_SPI_GETCLIENTAREAANIMATION = 0x1042


def animations_enabled() -> bool:
    """Windows "Animation effects" (Settings > Accessibility > Visual effects).

    Off is Windows' Reduce Motion: idle waves then hold still, as on the Mac.
    True elsewhere or when unknown.
    """
    if sys.platform != "win32":
        return True
    try:
        value = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(
                _SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(value), 0):
            return bool(value.value)
    except Exception:
        pass
    return True

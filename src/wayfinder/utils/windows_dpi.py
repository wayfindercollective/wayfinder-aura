"""Sharp text and graphics on scaled Windows displays (125%, 150%, 175%...).

A Tk process that doesn't declare DPI awareness is drawn at 96 DPI and then
bitmap-stretched by Windows, so on a 175% laptop every glyph, rim and the hero
ribbon come out soft. The Mac never has this: Aqua draws in points at the
screen's native resolution.

``enable()`` declares the process system-DPI aware before the first window
exists, so Tk draws at the display's real resolution, and returns the Windows
scale factor (1.75 at 175%). The app keeps its geometry maths in the same
logical pixels as before (what saved configs hold) and multiplies by that
factor only where a size reaches Tk: CTk's widget scaling, window geometry,
minsize and the raw hero canvas. The on-screen size is unchanged; only the
sharpness improves.

System-aware (not per-monitor): a second monitor with a different scale is
stretched by Windows exactly as today. ``WAYFINDER_WINDOWS_DPI_AWARE=0``
restores the old behaviour. No-op everywhere but Windows.

Awareness can be set only once per process. If something declared it first
(the executable's manifest, the host Python), Windows refuses ours, but Tk
still draws at real resolution, so the scale factor is applied all the same.
"""

from __future__ import annotations

import os
import sys

_PROCESS_SYSTEM_DPI_AWARE = 1

_scale = 1.0


def scale() -> float:
    """The Windows scale factor this process renders at (1.0 unless enabled)."""
    return _scale


def _system_dpi() -> int:
    import ctypes

    user32 = ctypes.windll.user32
    try:
        dpi = int(user32.GetDpiForSystem())          # Windows 10 1607+
        if dpi > 0:
            return dpi
    except Exception:
        pass
    hdc = user32.GetDC(None)
    try:
        return int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
    finally:
        user32.ReleaseDC(None, hdc)


def _already_aware() -> bool:
    """Whether the process is DPI aware without us (system or per-monitor)."""
    import ctypes

    try:
        value = ctypes.c_int(0)
        if ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(value)) == 0:
            return value.value > 0
    except Exception:
        pass
    try:
        return bool(ctypes.windll.user32.IsProcessDPIAware())
    except Exception:
        return False


def enable() -> float:
    """Declare system DPI awareness (call before the Tk root exists) and return
    the scale factor. Returns 1.0, changing nothing, off Windows, when disabled,
    or if Windows refuses."""
    global _scale
    if sys.platform != "win32" or os.environ.get("WAYFINDER_WINDOWS_DPI_AWARE", "1") == "0":
        return _scale
    try:
        import ctypes

        try:
            hr = ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_SYSTEM_DPI_AWARE)
            ok = hr == 0 or _already_aware()
        except Exception:
            ok = bool(ctypes.windll.user32.SetProcessDPIAware())   # Vista+ fallback
        if not ok:
            return _scale
        dpi = _system_dpi()
        if 96 <= dpi <= 96 * 5:
            _scale = dpi / 96.0
    except Exception:
        pass
    return _scale


def to_px(value: float) -> int:
    """Logical pixels (the app's geometry units) to real pixels."""
    return int(round(value * _scale))


def to_logical(value: float) -> int:
    """Real pixels (what Tk reports) to the app's logical pixels."""
    return int(round(value / _scale))

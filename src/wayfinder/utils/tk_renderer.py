"""Release probe for the Linux Tk font-rendering boundary.

CustomTkinter requires an Xft-enabled Tk build on X11/XWayland.  A Tk build
without Xft still imports and creates windows, but exposes only legacy bitmap
fonts: requested product families and sizes silently collapse to ``fixed``.
That failure is visual rather than an exception, so ordinary boot smoke tests
cannot detect it.
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
from typing import Callable


@dataclass(frozen=True)
class TkRendererResult:
    patchlevel: str
    windowing_system: str
    font_system: str
    requested_family: str
    actual_family: str
    family_count: int


def probe_tk_renderer(
    *,
    root_factory: Callable[[], object] | None = None,
    font_factory: Callable[..., object] | None = None,
) -> TkRendererResult:
    """Create a hidden root and report the renderer used by product fonts."""
    if root_factory is None or font_factory is None:
        import tkinter as tk
        import tkinter.font as tkfont

        root_factory = root_factory or tk.Tk
        font_factory = font_factory or tkfont.Font

    root = root_factory()
    try:
        root.withdraw()
        patchlevel = str(root.tk.call("info", "patchlevel"))
        windowing_system = str(root.tk.call("tk", "windowingsystem"))
        try:
            font_system = str(
                root.tk.call("::tk::pkgconfig", "get", "fontsystem")
            )
        except Exception:
            font_system = "unknown"

        families = tuple(root.tk.splitlist(root.tk.call("font", "families")))
        requested_family = "DejaVu Sans"
        font = font_factory(root=root, family=requested_family, size=-26)
        actual_family = str(font.actual("family"))
        return TkRendererResult(
            patchlevel=patchlevel,
            windowing_system=windowing_system,
            font_system=font_system,
            requested_family=requested_family,
            actual_family=actual_family,
            family_count=len(families),
        )
    finally:
        root.destroy()


def require_compatible_tk_renderer(**kwargs) -> TkRendererResult:
    """Raise when Linux/X11 would silently render the legacy bitmap UI."""
    result = probe_tk_renderer(**kwargs)
    if not sys.platform.startswith("linux") or result.windowing_system != "x11":
        return result

    failures = []
    if result.font_system.casefold() != "xft":
        failures.append(f"font system is {result.font_system!r}, not 'xft'")
    if result.actual_family.casefold() != result.requested_family.casefold():
        failures.append(
            f"{result.requested_family!r} resolves to {result.actual_family!r}"
        )
    if result.family_count < 10:
        failures.append(f"only {result.family_count} font families are visible")

    if failures:
        raise RuntimeError("incompatible Tk renderer: " + "; ".join(failures))
    return result

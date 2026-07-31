"""Make Tk render pixel-sized fonts as actual pixels on every machine.

Tk converts negative (pixel) font sizes to points using its `tk scaling`
factor (initialized from the X screen, virtually always 96dpi under
XWayland), but Xft renders those points at the Xft.dpi X resource (192 on a
200% desktop). When the two disagree every font inflates by their ratio
while widget dimensions stay put — the "weirdly large text" 1.1.4 field
report. Tk 8.6.13+ fixed the named-font path but not the font-descriptor
path CustomTkinter uses, and the AppImage bundles jammy's Tk 8.6.12 where
both paths inflate. Setting `tk scaling` to Xft.dpi/72 makes every path
pixel-exact on every Tk version we ship or run from source (validated
empirically on 8.6.12 and 8.6.14 against a live 192-dpi KDE session).

Font size tokens are calibrated for this pixel-exact regime; without the
normalization they would render at half the intended size on 96-dpi-resource
machines, which is why the fallback assumes 96 rather than skipping.
"""

from __future__ import annotations

import re
import subprocess

from wayfinder.utils.hostexec import host_env

_XFT_DPI_RE = re.compile(r"^Xft\.dpi:\s*(\d+(?:\.\d+)?)\s*$", re.MULTILINE)


def read_xft_dpi() -> float | None:
    """Xft.dpi from the X resource database, or None if unavailable.

    xrdb is spawned with a host-clean environment: it is a host binary and
    the bundled jammy libraries would break it exactly like qdbus.
    """
    try:
        result = subprocess.run(
            ["xrdb", "-query"],
            capture_output=True,
            text=True,
            timeout=3,
            env=host_env(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = _XFT_DPI_RE.search(result.stdout or "")
    if not match:
        return None
    try:
        dpi = float(match.group(1))
    except ValueError:
        return None
    return dpi if 48.0 <= dpi <= 1024.0 else None


def normalize_tk_font_dpi(root) -> float:
    """Set `tk scaling` so pixel font sizes render pixel-exact. Returns the DPI used.

    Must run after the Tk root exists and before any font/widget is created.
    Best-effort: on failure Tk keeps its own default (correct on 96-dpi
    resource machines, the same machines where xrdb tends to be absent).
    """
    dpi = read_xft_dpi() or 96.0
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        return 96.0
    return dpi

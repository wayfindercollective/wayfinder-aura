"""Clean environment for spawning HOST binaries from a frozen/AppImage run.

PyInstaller points LD_LIBRARY_PATH at its extraction dir and the AppImage
AppRun prepends the mount to PATH/LD_LIBRARY_PATH before that, so a host
binary spawned with the inherited environment loads the bundle's (older)
libraries — host qdbus dies on Fedora 44 with "CXXABI_1.3.15 not found"
against the bundled jammy libstdc++, killing overlay placement; browsers
launched via xdg-open can fail the same way. LD_LIBRARY_PATH_ORIG is no
escape hatch: AppRun already polluted it. The only safe environment for a
host binary strips every bundle-owned path entry and bundle-only variable.

Bundled binaries (wtype, whisper-cli, llama server, the overlay subprocess)
NEED the bundle environment — never use this helper for them.
"""

from __future__ import annotations

import os
import sys

# Path-list variables that may contain bundle entries worth keeping otherwise.
_PATHLIST_VARS = ("PATH", "LD_LIBRARY_PATH", "XDG_DATA_DIRS", "GI_TYPELIB_PATH")

# Variables that only make sense inside the bundle process itself.
_DROP_VARS = (
    "LD_LIBRARY_PATH_ORIG",
    "PYTHONPATH",
    "PYTHONHOME",
    "TCL_LIBRARY",
    "TK_LIBRARY",
    "FONTCONFIG_FILE",  # AppRun points this at the bundle's font config
    "GDK_PIXBUF_MODULE_FILE",
)

_FALLBACK_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


def bundle_roots() -> list[str]:
    """Real paths of directories owned by the frozen bundle, if any."""
    roots = []
    for root in (getattr(sys, "_MEIPASS", None), os.environ.get("APPDIR")):
        if root:
            try:
                roots.append(os.path.realpath(root))
            except OSError:
                pass
    return roots


def _is_under(path: str, roots: list[str]) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return any(real == r or real.startswith(r + os.sep) for r in roots)


def host_env(overrides: dict | None = None) -> dict:
    """os.environ minus everything the bundle injected, for host binaries.

    Outside a bundle (source runs) this is os.environ.copy() plus overrides —
    call sites don't need to special-case frozen vs source.
    """
    env = os.environ.copy()
    roots = bundle_roots()
    if roots:
        for var in _PATHLIST_VARS:
            value = env.get(var)
            if not value:
                continue
            kept = [p for p in value.split(os.pathsep) if p and not _is_under(p, roots)]
            if kept:
                env[var] = os.pathsep.join(kept)
            else:
                env.pop(var, None)
        for var in _DROP_VARS:
            env.pop(var, None)
        env.setdefault("PATH", _FALLBACK_PATH)
    if overrides:
        env.update(overrides)
    return env

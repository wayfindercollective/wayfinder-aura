"""Resolve packaged native executables without persisting mount-point paths."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from wayfinder.utils.platform import get_wayfinder_appimage_dir, is_wayfinder_flatpak_env


def _executable(path: Path | str | None) -> str | None:
    if not path:
        return None
    expanded = Path(os.path.expanduser(str(path)))
    try:
        if expanded.is_file() and os.access(expanded, os.X_OK):
            return str(expanded)
    except OSError:
        pass
    return None


def find_whisper_binary(config: dict | None = None, *, cpu: bool = False) -> str | None:
    """Find the current runtime's whisper CLI, including an AppImage mount.

    AppImage mount paths change on every launch, so callers must resolve them
    from ``APPDIR`` rather than copying a path into their own lookup table.
    The CPU query prefers the independently linked ``whisper-cli-cpu`` sibling
    and falls back to the primary binary for packages without a safety twin.
    """
    configured = str((config or {}).get("whisper_binary") or "").strip()
    names = ["whisper-cli-cpu", "whisper-cli"] if cpu else ["whisper-cli"]
    candidates: list[Path | str] = []

    # A prior AppImage's FUSE mount can remain visible after exit: stat/access
    # may still claim its files are executable, but exec then fails with
    # ENOTCONN ("Transport endpoint is not connected"). The current APPDIR is
    # authoritative and must precede any persisted/configured mount path.
    appdir = get_wayfinder_appimage_dir()
    if appdir is not None:
        candidates.extend(appdir / "usr" / "bin" / name for name in names)

    if configured:
        configured_path = Path(os.path.expanduser(configured))
        if cpu and configured_path.name == "whisper-cli":
            candidates.append(configured_path.with_name("whisper-cli-cpu"))
        candidates.append(configured_path)

    if is_wayfinder_flatpak_env():
        candidates.extend(Path("/app/bin") / name for name in names)

    candidates.extend(
        Path.home() / "whisper.cpp" / "build" / "bin" / name
        for name in names
    )
    candidates.extend(Path("/usr/bin") / name for name in names)
    candidates.extend(Path("/usr/local/bin") / name for name in names)

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        resolved = _executable(candidate)
        if resolved:
            return resolved

    for name in names:
        resolved = _executable(shutil.which(name))
        if resolved:
            return resolved
    return None

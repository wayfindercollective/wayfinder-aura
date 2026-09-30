"""Resolve packaged native executables without persisting mount-point paths."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

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


def find_bundled_binary(name: str) -> str | None:
    """Find a native executable shipped inside a frozen desktop bundle.

    PyInstaller 6 places macOS binaries below ``Contents/Frameworks`` and
    exposes matching links below ``Contents/Resources``. Check both layouts so
    this also remains compatible with older PyInstaller bundles.
    """
    if not getattr(sys, "frozen", False):
        return None

    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "bin" / name)

    executable = Path(sys.executable).resolve()
    if executable.parent.name == "MacOS":
        contents = executable.parent.parent
        candidates.extend(
            [
                contents / "Resources" / "bin" / name,
                contents / "Frameworks" / "bin" / name,
            ]
        )

    for candidate in candidates:
        resolved = _executable(candidate)
        if resolved:
            return resolved
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

    bundled = find_bundled_binary(names[0])
    if bundled:
        candidates.append(bundled)
    if cpu:
        bundled_primary = find_bundled_binary("whisper-cli")
        if bundled_primary:
            candidates.append(bundled_primary)

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


def find_llama_binary(config: dict | None = None) -> str | None:
    """Find the current runtime's llama.cpp CLI, preferring llama-simple.

    ``llama-simple`` is Aura's reliable positional-prompt fallback, while
    ``llama-cli`` remains useful for installations that do not provide it.
    """
    configured = str((config or {}).get("llama_cpp_binary") or "").strip()
    names = ("llama-simple", "llama-cli", "llama")
    candidates: list[Path | str] = []

    for name in names:
        bundled = find_bundled_binary(name)
        if bundled:
            candidates.append(bundled)

    if configured:
        configured_path = Path(os.path.expanduser(configured))
        candidates.extend(configured_path.with_name(name) for name in names)
        candidates.append(configured_path)

    if is_wayfinder_flatpak_env():
        candidates.extend(Path("/app/bin") / name for name in names)

    candidates.extend(
        Path.home() / "llama.cpp" / "build" / "bin" / name
        for name in names
    )
    candidates.extend(Path("/usr/local/bin") / name for name in names)
    candidates.extend(Path("/opt/homebrew/bin") / name for name in names)

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

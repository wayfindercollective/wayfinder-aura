"""
Filesystem helpers for owner-only secret / transcript files.

Used by config, license, voice profile, logs, and control-socket path setup.
Best-effort on platforms that ignore Unix modes.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]


def restrict_owner_only(path: PathLike) -> None:
    """Best-effort chmod 0o600 for secret/transcript-bearing files."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def restrict_dir_owner_only(path: PathLike) -> None:
    """Best-effort chmod 0o700 for app-private directories."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def ensure_private_dir(path: PathLike) -> Path:
    """Create directory (parents ok) and set mode 0o700 when possible."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    restrict_dir_owner_only(p)
    return p


def atomic_write_text(path: PathLike, text: str, mode: int = 0o600) -> None:
    """Write text via same-dir temp file, chmod *mode* before replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        try:
            os.chmod(tmp_name, mode)
        except OSError:
            pass
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: PathLike, data: Any, mode: int = 0o600) -> None:
    """Serialize *data* as indented JSON via atomic_write_text."""
    atomic_write_text(path, json.dumps(data, indent=2) + "\n", mode=mode)


def owner_only_opener(path: str, flags: int) -> int:
    """opener for open()/FileHandler so new files are created 0o600."""
    return os.open(path, flags, 0o600)


def get_app_temp_dir() -> Path:
    """Return app-owned temp directory for recordings (mode 0700).

    Prefer XDG cache so Flatpak sandboxes get a writable private path without
    new finish-args. Falls back to system temp + wayfinder-aura-<uid>.
    """
    cache_home = os.environ.get("XDG_CACHE_HOME")
    if cache_home:
        base = Path(cache_home) / "wayfinder-aura" / "tmp"
    else:
        base = Path.home() / ".cache" / "wayfinder-aura" / "tmp"
    return ensure_private_dir(base)


def cleanup_app_temp_dir(max_age_seconds: float | None = None) -> int:
    """Remove files under the app temp dir only. Returns count removed.

    Never touches host /tmp generally. If *max_age_seconds* is set, only delete
    files older than that; if None, delete all regular files in the dir.
    """
    d = get_app_temp_dir()
    removed = 0
    try:
        entries = list(d.iterdir())
    except OSError:
        return 0
    import time

    now = time.time()
    for p in entries:
        try:
            if not p.is_file():
                continue
            if max_age_seconds is not None:
                age = now - p.stat().st_mtime
                if age < max_age_seconds:
                    continue
            p.unlink(missing_ok=True)
            removed += 1
        except OSError:
            pass
    return removed

"""Game Mode light ASR profile selection.

When SteamOS Game Mode dictation is active, prefer a smaller/faster ASR
profile so dictation steals less frame budget from the game. This module is
pure: it returns a *copy* of the effective settings and never writes config
to disk — the user's Desktop model preference is left untouched.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from wayfinder.config import IS_FLATPAK

# Prefer English tiny/base first (Deck default is base.en); then multilingual.
_LIGHT_BASENAMES = (
    "ggml-tiny.en.bin",
    "ggml-base.en.bin",
    "ggml-tiny.bin",
    "ggml-base.bin",
    "ggml-small.en.bin",
    "ggml-small.bin",
)


def _existing_file(path: str) -> bool:
    if not path:
        return False
    if path.startswith("/app/") and not IS_FLATPAK:
        return False
    try:
        p = Path(os.path.expanduser(path))
        return p.is_file()
    except Exception:
        return False


def _pick_light_model_path(user_model_path: str, explicit: str) -> str | None:
    """Resolve a light model file path, or None to keep the user's model."""
    explicit = (explicit or "").strip()
    if explicit and _existing_file(explicit):
        return os.path.expanduser(explicit)

    search_dirs: list[str] = []
    cur = os.path.expanduser(user_model_path or "")
    if cur:
        d = os.path.dirname(cur)
        if d:
            search_dirs.append(d)
    # Flatpak bundled models
    if IS_FLATPAK:
        search_dirs.append("/app/share/whisper-models")
    # Common download locations (best-effort)
    home = os.path.expanduser("~")
    for extra in (
        os.path.join(home, ".local", "share", "wayfinder-aura", "models"),
        os.path.join(home, "whisper.cpp", "models"),
    ):
        if extra not in search_dirs:
            search_dirs.append(extra)

    for directory in search_dirs:
        for name in _LIGHT_BASENAMES:
            candidate = os.path.join(directory, name)
            if _existing_file(candidate):
                # Don't "downgrade" to a larger-or-equal file than the user already uses
                # when the only hits are medium/large — light basenames are all small.
                return candidate
    return None


def _is_heavy_local_model(path_or_name: str) -> bool:
    n = (path_or_name or "").lower()
    return any(tok in n for tok in ("large", "medium", "turbo"))


def effective_asr_config(config: dict[str, Any], game_mode_active: bool) -> dict[str, Any]:
    """Return ASR settings for this session.

    * When Game Mode is inactive, or ``game_mode_light_asr`` is False, returns
      a shallow copy of *config* unchanged (Desktop path).
    * When Game Mode dictation is active and light ASR is enabled, returns a
      shallow copy with lighter accuracy / GPU / model overrides. The input
      dict is never mutated and never written to disk.
    """
    base = dict(config)
    if not game_mode_active or not base.get("game_mode_light_asr", True):
        return base

    out = dict(base)
    out["accuracy_mode"] = base.get("game_mode_accuracy_mode", "fast") or "fast"
    out["use_gpu"] = bool(base.get("game_mode_use_gpu", False))

    # faster-whisper size name (cloud backends are left alone — they don't burn local GPU/CPU).
    backend = (base.get("transcription_backend") or "whisper_cpp").lower()
    if backend == "faster_whisper":
        light_fw = (base.get("game_mode_faster_whisper_model") or "base").strip()
        if light_fw:
            out["faster_whisper_model"] = light_fw
        # Prefer int8 on CPU for handhelds when GPU is off.
        if not out["use_gpu"]:
            out["faster_whisper_compute_type"] = base.get(
                "game_mode_faster_whisper_compute_type", "int8"
            )

    if backend not in ("groq_whisper", "openai_whisper"):
        light_path = _pick_light_model_path(
            str(base.get("model_path") or ""),
            str(base.get("game_mode_model_path") or ""),
        )
        if light_path:
            out["model_path"] = light_path
        elif _is_heavy_local_model(str(base.get("model_path") or "")):
            # No light weight on disk — still apply fast/CPU overrides above so we
            # steal less frame budget even on the heavy model.
            pass

    # Cap threads so the game keeps cores; never raise above the user's setting.
    try:
        threads = int(base.get("threads") or 4)
    except (TypeError, ValueError):
        threads = 4
    out["threads"] = max(1, min(threads, 2))

    return out

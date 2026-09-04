#!/usr/bin/env python3
"""Native, side-effect-free smoke checks for Aura's platform contract.

This script is intentionally headless: it must not open a window, microphone,
global listener, or clipboard. CI runs it on native Linux, macOS, and Windows
hosts to catch unconditional platform imports and path regressions.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

SUPPORTED_PLATFORMS = ("linux", "darwin", "windows")
SMOKE_MODULES = (
    "wayfinder",
    "wayfinder.config",
    "wayfinder.state",
    "wayfinder.utils.platform",
    "wayfinder.core.injector",
    "wayfinder.core.recorder",
    "wayfinder.core.transcriber",
    "wayfinder.core.postprocessor",
    "wayfinder.hotkeys",
)


def _assert_path_contract(platform_name: str) -> None:
    from wayfinder.utils.platform import get_cache_dir, get_config_dir, get_data_dir

    config_dir = get_config_dir()
    data_dir = get_data_dir()
    cache_dir = get_cache_dir()

    for label, path in (
        ("config", config_dir),
        ("data", data_dir),
        ("cache", cache_dir),
    ):
        if not path.is_absolute():
            raise RuntimeError(f"{label} directory is not absolute: {path}")
        if path.name != "wayfinder-aura" and not (
            label == "cache" and path.parent.name == "wayfinder-aura"
        ):
            raise RuntimeError(f"{label} directory is outside the Aura namespace: {path}")

    normalized_config = str(config_dir).replace("\\", "/")
    normalized_data = str(data_dir).replace("\\", "/")
    normalized_cache = str(cache_dir).replace("\\", "/")

    if platform_name == "darwin":
        if "/Library/Application Support/wayfinder-aura" not in normalized_config:
            raise RuntimeError(f"unexpected macOS config directory: {config_dir}")
        if "/Library/Application Support/wayfinder-aura" not in normalized_data:
            raise RuntimeError(f"unexpected macOS data directory: {data_dir}")
        if "/Library/Caches/wayfinder-aura" not in normalized_cache:
            raise RuntimeError(f"unexpected macOS cache directory: {cache_dir}")
    elif platform_name == "windows":
        appdata = Path(os.environ["APPDATA"])
        localappdata = Path(os.environ["LOCALAPPDATA"])
        if config_dir != appdata / "wayfinder-aura":
            raise RuntimeError(f"unexpected Windows config directory: {config_dir}")
        if data_dir != localappdata / "wayfinder-aura":
            raise RuntimeError(f"unexpected Windows data directory: {data_dir}")
        if cache_dir != localappdata / "wayfinder-aura" / "cache":
            raise RuntimeError(f"unexpected Windows cache directory: {cache_dir}")


def run(expected: str) -> None:
    for module_name in SMOKE_MODULES:
        importlib.import_module(module_name)

    from wayfinder.utils.platform import get_platform

    actual = get_platform()
    if actual != expected:
        raise RuntimeError(f"expected native platform {expected!r}, detected {actual!r}")

    _assert_path_contract(actual)

    if actual != "linux":
        leaked = [
            name
            for name in ("wayfinder.hotkeys.evdev", "wayfinder.hotkeys.dbus")
            if name in sys.modules
        ]
        if leaked:
            raise RuntimeError(f"Linux-only hotkey modules imported on {actual}: {leaked}")

    if actual == "windows":
        from wayfinder.utils.platform import get_text_injector

        if get_text_injector() != "windows":
            raise RuntimeError(
                "Windows injection must report 'windows' (native SendInput adapter "
                "in wayfinder.core.injector_windows)"
            )

    print(f"PLATFORM_SMOKE_OK {actual}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", required=True, choices=SUPPORTED_PLATFORMS)
    args = parser.parse_args()
    run(args.expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

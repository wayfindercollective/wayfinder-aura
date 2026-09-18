"""Small, guarded helpers for macOS privacy permissions."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MacOSInputPermissionStatus:
    accessibility: bool | None
    input_monitoring: bool | None
    config_changed: bool = False


def macos_bundle_path() -> Path | None:
    """Return the enclosing .app path for a frozen Mac process."""
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    for parent in Path(sys.executable).resolve().parents:
        if parent.suffix.lower() == ".app":
            return parent
    return None


def macos_install_location_ready() -> bool:
    """TCC approvals should be granted to the stable /Applications copy."""
    bundle = macos_bundle_path()
    if bundle is None:
        return True
    try:
        bundle.relative_to("/Applications")
        return True
    except ValueError:
        return False


def request_accessibility_permission(*, prompt: bool = True) -> bool | None:
    """Return macOS Accessibility trust, optionally asking the OS to prompt.

    ``None`` means the native API could not be loaded. Other platforms return
    ``True`` so callers can use this as a guarded startup check.
    """
    if sys.platform != "darwin":
        return True
    try:
        from ApplicationServices import (
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        return bool(
            AXIsProcessTrustedWithOptions(
                {kAXTrustedCheckOptionPrompt: bool(prompt)}
            )
        )
    except Exception:
        return None


def request_input_monitoring_permission(*, prompt: bool = True) -> bool | None:
    """Return macOS Input Monitoring access, optionally requesting it."""
    if sys.platform != "darwin":
        return True
    try:
        from Quartz import CGPreflightListenEventAccess, CGRequestListenEventAccess

        trusted = bool(CGPreflightListenEventAccess())
        if not trusted and prompt:
            trusted = bool(CGRequestListenEventAccess())
        return trusted
    except Exception:
        return None


def request_startup_input_permissions(config: dict) -> MacOSInputPermissionStatus:
    """Request macOS input permissions without stacking two native prompts.

    Accessibility is requested first. Input Monitoring is requested only when
    Accessibility is already trusted; on current macOS versions the request may
    not add Aura to the pane, so the UI also gives manual ``+`` instructions.
    """
    if sys.platform != "darwin":
        return MacOSInputPermissionStatus(True, True, False)

    changed = False
    accessibility_attempted = bool(
        config.get("macos_accessibility_request_attempted_v2", False)
    )
    accessibility = request_accessibility_permission(
        prompt=not accessibility_attempted
    )
    if not accessibility_attempted:
        config["macos_accessibility_request_attempted_v2"] = True
        changed = True

    input_attempted = bool(
        config.get("macos_input_monitoring_request_attempted_v2", False)
    )
    should_request_input = accessibility is True and not input_attempted
    input_monitoring = request_input_monitoring_permission(
        prompt=should_request_input
    )
    if should_request_input:
        config["macos_input_monitoring_request_attempted_v2"] = True
        changed = True

    return MacOSInputPermissionStatus(
        accessibility=accessibility,
        input_monitoring=input_monitoring,
        config_changed=changed,
    )


def open_macos_privacy_settings(permission: str) -> bool:
    """Open the matching Privacy & Security pane."""
    if sys.platform != "darwin":
        return False
    if permission == "install_location":
        try:
            return subprocess.run(
                ["open", "/Applications"], capture_output=True, timeout=5
            ).returncode == 0
        except Exception:
            return False
    anchors = {
        "accessibility": "Privacy_Accessibility",
        "input_monitoring": "Privacy_ListenEvent",
    }
    anchor = anchors.get(permission)
    if anchor is None:
        return False
    try:
        result = subprocess.run(
            [
                "open",
                "x-apple.systempreferences:com.apple.preference.security?"
                + anchor,
            ],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False

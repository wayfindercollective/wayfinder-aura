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

    On a first run (setup not finished) nothing is prompted here: the setup
    guide's permissions step asks for each grant with context instead of
    system alerts appearing before the window does.
    """
    if sys.platform != "darwin":
        return MacOSInputPermissionStatus(True, True, False)
    if not config.get("welcome_completed", False):
        return MacOSInputPermissionStatus(
            request_accessibility_permission(prompt=False),
            request_input_monitoring_permission(prompt=False),
            False,
        )

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


# AVAuthorizationStatus
MIC_NOT_DETERMINED, MIC_RESTRICTED, MIC_DENIED, MIC_AUTHORIZED = 0, 1, 2, 3


def microphone_authorization() -> int | None:
    """AVCaptureDevice authorization for audio, or None if it cannot be read.

    When macOS blocks the microphone, Core Audio still opens and delivers
    digital silence, so the only honest diagnosis is to ask TCC directly.
    Loads AVFoundation through pyobjc-core (no extra wrapper package).
    """
    if sys.platform != "darwin":
        return None
    try:
        import objc
        from Foundation import NSBundle

        NSBundle.bundleWithPath_("/System/Library/Frameworks/AVFoundation.framework").load()
        device = objc.lookUpClass("AVCaptureDevice")
        return int(device.authorizationStatusForMediaType_("soun"))  # AVMediaTypeAudio
    except Exception:
        return None


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
        "microphone": "Privacy_Microphone",
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


# ---------------------------------------------------------------------------
# Setup-guide helpers: one live snapshot, native prompts, and a self-repair.
# ---------------------------------------------------------------------------

AURA_BUNDLE_ID = "io.wayfindercollective.WayfinderAura"
PERMISSIONS = ("microphone", "accessibility", "input_monitoring")
_TCC_SERVICES = {
    "microphone": "Microphone",
    "accessibility": "Accessibility",
    "input_monitoring": "ListenEvent",
}


def permission_snapshot() -> dict[str, bool | None]:
    """Current grant for each permission (True/False, None = unknown). Cheap."""
    if sys.platform != "darwin":
        return {name: True for name in PERMISSIONS}
    mic = microphone_authorization()
    return {
        "microphone": None if mic is None else mic == MIC_AUTHORIZED,
        "accessibility": request_accessibility_permission(prompt=False),
        "input_monitoring": request_input_monitoring_permission(prompt=False),
    }


def _register_mic_request_signature() -> None:
    """Tell PyObjC the completion block's type (no AVFoundation wrapper ships)."""
    import objc

    objc.registerMetaDataForSelector(
        b"AVCaptureDevice",
        b"requestAccessForMediaType:completionHandler:",
        {"arguments": {3: {"callable": {"retval": {"type": b"v"},
                                        "arguments": {0: {"type": b"^v"}, 1: {"type": b"Z"}}}}}},
    )


def request_microphone_access(on_done=None) -> bool:
    """Show macOS's microphone prompt the first time; open the pane after a Deny.

    ``on_done(granted)`` may run on a background thread. Returns True when a
    prompt was shown or the pane opened.
    """
    if sys.platform != "darwin":
        return False
    status = microphone_authorization()
    if status == MIC_AUTHORIZED:
        if on_done:
            on_done(True)
        return True
    if status == MIC_NOT_DETERMINED:
        try:
            import objc
            from Foundation import NSBundle

            NSBundle.bundleWithPath_("/System/Library/Frameworks/AVFoundation.framework").load()
            _register_mic_request_signature()
            device = objc.lookUpClass("AVCaptureDevice")
            device.requestAccessForMediaType_completionHandler_(
                "soun", lambda granted: on_done(bool(granted)) if on_done else None
            )
            return True
        except Exception:
            pass  # fall through to the pane
    return open_macos_privacy_settings("microphone")


def ask_for_permission(permission: str) -> bool:
    """The native request for one permission, then its Settings pane.

    Accessibility and Input Monitoring prompts add Aura to their lists so the
    user only flips a switch (no "+" hunting); the pane is opened as well
    because macOS shows each prompt only once.
    """
    if sys.platform != "darwin":
        return False
    if permission == "microphone":
        return request_microphone_access()
    if permission == "accessibility":
        request_accessibility_permission(prompt=True)
    elif permission == "input_monitoring":
        request_input_monitoring_permission(prompt=True)
    else:
        return False
    return open_macos_privacy_settings(permission)


def own_bundle_identifier() -> str | None:
    try:
        from Foundation import NSBundle

        value = NSBundle.mainBundle().bundleIdentifier()
        return str(value) if value else None
    except Exception:
        return None


def repair_permission(permission: str) -> bool:
    """Reset Aura's OWN stale entry for one permission, then ask again.

    After an update of an unsigned/ad-hoc build, System Settings can show Aura
    as allowed while macOS no longer trusts the new copy. Clearing Aura's own
    entry (what `tccutil reset <service> <bundle id>` does) lets the grant be
    made fresh. Only ever touches the Wayfinder Aura bundle - never another app
    (a source run is "Python", so it is refused).
    """
    if sys.platform != "darwin":
        return False
    service = _TCC_SERVICES.get(permission)
    bundle_id = own_bundle_identifier()
    if service is None or bundle_id != AURA_BUNDLE_ID:
        return False
    try:
        subprocess.run(["/usr/bin/tccutil", "reset", service, bundle_id],
                       capture_output=True, timeout=10, check=False)
    except Exception:
        return False
    return ask_for_permission(permission)

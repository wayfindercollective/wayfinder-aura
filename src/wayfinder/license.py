"""
Wayfinder Aura - License Management System

Offline-first license validation for premium features.
No phone home, no tracking, privacy-respecting.

License Key Format: WV-XXXX-XXXX-XXXX-XXXX
Where each X is alphanumeric (0-9, A-Z excluding confusing chars)
"""

import hashlib
import hmac
import json
import os
import platform
import re
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# Characters that aren't confusing (no 0/O, 1/I/L, etc.)
LICENSE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def _get_license_secret() -> str:
    """
    Get the license signing secret from environment variable.
    
    SECURITY: The license secret MUST be set via WAYFINDER_LICENSE_SECRET environment
    variable in production. For development/testing, a deterministic fallback is used.
    
    For production deployment:
    1. Generate a secret: python -c "import secrets; print(secrets.token_hex(32))"
    2. Set environment variable: export WAYFINDER_LICENSE_SECRET="your_64_char_hex_secret"
    3. For Flatpak/systemd: add to environment configuration
    
    Returns:
        The license secret string (64 hex characters recommended)
    """
    import os
    import warnings
    
    secret = os.environ.get("WAYFINDER_LICENSE_SECRET")
    
    if secret:
        return secret
    
    # Development fallback - generates deterministic secret from machine ID
    # This allows testing without setting env var, but is NOT secure for production
    warnings.warn(
        "WAYFINDER_LICENSE_SECRET not set. Using development fallback. "
        "Set WAYFINDER_LICENSE_SECRET environment variable for production.",
        RuntimeWarning,
        stacklevel=2
    )
    
    # Generate a deterministic but unique-per-machine development secret
    # This is NOT cryptographically secure but allows local testing
    machine_data = f"{platform.node()}:{platform.machine()}:wayfinder-dev-secret"
    return hashlib.sha256(machine_data.encode()).hexdigest()


# License secret - loaded from environment for security (legacy offline HMAC path)
LICENSE_SECRET = _get_license_secret()


# === Online activation (Convex licensing service) ===
# Ed25519 public key (hex) used to verify activation tokens OFFLINE. The matching private key
# lives ONLY in the licensing Convex deployment's environment — nothing secret ships in the app,
# and tokens cannot be forged. Overridable via env for testing.
LICENSE_PUBLIC_KEY_HEX = os.environ.get(
    "WAYFINDER_LICENSE_PUBKEY",
    "e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a",
)
# Activation endpoint. NOTE: points at the DEV deployment for now — switch the default to the
# production (shiny-goshawk-432) URL at go-live. Overridable via env for testing.
LICENSE_API_URL = os.environ.get(
    "WAYFINDER_LICENSE_API_URL",
    "https://valuable-stoat-578.convex.site/activate",
)
LICENSE_HTTP_TIMEOUT = 10  # seconds for the activation request


@dataclass
class LicenseInfo:
    """Information about a license."""
    is_valid: bool
    is_premium: bool
    license_key: Optional[str] = None
    machine_id: Optional[str] = None
    activated_date: Optional[str] = None
    error_message: Optional[str] = None


# === Premium Feature Definitions ===

PREMIUM_FEATURES = {
    # Feature ID: (display name, description)
    "faster_whisper": ("Faster-Whisper Backend", "CTranslate2 optimized inference engine"),
    "large_models": ("Large Models", "Access to Medium.en and Large v3 Turbo models"),
    "gpu_acceleration": ("GPU Acceleration", "Vulkan GPU transcription — much faster, and makes the large/turbo models usable on a Steam Deck"),
    "cloud_backends": ("Cloud Processing", "Groq/OpenAI Whisper transcription + GPT/Claude text cleanup — best quality for Strong & Caricature modes"),
    "chunked_recording": ("Chunked Recording", "Unlimited duration with real-time feedback"),
    "advanced_preprocessing": ("Advanced Audio", "Medium and Heavy preprocessing modes"),
    "high_beam_search": ("High Accuracy Mode", "Beam search 4-10 for better accuracy"),
    "typing_speeds": ("Typing Speed Options", "Fast, Normal, Slow, Very Slow modes"),
    "custom_vocabulary": ("Custom Vocabulary", "Add your own terms and names"),
    "voice_profiles": ("Voice Profiles", "Learns your speech patterns for better accuracy"),
    "tone_system": ("Tone Presets", "Professional, Casual, Dev, and Personal writing styles"),
}

FREE_FEATURES = {
    "basic_transcription": ("Basic Transcription", "whisper.cpp transcription with GPU support"),
    "small_models": ("Standard Models", "Tiny.en, Base.en, Small.en"),
    "standard_recording": ("Standard Recording", "Single-session recording"),
    "light_preprocessing": ("Light Audio Processing", "Gain normalization"),
    "instant_typing": ("Instant Paste", "Clipboard-based text injection"),
    "basic_overlay": ("Status Overlay", "Real-time recording status display"),
    "basic_postprocessing": ("LLM Cleanup", "Local llama.cpp text post-processing"),
}


# === Machine ID Generation ===

def get_machine_id() -> str:
    """
    Generate a stable machine identifier.
    Used for optional machine-binding of licenses.
    """
    # Try various sources for a stable ID
    sources = []
    
    # Linux machine-id
    try:
        machine_id_path = Path("/etc/machine-id")
        if machine_id_path.exists():
            sources.append(machine_id_path.read_text().strip())
    except Exception:
        pass
    
    # DMI product UUID (requires root or readable sysfs)
    try:
        dmi_path = Path("/sys/class/dmi/id/product_uuid")
        if dmi_path.exists():
            sources.append(dmi_path.read_text().strip())
    except Exception:
        pass
    
    # Fallback to hostname + platform
    sources.append(platform.node())
    sources.append(platform.machine())
    
    # Hash all sources together
    combined = ":".join(sources)
    return hashlib.sha256(combined.encode()).hexdigest()[:16].upper()


# === License Key Generation (for your backend/admin tool) ===

def generate_license_key(machine_id: Optional[str] = None) -> str:
    """
    Generate a valid license key.
    
    This function should be used in your admin/sales backend,
    NOT distributed with the app.
    
    Args:
        machine_id: Optional machine ID for bound licenses
    
    Returns:
        License key in format WV-XXXX-XXXX-XXXX-XXXX
    """
    # Generate random base
    import secrets
    random_part = ''.join(secrets.choice(LICENSE_CHARS) for _ in range(12))
    
    # Create payload
    payload = random_part
    if machine_id:
        payload += f":{machine_id}"
    
    # Generate checksum (last 4 chars)
    signature = hmac.new(
        LICENSE_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    
    # Convert signature to license chars
    checksum = ""
    for i in range(4):
        idx = int(signature[i*2:i*2+2], 16) % len(LICENSE_CHARS)
        checksum += LICENSE_CHARS[idx]
    
    # Format: WV-XXXX-XXXX-XXXX-XXXX
    full_key = random_part + checksum
    formatted = f"WV-{full_key[0:4]}-{full_key[4:8]}-{full_key[8:12]}-{full_key[12:16]}"
    
    return formatted


def validate_license_key(key: str, machine_id: Optional[str] = None) -> LicenseInfo:
    """
    Validate a license key offline.
    
    Args:
        key: License key to validate
        machine_id: Optional machine ID for bound license check
    
    Returns:
        LicenseInfo with validation result
    """
    # Clean up key
    key = key.upper().strip()
    
    # Check format
    pattern = r'^WV-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-Z0-9]{4})-([A-Z0-9]{4})$'
    match = re.match(pattern, key)
    
    if not match:
        return LicenseInfo(
            is_valid=False,
            is_premium=False,
            error_message="Invalid license key format"
        )
    
    # Extract parts
    parts = ''.join(match.groups())
    random_part = parts[:12]
    provided_checksum = parts[12:16]
    
    # Verify checksum
    payload = random_part
    signature = hmac.new(
        LICENSE_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    
    expected_checksum = ""
    for i in range(4):
        idx = int(signature[i*2:i*2+2], 16) % len(LICENSE_CHARS)
        expected_checksum += LICENSE_CHARS[idx]
    
    if provided_checksum != expected_checksum:
        # Try with machine ID binding
        if machine_id:
            payload_with_machine = f"{random_part}:{machine_id}"
            signature = hmac.new(
                LICENSE_SECRET.encode(),
                payload_with_machine.encode(),
                hashlib.sha256
            ).hexdigest()
            
            expected_checksum = ""
            for i in range(4):
                idx = int(signature[i*2:i*2+2], 16) % len(LICENSE_CHARS)
                expected_checksum += LICENSE_CHARS[idx]
            
            if provided_checksum != expected_checksum:
                return LicenseInfo(
                    is_valid=False,
                    is_premium=False,
                    error_message="Invalid license key"
                )
        else:
            return LicenseInfo(
                is_valid=False,
                is_premium=False,
                error_message="Invalid license key"
            )
    
    return LicenseInfo(
        is_valid=True,
        is_premium=True,
        license_key=key,
        machine_id=machine_id,
        activated_date=datetime.now().isoformat()
    )


# === License Storage ===

def get_license_path() -> Path:
    """Get path to license storage file."""
    config_dir = Path.home() / ".config" / "wayfinder-aura"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / "license.json"


def _verify_token(token: str, machine_id: Optional[str] = None) -> Optional[dict]:
    """Verify an Ed25519 activation token OFFLINE with the embedded public key.

    Returns the payload dict when the signature is valid, the token is unexpired, and (if given)
    the machine matches; otherwise None. Lets a previously-activated install keep working offline
    for the token's grace window without contacting the server.
    """
    if not token or "." not in token:
        return None
    try:
        import base64
        import time as _time
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        payload_b64, sig_b64 = token.split(".", 1)

        def _b64url(s: str) -> bytes:
            return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(LICENSE_PUBLIC_KEY_HEX))
        pub.verify(_b64url(sig_b64), payload_b64.encode())  # raises on a bad signature
        payload = json.loads(_b64url(payload_b64))
        if float(payload.get("exp", 0)) < _time.time():
            return None  # grace window elapsed
        if machine_id is not None and payload.get("machineId") != machine_id:
            return None  # token bound to a different machine
        return payload
    except Exception:
        return None


def activate_online(key: str, machine_id: str):
    """Activate/refresh against the licensing service.

    Returns (LicenseInfo, token_or_None, reachable). reachable=False means the server couldn't be
    reached (offline / down) — callers then fall back to the cached offline token within its grace.
    """
    try:
        import requests

        resp = requests.post(
            LICENSE_API_URL,
            json={"key": key, "machineId": machine_id},
            timeout=LICENSE_HTTP_TIMEOUT,
        )
        data = resp.json()
    except Exception:
        return (
            LicenseInfo(is_valid=False, is_premium=False, error_message="offline"),
            None,
            False,
        )

    if data.get("valid"):
        return (
            LicenseInfo(
                is_valid=True,
                is_premium=True,
                license_key=key,
                machine_id=machine_id,
            ),
            data.get("token"),
            True,
        )

    reason = data.get("reason", "invalid")
    msg = {
        "not_found": "License key not found",
        "activation_limit": "Activation limit reached for this license",
        "revoked": "This license has been revoked",
        "refunded": "This license was refunded",
        "missing_fields": "Invalid request",
    }.get(reason, f"License invalid ({reason})")
    return (LicenseInfo(is_valid=False, is_premium=False, error_message=msg), None, True)


def load_stored_license() -> LicenseInfo:
    """Load the stored license: trust a valid offline token, refresh online when reachable.

    Works offline within the token's grace window; refreshes (catching refunds/revocations and
    extending the window) whenever the server is reachable and the token is missing/old.
    """
    license_path = get_license_path()
    if not license_path.exists():
        return LicenseInfo(is_valid=False, is_premium=False, error_message="No license found")

    try:
        data = json.loads(license_path.read_text())
    except Exception as e:
        return LicenseInfo(is_valid=False, is_premium=False, error_message=f"Error loading license: {e}")

    key = data.get("license_key", "")
    machine_id = data.get("machine_id") or get_machine_id()
    payload = _verify_token(data.get("token", ""), machine_id)

    # Refresh online if the token is missing/expired, or past the midpoint of its grace window.
    needs_refresh = payload is None
    if payload is not None:
        try:
            import time as _time

            iat = float(payload.get("iat", 0))
            exp = float(payload.get("exp", 0))
            if iat and exp and _time.time() > iat + (exp - iat) / 2:
                needs_refresh = True
        except Exception:
            pass

    if needs_refresh and key:
        info, new_token, reachable = activate_online(key, machine_id)
        if reachable:
            if info.is_valid and new_token:
                data["token"] = new_token
                data["machine_id"] = machine_id
                try:
                    license_path.write_text(json.dumps(data, indent=2))
                except Exception:
                    pass
                info.activated_date = data.get("activated_date")
                return info
            return info  # server says invalid (revoked/refunded/limit) — premium off
        # unreachable: fall back to the cached token below

    if payload is not None:
        return LicenseInfo(
            is_valid=True,
            is_premium=True,
            license_key=key,
            machine_id=machine_id,
            activated_date=data.get("activated_date"),
        )
    return LicenseInfo(
        is_valid=False,
        is_premium=False,
        error_message="License needs re-activation (offline grace expired)",
    )


def store_license(key: str) -> LicenseInfo:
    """Activate a license key online and store the signed offline token for future grace use."""
    key = (key or "").strip().upper()
    machine_id = get_machine_id()
    info, token, _reachable = activate_online(key, machine_id)

    if info.is_valid and token:
        license_path = get_license_path()
        data = {
            "license_key": key,
            "machine_id": machine_id,
            "token": token,
            "activated_date": datetime.now().isoformat(),
        }
        license_path.write_text(json.dumps(data, indent=2))
        # The license token is a bearer credential — restrict to owner-only.
        try:
            os.chmod(license_path, 0o600)
        except OSError:
            pass
        info.activated_date = data["activated_date"]

    return info


def remove_license() -> None:
    """Remove stored license."""
    license_path = get_license_path()
    if license_path.exists():
        license_path.unlink()


# === Feature Gating ===


# === DEV-UNLOCK (remove before GA) =========================================
# Developer-only override that forces every premium feature ON so licensing can
# be exercised without a real key. Enabled by the "DEV — Unlock all premium
# features" checkbox (writes "dev_unlock_all": true to config.json) or by the
# WAYFINDER_DEV_UNLOCK=1 environment variable (handy for headless tests). To
# remove before shipping real licensing: delete this block and the two
# `_dev_unlock` lines in FeatureGate, plus the checkbox in wayfinder_main.py
# (grep the tree for: DEV-UNLOCK).
def _dev_unlock_enabled() -> bool:
    env = os.environ.get("WAYFINDER_DEV_UNLOCK", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    try:
        import wayfinder.config as _wc
        cfg_path = Path(_wc.CONFIG_FILE)
    except Exception:
        cfg_path = Path.home() / ".config" / "wayfinder-aura" / "config.json"
    try:
        if cfg_path.exists():
            return bool(json.loads(cfg_path.read_text()).get("dev_unlock_all", False))
    except Exception:
        pass
    return False
# === end DEV-UNLOCK ========================================================


class FeatureGate:
    """
    Controls access to premium features.
    
    Usage:
        gate = FeatureGate()
        
        if gate.is_premium:
            # Show premium badge
            
        if gate.has_feature("gpu_acceleration"):
            # Enable GPU settings
        else:
            # Show upgrade prompt
    """
    
    def __init__(self):
        self._license_info: Optional[LicenseInfo] = None
        self.refresh()
    
    def refresh(self) -> None:
        """Reload license status."""
        self._license_info = load_stored_license()
        self._dev_unlock = _dev_unlock_enabled()  # DEV-UNLOCK (remove before GA)
    
    @property
    def is_premium(self) -> bool:
        """Check if user has premium license."""
        if getattr(self, "_dev_unlock", False):  # DEV-UNLOCK (remove before GA)
            return True
        return self._license_info.is_valid and self._license_info.is_premium
    
    @property
    def license_info(self) -> LicenseInfo:
        """Get current license info."""
        return self._license_info
    
    def has_feature(self, feature_id: str) -> bool:
        """
        Check if a specific feature is available.
        
        Args:
            feature_id: Feature identifier (e.g., "gpu_acceleration")
        
        Returns:
            True if feature is available (premium or free feature)
        """
        if feature_id in FREE_FEATURES:
            return True
        
        if feature_id in PREMIUM_FEATURES:
            return self.is_premium
        
        # Unknown feature - default to requiring premium
        return self.is_premium
    
    def get_upgrade_message(self, feature_id: str) -> str:
        """Get upgrade prompt for a locked feature."""
        if feature_id in PREMIUM_FEATURES:
            name, desc = PREMIUM_FEATURES[feature_id]
            return (f"🔒 {name} requires Wayfinder Aura Ultra.\n\n{desc}\n\n"
                    f"Support the project — get Ultra for $20 (launch price, reg. $40) "
                    f"at wayfinder.dev/premium and earn your halo 😇")
        return "This feature requires Wayfinder Aura Ultra."
    
    def activate(self, key: str) -> LicenseInfo:
        """
        Activate a license key.
        
        Args:
            key: License key to activate
        
        Returns:
            LicenseInfo with result
        """
        result = store_license(key)
        if result.is_valid:
            self._license_info = result
        return result
    
    def deactivate(self) -> None:
        """Remove current license."""
        remove_license()
        self.refresh()


# === Singleton for app-wide access ===

_feature_gate: Optional[FeatureGate] = None

def get_feature_gate(force_refresh: bool = False) -> FeatureGate:
    """Get the global feature gate instance."""
    global _feature_gate
    if _feature_gate is None or force_refresh:
        _feature_gate = FeatureGate()
    return _feature_gate


# === CLI for testing ===

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        
        if cmd == "generate":
            # Generate a new license key (admin only!)
            key = generate_license_key()
            print(f"Generated license key: {key}")
            
        elif cmd == "generate-bound":
            # Generate machine-bound key
            machine_id = get_machine_id()
            key = generate_license_key(machine_id)
            print(f"Machine ID: {machine_id}")
            print(f"Bound license key: {key}")
            
        elif cmd == "validate":
            if len(sys.argv) > 2:
                key = sys.argv[2]
                result = validate_license_key(key)
                print(f"Valid: {result.is_valid}")
                print(f"Premium: {result.is_premium}")
                if result.error_message:
                    print(f"Error: {result.error_message}")
            else:
                print("Usage: python license.py validate <KEY>")
                
        elif cmd == "activate":
            if len(sys.argv) > 2:
                key = sys.argv[2]
                result = store_license(key)
                if result.is_valid:
                    print(f"✓ License activated!")
                    print(f"  Key: {result.license_key}")
                    print(f"  Premium: {result.is_premium}")
                else:
                    print(f"✗ Activation failed: {result.error_message}")
            else:
                print("Usage: python license.py activate <KEY>")
                
        elif cmd == "status":
            result = load_stored_license()
            print(f"License Status:")
            print(f"  Valid: {result.is_valid}")
            print(f"  Premium: {result.is_premium}")
            if result.license_key:
                print(f"  Key: {result.license_key}")
            if result.activated_date:
                print(f"  Activated: {result.activated_date}")
            if result.error_message:
                print(f"  Note: {result.error_message}")
                
        elif cmd == "machine-id":
            print(f"Machine ID: {get_machine_id()}")
            
        else:
            print(f"Unknown command: {cmd}")
            print("Commands: generate, generate-bound, validate, activate, status, machine-id")
    else:
        print("Wayfinder Aura License Manager")
        print("Commands: generate, generate-bound, validate <KEY>, activate <KEY>, status, machine-id")


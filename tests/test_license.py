"""
Tests for the license management module.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

SAMPLE_LICENSE_KEY = "WF-TEST-ONLINE-ACTIVATION"


class TestMachineId:
    """Test machine ID generation."""

    def test_get_machine_id_returns_string(self):
        """Test that get_machine_id returns a string."""
        from wayfinder.license import get_machine_id

        machine_id = get_machine_id()

        assert isinstance(machine_id, str)
        assert len(machine_id) > 0

    def test_get_machine_id_is_consistent(self):
        """Test that machine ID is consistent across calls."""
        from wayfinder.license import get_machine_id

        id1 = get_machine_id()
        id2 = get_machine_id()

        assert id1 == id2

    def test_get_machine_id_format(self):
        """Test that machine ID has expected format."""
        from wayfinder.license import get_machine_id

        machine_id = get_machine_id()

        # Should be 16 uppercase hex characters
        assert len(machine_id) == 16
        assert machine_id.isupper()
        assert all(c in "0123456789ABCDEF" for c in machine_id)


class TestLicenseStorage:
    """Test license storage functionality."""

    def test_get_license_path(self, temp_config_dir: Path):
        """Test license path generation."""
        from wayfinder.license import get_license_path

        path = get_license_path()

        assert path.name == "license.json"
        assert "wayfinder-aura" in str(path)

    def test_store_and_load_license(self, temp_config_dir: Path, mock_online_license):
        """Test storing and loading a license."""
        from wayfinder.license import store_license, load_stored_license

        key = SAMPLE_LICENSE_KEY
        store_result = store_license(key)

        assert store_result.is_valid

        loaded = load_stored_license()

        assert loaded.is_valid
        assert loaded.license_key == key

    def test_existing_license_permissions_are_repaired(self, temp_config_dir: Path, mock_online_license):
        """Existing token files are tightened to owner-only permissions on load."""
        import json
        import os
        from wayfinder.license import get_license_path, load_stored_license

        path = get_license_path()
        path.write_text(json.dumps({
            "license_key": SAMPLE_LICENSE_KEY,
            "machine_id": "MACHINE123",
            "token": "TEST.TOKEN",
            "activated_date": "2026-07-06T00:00:00",
        }))
        os.chmod(path, 0o644)

        loaded = load_stored_license()

        assert loaded.is_valid
        assert (path.stat().st_mode & 0o777) == 0o600

    def test_remove_license(self, temp_config_dir: Path, mock_online_license):
        """Test removing a stored license."""
        from wayfinder.license import (
            store_license,
            remove_license,
            load_stored_license,
        )

        key = SAMPLE_LICENSE_KEY
        store_license(key)
        remove_license()

        loaded = load_stored_license()

        assert not loaded.is_valid
        assert "No license" in loaded.error_message


class TestVerifyToken:
    """Real Ed25519 crypto coverage for _verify_token (the paid offline gate).

    These tests deliberately do NOT use the mock_online_license fixture (which
    stubs _verify_token out); they generate a throwaway Ed25519 keypair, point
    the module's public key at it via monkeypatch, and sign real tokens so the
    actual verify/expiry/machine-binding branches run.
    """

    @staticmethod
    def _make_token(private_key, payload: dict) -> str:
        """Build a token exactly as the licensing service does: base64url(JSON
        payload) '.' base64url(Ed25519 sig over the payload_b64 ASCII bytes)."""
        import base64
        import json

        payload_b64 = (
            base64.urlsafe_b64encode(json.dumps(payload).encode())
            .rstrip(b"=")
            .decode()
        )
        sig = private_key.sign(payload_b64.encode())
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
        return f"{payload_b64}.{sig_b64}"

    @pytest.fixture
    def signing_key(self, monkeypatch: pytest.MonkeyPatch):
        """Generate a test keypair and trust its public key in the module."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization

        private_key = Ed25519PrivateKey.generate()
        pub_hex = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        monkeypatch.setattr("wayfinder.license.LICENSE_PUBLIC_KEY_HEX", pub_hex)
        return private_key

    def test_valid_token_returns_payload(self, signing_key):
        """A correctly-signed, unexpired, machine-matched token verifies."""
        import time
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now, "exp": now + 3600, "v": 1}
        token = self._make_token(signing_key, payload)

        result = _verify_token(token, machine_id="MACHINE123")

        assert result is not None
        assert result["plan"] == "pro"
        assert result["machineId"] == "MACHINE123"

    def test_valid_token_without_machine_id(self, signing_key):
        """When no machine_id is supplied, binding is not enforced."""
        import time
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now, "exp": now + 3600}
        token = self._make_token(signing_key, payload)

        result = _verify_token(token)

        assert result is not None
        assert result["machineId"] == "MACHINE123"

    def test_tampered_signature_rejected(self, signing_key):
        """A token whose signature is corrupted fails verification."""
        import time
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now, "exp": now + 3600}
        token = self._make_token(signing_key, payload)

        payload_b64, sig_b64 = token.split(".", 1)
        # Flip a character in the signature segment.
        tampered_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        tampered = f"{payload_b64}.{tampered_sig}"

        assert _verify_token(tampered, machine_id="MACHINE123") is None

    def test_tampered_payload_rejected(self, signing_key):
        """Editing the payload after signing invalidates the signature."""
        import base64
        import json
        import time
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "free", "machineId": "MACHINE123", "iat": now, "exp": now + 3600}
        token = self._make_token(signing_key, payload)
        _, sig_b64 = token.split(".", 1)

        # Attacker swaps in an upgraded payload but keeps the original signature.
        forged_payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now, "exp": now + 3600}
        forged_b64 = (
            base64.urlsafe_b64encode(json.dumps(forged_payload).encode())
            .rstrip(b"=")
            .decode()
        )
        forged = f"{forged_b64}.{sig_b64}"

        assert _verify_token(forged, machine_id="MACHINE123") is None

    def test_expired_token_rejected(self, signing_key):
        """A validly-signed token past its exp is rejected (grace elapsed)."""
        import time
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now - 7200, "exp": now - 3600}
        token = self._make_token(signing_key, payload)

        assert _verify_token(token, machine_id="MACHINE123") is None

    def test_wrong_machine_id_rejected(self, signing_key):
        """A token bound to a different machine is rejected."""
        import time
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now, "exp": now + 3600}
        token = self._make_token(signing_key, payload)

        assert _verify_token(token, machine_id="OTHERMACHINE") is None

    def test_malformed_token_rejected(self, signing_key):
        """Empty / dot-less tokens are rejected without raising."""
        from wayfinder.license import _verify_token

        assert _verify_token("") is None
        assert _verify_token("no-dot-here") is None

    def test_wrong_public_key_rejected(self, signing_key):
        """A token signed by a different key does not verify against the trusted key."""
        import time
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from wayfinder.license import _verify_token

        now = int(time.time())
        payload = {"plan": "pro", "machineId": "MACHINE123", "iat": now, "exp": now + 3600}
        # Sign with an untrusted key (signing_key fixture trusts a different pubkey).
        attacker_key = Ed25519PrivateKey.generate()
        token = self._make_token(attacker_key, payload)

        assert _verify_token(token, machine_id="MACHINE123") is None


class TestFeatureGate:
    """Test feature gating functionality."""

    def test_feature_gate_initialization(self, temp_config_dir: Path):
        """Test FeatureGate can be initialized."""
        from wayfinder.license import FeatureGate

        gate = FeatureGate()

        assert gate is not None
        assert hasattr(gate, "is_premium")

    def test_free_features_available(self, temp_config_dir: Path):
        """Test that free features are available without license."""
        from wayfinder.license import FeatureGate, FREE_FEATURES

        gate = FeatureGate()

        for feature_id in FREE_FEATURES:
            assert gate.has_feature(feature_id), f"Free feature {feature_id} should be available"

    def test_premium_features_locked(self, temp_config_dir: Path):
        """Test that premium features are locked without license."""
        from wayfinder.license import FeatureGate, PREMIUM_FEATURES

        gate = FeatureGate()

        for feature_id in PREMIUM_FEATURES:
            if not gate.is_premium:
                assert not gate.has_feature(feature_id), f"Premium feature {feature_id} should be locked"

    def test_activate_license(self, temp_config_dir: Path, mock_online_license):
        """Test activating a license."""
        from wayfinder.license import FeatureGate, PREMIUM_FEATURES

        gate = FeatureGate()
        key = SAMPLE_LICENSE_KEY

        result = gate.activate(key)

        assert result.is_valid
        assert gate.is_premium

        # Premium features should now be available
        for feature_id in PREMIUM_FEATURES:
            assert gate.has_feature(feature_id)

    def test_deactivate_license(self, temp_config_dir: Path, mock_online_license):
        """Test deactivating a license."""
        from wayfinder.license import FeatureGate

        gate = FeatureGate()
        key = SAMPLE_LICENSE_KEY
        gate.activate(key)
        gate.deactivate()

        assert not gate.is_premium

    def test_get_upgrade_message(self, temp_config_dir: Path):
        """Test upgrade message generation."""
        from wayfinder.license import FeatureGate

        gate = FeatureGate()
        message = gate.get_upgrade_message("faster_whisper")

        assert "Ultra" in message  # paid tier branding (renamed from Premium)
        assert "Faster" in message or "Whisper" in message

    def test_new_premium_features_exist(self, temp_config_dir: Path):
        """Test that new premium features are defined."""
        from wayfinder.license import PREMIUM_FEATURES

        expected = ["faster_whisper", "large_models", "cloud_backends",
                    "chunked_recording", "voice_profiles", "tone_system",
                    "custom_vocabulary", "gpu_acceleration", "large_cleanup_models"]
        for feat in expected:
            assert feat in PREMIUM_FEATURES, f"Missing premium feature: {feat}"
        # Paper tigers removed — free by honesty, not marketed as Ultra-only.
        for removed in ("high_beam_search", "typing_speeds", "advanced_preprocessing"):
            assert removed not in PREMIUM_FEATURES

    def test_gpu_is_premium(self, temp_config_dir: Path):
        """Full GPU (all models) remains a PREMIUM feature id."""
        from wayfinder.license import FREE_FEATURES, PREMIUM_FEATURES

        assert "gpu_acceleration" in PREMIUM_FEATURES
        assert "gpu_acceleration" not in FREE_FEATURES

    def test_free_tier_gpu_models_tiny_and_base_only(self, temp_config_dir: Path):
        """Free GPU applies to Tiny/Base only — not Small or larger."""
        from wayfinder.license import is_free_tier_gpu_model

        assert is_free_tier_gpu_model("ggml-tiny.en.bin")
        assert is_free_tier_gpu_model("~/models/ggml-base.en.bin")
        assert is_free_tier_gpu_model("base.en")
        assert is_free_tier_gpu_model("tiny")
        assert not is_free_tier_gpu_model("ggml-small.en.bin")
        assert not is_free_tier_gpu_model("ggml-medium.en.bin")
        assert not is_free_tier_gpu_model("ggml-large-v3-turbo.bin")
        assert not is_free_tier_gpu_model("large-v3")
        # Substring traps: path containing "base" must not free-GPU an Ultra weight.
        assert not is_free_tier_gpu_model("/home/user/base-station/ggml-small.en.bin")
        assert not is_free_tier_gpu_model("my-base-large-hack.bin")

    def test_gpu_allowed_free_tiny_base_not_small(self, temp_config_dir: Path):
        """Free user: GPU yes on Base, no on Small. Ultra: yes on Small."""
        from wayfinder.license import FeatureGate, gpu_allowed_for_model

        free = FeatureGate()
        assert not free.is_premium
        assert gpu_allowed_for_model("ggml-base.en.bin", free) is True
        assert gpu_allowed_for_model("ggml-tiny.en.bin", free) is True
        assert gpu_allowed_for_model("ggml-small.en.bin", free) is False

    def test_gpu_allowed_ultra_all_models(
        self, temp_config_dir: Path, mock_online_license
    ):
        from wayfinder.license import FeatureGate, gpu_allowed_for_model

        gate = FeatureGate()
        gate.activate(SAMPLE_LICENSE_KEY)
        assert gate.is_premium
        assert gpu_allowed_for_model("ggml-small.en.bin", gate) is True
        assert gpu_allowed_for_model("ggml-large-v3-turbo.bin", gate) is True

    def test_token_feature_list_gates_unknown_premium_ids(
        self, temp_config_dir: Path, mock_online_license
    ):
        """Signed features list is authoritative: not every string is Ultra."""
        from wayfinder.license import FeatureGate

        gate = FeatureGate()
        gate.activate(SAMPLE_LICENSE_KEY)
        assert gate.has_feature("large_models")
        assert gate.has_feature("large_cleanup_models")
        assert gate.get_bearer_token() == "TEST.TOKEN"

    def test_activate_online_rejects_unsigned_valid_response(self, temp_config_dir: Path):
        """Forged {valid:true} JSON without a verifiable token must not unlock Ultra."""
        from unittest.mock import MagicMock, patch
        from wayfinder.license import activate_online

        fake = MagicMock()
        fake.json.return_value = {
            "valid": True,
            "plan": "pro",
            "features": ["large_models", "gpu_acceleration"],
            "token": "NOT.SIGNED",
        }
        with patch("requests.post", return_value=fake):
            info, token, reachable = activate_online("WF-FAKE", "MACHINE123")
        assert reachable is True
        assert token is None
        assert info.is_valid is False
        assert info.is_premium is False

    def test_activate_online_rejects_v1_payload_even_with_features(
        self, temp_config_dir: Path
    ):
        """Desktop must match Worker: v>=2 required (features alone is not enough)."""
        from unittest.mock import MagicMock, patch
        from wayfinder.license import activate_online

        fake = MagicMock()
        fake.json.return_value = {"valid": True, "token": "signed.example"}
        with patch("requests.post", return_value=fake), patch(
            "wayfinder.license._verify_token",
            return_value={"v": 1, "plan": "pro", "features": ["large_models"]},
        ):
            info, token, reachable = activate_online("WF-TEST", "MACHINE")
        assert reachable is True
        assert info.is_premium is False
        assert token is None

    def test_premium_user_has_all_features(self, temp_config_dir: Path, mock_online_license):
        """Test that premium user can access all premium features."""
        from wayfinder.license import FeatureGate, PREMIUM_FEATURES

        gate = FeatureGate()
        key = SAMPLE_LICENSE_KEY
        gate.activate(key)

        for feat_id in PREMIUM_FEATURES:
            assert gate.has_feature(feat_id), f"Premium user should have {feat_id}"

    def test_free_user_blocked_from_premium(self, temp_config_dir: Path):
        """Test that free user cannot access premium features."""
        from wayfinder.license import FeatureGate, PREMIUM_FEATURES

        gate = FeatureGate()
        assert not gate.is_premium

        for feat_id in PREMIUM_FEATURES:
            assert not gate.has_feature(feat_id), f"Free user should not have {feat_id}"

    def test_force_refresh_feature_gate(self, temp_config_dir: Path, mock_online_license):
        """Test that force_refresh creates a new FeatureGate instance."""
        import wayfinder.license as lic
        from wayfinder.license import get_feature_gate, store_license

        # Establish a clean non-premium baseline against the (empty) temp config dir. The module
        # -level _feature_gate singleton persists across tests, so without force_refresh here a
        # prior test that stored a license could leave gate1 premium (test-ordering flake).
        gate1 = get_feature_gate(force_refresh=True)
        assert not gate1.is_premium

        key = SAMPLE_LICENSE_KEY
        store_license(key)

        # Without force_refresh, should return cached instance
        gate2 = get_feature_gate()
        assert gate2 is gate1

        # With force_refresh, should return new instance
        gate3 = get_feature_gate(force_refresh=True)
        assert gate3 is not gate1
        assert gate3.is_premium

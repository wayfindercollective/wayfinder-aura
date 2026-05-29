"""
Tests for the license management module.
"""

from pathlib import Path
from unittest.mock import patch

import pytest


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


class TestLicenseKeyValidation:
    """Test license key validation."""

    def test_valid_key_format(self):
        """Test validation of key format."""
        from wayfinder.license import validate_license_key

        # Invalid format
        result = validate_license_key("invalid-key")

        assert not result.is_valid
        assert "format" in result.error_message.lower()

    def test_key_format_pattern(self):
        """Test that key must match WV-XXXX-XXXX-XXXX-XXXX pattern."""
        from wayfinder.license import validate_license_key

        # Too short
        result = validate_license_key("WV-AAAA-BBBB-CCCC")
        assert not result.is_valid

        # Wrong prefix
        result = validate_license_key("XX-AAAA-BBBB-CCCC-DDDD")
        assert not result.is_valid

    def test_generate_and_validate_key(self):
        """Test that generated keys validate correctly."""
        from wayfinder.license import generate_license_key, validate_license_key

        key = generate_license_key()
        result = validate_license_key(key)

        assert result.is_valid
        assert result.is_premium
        assert result.license_key == key

    def test_generate_bound_key(self):
        """Test machine-bound license key generation."""
        from wayfinder.license import generate_license_key, validate_license_key, get_machine_id

        machine_id = get_machine_id()
        key = generate_license_key(machine_id)

        # Should validate with correct machine ID
        result = validate_license_key(key, machine_id)
        assert result.is_valid

    def test_invalid_checksum(self):
        """Test that tampered keys are rejected."""
        from wayfinder.license import generate_license_key, validate_license_key

        key = generate_license_key()
        # Tamper with the key
        tampered = key[:-1] + ("A" if key[-1] != "A" else "B")

        result = validate_license_key(tampered)
        assert not result.is_valid


class TestLicenseStorage:
    """Test license storage functionality."""

    def test_get_license_path(self, temp_config_dir: Path):
        """Test license path generation."""
        from wayfinder.license import get_license_path

        path = get_license_path()

        assert path.name == "license.json"
        assert "wayfinder-aura" in str(path)

    def test_store_and_load_license(self, temp_config_dir: Path):
        """Test storing and loading a license."""
        from wayfinder.license import generate_license_key, store_license, load_stored_license

        key = generate_license_key()
        store_result = store_license(key)

        assert store_result.is_valid

        loaded = load_stored_license()

        assert loaded.is_valid
        assert loaded.license_key == key

    def test_remove_license(self, temp_config_dir: Path):
        """Test removing a stored license."""
        from wayfinder.license import (
            generate_license_key,
            store_license,
            remove_license,
            load_stored_license,
        )

        key = generate_license_key()
        store_license(key)
        remove_license()

        loaded = load_stored_license()

        assert not loaded.is_valid
        assert "No license" in loaded.error_message


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

    def test_activate_license(self, temp_config_dir: Path):
        """Test activating a license."""
        from wayfinder.license import FeatureGate, generate_license_key, PREMIUM_FEATURES

        gate = FeatureGate()
        key = generate_license_key()

        result = gate.activate(key)

        assert result.is_valid
        assert gate.is_premium

        # Premium features should now be available
        for feature_id in PREMIUM_FEATURES:
            assert gate.has_feature(feature_id)

    def test_deactivate_license(self, temp_config_dir: Path):
        """Test deactivating a license."""
        from wayfinder.license import FeatureGate, generate_license_key

        gate = FeatureGate()
        key = generate_license_key()
        gate.activate(key)
        gate.deactivate()

        assert not gate.is_premium

    def test_get_upgrade_message(self, temp_config_dir: Path):
        """Test upgrade message generation."""
        from wayfinder.license import FeatureGate

        gate = FeatureGate()
        message = gate.get_upgrade_message("faster_whisper")

        assert "Premium" in message
        assert "Faster" in message or "Whisper" in message

    def test_new_premium_features_exist(self, temp_config_dir: Path):
        """Test that new premium features are defined."""
        from wayfinder.license import PREMIUM_FEATURES

        expected = ["faster_whisper", "large_models", "cloud_backends",
                    "chunked_recording", "voice_profiles", "tone_system",
                    "custom_vocabulary", "high_beam_search", "typing_speeds"]
        for feat in expected:
            assert feat in PREMIUM_FEATURES, f"Missing premium feature: {feat}"

    def test_gpu_is_free(self, temp_config_dir: Path):
        """Test that GPU acceleration is a free feature."""
        from wayfinder.license import FREE_FEATURES

        assert "gpu_acceleration" in FREE_FEATURES

    def test_premium_user_has_all_features(self, temp_config_dir: Path):
        """Test that premium user can access all premium features."""
        from wayfinder.license import FeatureGate, PREMIUM_FEATURES, generate_license_key

        gate = FeatureGate()
        key = generate_license_key()
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

    def test_force_refresh_feature_gate(self, temp_config_dir: Path):
        """Test that force_refresh creates a new FeatureGate instance."""
        from wayfinder.license import get_feature_gate, generate_license_key, store_license

        # Establish a clean non-premium baseline against the (empty) temp config dir. The module
        # -level _feature_gate singleton persists across tests, so without force_refresh here a
        # prior test that stored a license could leave gate1 premium (test-ordering flake).
        gate1 = get_feature_gate(force_refresh=True)
        assert not gate1.is_premium

        key = generate_license_key()
        store_license(key)

        # Without force_refresh, should return cached instance
        gate2 = get_feature_gate()
        assert gate2 is gate1

        # With force_refresh, should return new instance
        gate3 = get_feature_gate(force_refresh=True)
        assert gate3 is not gate1
        assert gate3.is_premium

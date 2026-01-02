"""
Tests for the configuration module.
"""

import json
from pathlib import Path

import pytest


class TestConfigLoading:
    """Test configuration loading and saving."""

    def test_default_config_has_required_keys(self):
        """Verify DEFAULT_CONFIG contains all required keys."""
        from wayfinder.config import DEFAULT_CONFIG

        required_keys = [
            "whisper_binary",
            "model_path",
            "hotkey_key",
            "sample_rate",
            "threads",
            "timeout",
            "typing_speed",
            "use_gpu",
        ]

        for key in required_keys:
            assert key in DEFAULT_CONFIG, f"Missing required key: {key}"

    def test_load_config_creates_default(self, temp_config_dir: Path):
        """Test that load_config creates default config if none exists."""
        from wayfinder.config import load_config, CONFIG_FILE

        # Ensure config doesn't exist
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()

        config = load_config()

        assert config is not None
        assert isinstance(config, dict)
        assert "hotkey_key" in config

    def test_save_and_load_config(self, temp_config_dir: Path, sample_config: dict):
        """Test saving and loading configuration."""
        from wayfinder.config import save_config, load_config

        # Save custom config
        save_config(sample_config)

        # Load it back
        loaded = load_config()

        assert loaded["threads"] == sample_config["threads"]
        assert loaded["timeout"] == sample_config["timeout"]

    def test_config_merges_with_defaults(self, temp_config_dir: Path):
        """Test that partial configs merge with defaults."""
        from wayfinder.config import load_config, save_config, DEFAULT_CONFIG, CONFIG_FILE

        # Save partial config
        partial = {"threads": 8}
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(partial, f)

        config = load_config()

        # Should have custom value
        assert config["threads"] == 8
        # Should have default for missing keys
        assert config["sample_rate"] == DEFAULT_CONFIG["sample_rate"]


class TestKeyCodeMappings:
    """Test key code utilities."""

    def test_get_key_name_known_key(self):
        """Test getting name for known key codes."""
        from wayfinder.config import get_key_name

        assert get_key_name(67) == "F9"
        assert get_key_name(68) == "F10"
        assert get_key_name(59) == "F1"

    def test_get_key_name_unknown_key(self):
        """Test getting name for unknown key code."""
        from wayfinder.config import get_key_name

        result = get_key_name(999)
        assert result.startswith("KEY_")

    def test_modifier_codes_structure(self):
        """Test modifier codes have expected structure."""
        from wayfinder.config import MODIFIER_CODES

        assert "ctrl" in MODIFIER_CODES
        assert "alt" in MODIFIER_CODES
        assert "shift" in MODIFIER_CODES

        # Each modifier should have left and right variants
        for codes in MODIFIER_CODES.values():
            assert isinstance(codes, list)
            assert len(codes) >= 2

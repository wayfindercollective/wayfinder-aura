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


class TestHotkeyDefaultMigration:
    """Existing installs keep bare F3/F10; only fresh installs get Super+F2/F3.

    Regression: a config that saved hotkey_key=61 but no modifiers got the NEW
    default modifiers ['super'] merged on top, silently breaking a mouse button
    mapped to bare F3 — and a later settings save baked the corruption in.
    """

    def _write_config(self, data: dict):
        from wayfinder.config import CONFIG_FILE
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f)

    def test_existing_config_without_hotkeys_keeps_legacy_f3(self, temp_config_dir: Path):
        from wayfinder.config import load_config

        self._write_config({"typing_speed": "instant"})  # old install, defaults never saved
        config = load_config()

        assert config["hotkey_key"] == 61          # F3, NOT the new F2 default
        assert config["hotkey_modifiers"] == []    # bare key, no super
        assert config["style_toggle_key"] == 68    # F10
        assert config["style_toggle_modifiers"] == []

    def test_saved_bare_key_does_not_gain_new_default_modifiers(self, temp_config_dir: Path):
        from wayfinder.config import load_config

        # User explicitly saved F3 long ago, before modifiers existed in the file.
        self._write_config({"hotkey_key": 61})
        config = load_config()

        assert config["hotkey_key"] == 61
        assert config["hotkey_modifiers"] == []

    def test_fresh_install_gets_super_f2_defaults(self, temp_config_dir: Path):
        from wayfinder.config import load_config, CONFIG_FILE

        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        config = load_config()

        assert config["hotkey_key"] == 60
        assert config["hotkey_modifiers"] == ["super"]
        assert config["style_toggle_key"] == 61
        assert config["style_toggle_modifiers"] == ["super"]

    def test_colliding_combos_repaired(self, temp_config_dir: Path):
        from wayfinder.config import load_config

        # The baked-in corruption: both actions on the same chord.
        self._write_config({
            "hotkey_key": 61, "hotkey_modifiers": ["super"],
            "style_toggle_key": 61, "style_toggle_modifiers": ["super"],
        })
        config = load_config()

        # Recording keeps the user's saved combo; style yields to legacy F10.
        assert config["hotkey_key"] == 61
        assert config["hotkey_modifiers"] == ["super"]
        assert config["style_toggle_key"] == 68
        assert config["style_toggle_modifiers"] == []

    def test_explicitly_saved_distinct_hotkeys_untouched(self, temp_config_dir: Path):
        from wayfinder.config import load_config

        self._write_config({
            "hotkey_key": 67, "hotkey_modifiers": ["ctrl"],
            "style_toggle_key": 68, "style_toggle_modifiers": ["alt"],
        })
        config = load_config()

        assert config["hotkey_key"] == 67
        assert config["hotkey_modifiers"] == ["ctrl"]
        assert config["style_toggle_key"] == 68
        assert config["style_toggle_modifiers"] == ["alt"]

"""
Tests for the configuration module.
"""

import json
import os
import subprocess
import sys
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

    def test_welcome_completed_default(self):
        """The first-run welcome tour gate is present and defaults to False."""
        from wayfinder.config import DEFAULT_CONFIG

        assert "welcome_completed" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["welcome_completed"] is False

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

    def test_overlay_enabled_is_sticky_across_reload(self, temp_config_dir: Path):
        """Show Overlay off survives save → next-launch load (sticky preference)."""
        from wayfinder.config import save_config, load_config, DEFAULT_CONFIG

        cfg = DEFAULT_CONFIG.copy()
        cfg["overlay_enabled"] = False
        save_config(cfg)

        reloaded = load_config()
        assert reloaded["overlay_enabled"] is False

        reloaded["overlay_enabled"] = True
        save_config(reloaded)
        assert load_config()["overlay_enabled"] is True

    def test_save_config_is_owner_only(self, temp_config_dir: Path, sample_config: dict):
        """config.json is written 0600 (may hold API keys)."""
        from wayfinder.config import save_config, CONFIG_FILE

        save_config(sample_config)
        assert CONFIG_FILE.exists()
        assert (CONFIG_FILE.stat().st_mode & 0o777) == 0o600

    def test_load_repairs_config_backup_modes(self, temp_config_dir: Path):
        """Existing config.json* backups are tightened to 0600 on load."""
        import json
        import os
        from wayfinder.config import load_config, CONFIG_FILE, CONFIG_DIR

        CONFIG_FILE.write_text(json.dumps({"threads": 2}))
        bak = CONFIG_DIR / "config.json.bak-test"
        bak.write_text(json.dumps({"groq_api_key": "secret"}))
        os.chmod(bak, 0o644)
        load_config()
        assert (bak.stat().st_mode & 0o777) == 0o600

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

    def test_foreign_parent_flatpak_id_does_not_select_flatpak_defaults(self):
        """Import-time config must ignore FLATPAK_ID leaked by another Flatpak app."""
        repo = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo / "src")
        env["FLATPAK_ID"] = "com.visualstudio.code"
        env.pop("WAYFINDER_FLATPAK", None)

        code = """
from wayfinder import config
assert config.IS_FLATPAK is False
assert not config.DEFAULT_CONFIG["whisper_binary"].startswith("/app/")
assert not config.DEFAULT_CONFIG["model_path"].startswith("/app/")
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr or result.stdout

    def test_foreign_parent_appdir_does_not_select_appimage_defaults(self, tmp_path: Path):
        """Import-time config must ignore APPDIR leaked by another AppImage app."""
        repo = Path(__file__).resolve().parent.parent
        foreign_appdir = tmp_path / "Foreign.AppDir"
        (foreign_appdir / "usr" / "bin").mkdir(parents=True)
        (foreign_appdir / "usr" / "bin" / "whisper-cli").write_text("#!/bin/sh\n")

        env = os.environ.copy()
        env["PYTHONPATH"] = str(repo / "src")
        env["APPIMAGE"] = str(tmp_path / "Foreign.AppImage")
        env["APPDIR"] = str(foreign_appdir)

        code = f"""
from wayfinder import config
assert config.IS_APPIMAGE is False
assert config.APPDIR == ""
assert {str(foreign_appdir)!r} not in config.DEFAULT_CONFIG["whisper_binary"]
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr or result.stdout


class TestStalePathRepair:
    """load_config heals saved paths that no longer exist on this machine by
    falling back to the auto-detected default — e.g. a from-source config with
    llama_cpp_binary=~/llama.cpp/... is meaningless inside the Flatpak sandbox,
    where the bundled /app/bin/llama-simple is the working default."""

    def test_stale_llama_binary_repaired_to_existing_default(
        self, temp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from wayfinder import config as cfg

        bundled = tmp_path / "llama-simple"
        bundled.write_text("")
        monkeypatch.setitem(cfg.DEFAULT_CONFIG, "llama_cpp_binary", str(bundled))

        cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.CONFIG_FILE, "w") as f:
            json.dump({"llama_cpp_binary": "/nonexistent/llama.cpp/build/bin/llama-cli"}, f)

        config = cfg.load_config()
        assert config["llama_cpp_binary"] == str(bundled)

    def test_existing_llama_binary_is_kept(
        self, temp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from wayfinder import config as cfg

        bundled = tmp_path / "llama-simple"
        bundled.write_text("")
        user_binary = tmp_path / "my-llama-cli"
        user_binary.write_text("")
        monkeypatch.setitem(cfg.DEFAULT_CONFIG, "llama_cpp_binary", str(bundled))

        cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.CONFIG_FILE, "w") as f:
            json.dump({"llama_cpp_binary": str(user_binary)}, f)

        config = cfg.load_config()
        assert config["llama_cpp_binary"] == str(user_binary)

    def test_blank_whisper_binary_repaired_to_existing_host_candidate(
        self, temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A saved empty whisper_binary must not survive as ''. Path('') is cwd,
        which made the transcriber think an empty server binary was available."""
        from wayfinder import config as cfg

        home = temp_config_dir.parents[1]
        host_cli = home / "whisper.cpp" / "build" / "bin" / "whisper-cli"
        host_cli.parent.mkdir(parents=True, exist_ok=True)
        host_cli.write_text("#!/bin/sh\n")
        monkeypatch.setitem(cfg.DEFAULT_CONFIG, "whisper_binary", "/missing/default/whisper-cli")

        cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.CONFIG_FILE, "w") as f:
            json.dump({"whisper_binary": ""}, f)

        config = cfg.load_config()
        assert config["whisper_binary"] == str(host_cli)

    def test_source_mode_ignores_existing_flatpak_app_path(
        self, temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A stale Flatpak /app path must not win after moving back to source mode."""
        from wayfinder import config as cfg

        home = temp_config_dir.parents[1]
        host_cli = home / "whisper.cpp" / "build" / "bin" / "whisper-cli"
        host_cli.parent.mkdir(parents=True, exist_ok=True)
        host_cli.write_text("#!/bin/sh\n")

        monkeypatch.setattr(cfg, "IS_FLATPAK", False)
        monkeypatch.setitem(cfg.DEFAULT_CONFIG, "whisper_binary", "/missing/default/whisper-cli")
        monkeypatch.setattr(cfg.shutil, "which", lambda name: "/app/bin/whisper-cli")

        cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.CONFIG_FILE, "w") as f:
            json.dump({"whisper_binary": "/app/bin/whisper-cli"}, f)

        config = cfg.load_config()
        assert config["whisper_binary"] == str(host_cli)

    def test_flatpak_mode_allows_bundled_app_path(
        self, temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The /app guard must not break real Flatpak defaults."""
        from wayfinder import config as cfg

        monkeypatch.setattr(cfg, "IS_FLATPAK", True)
        monkeypatch.setitem(cfg.DEFAULT_CONFIG, "whisper_binary", "/missing/default/whisper-cli")
        # Path.exists() passes Path objects into os.path.exists — compare as str,
        # and keep real exists for config/temp paths used by load_config.
        _real_exists = cfg.os.path.exists

        def _exists(path):
            s = str(path)
            if s == "/app/bin/whisper-cli":
                return True
            if s.startswith("/app/"):
                return False
            return _real_exists(path)

        monkeypatch.setattr(cfg.os.path, "exists", _exists)
        monkeypatch.setattr(cfg.shutil, "which", lambda name: "/app/bin/whisper-cli")

        cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg.CONFIG_FILE, "w") as f:
            json.dump({"whisper_binary": "/app/bin/whisper-cli"}, f)

        config = cfg.load_config()
        assert config["whisper_binary"] == "/app/bin/whisper-cli"


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

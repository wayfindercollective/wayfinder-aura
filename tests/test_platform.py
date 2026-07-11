"""
Tests for wayfinder.utils.platform module.

Covers platform detection, directory paths, binary detection, and tool detection.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.utils.platform import (
    find_executable,
    get_appimage_dir,
    get_cache_dir,
    get_config_dir,
    get_data_dir,
    get_default_llama_binary,
    get_default_model_dir,
    get_default_whisper_binary,
    get_default_ydotool_binary,
    get_desktop_environment,
    get_portal_app_id,
    get_wm_class,
    get_platform,
    get_platform_info,
    get_session_type,
    get_text_injector,
    ensure_directories,
    is_appimage,
    is_flatpak,
    get_wayfinder_appimage_dir,
    is_wayfinder_flatpak_env,
    is_gnome,
    is_kde,
    is_linux,
    is_macos,
    is_wayland,
    is_windows,
    is_x11,
    is_xdotool_available,
    is_ydotool_available,
)

import wayfinder.utils.platform as platform_mod
from wayfinder.utils.platform import (
    _detect_steam_platform,
    get_steam_platform,
    get_steam_platform_label,
    is_steamos,
)


# =============================================================================
# Platform Detection
# =============================================================================


class TestPlatformDetection:
    """Tests for basic platform detection functions."""

    def test_is_linux_returns_true_on_linux(self):
        """is_linux() should return True when sys.platform starts with 'linux'."""
        with patch.object(sys, "platform", "linux"):
            assert is_linux() is True

    def test_is_linux_returns_false_on_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            assert is_linux() is False

    def test_get_platform_returns_linux(self):
        with patch.object(sys, "platform", "linux"):
            assert get_platform() == "linux"

    def test_get_platform_returns_darwin(self):
        with patch.object(sys, "platform", "darwin"):
            assert get_platform() == "darwin"

    def test_get_platform_returns_windows(self):
        with patch.object(sys, "platform", "win32"):
            assert get_platform() == "windows"


# =============================================================================
# Linux-Specific Detection
# =============================================================================


class TestFlatpakDetection:
    """Tests for Flatpak environment detection."""

    def test_is_flatpak_with_flatpak_id(self, flatpak_env):
        """is_flatpak() returns True when FLATPAK_ID is set."""
        assert is_flatpak() is True

    def test_is_flatpak_without_env_vars(self):
        """is_flatpak() returns False when no Flatpak env vars are set."""
        # clean_environment autouse fixture already clears FLATPAK_ID
        assert is_flatpak() is False

    def test_wm_class_matches_flatpak_desktop_and_separates_source_runs(self):
        flatpak_env = {"FLATPAK_ID": "io.wayfindercollective.WayfinderAura"}
        assert get_wm_class(flatpak_env) == "Wayfinder-aura"
        assert get_wm_class({}) == "WayfinderAura"
        assert get_wm_class({"FLATPAK_ID": "com.visualstudio.code"}) == "WayfinderAura"

    def test_foreign_parent_flatpak_id_is_not_wayfinder_flatpak(self, monkeypatch):
        """A Flatpak-hosted editor should not make source runs use /app defaults."""
        monkeypatch.setenv("FLATPAK_ID", "com.visualstudio.code")
        monkeypatch.delenv("WAYFINDER_FLATPAK", raising=False)

        assert is_wayfinder_flatpak_env() is False
        assert is_flatpak() is False
        assert get_portal_app_id() == "wayfinder-aura"


class TestAppImageDetection:
    """Tests for AppImage environment detection."""

    def test_is_appimage_with_appdir(self, appimage_env):
        """is_appimage() returns True when APPDIR is set."""
        assert is_appimage() is True

    def test_is_appimage_without_env_vars(self):
        """is_appimage() returns False when no AppImage env vars are set."""
        assert is_appimage() is False

    def test_get_appimage_dir_returns_path_when_set(self, appimage_env):
        """get_appimage_dir() returns a Path when APPDIR is set."""
        result = get_appimage_dir()
        assert result is not None
        assert isinstance(result, Path)
        assert result == appimage_env

    def test_get_appimage_dir_returns_none_otherwise(self):
        """get_appimage_dir() returns None when not in AppImage."""
        assert get_appimage_dir() is None

    def test_foreign_parent_appdir_is_not_wayfinder_appimage(self, monkeypatch, tmp_path):
        """APPDIR must contain Wayfinder's executable before bundled paths are trusted."""
        appdir = tmp_path / "Foreign.AppDir"
        (appdir / "usr" / "bin").mkdir(parents=True)
        (appdir / "usr" / "bin" / "ydotool").write_text("#!/bin/sh\n")
        monkeypatch.setenv("APPIMAGE", str(tmp_path / "Foreign.AppImage"))
        monkeypatch.setenv("APPDIR", str(appdir))

        assert get_wayfinder_appimage_dir() is None
        assert is_appimage() is False
        assert get_appimage_dir() is None


class TestSessionDetection:
    """Tests for Wayland/X11 and desktop environment detection."""

    def test_is_wayland_true(self, wayland_env):
        """is_wayland() returns True when XDG_SESSION_TYPE=wayland."""
        assert is_wayland() is True

    def test_is_x11_false_on_wayland(self, wayland_env):
        """is_x11() returns False when on Wayland."""
        assert is_x11() is False

    def test_is_x11_true(self, x11_env):
        """is_x11() returns True when XDG_SESSION_TYPE=x11."""
        assert is_x11() is True

    def test_is_wayland_false_on_x11(self, x11_env):
        """is_wayland() returns False when on X11."""
        assert is_wayland() is False

    def test_is_wayland_true_via_wayland_display_when_session_type_unset(self, monkeypatch):
        """Regression: a KDE Wayland session can launch the app with XDG_SESSION_TYPE
        unset (only WAYLAND_DISPLAY set). is_wayland() must still detect Wayland, or we
        fall to the X11/xdotool path and hit XWayland's gated, truncating XTEST."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert is_wayland() is True

    def test_is_wayland_x11_session_wins_over_stray_wayland_display(self, monkeypatch):
        """An explicit XDG_SESSION_TYPE=x11 must keep is_wayland() False even if a stray
        WAYLAND_DISPLAY lingers (e.g. nested/XWayland helpers)."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        assert is_wayland() is False

    def test_is_wayland_false_when_neither_signal_present(self, monkeypatch):
        """No XDG_SESSION_TYPE and no WAYLAND_DISPLAY → not Wayland."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        assert is_wayland() is False

    def test_is_kde_true(self, wayland_env):
        """is_kde() returns True when KDE is in XDG_CURRENT_DESKTOP."""
        # wayland_env sets XDG_CURRENT_DESKTOP=KDE
        assert is_kde() is True

    def test_is_gnome_true(self, x11_env):
        """is_gnome() returns True when GNOME is in XDG_CURRENT_DESKTOP."""
        # x11_env sets XDG_CURRENT_DESKTOP=GNOME
        assert is_gnome() is True

    def test_get_desktop_environment(self, wayland_env):
        assert get_desktop_environment() == "KDE"

    def test_get_session_type_wayland(self, wayland_env):
        assert get_session_type() == "wayland"

    def test_get_session_type_x11(self, x11_env):
        assert get_session_type() == "x11"


# =============================================================================
# Directory Paths
# =============================================================================


class TestDirectoryPaths:
    """Tests for config/data/cache directory path resolution."""

    def test_get_config_dir_returns_xdg_path(self, monkeypatch):
        """get_config_dir() uses XDG_CONFIG_HOME on Linux."""
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        with patch.object(sys, "platform", "linux"):
            result = get_config_dir()
            assert result.name == "wayfinder-aura"
            assert ".config" in str(result)

    def test_get_data_dir_returns_xdg_path(self, monkeypatch):
        """get_data_dir() uses XDG_DATA_HOME on Linux."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        with patch.object(sys, "platform", "linux"):
            result = get_data_dir()
            assert result.name == "wayfinder-aura"
            assert ".local" in str(result) or "share" in str(result)

    def test_get_cache_dir_returns_xdg_path(self, monkeypatch):
        """get_cache_dir() uses XDG_CACHE_HOME on Linux."""
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        with patch.object(sys, "platform", "linux"):
            result = get_cache_dir()
            assert result.name == "wayfinder-aura"
            assert ".cache" in str(result)

    def test_ensure_directories_creates_all_dirs(self, temp_dir, monkeypatch):
        """ensure_directories() creates config, data, and cache dirs."""
        monkeypatch.setenv("HOME", str(temp_dir))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

        with patch.object(sys, "platform", "linux"):
            ensure_directories()

            assert get_config_dir().exists()
            assert get_data_dir().exists()
            assert get_cache_dir().exists()


# =============================================================================
# Binary Detection
# =============================================================================


class TestBinaryDetection:
    """Tests for whisper/llama/ydotool binary path detection."""

    def test_get_default_whisper_binary_appimage(self, appimage_env):
        """Returns AppImage-bundled path when whisper-cli exists in AppDir."""
        bundled = appimage_env / "usr" / "bin" / "whisper-cli"
        bundled.touch()

        result = get_default_whisper_binary()
        assert result == str(bundled)

    def test_get_default_whisper_binary_normal(self):
        """Returns home-based path when not in AppImage."""
        result = get_default_whisper_binary()
        assert "whisper.cpp" in result
        assert result.endswith("whisper-cli")

    def test_get_default_llama_binary_appimage(self, appimage_env):
        """Returns AppImage-bundled path when llama-cli exists in AppDir."""
        bundled = appimage_env / "usr" / "bin" / "llama-cli"
        bundled.touch()

        result = get_default_llama_binary()
        assert result == str(bundled)

    def test_get_default_model_dir_appimage_bundled(self, appimage_env):
        """Returns bundled model dir when it exists in AppImage."""
        # appimage_env fixture already creates usr/share/whisper-models
        result = get_default_model_dir()
        assert str(result).endswith("whisper-models")
        assert "AppDir" in str(result)

    def test_get_default_model_dir_fallback(self):
        """Returns data_dir/models when not in AppImage."""
        result = get_default_model_dir()
        assert str(result).endswith("models")

    def test_get_default_ydotool_binary_appimage(self, appimage_env):
        """Returns bundled ydotool when it exists in AppDir."""
        bundled = appimage_env / "usr" / "bin" / "ydotool"
        bundled.touch()

        result = get_default_ydotool_binary()
        assert result == str(bundled)


# =============================================================================
# Text Injector Detection
# =============================================================================


class TestTextInjector:
    """Tests for get_text_injector() tool selection."""

    def test_get_text_injector_wayland_prefers_ydotool_when_ready(self, wayland_env):
        """Wayland prefers ydotool when its daemon is reachable — avoids KDE's per-use
        input-control prompt that wtype's virtual-keyboard protocol triggers."""
        with patch("wayfinder.utils.platform.is_ydotool_available", return_value=True), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "ready")), \
             patch("wayfinder.utils.platform.is_wtype_available", return_value=True):
            assert get_text_injector() == "ydotool"

    def test_get_text_injector_wayland_uses_wtype_when_ydotool_not_ready(self, wayland_env):
        """Wayland falls back to wtype when the ydotool daemon isn't reachable (e.g. Flatpak —
        no uinput device)."""
        with patch("wayfinder.utils.platform.is_ydotool_available", return_value=True), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(False, "no socket")), \
             patch("wayfinder.utils.platform.is_wtype_available", return_value=True):
            assert get_text_injector() == "wtype"

    def test_get_text_injector_wayland_falls_back_to_ydotool(self, wayland_env):
        """Wayland uses ydotool when wtype is unavailable, even if the daemon probe is negative."""
        with patch("wayfinder.utils.platform.is_wtype_available", return_value=False), \
             patch("wayfinder.utils.platform.is_ydotool_available", return_value=True), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(False, "no socket")):
            assert get_text_injector() == "ydotool"

    def test_get_text_injector_x11_prefers_xdotool(self, x11_env):
        """Returns 'xdotool' on X11 when xdotool is available."""
        with patch("wayfinder.utils.platform.is_xdotool_available", return_value=True):
            assert get_text_injector() == "xdotool"

    def test_get_text_injector_x11_falls_back_to_ydotool(self, x11_env):
        """Returns 'ydotool' on X11 when only ydotool is available."""
        with patch("wayfinder.utils.platform.is_xdotool_available", return_value=False), \
             patch("wayfinder.utils.platform.is_ydotool_available", return_value=True):
            assert get_text_injector() == "ydotool"

    def test_get_text_injector_none_when_nothing_available(self, x11_env):
        """Returns 'none' when no injector tools are available."""
        with patch("wayfinder.utils.platform.is_xdotool_available", return_value=False), \
             patch("wayfinder.utils.platform.is_ydotool_available", return_value=False):
            assert get_text_injector() == "none"


# =============================================================================
# Platform Info Summary
# =============================================================================


class TestPlatformInfo:
    """Tests for get_platform_info() summary dict."""

    def test_get_platform_info_returns_dict_with_expected_keys(self):
        """get_platform_info() returns a dict with core keys."""
        info = get_platform_info()
        assert isinstance(info, dict)
        for key in ["platform", "is_linux", "is_macos", "is_windows",
                     "config_dir", "data_dir", "cache_dir", "text_injector"]:
            assert key in info, f"Missing key: {key}"

    def test_get_platform_info_includes_linux_keys_on_linux(self, wayland_env):
        """On Linux, get_platform_info() includes Linux-specific fields."""
        with patch.object(sys, "platform", "linux"):
            info = get_platform_info()
            for key in ["is_wayland", "is_x11", "is_flatpak", "is_appimage",
                         "desktop_environment", "session_type"]:
                assert key in info, f"Missing Linux key: {key}"


# =============================================================================
# Steam Platform Identity (Steam Deck / Steam Machine / SteamOS)
# =============================================================================


def _dmi(**fields):
    """Build a fake _read_dmi that returns `fields[field]` (default '')."""
    return lambda field: fields.get(field, "")


class TestSteamPlatform:
    """Tests for get_steam_platform() / get_steam_platform_label() / is_steamos().

    The AMD gate (branding only shows on AMD) lives in the Benchmark-tile UI,
    which needs full Tk and isn't unit-tested here; these cover the identity
    logic the UI depends on.
    """

    @pytest.fixture(autouse=True)
    def _reset_steam(self, monkeypatch):
        """Drop the module-level cache + any override before/after each test so
        detection is recomputed hermetically."""
        platform_mod._steam_platform_cache = None
        monkeypatch.delenv("WAYFINDER_STEAM_PLATFORM", raising=False)
        monkeypatch.setattr(sys, "platform", "linux")
        yield
        platform_mod._steam_platform_cache = None

    # --- env override ------------------------------------------------------

    @pytest.mark.parametrize("value,expected", [
        ("deck", "deck"), ("machine", "machine"), ("steamos", "steamos"),
        ("DECK", "deck"), ("  machine  ", "machine"),
    ])
    def test_override_forces_value(self, monkeypatch, value, expected):
        monkeypatch.setenv("WAYFINDER_STEAM_PLATFORM", value)
        assert _detect_steam_platform() == expected

    @pytest.mark.parametrize("value", ["", "nonsense", "windows"])
    def test_override_garbage_is_none(self, monkeypatch, value):
        monkeypatch.setenv("WAYFINDER_STEAM_PLATFORM", value)
        assert _detect_steam_platform() is None

    def test_override_wins_over_hardware(self, monkeypatch):
        """An explicit override ignores real DMI (so previews work on any box)."""
        monkeypatch.setenv("WAYFINDER_STEAM_PLATFORM", "steamos")
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(product_name="Jupiter"))
        assert _detect_steam_platform() == "steamos"

    # --- hardware (DMI) ----------------------------------------------------

    @pytest.mark.parametrize("product", ["Jupiter", "Galileo", "jupiter", "GALILEO"])
    def test_steam_deck_hardware(self, monkeypatch, product):
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(product_name=product))
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: False)
        assert _detect_steam_platform() == "deck"

    def test_valve_non_deck_hardware_is_machine(self, monkeypatch):
        """Valve hardware that isn't a known Deck board → Steam Machine (future-proof)."""
        monkeypatch.setattr(
            platform_mod, "_read_dmi",
            _dmi(sys_vendor="Valve", product_name="Fremont"),
        )
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: False)
        assert _detect_steam_platform() == "machine"

    def test_deck_board_wins_over_valve_vendor(self, monkeypatch):
        """A real Deck reports sys_vendor=Valve AND product=Jupiter → 'deck', not 'machine'."""
        monkeypatch.setattr(
            platform_mod, "_read_dmi", _dmi(sys_vendor="Valve", product_name="Jupiter"),
        )
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: False)
        assert _detect_steam_platform() == "deck"

    def test_hardware_wins_over_steamos(self, monkeypatch):
        """Steam Machine running SteamOS reads as 'machine' (hardware beats OS)."""
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(sys_vendor="Valve"))
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: True)
        assert _detect_steam_platform() == "machine"

    # --- OS (SteamOS on non-Valve hardware) --------------------------------

    def test_steamos_on_generic_hardware(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(sys_vendor="ASUS", product_name="ROG"))
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: True)
        assert _detect_steam_platform() == "steamos"

    def test_plain_amd_desktop_is_none(self, monkeypatch):
        """A generic AMD box (not Valve, not SteamOS) gets no branding."""
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(sys_vendor="ASUS", product_name="TUF"))
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: False)
        assert _detect_steam_platform() is None

    def test_non_linux_is_none(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(product_name="Jupiter"))
        assert _detect_steam_platform() is None

    # --- is_steamos parsing (/etc/os-release) ------------------------------

    def _patch_os_release(self, monkeypatch, content):
        real = platform_mod.Path.read_text

        def fake(self, *a, **k):
            if str(self) == "/etc/os-release":
                return content
            return real(self, *a, **k)

        monkeypatch.setattr(platform_mod.Path, "read_text", fake)

    @pytest.mark.parametrize("content", [
        'ID=steamos\n',
        'ID="steamos"\n',
        'NAME="SteamOS"\nID=steamos\nID_LIKE=arch\n',
        'ID=arch\nID_LIKE="archarm steamos"\n',
        'ID=holo\nVARIANT_ID=steamos\n',
    ])
    def test_is_steamos_true(self, monkeypatch, content):
        self._patch_os_release(monkeypatch, content)
        assert is_steamos() is True

    @pytest.mark.parametrize("content", [
        'ID=bazzite\nID_LIKE="fedora"\n',
        'ID=fedora\n',
        'ID=arch\nID_LIKE=arch\n',
        '',
    ])
    def test_is_steamos_false(self, monkeypatch, content):
        self._patch_os_release(monkeypatch, content)
        assert is_steamos() is False

    def test_is_steamos_missing_file_is_false(self, monkeypatch):
        def boom(self, *a, **k):
            raise OSError("no such file")
        monkeypatch.setattr(platform_mod.Path, "read_text", boom)
        assert is_steamos() is False

    # --- label mapping + caching -------------------------------------------

    @pytest.mark.parametrize("key,label", [
        ("deck", "Steam Deck"), ("machine", "Steam Machine"), ("steamos", "SteamOS"),
    ])
    def test_label_mapping(self, monkeypatch, key, label):
        monkeypatch.setenv("WAYFINDER_STEAM_PLATFORM", key)
        assert get_steam_platform_label() == label

    def test_label_is_none_when_not_steam(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi())
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: False)
        assert get_steam_platform_label() is None

    def test_result_is_cached(self, monkeypatch):
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi(product_name="Jupiter"))
        monkeypatch.setattr(platform_mod, "is_steamos", lambda: False)
        assert get_steam_platform() == "deck"
        # Underlying hardware "changes" — cached result must not.
        monkeypatch.setattr(platform_mod, "_read_dmi", _dmi())
        assert get_steam_platform() == "deck"

"""
UI end-to-end tests for Wayfinder Aura.

These tests validate data structures, imports, and configuration consistency
that the UI layer depends on. They do NOT create Tk windows or start event loops.

All tests are marked @pytest.mark.ui so they can be skipped in headless CI:
    pytest -m "not ui"
"""

import json

import pytest

# Skip entire file if CustomTkinter is not available
ctk = pytest.importorskip("customtkinter")

pytestmark = pytest.mark.ui


# =============================================================================
# App Import
# =============================================================================


class TestAppImport:
    """Verify that the main application module is importable."""

    def test_wayfinder_app_importable(self):
        """WayfinderApp class should be importable from wayfinder_main."""
        from wayfinder_main import WayfinderApp

        assert WayfinderApp is not None

    def test_core_modules_importable(self):
        """All core submodules should be importable."""
        from wayfinder.core import recorder, transcriber, injector, postprocessor
        from wayfinder import config, state

        assert recorder is not None
        assert transcriber is not None
        assert injector is not None
        assert postprocessor is not None
        assert config is not None
        assert state is not None


# =============================================================================
# Theme Consistency
# =============================================================================


class TestThemeConsistency:
    """Verify theme tokens are well-formed and complete."""

    def test_state_colors_cover_all_states(self):
        """STATE_COLORS should have an entry for every AppState."""
        from wayfinder.state import AppState
        from wayfinder.ui.theme import STATE_COLORS

        for s in AppState:
            assert s in STATE_COLORS, f"Missing STATE_COLORS entry for {s.name}"

    def test_state_colors_are_hex_strings(self):
        """Each state color should be a valid hex color string."""
        from wayfinder.ui.theme import STATE_COLORS

        for state, color in STATE_COLORS.items():
            assert isinstance(color, str)
            assert color.startswith("#"), f"{state.name} color {color!r} is not a hex color"
            assert len(color) == 7, f"{state.name} color {color!r} is not #RRGGBB format"

    def test_required_color_keys_exist(self):
        """COLORS dict should have all keys the UI references."""
        from wayfinder.ui.theme import COLORS

        required_keys = [
            "bg_base", "bg_surface", "bg_card", "bg_hover", "bg_elevated", "bg_input",
            "accent", "text_bright", "text_primary", "text_secondary", "text_muted",
            "border", "state_ready", "state_recording", "state_processing", "state_typing",
        ]
        for key in required_keys:
            assert key in COLORS, f"Missing COLORS key: {key}"

    def test_font_sizes_are_positive_ints(self):
        """All FONT_SIZES values should be positive integers."""
        from wayfinder.ui.theme import FONT_SIZES

        for name, size in FONT_SIZES.items():
            assert isinstance(size, int), f"FONT_SIZES[{name!r}] is not int"
            assert size > 0, f"FONT_SIZES[{name!r}] is not positive"

    def test_radius_tokens_exist(self):
        """RADIUS dict should have sm, md, lg, xl keys."""
        from wayfinder.ui.theme import RADIUS

        for key in ["sm", "md", "lg", "xl"]:
            assert key in RADIUS
            assert isinstance(RADIUS[key], int)
            assert RADIUS[key] > 0


# =============================================================================
# Config Defaults Match UI Expectations
# =============================================================================


class TestConfigDefaultsForUI:
    """Verify that DEFAULT_CONFIG has all keys the UI needs."""

    def test_ui_critical_keys_exist(self):
        """All config keys referenced by the UI should exist in DEFAULT_CONFIG."""
        from wayfinder.config import DEFAULT_CONFIG

        ui_keys = [
            "typing_speed", "output_tone", "strong_mode",
            "overlay_type", "overlay_scale", "start_minimized",
            "hotkey_key", "hotkey_modifiers",
            "style_toggle_key", "style_toggle_modifiers",
            "post_processing_enabled", "post_processing_backend",
            "transcription_backend", "use_gpu",
            "audio_ducking_enabled", "audio_ducking_percent",
            "processing_mode",
        ]
        for key in ui_keys:
            assert key in DEFAULT_CONFIG, f"Missing DEFAULT_CONFIG key: {key}"

    def test_typing_speed_default_is_valid(self):
        """Default typing_speed should be one of the valid presets."""
        from wayfinder.config import DEFAULT_CONFIG
        from wayfinder.core.injector import TYPING_SPEEDS

        default_speed = DEFAULT_CONFIG["typing_speed"]
        assert default_speed in TYPING_SPEEDS

    def test_output_tone_default_is_valid(self):
        """Default output_tone should be one of the 5 presets."""
        from wayfinder.config import DEFAULT_CONFIG
        from wayfinder.core.postprocessor import get_tone_options

        valid_ids = {t["id"] for t in get_tone_options()}
        assert DEFAULT_CONFIG["output_tone"] in valid_ids


# =============================================================================
# Overlay Module
# =============================================================================


class TestOverlayModule:
    """Test overlay module import and IPC format assumptions."""

    def test_overlay_module_importable(self):
        """The overlay module should be importable."""
        from wayfinder.ui import overlay
        assert overlay is not None

    def test_overlay_state_json_serializable(self):
        """State data sent to the overlay process must be JSON-serializable."""
        from wayfinder.state import AppState

        # Simulate the state message the main process sends to the overlay
        state_msg = {
            "state": AppState.RECORDING.name,
            "text": "Listening...",
        }
        serialized = json.dumps(state_msg)
        deserialized = json.loads(serialized)
        assert deserialized["state"] == "RECORDING"
        assert deserialized["text"] == "Listening..."


# =============================================================================
# Typing Speed Presets
# =============================================================================


class TestTypingSpeedPresets:
    """Validate the TYPING_SPEEDS constant."""

    def test_all_five_speeds_exist(self):
        """TYPING_SPEEDS should contain exactly 5 presets."""
        from wayfinder.core.injector import TYPING_SPEEDS

        expected = {"instant", "fast", "normal", "slow", "very_slow"}
        assert set(TYPING_SPEEDS.keys()) == expected

    def test_speed_values_are_tuples_of_two_ints(self):
        """Each speed value should be a (key_delay, key_hold) tuple."""
        from wayfinder.core.injector import TYPING_SPEEDS

        for name, (delay, hold) in TYPING_SPEEDS.items():
            assert isinstance(delay, (int, float)), f"{name}: delay is not numeric"
            assert isinstance(hold, (int, float)), f"{name}: hold is not numeric"
            assert delay >= 0, f"{name}: delay is negative"
            assert hold >= 0, f"{name}: hold is negative"

    def test_instant_has_zero_delays(self):
        """The 'instant' preset should have zero delay and hold."""
        from wayfinder.core.injector import TYPING_SPEEDS

        delay, hold = TYPING_SPEEDS["instant"]
        assert delay == 0
        assert hold == 0


# =============================================================================
# Tone Options
# =============================================================================


class TestToneOptions:
    """Validate get_tone_options() output format."""

    def test_tone_options_required_fields(self):
        """Each tone option dict should have id, name, icon, description."""
        from wayfinder.core.postprocessor import get_tone_options

        for tone in get_tone_options():
            for field in ("id", "name", "icon", "description"):
                assert field in tone, f"Tone {tone.get('id', '?')} missing field: {field}"
                assert isinstance(tone[field], str)
                assert len(tone[field]) > 0

    def test_tone_ids_are_unique(self):
        """No duplicate tone IDs."""
        from wayfinder.core.postprocessor import get_tone_options

        ids = [t["id"] for t in get_tone_options()]
        assert len(ids) == len(set(ids))


# =============================================================================
# Hotkey Defaults
# =============================================================================


class TestHotkeyDefaults:
    """Validate hotkey configuration defaults."""

    def test_hotkey_key_is_valid_code(self):
        """Default hotkey_key should be a positive integer (evdev key code)."""
        from wayfinder.config import DEFAULT_CONFIG, KEY_CODES

        key = DEFAULT_CONFIG["hotkey_key"]
        assert isinstance(key, int)
        assert key > 0
        # Should be one of the known key codes
        assert key in KEY_CODES.values(), f"hotkey_key {key} not in KEY_CODES"

    def test_style_toggle_key_is_valid(self):
        """Default style_toggle_key should be a valid key code."""
        from wayfinder.config import DEFAULT_CONFIG, KEY_CODES

        key = DEFAULT_CONFIG["style_toggle_key"]
        assert isinstance(key, int)
        assert key > 0
        assert key in KEY_CODES.values()

    def test_hotkey_modifiers_is_list(self):
        """hotkey_modifiers should default to an empty list."""
        from wayfinder.config import DEFAULT_CONFIG

        mods = DEFAULT_CONFIG["hotkey_modifiers"]
        assert isinstance(mods, list)

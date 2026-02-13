"""
Tests for wayfinder.ui.theme module.

Covers color constants, typography tokens, spacing/radius dicts,
state color mapping, and helper functions.
"""

import re

import pytest

from wayfinder.state import AppState
from wayfinder.ui.theme import (
    COLORS,
    FONTS,
    FONT_SIZES,
    RADIUS,
    SPACING,
    STATE_COLORS,
    get_font,
    get_font_size,
    get_state_color,
    scaled_font_size,
)


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


# =============================================================================
# Color Constants
# =============================================================================


class TestColors:
    """Tests for the COLORS palette dict."""

    def test_all_colors_are_valid_hex(self):
        """Every value in COLORS must be a 6-digit hex string."""
        for key, value in COLORS.items():
            assert HEX_COLOR_RE.match(value), (
                f"COLORS['{key}'] = '{value}' is not a valid #RRGGBB hex"
            )

    def test_essential_keys_exist(self):
        """COLORS must contain the core background and text keys."""
        required = [
            "bg_base", "bg_surface", "bg_card",
            "accent",
            "state_ready", "state_recording", "state_processing", "state_typing",
            "text_primary", "text_secondary", "text_muted",
        ]
        for key in required:
            assert key in COLORS, f"Missing COLORS key: {key}"

    def test_state_colors_are_valid_hex(self):
        """All four state color values must be valid hex."""
        for key in ["state_ready", "state_recording", "state_processing", "state_typing"]:
            assert HEX_COLOR_RE.match(COLORS[key]), f"Bad hex for {key}: {COLORS[key]}"


# =============================================================================
# STATE_COLORS Mapping
# =============================================================================


class TestStateColors:
    """Tests for the STATE_COLORS dict and get_state_color() helper."""

    def test_all_four_states_mapped(self):
        """STATE_COLORS must map every AppState to a color."""
        for state in [AppState.IDLE, AppState.RECORDING, AppState.PROCESSING, AppState.PASTING]:
            assert state in STATE_COLORS, f"Missing state: {state}"

    def test_all_state_color_values_are_hex(self):
        for state, color in STATE_COLORS.items():
            assert HEX_COLOR_RE.match(color), f"Bad hex for {state}: {color}"

    def test_get_state_color_idle(self):
        assert get_state_color(AppState.IDLE) == COLORS["state_ready"]

    def test_get_state_color_recording(self):
        assert get_state_color(AppState.RECORDING) == COLORS["state_recording"]

    def test_get_state_color_processing(self):
        assert get_state_color(AppState.PROCESSING) == COLORS["state_processing"]

    def test_get_state_color_pasting(self):
        assert get_state_color(AppState.PASTING) == COLORS["state_typing"]

    def test_get_state_color_unknown_returns_accent(self):
        """An unrecognized state should fall back to the accent color."""
        # Use a mock state value that won't exist in the mapping
        class FakeState:
            pass

        assert get_state_color(FakeState()) == COLORS["accent"]


# =============================================================================
# Typography
# =============================================================================


class TestFonts:
    """Tests for FONTS and font helper functions."""

    def test_all_fonts_are_non_empty_tuples(self):
        """Every FONTS entry must be a non-empty tuple of strings."""
        for key, value in FONTS.items():
            assert isinstance(value, tuple), f"FONTS['{key}'] should be a tuple"
            assert len(value) > 0, f"FONTS['{key}'] should be non-empty"
            for item in value:
                assert isinstance(item, str), f"FONTS['{key}'] contains non-string: {item}"

    def test_get_font_returns_correct_tuple(self):
        assert get_font("display") == FONTS["display"]
        assert get_font("header") == FONTS["header"]
        assert get_font("body") == FONTS["body"]
        assert get_font("mono") == FONTS["mono"]

    def test_get_font_unknown_returns_body_fallback(self):
        """Unknown font type should fall back to body font."""
        assert get_font("nonexistent") == FONTS["body"]


class TestFontSizes:
    """Tests for FONT_SIZES and font size helper functions."""

    def test_all_font_sizes_are_positive_ints(self):
        for key, value in FONT_SIZES.items():
            assert isinstance(value, int), f"FONT_SIZES['{key}'] should be int"
            assert value > 0, f"FONT_SIZES['{key}'] should be positive"

    def test_expected_size_values(self):
        assert FONT_SIZES["display"] == 18
        assert FONT_SIZES["title"] == 15
        assert FONT_SIZES["heading"] == 13
        assert FONT_SIZES["body"] == 13
        assert FONT_SIZES["small"] == 11
        assert FONT_SIZES["caption"] == 10

    def test_get_font_size_returns_correct_value(self):
        assert get_font_size("display") == 18
        assert get_font_size("caption") == 10

    def test_get_font_size_unknown_returns_body(self):
        """Unknown size name should fall back to body size."""
        assert get_font_size("nonexistent") == FONT_SIZES["body"]


# =============================================================================
# Scaled Font Size
# =============================================================================


class TestScaledFontSize:
    """Tests for scaled_font_size() helper."""

    def test_scale_1x(self):
        assert scaled_font_size(13, 1.0) == 13

    def test_scale_2x(self):
        assert scaled_font_size(10, 2.0) == 20

    def test_scale_fractional(self):
        assert scaled_font_size(10, 1.5) == 15

    def test_scale_zero(self):
        assert scaled_font_size(10, 0.0) == 0

    def test_truncates_to_int(self):
        """Result should be truncated (int()) not rounded."""
        assert scaled_font_size(10, 0.75) == 7  # int(7.5) = 7


# =============================================================================
# Spacing & Radius Tokens
# =============================================================================


class TestRadius:
    """Tests for RADIUS design tokens."""

    def test_expected_keys_and_values(self):
        assert RADIUS["sm"] == 8
        assert RADIUS["md"] == 12
        assert RADIUS["lg"] == 24
        assert RADIUS["xl"] == 28

    def test_all_values_are_positive_ints(self):
        for key, value in RADIUS.items():
            assert isinstance(value, int) and value > 0, f"Bad RADIUS['{key}']: {value}"


class TestSpacing:
    """Tests for SPACING design tokens."""

    def test_expected_keys_exist(self):
        expected = ["gutter", "tile_pad", "xs", "sm", "md", "lg", "xl", "2xl"]
        for key in expected:
            assert key in SPACING, f"Missing SPACING key: {key}"

    def test_expected_values(self):
        assert SPACING["gutter"] == 24
        assert SPACING["tile_pad"] == 20
        assert SPACING["xs"] == 4
        assert SPACING["sm"] == 8
        assert SPACING["md"] == 12
        assert SPACING["lg"] == 16
        assert SPACING["xl"] == 24
        assert SPACING["2xl"] == 32

    def test_all_values_are_positive_ints(self):
        for key, value in SPACING.items():
            assert isinstance(value, int) and value > 0, f"Bad SPACING['{key}']: {value}"

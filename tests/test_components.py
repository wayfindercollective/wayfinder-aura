"""
Tests for wayfinder.ui.components module.

Focuses on data structures and logic that don't require a running Tk instance:
- SETTING_TOOLTIPS dictionary
- MODEL_RECOMMENDATIONS string
- get_dynamic_tooltip() function

These checks belong in the regular CI suite; a display server is unnecessary.
"""

import pytest

# Skip entire file if CustomTkinter is not available
ctk = pytest.importorskip("customtkinter")

class TestSettingTooltips:
    """Tests for the SETTING_TOOLTIPS dictionary."""

    def test_is_non_empty_dict(self):
        """SETTING_TOOLTIPS should be a non-empty dictionary."""
        from wayfinder.ui.components import SETTING_TOOLTIPS

        assert isinstance(SETTING_TOOLTIPS, dict)
        assert len(SETTING_TOOLTIPS) > 0

    def test_keys_are_strings(self):
        """All keys should be strings."""
        from wayfinder.ui.components import SETTING_TOOLTIPS

        for key in SETTING_TOOLTIPS:
            assert isinstance(key, str), f"Key {key!r} is not a string"

    def test_values_are_non_empty_strings(self):
        """All values should be non-empty strings."""
        from wayfinder.ui.components import SETTING_TOOLTIPS

        for key, val in SETTING_TOOLTIPS.items():
            assert isinstance(val, str), f"SETTING_TOOLTIPS[{key!r}] value is not a string"
            assert len(val.strip()) > 0, f"SETTING_TOOLTIPS[{key!r}] is empty"

    def test_every_key_the_ui_looks_up_exists(self):
        """SETTING_TOOLTIPS["x"] in a pane builder raises KeyError and the pane
        stops building, so every subscripted key must exist."""
        import re
        from pathlib import Path

        from wayfinder.ui.components import SETTING_TOOLTIPS

        repo = Path(__file__).resolve().parent.parent
        sources = [repo / "wayfinder_main.py", *sorted((repo / "src" / "wayfinder" / "ui").glob("*.py"))]
        looked_up = set()
        for path in sources:
            looked_up |= set(re.findall(r'SETTING_TOOLTIPS\["([a-z_]+)"\]', path.read_text(encoding="utf-8")))
        assert {"hotkey", "microphone", "language", "gpu_device"} <= looked_up
        missing = sorted(looked_up - set(SETTING_TOOLTIPS))
        assert not missing, f"Missing tooltips for: {missing}"

    def test_hints_are_plain_text(self):
        """Rule 11: no emoji in setting hints (the retired copy was full of them)."""
        from wayfinder.ui.components import SETTING_TOOLTIPS

        for key, text in SETTING_TOOLTIPS.items():
            assert all(ord(ch) < 0x2600 or ch in "—–…→·•" for ch in text), key

    def test_backend_tooltip_discloses_free_ultra_boundary(self):
        from wayfinder.ui.components import SETTING_TOOLTIPS

        assert "Free uses Base on the CPU" in SETTING_TOOLTIPS["backend"]
        assert "Ultra" in SETTING_TOOLTIPS["backend"]


class TestModelRecommendations:
    """Tests for the MODEL_RECOMMENDATIONS string."""

    def test_is_non_empty_string(self):
        """MODEL_RECOMMENDATIONS should be a non-empty string."""
        from wayfinder.ui.components import MODEL_RECOMMENDATIONS

        assert isinstance(MODEL_RECOMMENDATIONS, str)
        assert len(MODEL_RECOMMENDATIONS.strip()) > 0

    def test_contains_model_names(self):
        """MODEL_RECOMMENDATIONS should mention at least a couple of model names."""
        from wayfinder.ui.components import MODEL_RECOMMENDATIONS

        # Should mention at least one recommended model
        text_lower = MODEL_RECOMMENDATIONS.lower()
        assert "qwen" in text_lower or "llama" in text_lower or "phi" in text_lower


class TestGetDynamicTooltip:
    """Tests for get_dynamic_tooltip() function."""

    def test_returns_string(self):
        """get_dynamic_tooltip should always return a string."""
        from wayfinder.ui.components import get_dynamic_tooltip

        result = get_dynamic_tooltip("hotkey", {})
        assert isinstance(result, str)

    def test_falls_back_to_static_tooltip(self):
        """With empty config (no benchmarks), should fall back to static tooltip text."""
        from wayfinder.ui.components import get_dynamic_tooltip, SETTING_TOOLTIPS

        result = get_dynamic_tooltip("hotkey", {})
        assert result == SETTING_TOOLTIPS["hotkey"] and result

    def test_whisper_model_without_benchmarks(self):
        """whisper_model tooltip without benchmarks should prompt user to run benchmark."""
        from wayfinder.ui.components import get_dynamic_tooltip

        result = get_dynamic_tooltip("whisper_model", {})
        assert "benchmark" in result.lower() or "machine" in result.lower()

    def test_whisper_model_with_benchmarks(self):
        """whisper_model tooltip with benchmark data should include speed info."""
        from wayfinder.ui.components import get_dynamic_tooltip

        config = {
            "benchmark_results": {
                "base.en": {"cpu_10s": 4.0, "gpu_10s": 0.8, "method": "warm"},
                "small.en": {"cpu_10s": 6.0, "gpu_10s": 1.2, "method": "warm"},
            },
            "benchmark_fastest_processor": "gpu",
        }
        result = get_dynamic_tooltip("whisper_model", config)
        # Should contain speed data from benchmarks
        assert "GPU" in result or "gpu" in result
        assert "Base" in result or "Small" in result

    def test_legacy_cold_benchmarks_are_retired(self):
        """Entries without the warm-method marker were cold single shots that
        lumped model load + GPU pipeline compile into the number — tooltips
        must not present them as hardware speeds."""
        from wayfinder.ui.components import get_dynamic_tooltip

        config = {
            "benchmark_results": {
                "base.en": {"cpu_10s": 0.6, "gpu_10s": 0.8},  # legacy, no marker
            },
            "benchmark_fastest_processor": "cpu",
        }
        result = get_dynamic_tooltip("whisper_model", config)
        assert "0.8" not in result and "0.6" not in result
        assert "run benchmark" in result.lower()

    def test_unknown_key_returns_empty_string(self):
        """get_dynamic_tooltip for an unknown key should return empty string."""
        from wayfinder.ui.components import get_dynamic_tooltip

        result = get_dynamic_tooltip("nonexistent_setting_xyz", {})
        assert result == ""

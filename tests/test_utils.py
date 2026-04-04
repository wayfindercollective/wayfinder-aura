"""
Tests for utility modules:
- wayfinder.utils.logging
- wayfinder.utils.lazy_imports
- wayfinder.utils.audio_ducker
"""

import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# =============================================================================
# Logging Module Tests
# =============================================================================


class TestLoggingConfiguration:
    """Tests for wayfinder.utils.logging module."""

    def setup_method(self):
        """Reset logging module state before each test."""
        import wayfinder.utils.logging as log_mod
        log_mod._configured = False
        log_mod._loggers.clear()
        # Clear existing handlers on the wayfinder logger
        root = logging.getLogger("wayfinder")
        root.handlers.clear()

    def test_get_logger_returns_logger_instance(self):
        """get_logger should return a stdlib Logger."""
        from wayfinder.utils.logging import get_logger

        logger = get_logger("wayfinder.test_module")
        assert isinstance(logger, logging.Logger)

    def test_get_logger_adds_wayfinder_prefix(self):
        """get_logger should prefix non-wayfinder names."""
        from wayfinder.utils.logging import get_logger

        logger = get_logger("mymodule")
        assert logger.name == "wayfinder.mymodule"

    def test_get_logger_keeps_wayfinder_prefix(self):
        """get_logger should not double-prefix wayfinder names."""
        from wayfinder.utils.logging import get_logger

        logger = get_logger("wayfinder.core.recorder")
        assert logger.name == "wayfinder.core.recorder"

    def test_configure_logging_sets_level(self):
        """configure_logging should set the requested level on root logger."""
        from wayfinder.utils.logging import configure_logging

        configure_logging(level=logging.DEBUG, log_to_file=False, log_to_console=True)
        root = logging.getLogger("wayfinder")
        assert root.level == logging.DEBUG

    def test_configure_logging_console_handler(self):
        """configure_logging with log_to_console adds a StreamHandler."""
        from wayfinder.utils.logging import configure_logging

        configure_logging(log_to_file=False, log_to_console=True)
        root = logging.getLogger("wayfinder")
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert len(stream_handlers) >= 1

    def test_set_level_changes_all_handlers(self):
        """set_level should update both the logger and its handlers."""
        from wayfinder.utils.logging import configure_logging, set_level

        configure_logging(level=logging.INFO, log_to_file=False, log_to_console=True)
        set_level(logging.WARNING)

        root = logging.getLogger("wayfinder")
        assert root.level == logging.WARNING
        for handler in root.handlers:
            assert handler.level == logging.WARNING

    def test_get_log_file_path_returns_path(self, tmp_path, monkeypatch):
        """get_log_file_path should return a Path under the cache dir."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from wayfinder.utils.logging import get_log_file_path

        path = get_log_file_path()
        assert path is not None
        assert path.name == "wayfinder.log"
        assert "wayfinder-aura" in str(path)

    def test_convenience_functions_do_not_raise(self):
        """Module-level debug/info/warning/error/exception should not raise."""
        from wayfinder.utils.logging import configure_logging
        configure_logging(log_to_file=False, log_to_console=False)

        from wayfinder.utils import logging as wf_log
        wf_log.debug("test debug")
        wf_log.info("test info")
        wf_log.warning("test warning")
        wf_log.error("test error")
        # exception is fine outside an except block if exc_info not required
        wf_log.exception("test exception")


# =============================================================================
# Lazy Imports Module Tests
# =============================================================================


class TestLazyModule:
    """Tests for LazyModule deferred-import behavior."""

    def test_lazy_module_defers_import(self):
        """LazyModule should NOT import the module at construction time."""
        from wayfinder.utils.lazy_imports import LazyModule

        lazy = LazyModule("json")
        # Internal _module should still be None
        assert lazy._module is None

    def test_lazy_module_loads_on_attribute_access(self):
        """Accessing an attribute should trigger the actual import."""
        from wayfinder.utils.lazy_imports import LazyModule

        lazy = LazyModule("json")
        # Access an attribute from the json module
        _ = lazy.dumps
        assert lazy._module is not None

    def test_lazy_module_bool_true_for_available(self):
        """bool(LazyModule) should be True for available modules."""
        from wayfinder.utils.lazy_imports import LazyModule

        lazy = LazyModule("json")
        assert bool(lazy) is True

    def test_lazy_module_bool_false_for_missing(self):
        """bool(LazyModule) should be False for missing modules."""
        from wayfinder.utils.lazy_imports import LazyModule

        lazy = LazyModule("nonexistent_module_xyz_abc_123")
        assert bool(lazy) is False

    def test_lazy_module_is_available_property(self):
        """is_available should reflect actual module availability."""
        from wayfinder.utils.lazy_imports import LazyModule

        available = LazyModule("os")
        missing = LazyModule("nonexistent_module_xyz_abc_123")

        assert available.is_available is True
        assert missing.is_available is False

    def test_lazy_module_raises_on_missing_attribute(self):
        """Accessing an attr on an unavailable module should raise ImportError."""
        from wayfinder.utils.lazy_imports import LazyModule

        lazy = LazyModule("nonexistent_module_xyz_abc_123")
        with pytest.raises(ImportError, match="not available"):
            _ = lazy.some_function

    def test_get_optional_dependencies_status_returns_dict(self):
        """get_optional_dependencies_status should return a dict of booleans."""
        from wayfinder.utils.lazy_imports import get_optional_dependencies_status

        status = get_optional_dependencies_status()
        assert isinstance(status, dict)
        assert len(status) > 0
        for key, val in status.items():
            assert isinstance(key, str)
            assert isinstance(val, bool)

    def test_lazy_module_submodule(self):
        """LazyModule with submodule should resolve the sub-attribute."""
        from wayfinder.utils.lazy_imports import LazyModule

        lazy = LazyModule("os", submodule="path")
        # After load, the module should be os.path
        assert lazy.is_available is True
        result = lazy.join("a", "b")
        assert result == "a/b" or result == "a\\b"  # platform-dependent


# =============================================================================
# Audio Ducker Module Tests
# =============================================================================


class TestParseSinkInputs:
    """Tests for _parse_sink_inputs parsing logic (no subprocess needed)."""

    def test_parse_single_sink_input(self):
        """Parse a single sink input entry."""
        from wayfinder.utils.audio_ducker import _parse_sink_inputs

        output = (
            "Sink Input #17961\n"
            "    Driver: PipeWire\n"
            "    Volume: front-left: 55706 /  85% / -4.23 dB,   front-right: 55706 /  85% / -4.23 dB\n"
            "    Properties:\n"
            '        application.name = "Chromium"\n'
        )
        result = _parse_sink_inputs(output)
        assert len(result) == 1
        assert result[0]["id"] == 17961
        assert result[0]["volume_percent"] == 85
        assert result[0]["app_name"] == "Chromium"

    def test_parse_multiple_sink_inputs(self):
        """Parse multiple sink inputs in one output."""
        from wayfinder.utils.audio_ducker import _parse_sink_inputs

        output = (
            "Sink Input #100\n"
            "    Volume: front-left: 65536 / 100% / 0.00 dB\n"
            "    Properties:\n"
            '        application.name = "Firefox"\n'
            "Sink Input #200\n"
            "    Volume: front-left: 32768 /  50% / -18.06 dB\n"
            "    Properties:\n"
            '        application.name = "Spotify"\n'
        )
        result = _parse_sink_inputs(output)
        assert len(result) == 2
        assert result[0]["app_name"] == "Firefox"
        assert result[1]["volume_percent"] == 50

    def test_parse_empty_output(self):
        """Parse empty output returns empty list."""
        from wayfinder.utils.audio_ducker import _parse_sink_inputs

        assert _parse_sink_inputs("") == []
        assert _parse_sink_inputs("\n\n") == []


class TestAudioDucker:
    """Tests for AudioDucker class with mocked pactl."""

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_ducker_init_available(self, mock_avail):
        """AudioDucker marks itself available when pactl is present."""
        from wayfinder.utils.audio_ducker import AudioDucker

        ducker = AudioDucker(duck_percent=20.0)
        assert ducker.is_available is True
        assert ducker.is_ducked is False

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=False)
    def test_ducker_init_unavailable(self, mock_avail):
        """AudioDucker gracefully handles missing pactl."""
        from wayfinder.utils.audio_ducker import AudioDucker

        ducker = AudioDucker()
        assert ducker.is_available is False
        assert ducker.duck() is False
        assert ducker.restore() is False

    @patch("wayfinder.utils.audio_ducker.set_sink_input_volume", return_value=True)
    @patch("wayfinder.utils.audio_ducker.get_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_duck_reduces_volume(self, mock_avail, mock_get_sinks, mock_set_vol):
        """duck() should reduce each sink input volume by the duck percentage."""
        from wayfinder.utils.audio_ducker import AudioDucker

        mock_get_sinks.return_value = [
            {"id": 100, "volume_percent": 100, "app_name": "Firefox"},
        ]
        ducker = AudioDucker(duck_percent=25.0)
        result = ducker.duck()

        assert result is True
        assert ducker.is_ducked is True
        # 100% * (1 - 0.25) = 75%
        mock_set_vol.assert_called_once_with(100, 75)

    @patch("wayfinder.utils.audio_ducker.set_sink_input_volume", return_value=True)
    @patch("wayfinder.utils.audio_ducker.get_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_duck_excludes_apps(self, mock_avail, mock_get_sinks, mock_set_vol):
        """duck() should skip excluded applications."""
        from wayfinder.utils.audio_ducker import AudioDucker

        mock_get_sinks.return_value = [
            {"id": 100, "volume_percent": 80, "app_name": "Firefox"},
            {"id": 200, "volume_percent": 90, "app_name": "Wayfinder"},
        ]
        ducker = AudioDucker(duck_percent=50.0, exclude_apps=["Wayfinder"])
        ducker.duck()

        # Only Firefox should be ducked, not Wayfinder
        mock_set_vol.assert_called_once_with(100, 40)

    @patch("wayfinder.utils.audio_ducker.set_sink_input_volume", return_value=True)
    @patch("wayfinder.utils.audio_ducker.get_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_restore_returns_original_volume(self, mock_avail, mock_get_sinks, mock_set_vol):
        """restore() should set volumes back to their original values."""
        from wayfinder.utils.audio_ducker import AudioDucker

        mock_get_sinks.return_value = [
            {"id": 100, "volume_percent": 80, "app_name": "Firefox"},
        ]
        ducker = AudioDucker(duck_percent=50.0)
        ducker.duck()
        mock_set_vol.reset_mock()

        ducker.restore()
        assert ducker.is_ducked is False
        mock_set_vol.assert_called_once_with(100, 80)

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_double_duck_prevented(self, mock_avail):
        """Calling duck() twice should not double-duck."""
        from wayfinder.utils.audio_ducker import AudioDucker

        with patch("wayfinder.utils.audio_ducker.get_sink_inputs", return_value=[
            {"id": 1, "volume_percent": 100, "app_name": "App"},
        ]), patch("wayfinder.utils.audio_ducker.set_sink_input_volume", return_value=True):
            ducker = AudioDucker(duck_percent=20.0)
            ducker.duck()
            # Second duck should return False
            assert ducker.duck() is False

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_set_duck_percent_clamps(self, mock_avail):
        """set_duck_percent should clamp to 0-100 range."""
        from wayfinder.utils.audio_ducker import AudioDucker

        ducker = AudioDucker(duck_percent=50.0)

        ducker.set_duck_percent(150)
        assert ducker._duck_percent == 100

        ducker.set_duck_percent(-10)
        assert ducker._duck_percent == 0

    @patch("wayfinder.utils.audio_ducker.subprocess.run")
    def test_set_sink_input_volume_clamps_to_150(self, mock_run):
        """set_sink_input_volume should cap at 150%."""
        from wayfinder.utils.audio_ducker import set_sink_input_volume

        mock_run.return_value = MagicMock(returncode=0)
        set_sink_input_volume(1, 200)
        # Should be clamped to 150%
        args = mock_run.call_args[0][0]
        assert "150%" in args


# =============================================================================
# Lazy Imports Availability Check Tests
# =============================================================================


class TestLazyImportsAvailabilityChecks:
    """Tests that is_*_available() functions don't crash (importlib.util fix)."""

    def test_is_pyqt6_available_no_crash(self):
        """is_pyqt6_available should return a bool without crashing."""
        from wayfinder.utils.lazy_imports import is_pyqt6_available
        result = is_pyqt6_available()
        assert isinstance(result, bool)

    def test_is_faster_whisper_available_no_crash(self):
        """is_faster_whisper_available should return a bool without crashing."""
        from wayfinder.utils.lazy_imports import is_faster_whisper_available
        result = is_faster_whisper_available()
        assert isinstance(result, bool)

    def test_is_scipy_available_no_crash(self):
        """is_scipy_available should return a bool without crashing."""
        from wayfinder.utils.lazy_imports import is_scipy_available
        result = is_scipy_available()
        assert isinstance(result, bool)

    def test_is_anthropic_available_no_crash(self):
        """is_anthropic_available should return a bool without crashing."""
        from wayfinder.utils.lazy_imports import is_anthropic_available
        result = is_anthropic_available()
        assert isinstance(result, bool)

    def test_is_llama_cpp_available_no_crash(self):
        """is_llama_cpp_available should return a bool without crashing."""
        from wayfinder.utils.lazy_imports import is_llama_cpp_available
        result = is_llama_cpp_available()
        assert isinstance(result, bool)

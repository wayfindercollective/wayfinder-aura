"""
Tests for utility modules:
- wayfinder.utils.logging
- wayfinder.utils.lazy_imports
- wayfinder.utils.audio_ducker
"""

import importlib
import json
import logging
import os
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

    def test_json_parser_preserves_channels_mute_and_serial(self):
        from wayfinder.utils.audio_ducker import _parse_sink_inputs_json

        result = _parse_sink_inputs_json(json.dumps([{
            "index": 42,
            "client": 7,
            "mute": True,
            "volume": {
                "front-left": {"value": 65536, "value_percent": "100%"},
                "front-right": {"value": 32768, "value_percent": "50%"},
            },
            "properties": {
                "application.name": "Player",
                "object.serial": "9001",
            },
        }]))

        assert result[0]["channel_volumes"] == [65536, 32768]
        assert result[0]["muted"] is True
        assert result[0]["identity"] == "serial:9001"


class TestAudioDucker:
    """Tests for AudioDucker class with mocked pactl."""

    @staticmethod
    def stream(
        sink_id=100,
        volumes=(65536, 65536),
        app_name="Firefox",
        serial="serial-100",
    ):
        from wayfinder.utils.audio_ducker import _finish_stream

        return _finish_stream(
            {
                "id": sink_id,
                "client": "client-1",
                "channel_volumes": list(volumes),
                "muted": False,
                "app_name": app_name,
                "serial": serial,
                "properties": {
                    "application.name": app_name,
                    "application.process.id": "1234",
                    "object.serial": serial,
                },
            }
        )

    @staticmethod
    def query_ok():
        from wayfinder.utils.audio_ducker import _PactlResult

        return _PactlResult(True)

    @staticmethod
    def set_ok():
        from wayfinder.utils.audio_ducker import _PactlResult

        return _PactlResult(True)

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_ducker_init_available(self, mock_avail):
        """AudioDucker marks itself available when pactl is present."""
        from wayfinder.utils.audio_ducker import AudioDucker

        ducker = AudioDucker(duck_percent=20.0, recovery_path=None)
        assert ducker.is_available is True
        assert ducker.is_ducked is False

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=False)
    def test_ducker_init_unavailable(self, mock_avail):
        """AudioDucker gracefully handles missing pactl."""
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus

        ducker = AudioDucker(recovery_path=None)
        assert ducker.is_available is False
        assert ducker.duck().status is DuckingStatus.UNAVAILABLE
        assert ducker.restore().status is DuckingStatus.UNAVAILABLE

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_duck_reduces_each_channel(self, mock_avail, mock_query, mock_set):
        """duck() should reduce every channel by the configured percentage."""
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus

        stream = self.stream(volumes=(65536, 32768))
        mock_query.side_effect = [([stream], self.query_ok()), ([
            self.stream(volumes=(49152, 24576))
        ], self.query_ok())]
        mock_set.return_value = self.set_ok()
        ducker = AudioDucker(duck_percent=25.0, recovery_path=None)
        result = ducker.duck()

        assert result.status is DuckingStatus.APPLIED
        assert ducker.is_ducked is True
        mock_set.assert_called_once_with(100, [49152, 24576])
        assert ducker.restore().status is DuckingStatus.RESTORED
        assert mock_set.call_args_list[-1] == call(100, [65536, 32768])

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_duck_100_percent_means_silence(self, mock_avail, mock_query, mock_set):
        from wayfinder.utils.audio_ducker import AudioDucker

        mock_query.return_value = ([self.stream()], self.query_ok())
        mock_set.return_value = self.set_ok()
        ducker = AudioDucker(duck_percent=100, recovery_path=None)
        ducker.duck()

        mock_set.assert_called_once_with(100, [0, 0])
        ducker._is_ducked = False

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_duck_excludes_aura_and_configured_apps(self, mock_avail, mock_query, mock_set):
        from wayfinder.utils.audio_ducker import AudioDucker

        mock_query.return_value = ([
            self.stream(100, app_name="Firefox", serial="a"),
            self.stream(200, app_name="Wayfinder Aura", serial="b"),
            self.stream(300, app_name="Music Test", serial="c"),
        ], self.query_ok())
        mock_set.return_value = self.set_ok()
        ducker = AudioDucker(
            duck_percent=50.0,
            exclude_apps=["Music Test"],
            recovery_path=None,
        )
        ducker.duck()

        mock_set.assert_called_once_with(100, [32768, 32768])
        ducker._is_ducked = False

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_all_denied_is_truthful(self, mock_avail, mock_query, mock_set):
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus, _PactlResult

        mock_query.return_value = ([self.stream()], self.query_ok())
        mock_set.return_value = _PactlResult(False, "permission_denied", "Access denied")
        ducker = AudioDucker(duck_percent=50, recovery_path=None)
        result = ducker.duck()

        assert result.status is DuckingStatus.PERMISSION_DENIED
        assert ducker.is_ducked is False
        assert ducker._records == {}

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_partial_success_tracks_only_successful_writes(self, mock_avail, mock_query, mock_set):
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus, _PactlResult

        mock_query.return_value = ([
            self.stream(100, serial="a"),
            self.stream(200, serial="b"),
        ], self.query_ok())
        mock_set.side_effect = [self.set_ok(), _PactlResult(False, "permission_denied")]
        ducker = AudioDucker(duck_percent=50, recovery_path=None)
        result = ducker.duck()

        assert result.status is DuckingStatus.PARTIAL
        assert result.changed_count == 1
        assert result.failed_count == 1
        assert set(ducker._records) == {100}
        ducker._is_ducked = False

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_double_duck_prevented(self, mock_avail):
        """Calling duck() twice should not double-duck."""
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus

        with patch("wayfinder.utils.audio_ducker._query_sink_inputs", return_value=(
            [self.stream(1)], self.query_ok()
        )), patch(
            "wayfinder.utils.audio_ducker._set_sink_input_channel_volumes",
            return_value=self.set_ok(),
        ):
            ducker = AudioDucker(duck_percent=20.0, recovery_path=None)
            ducker.duck()
            assert ducker.duck().status is DuckingStatus.ALREADY_DUCKED
            ducker._is_ducked = False

    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_set_duck_percent_clamps(self, mock_avail):
        """set_duck_percent should clamp to 0-100 range."""
        from wayfinder.utils.audio_ducker import AudioDucker

        ducker = AudioDucker(duck_percent=50.0, recovery_path=None)

        ducker.set_duck_percent(150)
        assert ducker._duck_percent == 100

        ducker.set_duck_percent(-10)
        assert ducker._duck_percent == 0

    @patch("wayfinder.utils.audio_ducker.subprocess.run")
    def test_set_sink_input_volume_uses_manager_env_and_clamps(self, mock_run):
        """Only pactl mutations receive the narrow manager identity."""
        from wayfinder.utils.audio_ducker import set_sink_input_volume

        mock_run.return_value = MagicMock(returncode=0)
        set_sink_input_volume(1, 200)
        args = mock_run.call_args[0][0]
        assert "150%" in args
        env = mock_run.call_args.kwargs["env"]
        assert env["PULSE_PROP_media.category"] == "Manager"
        assert env["LC_ALL"] == "C"

    @patch.dict(os.environ, {"PULSE_PROP_media.category": "Manager"})
    def test_read_only_query_environment_drops_manager_identity(self):
        from wayfinder.utils.audio_ducker import _pactl_env

        assert "PULSE_PROP_media.category" not in _pactl_env(manager=False)

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_restore_skips_reused_stream_id(self, mock_avail, mock_query, mock_set):
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus

        original = self.stream(100, serial="old")
        reused = self.stream(100, volumes=(32768, 32768), serial="new")
        mock_query.side_effect = [([original], self.query_ok()), ([reused], self.query_ok())]
        mock_set.return_value = self.set_ok()
        ducker = AudioDucker(duck_percent=50, recovery_path=None)
        ducker.duck()
        mock_set.reset_mock()

        result = ducker.restore()

        assert result.status is DuckingStatus.NO_CHANGE
        assert result.skipped_count == 1
        mock_set.assert_not_called()
        assert ducker.is_ducked is False

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_restore_does_not_overwrite_user_change(self, mock_avail, mock_query, mock_set):
        from wayfinder.utils.audio_ducker import AudioDucker

        original = self.stream(100, volumes=(65536, 32768))
        user_changed = self.stream(100, volumes=(40000, 20000))
        mock_query.side_effect = [([original], self.query_ok()), ([user_changed], self.query_ok())]
        mock_set.return_value = self.set_ok()
        ducker = AudioDucker(duck_percent=50, recovery_path=None)
        ducker.duck()
        mock_set.reset_mock()

        result = ducker.restore()

        assert result.skipped_count == 1
        mock_set.assert_not_called()
        assert ducker.is_ducked is False

    @patch("wayfinder.utils.audio_ducker._set_sink_input_channel_volumes")
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_stale_journal_restores_after_crash(self, mock_avail, mock_query, mock_set, tmp_path):
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus

        journal = tmp_path / "audio-duck-recovery.json"
        original = self.stream(100)
        ducked = self.stream(100, volumes=(32768, 32768))
        record = AudioDucker._record_for(original, [32768, 32768])
        journal.write_text(json.dumps({"version": 1, "pid": 0, "streams": [record]}))
        mock_query.return_value = ([ducked], self.query_ok())
        mock_set.return_value = self.set_ok()

        ducker = AudioDucker(duck_percent=50, recovery_path=journal)

        assert ducker.recovery_result.status is DuckingStatus.RESTORED
        mock_set.assert_called_once_with(100, [65536, 65536])
        assert not journal.exists()

    @patch("wayfinder.utils.audio_ducker._pid_is_alive", return_value=True)
    @patch("wayfinder.utils.audio_ducker._query_sink_inputs")
    @patch("wayfinder.utils.audio_ducker.is_pactl_available", return_value=True)
    def test_live_process_journal_is_not_touched(self, mock_avail, mock_query, mock_alive, tmp_path):
        from wayfinder.utils.audio_ducker import AudioDucker, DuckingStatus

        journal = tmp_path / "audio-duck-recovery.json"
        record = AudioDucker._record_for(self.stream(), [32768, 32768])
        journal.write_text(json.dumps({"version": 1, "pid": os.getpid() + 10_000, "streams": [record]}))

        ducker = AudioDucker(duck_percent=50, recovery_path=journal)

        assert ducker.recovery_result.status is DuckingStatus.NO_CHANGE
        mock_query.assert_not_called()
        assert journal.exists()


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

"""
Tests for the audio recorder module.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestDeviceSelection:
    """Test audio device selection logic."""

    def test_preferred_mic_keywords_exist(self):
        """Verify preferred microphone keywords are defined."""
        from wayfinder.core.recorder import PREFERRED_MIC_KEYWORDS

        assert isinstance(PREFERRED_MIC_KEYWORDS, list)
        assert len(PREFERRED_MIC_KEYWORDS) > 0

    def test_excluded_device_keywords_exist(self):
        """Verify excluded device keywords are defined."""
        from wayfinder.core.recorder import EXCLUDED_DEVICE_KEYWORDS

        assert isinstance(EXCLUDED_DEVICE_KEYWORDS, list)
        assert len(EXCLUDED_DEVICE_KEYWORDS) > 0

    @patch("wayfinder.core.recorder.sd")
    def test_list_input_devices_returns_list(self, mock_sd):
        """Test that list_input_devices returns a list."""
        from wayfinder.core.recorder import list_input_devices

        mock_sd.query_devices.return_value = [
            {"name": "Test Mic", "max_input_channels": 2},
            {"name": "Test Speaker", "max_input_channels": 0},
        ]

        devices = list_input_devices()

        assert isinstance(devices, list)

    @patch("wayfinder.core.recorder.sd")
    def test_list_input_devices_filters_outputs(self, mock_sd):
        """Test that output-only devices are filtered."""
        from wayfinder.core.recorder import list_input_devices

        mock_sd.query_devices.return_value = [
            {"name": "USB Microphone", "max_input_channels": 2},
            {"name": "HDMI Output", "max_input_channels": 0},
            {"name": "Speakers", "max_input_channels": 0},
        ]

        devices = list_input_devices()

        # Only the microphone should be included
        device_names = [d["name"] for d in devices]
        assert "USB Microphone" in device_names
        assert "HDMI Output" not in device_names


class TestAudioRecorder:
    """Test the AudioRecorder class."""

    @patch("wayfinder.core.recorder.sd")
    def test_recorder_initialization(self, mock_sd):
        """Test AudioRecorder can be initialized."""
        from wayfinder.core.recorder import AudioRecorder

        mock_sd.query_devices.return_value = [
            {"name": "Default", "max_input_channels": 2, "default_samplerate": 44100},
        ]
        mock_sd.default.device = [0, 0]

        recorder = AudioRecorder()

        assert recorder is not None
        assert recorder.sample_rate == 16000  # Whisper sample rate

    @patch("wayfinder.core.recorder.sd")
    def test_recorder_with_custom_device(self, mock_sd):
        """Test AudioRecorder with specific device."""
        from wayfinder.core.recorder import AudioRecorder

        recorder = AudioRecorder(device=1)

        assert recorder.device == 1


class TestChunkedRecorder:
    """Test the ChunkedRecorder class."""

    @patch("wayfinder.core.recorder.sd")
    def test_chunked_recorder_initialization(self, mock_sd):
        """Test ChunkedRecorder can be initialized."""
        from wayfinder.core.recorder import ChunkedRecorder

        recorder = ChunkedRecorder(chunk_duration=5.0, chunk_overlap=1.0)

        assert recorder.chunk_duration == 5.0
        assert recorder.chunk_overlap == 1.0


class TestAudioProcessing:
    """Test audio processing utilities."""

    def test_resample_audio_no_change(self):
        """Test resampling when sample rate is same."""
        import numpy as np
        from wayfinder.core.recorder import resample_audio

        audio = np.zeros(1000, dtype=np.float32)
        result = resample_audio(audio, 16000, 16000)

        assert len(result) == len(audio)

    def test_resample_audio_downsample(self):
        """Test downsampling audio."""
        import numpy as np
        from wayfinder.core.recorder import resample_audio

        # 44100 Hz to 16000 Hz
        audio = np.zeros(44100, dtype=np.float32)
        result = resample_audio(audio, 44100, 16000)

        # Should be approximately 16000 samples for 1 second
        assert abs(len(result) - 16000) < 100

    def test_preprocess_audio_off(self):
        """Test preprocessing with mode 'off' passes through unchanged."""
        import numpy as np
        from wayfinder.core.recorder import preprocess_audio

        audio = np.array([0.5, 0.5, 0.5], dtype=np.float32)
        result = preprocess_audio(audio, 16000, "off")

        np.testing.assert_array_equal(result, audio)

    def test_preprocess_audio_light(self):
        """Test preprocessing with mode 'light' applies gain normalization."""
        import numpy as np
        from wayfinder.core.recorder import preprocess_audio

        # Create audio with low amplitude
        audio = np.array([0.1, 0.1, 0.1], dtype=np.float32)
        result = preprocess_audio(audio, 16000, "light")

        # Light mode applies gain normalization to -3dB (0.707)
        assert result is not None
        assert len(result) == len(audio)
        # Peak should be close to 0.707 after normalization
        assert abs(np.max(np.abs(result)) - 0.707) < 0.01

    def test_get_audio_level(self):
        """Test audio level calculation via AudioRecorder method."""
        import numpy as np
        from wayfinder.core.recorder import AudioRecorder

        with patch("wayfinder.core.recorder.sd"):
            recorder = AudioRecorder()

        # No frames should give 0
        recorder.frames = []
        level = recorder.get_audio_level()
        assert level == 0.0

        # With some data in frames, should return a level
        recorder.frames = [np.ones(1000, dtype=np.float32)]
        level = recorder.get_audio_level()
        assert 0.0 <= level <= 1.0

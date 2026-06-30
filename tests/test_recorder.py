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


class TestSilenceDetection:
    """Peak tracking feeds the silence guard that replaces 'no output' confusion."""

    def test_threshold_constant(self):
        from wayfinder.core.recorder import SILENCE_PEAK_THRESHOLD

        # True silence is exact zeros; live-mic noise floor is well above 0.001.
        assert 0 < SILENCE_PEAK_THRESHOLD < 0.01

    def test_recorder_tracks_peak(self):
        import numpy as np
        from wayfinder.core.recorder import AudioRecorder

        with patch("wayfinder.core.recorder.sd"):
            recorder = AudioRecorder()

        recorder._audio_callback(np.full(100, 0.2, dtype=np.float32), 100, None, None)
        recorder._audio_callback(np.full(100, -0.5, dtype=np.float32), 100, None, None)
        recorder._audio_callback(np.full(100, 0.1, dtype=np.float32), 100, None, None)
        assert recorder.get_peak_amplitude() == pytest.approx(0.5)

    def test_silent_capture_stays_below_threshold(self):
        import numpy as np
        from wayfinder.core.recorder import AudioRecorder, SILENCE_PEAK_THRESHOLD

        with patch("wayfinder.core.recorder.sd"):
            recorder = AudioRecorder()

        recorder._audio_callback(np.zeros(1600, dtype=np.float32), 1600, None, None)
        assert recorder.get_peak_amplitude() < SILENCE_PEAK_THRESHOLD

    def test_chunked_recorder_tracks_peak(self):
        import numpy as np
        from wayfinder.core.recorder import ChunkedRecorder

        with patch("wayfinder.core.recorder.sd"):
            recorder = ChunkedRecorder()

        recorder._audio_callback(np.full(100, 0.3, dtype=np.float32), 100, None, None)
        assert recorder.get_peak_amplitude() == pytest.approx(0.3)


# Real-world snapshot (Bazzite desktop, PipeWire): PortAudio shows raw ALSA dupes,
# virtual PCMs, and sink monitors exposed as JACK capture nodes — while pactl lists
# exactly the real mics with the names KDE shows.
PIPEWIRE_DESKTOP_DEVICES = [
    {"name": "Shure MV7: USB Audio (hw:3,0)", "max_input_channels": 1, "hostapi": 0},
    {"name": "USB Audio: - (hw:4,0)", "max_input_channels": 2, "hostapi": 0},
    {"name": "USB Audio: #1 (hw:4,1)", "max_input_channels": 2, "hostapi": 0},
    {"name": "USB Audio: #2 (hw:4,2)", "max_input_channels": 2, "hostapi": 0},
    {"name": "pulse", "max_input_channels": 32, "hostapi": 0},
    {"name": "default", "max_input_channels": 32, "hostapi": 0},
    {"name": "Shure MV7 Mono", "max_input_channels": 1, "hostapi": 1},
    {"name": "OBSBOT Tiny 2 Lite Analog Stereo", "max_input_channels": 2, "hostapi": 1},
    {"name": "USB Audio Microphone", "max_input_channels": 2, "hostapi": 1},
    {"name": "Shure MV7 Analog Stereo", "max_input_channels": 2, "hostapi": 1},
    {"name": "USB Audio Speakers", "max_input_channels": 2, "hostapi": 1},
]
PIPEWIRE_DESKTOP_HOSTAPIS = [
    {"name": "ALSA"},
    {"name": "JACK Audio Connection Kit"},
]

PACTL_LIST_SOURCES = """Source #68
\tName: alsa_input.usb-Remo_Tech_OBSBOT_Tiny_2_Lite-02.analog-stereo
\tDescription: OBSBOT Tiny 2 Lite Analog Stereo
\t\talsa.card = "5"
\t\talsa.device = "0"
Source #70
\tName: alsa_output.usb-Generic_USB_Audio-00.HiFi__SPDIF__sink.monitor
\tDescription: Monitor of USB Audio S/PDIF Output
\t\talsa.card = "4"
\t\talsa.device = "2"
Source #74
\tName: alsa_input.usb-Generic_USB_Audio-00.HiFi__Mic__source
\tDescription: USB Audio Microphone
\t\talsa.card = "4"
\t\talsa.device = "2"
Source #75
\tName: alsa_input.usb-Shure_Inc_Shure_MV7-00.mono-fallback
\tDescription: Shure MV7 Mono
\t\talsa.card = "3"
\t\talsa.device = "0"
"""


class TestPactlCuration:
    """pactl-curated mic picker: show exactly the OS's real capture sources."""

    def _mock_pactl(self, list_output, default_source=""):
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            if "get-default-source" in cmd:
                result.returncode = 0
                result.stdout = default_source
            else:
                result.returncode = 0
                result.stdout = list_output
            return result
        return fake_run

    def test_parse_skips_monitors_and_flags_default(self):
        from wayfinder.core.recorder import _pactl_input_sources

        with patch("wayfinder.core.recorder.subprocess.run",
                   side_effect=self._mock_pactl(
                       PACTL_LIST_SOURCES,
                       "alsa_input.usb-Shure_Inc_Shure_MV7-00.mono-fallback")):
            sources = _pactl_input_sources()

        descs = [s["description"] for s in sources]
        assert descs == ["OBSBOT Tiny 2 Lite Analog Stereo", "USB Audio Microphone",
                         "Shure MV7 Mono"]
        assert [s["is_default"] for s in sources] == [False, False, True]
        shure = sources[-1]
        assert shure["alsa_card"] == "3" and shure["alsa_device"] == "0"

    def test_parse_returns_empty_when_pactl_missing(self):
        from wayfinder.core.recorder import _pactl_input_sources

        with patch("wayfinder.core.recorder.subprocess.run",
                   side_effect=FileNotFoundError):
            assert _pactl_input_sources() == []

    def test_match_prefers_exact_jack_name(self):
        from wayfinder.core.recorder import _match_source_to_device

        src = {"description": "Shure MV7 Mono", "alsa_card": "3", "alsa_device": "0"}
        idx = _match_source_to_device(src, PIPEWIRE_DESKTOP_DEVICES,
                                      PIPEWIRE_DESKTOP_HOSTAPIS)
        assert PIPEWIRE_DESKTOP_DEVICES[idx]["name"] == "Shure MV7 Mono"
        assert PIPEWIRE_DESKTOP_DEVICES[idx]["hostapi"] == 1

    def test_match_falls_back_to_alsa_hw_tag(self):
        """Pulse-only system: no JACK names, source maps via alsa.card/device."""
        from wayfinder.core.recorder import _match_source_to_device

        alsa_only = [d for d in PIPEWIRE_DESKTOP_DEVICES if d["hostapi"] == 0]
        src = {"description": "Shure MV7 Mono", "alsa_card": "3", "alsa_device": "0"}
        idx = _match_source_to_device(src, alsa_only, [{"name": "ALSA"}])
        assert alsa_only[idx]["name"] == "Shure MV7: USB Audio (hw:3,0)"

    def test_match_returns_none_when_unmappable(self):
        from wayfinder.core.recorder import _match_source_to_device

        src = {"description": "Bluetooth Headset", "alsa_card": "", "alsa_device": ""}
        only_pulse = [{"name": "pulse", "max_input_channels": 32, "hostapi": 0}]
        assert _match_source_to_device(src, only_pulse, [{"name": "ALSA"}]) is None

    @patch("wayfinder.core.recorder._is_steam_deck", return_value=False)
    @patch("wayfinder.core.recorder.sd")
    def test_curated_list_replaces_portaudio_clutter(self, mock_sd, _deck):
        from wayfinder.core.recorder import list_input_devices

        mock_sd.query_devices.return_value = PIPEWIRE_DESKTOP_DEVICES
        mock_sd.query_hostapis.return_value = PIPEWIRE_DESKTOP_HOSTAPIS
        with patch("wayfinder.core.recorder.subprocess.run",
                   side_effect=self._mock_pactl(
                       PACTL_LIST_SOURCES,
                       "alsa_input.usb-Shure_Inc_Shure_MV7-00.mono-fallback")):
            devices = list_input_devices(exclude_outputs=True)

        names = [d["name"] for d in devices]
        # Exactly the real sources, by friendly name — no monitors, no hw: dupes,
        # no sink-monitor JACK nodes ("Shure MV7 Analog Stereo", "USB Audio Speakers").
        assert names == ["OBSBOT Tiny 2 Lite Analog Stereo", "USB Audio Microphone",
                         "Shure MV7 Mono"]
        assert all(d["index"] is not None for d in devices)
        # The OS default mic is recommended.
        shure = devices[-1]
        assert shure["recommended"] is True

    @patch("wayfinder.core.recorder._is_steam_deck", return_value=False)
    @patch("wayfinder.core.recorder.sd")
    def test_legacy_list_when_pactl_unavailable(self, mock_sd, _deck):
        from wayfinder.core.recorder import list_input_devices

        mock_sd.query_devices.return_value = [
            {"name": "USB Microphone", "max_input_channels": 2, "hostapi": 0},
        ]
        mock_sd.query_hostapis.return_value = [{"name": "ALSA"}]
        with patch("wayfinder.core.recorder.subprocess.run",
                   side_effect=FileNotFoundError):
            devices = list_input_devices(exclude_outputs=True)

        assert [d["name"] for d in devices] == ["USB Microphone"]

    @patch("wayfinder.core.recorder._is_steam_deck", return_value=False)
    @patch("wayfinder.core.recorder.sd")
    def test_legacy_list_when_nothing_maps(self, mock_sd, _deck):
        """Sandbox case: pactl sees host sources but PortAudio only has the pulse PCM."""
        from wayfinder.core.recorder import list_input_devices

        mock_sd.query_devices.return_value = [
            {"name": "pulse", "max_input_channels": 32, "hostapi": 0},
        ]
        mock_sd.query_hostapis.return_value = [{"name": "ALSA"}]
        with patch("wayfinder.core.recorder.subprocess.run",
                   side_effect=self._mock_pactl(PACTL_LIST_SOURCES)), \
             patch("wayfinder.core.recorder._system_default_input_index",
                   return_value=0):
            devices = list_input_devices(exclude_outputs=True)

        # Falls through to the single System Default entry, not an empty picker.
        assert len(devices) == 1
        assert devices[0]["index"] == 0

    def test_get_device_by_name_resolves_pactl_description(self):
        from wayfinder.core.recorder import get_input_device_by_name

        alsa_only = [d for d in PIPEWIRE_DESKTOP_DEVICES if d["hostapi"] == 0]
        with patch("wayfinder.core.recorder.sd") as mock_sd, \
             patch("wayfinder.core.recorder.subprocess.run",
                   side_effect=self._mock_pactl(PACTL_LIST_SOURCES)):
            mock_sd.query_devices.return_value = alsa_only
            mock_sd.query_hostapis.return_value = [{"name": "ALSA"}]
            # "USB Audio Microphone" is no substring of any ALSA name — only the
            # pactl alsa.card/device mapping can resolve it (to hw:4,2).
            idx = get_input_device_by_name("USB Audio Microphone")

        assert alsa_only[idx]["name"] == "USB Audio: #2 (hw:4,2)"

    @patch("wayfinder.core.recorder.sys")
    def test_get_device_by_name_avoids_jack_clone(self, mock_sys):
        """A mic exposed only as a pipewire-jack clone must NOT be captured via JACK
        (its tiny ~3ms buffer xruns on every ducking). Route to the system-default
        Pulse PCM instead. Regression for the 'input overflow' + stuck-Processing bug."""
        from wayfinder.core.recorder import get_input_device_by_name
        mock_sys.platform = "linux"
        devices = [
            {"name": "pulse", "max_input_channels": 32, "hostapi": 0},          # 0 ALSA
            {"name": "Shure MV7 Mono", "max_input_channels": 1, "hostapi": 1},  # 1 JACK
        ]
        with patch("wayfinder.core.recorder.sd") as mock_sd, \
             patch("wayfinder.core.recorder._system_default_input_index", return_value=0):
            mock_sd.query_devices.return_value = devices
            mock_sd.query_hostapis.return_value = [
                {"name": "ALSA"}, {"name": "JACK Audio Connection Kit"},
            ]
            idx = get_input_device_by_name("Shure MV7 Mono")
        assert idx == 0  # the pulse PCM, NOT the JACK clone at index 1

    @patch("wayfinder.core.recorder.sys")
    def test_get_device_by_name_prefers_nonjack_match(self, mock_sys):
        """When both a non-JACK and a JACK match exist, take the non-JACK one directly."""
        from wayfinder.core.recorder import get_input_device_by_name
        mock_sys.platform = "linux"
        devices = [
            {"name": "Shure MV7 Mono", "max_input_channels": 1, "hostapi": 1},  # 0 JACK
            {"name": "Shure MV7 Mono", "max_input_channels": 1, "hostapi": 0},  # 1 ALSA
        ]
        with patch("wayfinder.core.recorder.sd") as mock_sd:
            mock_sd.query_devices.return_value = devices
            mock_sd.query_hostapis.return_value = [
                {"name": "ALSA"}, {"name": "JACK Audio Connection Kit"},
            ]
            idx = get_input_device_by_name("Shure MV7 Mono")
        assert idx == 1  # the ALSA match, skipping the JACK clone at index 0

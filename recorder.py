"""
Audio recording module for Wayfinder Voice.
Captures 16kHz mono audio using sounddevice with callback-based streaming.
"""

import tempfile
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd


class AudioRecorder:
    """Records audio to a temporary WAV file for transcription."""

    def __init__(self, sample_rate: int = 16000, channels: int = 1, device: int | None = None):
        """
        Initialize the audio recorder.

        Args:
            sample_rate: Audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self._temp_file: tempfile.NamedTemporaryFile | None = None

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback function for sounddevice stream."""
        if status:
            print(f"Audio status: {status}")
        # Make a copy since indata is reused
        self.frames.append(indata.copy())

    def start(self) -> None:
        """Start recording audio."""
        self.frames = []
        self.stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            device=self.device,
            dtype=np.float32,
            callback=self._audio_callback,
        )
        self.stream.start()

    def stop(self) -> str:
        """
        Stop recording and save to a temporary WAV file.

        Returns:
            Path to the temporary WAV file containing the recording.
        """
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

        if not self.frames:
            raise ValueError("No audio recorded")

        # Concatenate all frames
        audio_data = np.concatenate(self.frames, axis=0)

        # Convert float32 [-1.0, 1.0] to int16
        audio_int16 = (audio_data * 32767).astype(np.int16)

        # Create temporary WAV file
        self._temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_path = self._temp_file.name

        # Write WAV file
        with wave.open(temp_path, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)  # 16-bit = 2 bytes
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(audio_int16.tobytes())

        return temp_path

    def get_duration(self) -> float:
        """
        Get the duration of recorded audio in seconds.

        Returns:
            Duration in seconds, or 0 if no frames recorded.
        """
        if not self.frames:
            return 0.0
        total_frames = sum(f.shape[0] for f in self.frames)
        return total_frames / self.sample_rate

    def cleanup(self) -> None:
        """Clean up temporary files."""
        if self._temp_file is not None:
            try:
                Path(self._temp_file.name).unlink(missing_ok=True)
            except Exception:
                pass
            self._temp_file = None

    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self.stream is not None and self.stream.active




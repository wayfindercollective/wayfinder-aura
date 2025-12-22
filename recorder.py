"""
Audio recording module for Wayfinder Voice.
Captures 16kHz mono audio using sounddevice with callback-based streaming.
Includes audio preprocessing for improved transcription accuracy.
Supports chunked recording for indefinite duration sessions.
"""

import queue
import tempfile
import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

# Try to import scipy for audio filtering
try:
    from scipy.signal import butter, filtfilt
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


def preprocess_audio(audio_data: np.ndarray, sample_rate: int, enable: bool = True) -> np.ndarray:
    """
    Preprocess audio for better transcription accuracy.
    
    Applies:
    - Gain normalization to -3dB peak
    - High-pass filter at 80Hz to remove low-frequency rumble
    - Simple noise gate to reduce background noise
    
    Args:
        audio_data: Audio samples as float32 array
        sample_rate: Sample rate in Hz
        enable: Whether to apply preprocessing
        
    Returns:
        Preprocessed audio data
    """
    if not enable:
        return audio_data
    
    # Work with a copy
    audio = audio_data.copy()
    
    # Flatten if needed (mono)
    if audio.ndim > 1:
        audio = audio.flatten()
    
    # 1. High-pass filter at 80Hz (remove rumble/noise)
    if SCIPY_AVAILABLE:
        try:
            # Butterworth high-pass filter
            nyquist = sample_rate / 2
            cutoff = 80 / nyquist
            if cutoff < 1:  # Ensure valid cutoff
                b, a = butter(2, cutoff, btype='high')
                audio = filtfilt(b, a, audio).astype(np.float32)
        except Exception:
            pass  # Skip filtering on error
    
    # 2. Noise gate - reduce very quiet sections (soft gate to preserve speech edges)
    # -50dB threshold is gentler than -40dB to avoid cutting off soft consonants
    noise_floor = 0.003  # -50dB (was 0.01 / -40dB)
    gate_reduction = 0.3  # Reduce to 30% instead of 10% (gentler)
    audio = np.where(np.abs(audio) < noise_floor, audio * gate_reduction, audio)
    
    # 3. Gain normalization to -3dB peak (0.707)
    peak = np.max(np.abs(audio))
    if peak > 0.001:  # Avoid division by near-zero
        target_peak = 0.707  # -3dB
        audio = audio * (target_peak / peak)
    
    # Ensure output shape matches input
    if audio_data.ndim > 1:
        audio = audio.reshape(audio_data.shape)
    
    return audio.astype(np.float32)


class AudioRecorder:
    """Records audio to a temporary WAV file for transcription."""

    def __init__(
        self, 
        sample_rate: int = 16000, 
        channels: int = 1, 
        device: int | None = None,
        preprocessing: bool = True,
    ):
        """
        Initialize the audio recorder.

        Args:
            sample_rate: Audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
            preprocessing: Enable audio preprocessing (normalization, filtering)
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self.preprocessing = preprocessing
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

    def stop(self, apply_preprocessing: bool | None = None) -> str:
        """
        Stop recording and save to a temporary WAV file.

        Args:
            apply_preprocessing: Override instance preprocessing setting
            
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
        
        # Apply preprocessing if enabled
        do_preprocess = apply_preprocessing if apply_preprocessing is not None else self.preprocessing
        if do_preprocess:
            audio_data = preprocess_audio(audio_data, self.sample_rate)

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

    def get_audio_level(self) -> float:
        """
        Get current audio level (RMS) for visual feedback.
        
        Returns:
            RMS level between 0.0 and 1.0
        """
        if not self.frames:
            return 0.0
        # Use last few frames for responsiveness
        recent_frames = self.frames[-5:] if len(self.frames) >= 5 else self.frames
        audio = np.concatenate(recent_frames, axis=0)
        rms = np.sqrt(np.mean(audio ** 2))
        return min(1.0, rms * 3)  # Scale up for visibility

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


class ChunkedRecorder:
    """
    Records audio in chunks for indefinite duration sessions.
    
    Features:
    - Automatic chunk extraction at configurable intervals
    - Overlap between chunks to avoid cutting words
    - Background processing via callback
    - No maximum recording duration
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        device: int | None = None,
        preprocessing: bool = True,
        chunk_duration: float = 30.0,
        chunk_overlap: float = 2.0,
        on_chunk_ready: Callable[[str, int], None] | None = None,
    ):
        """
        Initialize the chunked audio recorder.

        Args:
            sample_rate: Audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
            preprocessing: Enable audio preprocessing
            chunk_duration: Duration of each chunk in seconds
            chunk_overlap: Overlap between chunks in seconds
            on_chunk_ready: Callback when a chunk is ready (path, chunk_index)
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self.preprocessing = preprocessing
        self.chunk_duration = chunk_duration
        self.chunk_overlap = chunk_overlap
        self.on_chunk_ready = on_chunk_ready
        
        # Audio buffer (ring buffer style)
        self._buffer: list[np.ndarray] = []
        self._buffer_lock = threading.Lock()
        
        # Chunk management
        self._chunk_queue: queue.Queue = queue.Queue()
        self._chunk_index = 0
        self._last_chunk_end = 0  # Sample position of last chunk end
        
        # Recording state
        self._stream: sd.InputStream | None = None
        self._stop_event = threading.Event()
        self._chunk_thread: threading.Thread | None = None
        self._temp_files: list[str] = []
        
        # Calculate samples
        self._chunk_samples = int(chunk_duration * sample_rate)
        self._overlap_samples = int(chunk_overlap * sample_rate)

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback function for sounddevice stream."""
        if status:
            print(f"Audio status: {status}")
        with self._buffer_lock:
            self._buffer.append(indata.copy())

    def _get_total_samples(self) -> int:
        """Get total number of samples in buffer."""
        with self._buffer_lock:
            return sum(f.shape[0] for f in self._buffer)

    def _extract_chunk(self, start_sample: int, end_sample: int) -> np.ndarray | None:
        """Extract a chunk of audio from the buffer."""
        with self._buffer_lock:
            if not self._buffer:
                return None
            
            # Concatenate buffer
            all_audio = np.concatenate(self._buffer, axis=0)
            
            if end_sample > len(all_audio):
                return None
            
            return all_audio[start_sample:end_sample].copy()

    def _chunk_monitor_thread(self) -> None:
        """Background thread that monitors for ready chunks."""
        while not self._stop_event.is_set():
            total_samples = self._get_total_samples()
            
            # Calculate if we have enough for a new chunk
            next_chunk_end = self._last_chunk_end + self._chunk_samples
            
            if total_samples >= next_chunk_end:
                # We have enough audio for a chunk
                # Start with overlap from previous chunk (except first)
                if self._chunk_index == 0:
                    start = 0
                else:
                    start = self._last_chunk_end - self._overlap_samples
                    start = max(0, start)
                
                end = next_chunk_end
                
                # Extract and save chunk
                chunk_audio = self._extract_chunk(start, end)
                if chunk_audio is not None:
                    chunk_path = self._save_chunk(chunk_audio)
                    if chunk_path:
                        self._chunk_queue.put((chunk_path, self._chunk_index))
                        
                        # Call callback if provided
                        if self.on_chunk_ready:
                            try:
                                self.on_chunk_ready(chunk_path, self._chunk_index)
                            except Exception as e:
                                print(f"Chunk callback error: {e}")
                        
                        self._chunk_index += 1
                        self._last_chunk_end = end
            
            # Small sleep to avoid busy waiting
            self._stop_event.wait(0.5)

    def _save_chunk(self, audio_data: np.ndarray) -> str | None:
        """Save a chunk to a temporary WAV file."""
        try:
            # Apply preprocessing if enabled
            if self.preprocessing:
                audio_data = preprocess_audio(audio_data, self.sample_rate)
            
            # Convert float32 to int16
            audio_int16 = (audio_data * 32767).astype(np.int16)
            
            # Create temp file
            temp_file = tempfile.NamedTemporaryFile(
                suffix=f"_chunk{self._chunk_index}.wav",
                delete=False
            )
            temp_path = temp_file.name
            self._temp_files.append(temp_path)
            
            # Write WAV
            with wave.open(temp_path, "wb") as wav_file:
                wav_file.setnchannels(self.channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(audio_int16.tobytes())
            
            return temp_path
        except Exception as e:
            print(f"Error saving chunk: {e}")
            return None

    def start(self) -> None:
        """Start chunked recording."""
        self._buffer = []
        self._chunk_index = 0
        self._last_chunk_end = 0
        self._temp_files = []
        self._stop_event.clear()
        
        # Clear queue
        while not self._chunk_queue.empty():
            try:
                self._chunk_queue.get_nowait()
            except queue.Empty:
                break
        
        # Start audio stream
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            device=self.device,
            dtype=np.float32,
            callback=self._audio_callback,
        )
        self._stream.start()
        
        # Start chunk monitoring thread
        self._chunk_thread = threading.Thread(target=self._chunk_monitor_thread, daemon=True)
        self._chunk_thread.start()

    def stop(self) -> tuple[str | None, list[str]]:
        """
        Stop recording and return final chunk and all chunk paths.
        
        Returns:
            Tuple of (final_chunk_path, list_of_all_chunk_paths)
        """
        self._stop_event.set()
        
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        
        if self._chunk_thread is not None:
            self._chunk_thread.join(timeout=2.0)
            self._chunk_thread = None
        
        # Save any remaining audio as final chunk
        final_path = None
        with self._buffer_lock:
            if self._buffer:
                all_audio = np.concatenate(self._buffer, axis=0)
                
                # Get remaining audio after last chunk
                if self._chunk_index == 0:
                    # No chunks extracted yet, save everything
                    remaining = all_audio
                else:
                    # Save from overlap point before last chunk end
                    start = max(0, self._last_chunk_end - self._overlap_samples)
                    remaining = all_audio[start:]
                
                if len(remaining) > self.sample_rate * 0.5:  # At least 0.5 seconds
                    final_path = self._save_chunk(remaining)
        
        return final_path, self._temp_files.copy()

    def get_duration(self) -> float:
        """Get total duration of recorded audio in seconds."""
        return self._get_total_samples() / self.sample_rate

    def get_chunk_count(self) -> int:
        """Get number of chunks extracted so far."""
        return self._chunk_index

    def get_pending_chunks(self) -> list[tuple[str, int]]:
        """Get all pending chunks from the queue."""
        chunks = []
        while not self._chunk_queue.empty():
            try:
                chunks.append(self._chunk_queue.get_nowait())
            except queue.Empty:
                break
        return chunks

    def get_audio_level(self) -> float:
        """Get current audio level (RMS) for visual feedback."""
        with self._buffer_lock:
            if not self._buffer:
                return 0.0
            # Use last few frames for responsiveness
            recent_frames = self._buffer[-5:] if len(self._buffer) >= 5 else self._buffer
            audio = np.concatenate(recent_frames, axis=0)
            rms = np.sqrt(np.mean(audio ** 2))
            return min(1.0, rms * 3)

    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._stream is not None and self._stream.active

    def cleanup(self) -> None:
        """Clean up all temporary files."""
        for path in self._temp_files:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        self._temp_files = []

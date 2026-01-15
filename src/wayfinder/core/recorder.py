"""
Audio recording module for Wayfinder Aura.
Captures audio using sounddevice with callback-based streaming.
Automatically resamples to 16kHz for Whisper compatibility.
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

# Try to import scipy for audio filtering and resampling
try:
    from scipy.signal import butter, filtfilt, resample_poly
    from math import gcd
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


# Target sample rate for Whisper
WHISPER_SAMPLE_RATE = 16000

# Known microphone brands/keywords to prioritize
PREFERRED_MIC_KEYWORDS = [
    "shure", "mv7", "sm7", "blue", "yeti", "snowball", "rode", "nt-usb", "ntusb",
    "audio-technica", "at2020", "at2035", "hyperx", "quadcast", "elgato", "wave",
    "focusrite", "scarlett", "behringer", "samson", "fifine", "maono", "tonor",
    "microphone", "mic input", "headset",
]

# Keywords indicating the device is NOT a microphone input
EXCLUDED_DEVICE_KEYWORDS = [
    # Output devices
    "monitor", "hdmi", "loopback", "output", "speaker", "playback", 
    "digital stereo (hdmi", "s/pdif", "headphone", "front headphone",
    # Virtual/system devices (when they appear as standalone device names)
    "pipewire", "pulse", "default",
    # ALSA output nodes
    "alsa_output",
]


def find_best_input_device(preferred_name: str | None = None) -> int | None:
    """
    Intelligently find the best audio input device for voice recording.
    
    Strategy:
    1. If preferred_name is given, try to find a device matching that name
    2. Prefer PipeWire/PulseAudio/JACK virtual devices over raw ALSA
    3. Prioritize devices with microphone-related keywords
    4. Filter out monitor, HDMI, and loopback devices
    5. Ensure the device has input channels
    
    Args:
        preferred_name: Optional device name to look for (partial match)
        
    Returns:
        Device index, or None to use system default
    """
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    
    candidates = []
    
    for i, dev in enumerate(devices):
        name = dev.get('name', '').lower()
        max_inputs = dev.get('max_input_channels', 0)
        
        # Skip devices with no input channels
        if max_inputs < 1:
            continue
        
        # Skip excluded device types
        if any(excl in name for excl in EXCLUDED_DEVICE_KEYWORDS):
            continue
        
        # Calculate a priority score
        score = 0
        
        # Preferred name match (highest priority)
        if preferred_name and preferred_name.lower() in name:
            score += 1000
        
        # Prefer JACK/PipeWire virtual devices (they handle routing properly)
        hostapi = dev.get('hostapi', -1)
        try:
            hostapi_info = sd.query_hostapis(hostapi)
            hostapi_name = hostapi_info.get('name', '').lower()
            if 'jack' in hostapi_name:
                score += 100  # JACK devices are well-routed
            elif 'pulse' in hostapi_name or 'pipewire' in hostapi_name:
                score += 80
        except Exception:
            pass
        
        # Prefer known microphone brands/keywords
        for keyword in PREFERRED_MIC_KEYWORDS:
            if keyword in name:
                score += 50
                break
        
        # Prefer "mono" for voice (cleaner for speech recognition)
        if 'mono' in name:
            score += 20
        
        # Slight preference for devices with fewer channels (more likely dedicated mics)
        if max_inputs == 1:
            score += 10
        elif max_inputs == 2:
            score += 5
        
        # Penalize raw ALSA hw: devices (IDs can shift)
        if 'hw:' in name:
            score -= 30
        
        candidates.append((score, i, dev['name']))
    
    if not candidates:
        return None
    
    # Sort by score (highest first)
    candidates.sort(key=lambda x: x[0], reverse=True)
    
    # Return the best candidate
    best_score, best_idx, best_name = candidates[0]
    
    # Only use auto-detected device if it has a reasonable score
    if best_score > 0:
        print(f"Auto-selected audio device: {best_name} (score: {best_score})")
        return best_idx
    
    # Fall back to system default if no good candidates
    return None


def get_input_device_by_name(name: str) -> int | None:
    """
    Find a device by name (partial match, case-insensitive).
    
    Args:
        name: Device name to search for
        
    Returns:
        Device index or None if not found
    """
    if not name:
        return None
    
    try:
        devices = sd.query_devices()
        name_lower = name.lower()
        
        for i, dev in enumerate(devices):
            if name_lower in dev.get('name', '').lower():
                if dev.get('max_input_channels', 0) > 0:
                    return i
    except Exception:
        pass
    
    return None


def list_input_devices() -> list[dict]:
    """
    List all available input devices with relevant info.
    
    Returns:
        List of dicts with device info (index, name, channels, recommended)
    """
    result = []
    
    try:
        devices = sd.query_devices()
        
        for i, dev in enumerate(devices):
            max_inputs = dev.get('max_input_channels', 0)
            if max_inputs < 1:
                continue
            
            name = dev.get('name', f'Device {i}')
            name_lower = name.lower()
            
            # Skip excluded types
            is_excluded = any(excl in name_lower for excl in EXCLUDED_DEVICE_KEYWORDS)
            
            # Check if it's a recommended device
            is_recommended = (
                not is_excluded and
                any(kw in name_lower for kw in PREFERRED_MIC_KEYWORDS)
            )
            
            result.append({
                'index': i,
                'name': name,
                'channels': max_inputs,
                'recommended': is_recommended,
                'excluded': is_excluded,
            })
    except Exception:
        pass
    
    return result


def get_supported_sample_rate(device: int | None = None, target_rate: int = 16000) -> int:
    """
    Find a supported sample rate for the given device.
    
    Args:
        device: Audio device index (None for default)
        target_rate: Preferred sample rate
        
    Returns:
        Supported sample rate (may be different from target)
    """
    # Common sample rates to try, in order of preference
    sample_rates_to_try = [target_rate, 44100, 48000, 22050, 32000, 96000]
    
    # Get device info
    try:
        if device is not None:
            device_info = sd.query_devices(device)
        else:
            device_info = sd.query_devices(kind='input')
        
        # Try the device's default rate first
        default_rate = int(device_info.get('default_samplerate', 44100))
        if default_rate not in sample_rates_to_try:
            sample_rates_to_try.insert(0, default_rate)
    except Exception:
        pass
    
    # Test each sample rate
    for rate in sample_rates_to_try:
        try:
            # Test if this rate works by checking device compatibility
            sd.check_input_settings(device=device, samplerate=rate, channels=1)
            return rate
        except sd.PortAudioError:
            continue
    
    # Fallback to 44100 which is most universally supported
    return 44100


def resample_audio(audio: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
    """
    Resample audio to target sample rate.
    
    Args:
        audio: Audio data as numpy array
        orig_rate: Original sample rate
        target_rate: Target sample rate
        
    Returns:
        Resampled audio
    """
    if orig_rate == target_rate:
        return audio
    
    if SCIPY_AVAILABLE:
        # Use scipy's polyphase resampling for high quality
        g = gcd(orig_rate, target_rate)
        up = target_rate // g
        down = orig_rate // g
        return resample_poly(audio, up, down).astype(np.float32)
    else:
        # Simple linear interpolation fallback
        duration = len(audio) / orig_rate
        target_length = int(duration * target_rate)
        indices = np.linspace(0, len(audio) - 1, target_length)
        return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def preprocess_audio(
    audio_data: np.ndarray, 
    sample_rate: int, 
    level: str = "light"
) -> np.ndarray:
    """
    Preprocess audio for transcription.
    
    Levels:
    - "off": No processing at all (raw audio)
    - "light": Only gain normalization (recommended for most cases)
    - "medium": Gain normalization + gentle high-pass filter
    - "heavy": Full processing including noise gate (may cut off words)
    
    Args:
        audio_data: Audio samples as float32 array
        sample_rate: Sample rate in Hz
        level: Processing level - "off", "light", "medium", "heavy"
        
    Returns:
        Preprocessed audio data
    """
    if level == "off" or not level:
        return audio_data
    
    # Work with a copy
    audio = audio_data.copy()
    
    # Flatten if needed (mono)
    if audio.ndim > 1:
        audio = audio.flatten()
    
    # Gain normalization - applied for all levels except "off"
    # Normalize to -3dB peak (0.707) to ensure good signal level
    peak = np.max(np.abs(audio))
    if peak > 0.001:  # Avoid division by near-zero
        target_peak = 0.707  # -3dB
        audio = audio * (target_peak / peak)
    
    # Medium and Heavy: High-pass filter at 80Hz (remove rumble/noise)
    if level in ("medium", "heavy") and SCIPY_AVAILABLE:
        try:
            nyquist = sample_rate / 2
            cutoff = 80 / nyquist
            if cutoff < 1:
                b, a = butter(2, cutoff, btype='high')
                audio = filtfilt(b, a, audio).astype(np.float32)
        except Exception:
            pass
    
    # Heavy only: Noise gate (can cut off soft consonants - use with caution!)
    if level == "heavy":
        noise_floor = 0.002  # -54dB - very gentle threshold
        gate_reduction = 0.5  # Only reduce to 50%, don't kill the signal
        audio = np.where(np.abs(audio) < noise_floor, audio * gate_reduction, audio)
    
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
        preprocessing: bool | str = "light",
    ):
        """
        Initialize the audio recorder.

        Args:
            sample_rate: Target audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
            preprocessing: Audio preprocessing level:
                - False or "off": No processing
                - True or "light": Only gain normalization (recommended)
                - "medium": Normalization + high-pass filter
                - "heavy": Full processing with noise gate
        """
        self.target_sample_rate = sample_rate  # What we want (for Whisper)
        self.channels = channels
        self.device = device
        self.recording_sample_rate = None  # Will be set when starting
        # Convert bool to level string for backwards compatibility
        if preprocessing is True:
            self.preprocessing = "light"
        elif preprocessing is False:
            self.preprocessing = "off"
        else:
            self.preprocessing = preprocessing
        self.frames: list[np.ndarray] = []
        self.stream: sd.InputStream | None = None
        self._temp_file: tempfile.NamedTemporaryFile | None = None

    @property
    def sample_rate(self) -> int:
        """Return the current/target sample rate."""
        return self.recording_sample_rate or self.target_sample_rate

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback function for sounddevice stream."""
        if status:
            print(f"Audio status: {status}")
        # Make a copy since indata is reused
        self.frames.append(indata.copy())

    def start(self) -> None:
        """Start recording audio."""
        self.frames = []
        
        # Find a supported sample rate for this device
        self.recording_sample_rate = get_supported_sample_rate(
            device=self.device, 
            target_rate=self.target_sample_rate
        )
        
        if self.recording_sample_rate != self.target_sample_rate:
            print(f"Note: Recording at {self.recording_sample_rate}Hz, will resample to {self.target_sample_rate}Hz")
        
        self.stream = sd.InputStream(
            samplerate=self.recording_sample_rate,
            channels=self.channels,
            device=self.device,
            dtype=np.float32,
            callback=self._audio_callback,
        )
        self.stream.start()

    def stop(self, apply_preprocessing: bool | str | None = None) -> str:
        """
        Stop recording and save to a temporary WAV file.

        Args:
            apply_preprocessing: Override instance preprocessing setting
                Can be bool (True="light", False="off") or level string
            
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
        
        # Flatten if needed (mono)
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()
        
        # Resample to target rate if needed (16kHz for Whisper)
        if self.recording_sample_rate and self.recording_sample_rate != self.target_sample_rate:
            audio_data = resample_audio(
                audio_data, 
                self.recording_sample_rate, 
                self.target_sample_rate
            )
        
        # Determine preprocessing level
        level = apply_preprocessing if apply_preprocessing is not None else self.preprocessing
        if level is True:
            level = "light"
        elif level is False:
            level = "off"
        
        # Apply preprocessing (using target sample rate)
        if level and level != "off":
            audio_data = preprocess_audio(audio_data, self.target_sample_rate, level=level)

        # Convert float32 [-1.0, 1.0] to int16
        audio_int16 = (audio_data * 32767).astype(np.int16)

        # Create temporary WAV file
        self._temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_path = self._temp_file.name

        # Write WAV file at target sample rate (16kHz for Whisper)
        with wave.open(temp_path, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(2)  # 16-bit = 2 bytes
            wav_file.setframerate(self.target_sample_rate)
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
    - Automatic resampling to 16kHz for Whisper compatibility
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        device: int | None = None,
        preprocessing: bool | str = "light",
        chunk_duration: float = 30.0,
        chunk_overlap: float = 2.0,
        on_chunk_ready: Callable[[str, int], None] | None = None,
    ):
        """
        Initialize the chunked audio recorder.

        Args:
            sample_rate: Target audio sample rate in Hz (default 16000 for whisper)
            channels: Number of audio channels (default 1 for mono)
            device: Audio input device index (None for default)
            preprocessing: Audio preprocessing level:
                - False or "off": No processing
                - True or "light": Only gain normalization (recommended)
                - "medium": Normalization + high-pass filter
                - "heavy": Full processing with noise gate
            chunk_duration: Duration of each chunk in seconds
            chunk_overlap: Overlap between chunks in seconds
            on_chunk_ready: Callback when a chunk is ready (path, chunk_index)
        """
        self.target_sample_rate = sample_rate  # What we want (for Whisper)
        self.recording_sample_rate = None  # Will be set when starting
        self.channels = channels
        self.device = device
        # Convert bool to level string for backwards compatibility
        if preprocessing is True:
            self.preprocessing = "light"
        elif preprocessing is False:
            self.preprocessing = "off"
        else:
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
        
        # Calculate samples (will be updated based on actual recording rate)
        self._chunk_samples = int(chunk_duration * sample_rate)
        self._overlap_samples = int(chunk_overlap * sample_rate)

    @property
    def sample_rate(self) -> int:
        """Return the current/target sample rate."""
        return self.recording_sample_rate or self.target_sample_rate

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
            # Flatten if needed (mono)
            if audio_data.ndim > 1:
                audio_data = audio_data.flatten()
            
            # Resample to target rate if needed (16kHz for Whisper)
            if self.recording_sample_rate and self.recording_sample_rate != self.target_sample_rate:
                audio_data = resample_audio(
                    audio_data, 
                    self.recording_sample_rate, 
                    self.target_sample_rate
                )
            
            # Apply preprocessing based on level (using target sample rate)
            if self.preprocessing and self.preprocessing != "off":
                audio_data = preprocess_audio(audio_data, self.target_sample_rate, level=self.preprocessing)
            
            # Convert float32 to int16
            audio_int16 = (audio_data * 32767).astype(np.int16)
            
            # Create temp file
            temp_file = tempfile.NamedTemporaryFile(
                suffix=f"_chunk{self._chunk_index}.wav",
                delete=False
            )
            temp_path = temp_file.name
            self._temp_files.append(temp_path)
            
            # Write WAV at target sample rate (16kHz for Whisper)
            with wave.open(temp_path, "wb") as wav_file:
                wav_file.setnchannels(self.channels)
                wav_file.setsampwidth(2)
                wav_file.setframerate(self.target_sample_rate)
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
        
        # Find a supported sample rate for this device
        self.recording_sample_rate = get_supported_sample_rate(
            device=self.device, 
            target_rate=self.target_sample_rate
        )
        
        if self.recording_sample_rate != self.target_sample_rate:
            print(f"Note: Recording at {self.recording_sample_rate}Hz, will resample to {self.target_sample_rate}Hz")
        
        # Recalculate chunk samples based on actual recording rate
        self._chunk_samples = int(self.chunk_duration * self.recording_sample_rate)
        self._overlap_samples = int(self.chunk_overlap * self.recording_sample_rate)
        
        # Start audio stream
        self._stream = sd.InputStream(
            samplerate=self.recording_sample_rate,
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


# =============================================================================
# Audio Calibration
# =============================================================================

class AudioCalibrationResult:
    """Results from audio calibration analysis."""
    
    def __init__(
        self,
        peak_level: float,
        rms_level: float,
        noise_floor: float,
        clipping_detected: bool,
        clipping_percentage: float,
        signal_to_noise: float,
        recommended_preprocessing: str,
        issues: list[str],
        recommendations: list[str],
    ):
        self.peak_level = peak_level
        self.rms_level = rms_level
        self.noise_floor = noise_floor
        self.clipping_detected = clipping_detected
        self.clipping_percentage = clipping_percentage
        self.signal_to_noise = signal_to_noise
        self.recommended_preprocessing = recommended_preprocessing
        self.issues = issues
        self.recommendations = recommendations
    
    @property
    def overall_quality(self) -> str:
        """Get overall audio quality assessment."""
        if self.clipping_detected and self.clipping_percentage > 1.0:
            return "poor"
        elif self.clipping_detected or self.signal_to_noise < 20:
            return "fair"
        elif self.signal_to_noise > 40 and not self.clipping_detected:
            return "excellent"
        else:
            return "good"
    
    @property
    def quality_icon(self) -> str:
        """Get icon for quality level."""
        icons = {
            "excellent": "✅",
            "good": "✅",
            "fair": "⚠️",
            "poor": "❌",
        }
        return icons.get(self.overall_quality, "❓")


def analyze_audio_calibration(
    audio_data: np.ndarray,
    sample_rate: int = 16000,
) -> AudioCalibrationResult:
    """
    Analyze recorded audio to detect issues and recommend settings.
    
    Args:
        audio_data: Audio samples as float32 array (-1.0 to 1.0)
        sample_rate: Sample rate in Hz
        
    Returns:
        AudioCalibrationResult with analysis and recommendations
    """
    # Flatten if multi-channel
    if audio_data.ndim > 1:
        audio_data = audio_data.flatten()
    
    issues = []
    recommendations = []
    
    # === Basic level analysis ===
    peak_level = float(np.max(np.abs(audio_data)))
    rms_level = float(np.sqrt(np.mean(audio_data ** 2)))
    
    # === Clipping detection ===
    # Samples very close to or at max level indicate clipping
    clipping_threshold = 0.99
    clipping_samples = np.sum(np.abs(audio_data) >= clipping_threshold)
    total_samples = len(audio_data)
    clipping_percentage = (clipping_samples / total_samples) * 100
    clipping_detected = clipping_percentage > 0.1  # More than 0.1% clipping
    
    # === Noise floor estimation ===
    # Sort samples by absolute value and take the 10th percentile as noise floor
    sorted_abs = np.sort(np.abs(audio_data))
    noise_floor_idx = int(len(sorted_abs) * 0.1)
    noise_floor = float(sorted_abs[noise_floor_idx]) if noise_floor_idx < len(sorted_abs) else 0.001
    
    # Ensure noise floor isn't zero for SNR calculation
    noise_floor = max(noise_floor, 0.0001)
    
    # === Signal-to-noise ratio (rough estimate) ===
    # Use RMS as signal, noise floor as noise
    if noise_floor > 0:
        signal_to_noise = 20 * np.log10(rms_level / noise_floor) if rms_level > noise_floor else 0
    else:
        signal_to_noise = 60  # Assume good if noise floor is 0
    
    # === Issue detection and recommendations ===
    
    # Clipping issues
    if clipping_percentage > 5.0:
        issues.append(f"Severe clipping detected ({clipping_percentage:.1f}% of samples)")
        recommendations.append("Turn down your microphone gain significantly")
        recommendations.append("Move the microphone further from your mouth")
    elif clipping_percentage > 1.0:
        issues.append(f"Clipping detected ({clipping_percentage:.1f}% of samples)")
        recommendations.append("Turn down your microphone gain slightly")
    elif clipping_detected:
        issues.append(f"Minor clipping detected ({clipping_percentage:.2f}% of samples)")
        recommendations.append("Consider reducing microphone gain")
    
    # Signal level issues
    if peak_level < 0.1:
        issues.append("Signal is very quiet")
        recommendations.append("Turn up your microphone gain")
        recommendations.append("Move closer to the microphone")
    elif peak_level < 0.3:
        issues.append("Signal is somewhat quiet")
        recommendations.append("Consider turning up microphone gain slightly")
    
    # Noise floor issues
    if noise_floor > 0.05:
        issues.append("High background noise detected")
        recommendations.append("Try to reduce background noise in your environment")
        recommendations.append("Use a noise gate or 'Heavy' audio preprocessing")
    elif noise_floor > 0.02:
        issues.append("Moderate background noise")
        recommendations.append("Consider using 'Medium' audio preprocessing")
    
    # === Determine recommended preprocessing ===
    if clipping_detected and clipping_percentage > 1.0:
        # Hot signal - don't normalize, it will make clipping worse
        recommended_preprocessing = "off"
        recommendations.insert(0, "Disable audio preprocessing (normalization makes clipping worse)")
    elif peak_level < 0.2:
        # Quiet signal - normalization will help
        recommended_preprocessing = "light"
        if "Consider turning up microphone gain" not in recommendations:
            recommendations.append("Audio normalization will boost your signal")
    elif noise_floor > 0.03:
        # Noisy environment - use filtering
        recommended_preprocessing = "medium"
    elif noise_floor > 0.05:
        # Very noisy - use noise gate
        recommended_preprocessing = "heavy"
    else:
        # Normal signal - light preprocessing is fine
        recommended_preprocessing = "light"
    
    # Add positive feedback if everything looks good
    if not issues:
        if signal_to_noise > 40:
            issues.append("Excellent audio quality!")
        elif signal_to_noise > 30:
            issues.append("Good audio quality")
        else:
            issues.append("Audio quality is acceptable")
    
    return AudioCalibrationResult(
        peak_level=peak_level,
        rms_level=rms_level,
        noise_floor=noise_floor,
        clipping_detected=clipping_detected,
        clipping_percentage=clipping_percentage,
        signal_to_noise=signal_to_noise,
        recommended_preprocessing=recommended_preprocessing,
        issues=issues,
        recommendations=recommendations,
    )


class AudioCalibrator:
    """
    Records and analyzes audio for calibration purposes.
    
    Usage:
        calibrator = AudioCalibrator(device=device_id)
        calibrator.start()
        # ... wait for user to speak ...
        result = calibrator.stop_and_analyze()
        print(result.recommendations)
    """
    
    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | None = None,
    ):
        self.target_sample_rate = sample_rate
        self.device = device
        self.recording_sample_rate = None
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
    
    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Callback for audio stream."""
        if status:
            print(f"Audio calibration status: {status}")
        self._frames.append(indata.copy())
    
    def start(self) -> None:
        """Start recording for calibration."""
        self._frames = []
        
        # Find supported sample rate
        self.recording_sample_rate = get_supported_sample_rate(
            device=self.device,
            target_rate=self.target_sample_rate,
        )
        
        self._stream = sd.InputStream(
            samplerate=self.recording_sample_rate,
            channels=1,
            device=self.device,
            dtype=np.float32,
            callback=self._audio_callback,
        )
        self._stream.start()
    
    def stop_and_analyze(self) -> AudioCalibrationResult:
        """Stop recording and analyze the audio."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        
        if not self._frames:
            # Return empty result
            return AudioCalibrationResult(
                peak_level=0,
                rms_level=0,
                noise_floor=0,
                clipping_detected=False,
                clipping_percentage=0,
                signal_to_noise=0,
                recommended_preprocessing="light",
                issues=["No audio recorded"],
                recommendations=["Try recording again"],
            )
        
        # Concatenate frames
        audio_data = np.concatenate(self._frames, axis=0)
        
        # Flatten if multi-channel
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()
        
        # Resample if needed
        if self.recording_sample_rate != self.target_sample_rate:
            audio_data = resample_audio(
                audio_data,
                self.recording_sample_rate,
                self.target_sample_rate,
            )
        
        return analyze_audio_calibration(audio_data, self.target_sample_rate)
    
    def get_current_level(self) -> float:
        """Get current audio level for live feedback."""
        if not self._frames:
            return 0.0
        recent_frames = self._frames[-5:] if len(self._frames) >= 5 else self._frames
        audio = np.concatenate(recent_frames, axis=0)
        rms = np.sqrt(np.mean(audio ** 2))
        return min(1.0, rms * 3)
    
    def get_duration(self) -> float:
        """Get duration of recorded audio in seconds."""
        if not self._frames:
            return 0.0
        total_samples = sum(f.shape[0] for f in self._frames)
        rate = self.recording_sample_rate or self.target_sample_rate
        return total_samples / rate
    
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._stream is not None and self._stream.active

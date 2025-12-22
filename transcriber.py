"""
Transcription module for Wayfinder Voice.
Supports multiple backends: whisper.cpp (with Vulkan GPU) and Faster-Whisper (with ROCm).
"""

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class TranscriptionError(Exception):
    """Raised when transcription fails."""
    pass


class TranscriptionBackend(ABC):
    """Abstract base class for transcription backends."""
    
    @abstractmethod
    def transcribe(self, audio_path: str) -> str:
        """Transcribe an audio file to text."""
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """Check if this backend is available/installed."""
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the display name of this backend."""
        pass
    
    @abstractmethod
    def supports_gpu(self) -> bool:
        """Check if GPU acceleration is available for this backend."""
        pass


class WhisperCppBackend(TranscriptionBackend):
    """
    Whisper.cpp backend for transcription.
    Supports CPU and GPU (Vulkan/CUDA) acceleration.
    """
    
    def __init__(
        self,
        whisper_binary: str = "~/whisper.cpp/build/bin/whisper-cli",
        model_path: str = "~/whisper.cpp/models/ggml-small.bin",
        prompt: str = "Hello, this is a dictation with proper punctuation and grammar.",
        threads: int = 6,
        timeout: int = 120,
        use_gpu: bool = False,
        gpu_layers: int = 0,
        # Accuracy enhancement parameters
        beam_size: int = 5,
        best_of: int = 3,
        language: str = "en",
        entropy_threshold: float = 2.6,
        no_speech_threshold: float = 0.5,
        temperature: float = 0.0,
        temperature_fallback: float = 0.0,  # Increment if decoding fails (0 = disabled)
        # Vocabulary and suppression
        custom_vocabulary: list = None,
        suppress_nst: bool = False,  # Suppress non-speech tokens (can drop words)
    ):
        self.whisper_binary = os.path.expanduser(whisper_binary)
        self.model_path = os.path.expanduser(model_path)
        self.prompt = prompt
        self.threads = threads
        self.timeout = timeout
        self.use_gpu = use_gpu
        self.gpu_layers = gpu_layers
        self.beam_size = beam_size
        self.best_of = best_of
        self.language = language
        self.entropy_threshold = entropy_threshold
        self.no_speech_threshold = no_speech_threshold
        self.temperature = temperature
        self.temperature_fallback = temperature_fallback
        self.custom_vocabulary = custom_vocabulary or []
        self.suppress_nst = suppress_nst
        
        # Cache GPU support check
        self._gpu_supported: Optional[bool] = None
    
    def _build_prompt(self) -> str:
        """Build the final prompt by combining base prompt with custom vocabulary."""
        if not self.custom_vocabulary:
            return self.prompt
        
        # Append custom vocabulary terms to the prompt
        vocab_str = ", ".join(self.custom_vocabulary)
        if self.prompt:
            return f"{self.prompt} {vocab_str}."
        return vocab_str
    
    def get_name(self) -> str:
        return "whisper.cpp"
    
    def is_available(self) -> bool:
        """Check if whisper.cpp binary exists."""
        return Path(self.whisper_binary).exists()
    
    def supports_gpu(self) -> bool:
        """
        Check if the whisper.cpp binary was compiled with GPU support.
        Runs whisper-cli --help and checks for GPU-related flags.
        In Vulkan builds, GPU is enabled by default and --no-gpu disables it.
        """
        if self._gpu_supported is not None:
            return self._gpu_supported
        
        if not self.is_available():
            self._gpu_supported = False
            return False
        
        try:
            result = subprocess.run(
                [self.whisper_binary, "--help"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Check for GPU-related flags in help output
            # Vulkan builds have --no-gpu (GPU on by default)
            # Older builds might have -ngl or --gpu-layers
            help_text = result.stdout + result.stderr
            self._gpu_supported = (
                "--no-gpu" in help_text or 
                "-ngl" in help_text or 
                "--gpu-layers" in help_text
            )
            return self._gpu_supported
        except Exception:
            self._gpu_supported = False
            return False
    
    def transcribe(self, audio_path: str) -> str:
        """
        Transcribe an audio file using whisper.cpp.
        
        Args:
            audio_path: Path to the WAV file to transcribe
            
        Returns:
            Transcribed text string
            
        Raises:
            TranscriptionError: If transcription fails
        """
        # Validate paths
        if not Path(self.whisper_binary).exists():
            raise TranscriptionError(f"whisper.cpp binary not found: {self.whisper_binary}")
        if not Path(self.model_path).exists():
            raise TranscriptionError(f"Model file not found: {self.model_path}")
        if not Path(audio_path).exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")

        # Build the final prompt with custom vocabulary
        final_prompt = self._build_prompt()
        
        cmd = [
            self.whisper_binary,
            "-m", self.model_path,
            "-f", audio_path,
            "--no-timestamps",
            "--prompt", final_prompt,
            "-t", str(self.threads),
            # Accuracy enhancement flags
            "--beam-size", str(self.beam_size),
            "--best-of", str(self.best_of),
            "--entropy-thold", str(self.entropy_threshold),
            "--no-speech-thold", str(self.no_speech_threshold),
            "--temperature", str(self.temperature),
            # Temperature fallback: if decoding fails, increment temperature and retry
            "--temperature-inc", str(self.temperature_fallback),
        ]
        
        # Add language flag (skip if auto-detect)
        if self.language and self.language.lower() != "auto":
            cmd.extend(["--language", self.language])
        
        # Hallucination suppression flags
        # Use --suppress-nst to suppress non-speech tokens (works in current whisper.cpp)
        if self.suppress_nst:
            cmd.append("--suppress-nst")
        
        # GPU is enabled by default in Vulkan builds of whisper.cpp
        # Use --no-gpu to disable it if user doesn't want GPU acceleration
        if not self.use_gpu:
            cmd.append("--no-gpu")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )

            if result.returncode != 0:
                raise TranscriptionError(f"whisper.cpp failed: {result.stderr}")

            # Parse output - filter metadata and get transcription
            output_lines = result.stdout.strip().split("\n")
            text_lines = []
            
            for line in output_lines:
                line = line.strip()
                if not line:
                    continue
                # Skip whisper.cpp info lines
                if any(
                    line.startswith(prefix)
                    for prefix in [
                        "whisper_",
                        "main:",
                        "system_info:",
                        "operator():",
                        "log_mel_spectrogram",
                        "ggml_",
                        "vk_",  # Vulkan messages
                        "cuda_",  # CUDA messages
                    ]
                ):
                    continue
                text_lines.append(line)

            transcription = " ".join(text_lines).strip()
            return transcription

        except subprocess.TimeoutExpired:
            raise TranscriptionError(f"Transcription timed out after {self.timeout} seconds")
        except FileNotFoundError:
            raise TranscriptionError(f"Could not execute whisper.cpp: {self.whisper_binary}")


class FasterWhisperBackend(TranscriptionBackend):
    """
    Faster-Whisper backend for transcription.
    Uses CTranslate2 for optimized inference with GPU (ROCm/CUDA) support.
    """
    
    # Class-level model cache to avoid reloading
    _model_cache: dict = {}
    
    def __init__(
        self,
        model_size: str = "small",
        use_gpu: bool = False,
        compute_type: str = "float16",
        prompt: str = "Hello, this is a dictation with proper punctuation and grammar.",
        language: str = "en",
        beam_size: int = 5,
        best_of: int = 3,
        temperature: float = 0.0,
        custom_vocabulary: list = None,
    ):
        self.model_size = model_size
        self.use_gpu = use_gpu
        self.compute_type = compute_type
        self.prompt = prompt
        self.language = language
        self.beam_size = beam_size
        self.best_of = best_of
        self.temperature = temperature
        self.custom_vocabulary = custom_vocabulary or []
        
        self._model = None
        self._gpu_supported: Optional[bool] = None
    
    def _build_prompt(self) -> str:
        """Build the final prompt by combining base prompt with custom vocabulary."""
        if not self.custom_vocabulary:
            return self.prompt
        
        vocab_str = ", ".join(self.custom_vocabulary)
        if self.prompt:
            return f"{self.prompt} {vocab_str}."
        return vocab_str
    
    def get_name(self) -> str:
        return "Faster-Whisper"
    
    def is_available(self) -> bool:
        """Check if faster-whisper is installed."""
        try:
            import faster_whisper
            return True
        except ImportError:
            return False
    
    def supports_gpu(self) -> bool:
        """Check if GPU acceleration is available (ROCm or CUDA)."""
        if self._gpu_supported is not None:
            return self._gpu_supported
        
        if not self.is_available():
            self._gpu_supported = False
            return False
        
        try:
            import torch
            self._gpu_supported = torch.cuda.is_available()  # Works for both CUDA and ROCm
            return self._gpu_supported
        except ImportError:
            self._gpu_supported = False
            return False
    
    def _get_model(self):
        """Get or create the Whisper model (cached)."""
        if self._model is not None:
            return self._model
        
        cache_key = (self.model_size, self.use_gpu, self.compute_type)
        if cache_key in FasterWhisperBackend._model_cache:
            self._model = FasterWhisperBackend._model_cache[cache_key]
            return self._model
        
        try:
            from faster_whisper import WhisperModel
            
            device = "cuda" if self.use_gpu and self.supports_gpu() else "cpu"
            compute_type = self.compute_type if device == "cuda" else "int8"
            
            self._model = WhisperModel(
                self.model_size,
                device=device,
                compute_type=compute_type,
            )
            
            FasterWhisperBackend._model_cache[cache_key] = self._model
            return self._model
            
        except Exception as e:
            raise TranscriptionError(f"Failed to load Faster-Whisper model: {e}")
    
    def transcribe(self, audio_path: str) -> str:
        """
        Transcribe an audio file using Faster-Whisper.
        
        Args:
            audio_path: Path to the audio file to transcribe
            
        Returns:
            Transcribed text string
            
        Raises:
            TranscriptionError: If transcription fails
        """
        if not self.is_available():
            raise TranscriptionError(
                "Faster-Whisper is not installed. "
                "Install with: pip install faster-whisper"
            )
        
        if not Path(audio_path).exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")
        
        try:
            model = self._get_model()
            
            # Prepare transcription options
            language = self.language if self.language.lower() != "auto" else None
            
            # Build prompt with custom vocabulary
            final_prompt = self._build_prompt()
            
            segments, info = model.transcribe(
                audio_path,
                language=language,
                initial_prompt=final_prompt,
                beam_size=self.beam_size,
                best_of=self.best_of,
                temperature=self.temperature,
                vad_filter=True,  # Voice activity detection
            )
            
            # Collect all segments into text
            text_parts = []
            for segment in segments:
                text_parts.append(segment.text.strip())
            
            transcription = " ".join(text_parts).strip()
            return transcription
            
        except Exception as e:
            raise TranscriptionError(f"Faster-Whisper transcription failed: {e}")


def get_backend(config: dict) -> TranscriptionBackend:
    """
    Factory function to create the appropriate transcription backend.
    
    Args:
        config: Configuration dictionary with transcription settings
        
    Returns:
        A TranscriptionBackend instance
    """
    backend_type = config.get("transcription_backend", "whisper_cpp")
    
    if backend_type == "faster_whisper":
        return FasterWhisperBackend(
            model_size=config.get("faster_whisper_model", "small"),
            use_gpu=config.get("use_gpu", False),
            compute_type=config.get("faster_whisper_compute_type", "float16"),
            prompt=config.get("prompt", "Hello, this is a dictation with proper punctuation and grammar."),
            language=config.get("language", "en"),
            beam_size=config.get("beam_size", 5),
            best_of=config.get("best_of", 3),
            temperature=config.get("temperature", 0.0),
            custom_vocabulary=config.get("custom_vocabulary", []),
        )
    else:
        # Default to whisper.cpp
        return WhisperCppBackend(
            whisper_binary=config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli"),
            model_path=config.get("model_path", "~/whisper.cpp/models/ggml-small.bin"),
            prompt=config.get("prompt", "Hello, this is a dictation with proper punctuation and grammar."),
            threads=config.get("threads", 6),
            timeout=config.get("timeout", 120),
            use_gpu=config.get("use_gpu", False),
            gpu_layers=config.get("gpu_layers", 0),
            beam_size=config.get("beam_size", 5),
            best_of=config.get("best_of", 3),
            language=config.get("language", "en"),
            entropy_threshold=config.get("entropy_threshold", 2.6),
            no_speech_threshold=config.get("no_speech_threshold", 0.5),
            temperature=config.get("temperature", 0.0),
            temperature_fallback=config.get("temperature_fallback", 0.0),
            # Vocabulary and suppression
            custom_vocabulary=config.get("custom_vocabulary", []),
            suppress_nst=config.get("suppress_nst", False),
        )


def transcribe_with_config(audio_path: str, config: dict) -> str:
    """
    Transcribe using settings from config dictionary.
    This is the main entry point for transcription.

    Args:
        audio_path: Path to the WAV file to transcribe
        config: Configuration dictionary with transcription settings

    Returns:
        Transcribed text string
    """
    backend = get_backend(config)
    return backend.transcribe(audio_path)


# Legacy function for backwards compatibility
def transcribe(
    audio_path: str,
    whisper_binary: str = "~/whisper.cpp/build/bin/whisper-cli",
    model_path: str = "~/whisper.cpp/models/ggml-small.bin",
    prompt: str = "Hello, this is a dictation with proper punctuation and grammar.",
    threads: int = 6,
    timeout: int = 120,
    beam_size: int = 5,
    best_of: int = 3,
    language: str = "en",
    entropy_threshold: float = 2.4,
    no_speech_threshold: float = 0.6,
    temperature: float = 0.0,
) -> str:
    """
    Legacy transcription function using whisper.cpp.
    Kept for backwards compatibility.
    """
    backend = WhisperCppBackend(
        whisper_binary=whisper_binary,
        model_path=model_path,
        prompt=prompt,
        threads=threads,
        timeout=timeout,
        use_gpu=False,
        gpu_layers=0,
        beam_size=beam_size,
        best_of=best_of,
        language=language,
        entropy_threshold=entropy_threshold,
        no_speech_threshold=no_speech_threshold,
        temperature=temperature,
    )
    return backend.transcribe(audio_path)

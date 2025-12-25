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
    def transcribe(self, audio_path: str, context: str = "") -> str:
        """Transcribe an audio file to text.
        
        Args:
            audio_path: Path to the audio file
            context: Optional context from previous transcription for continuity
        """
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
    
    def _build_prompt(self, context: str = "") -> str:
        """
        Build the final prompt intelligently based on available context.
        
        Strategy:
        - First chunk (no context): Use base prompt + custom vocabulary
        - Subsequent chunks: Use chunk context + custom vocabulary
          (real speech is better context than a generic prompt)
        """
        parts = []
        
        if context:
            # We have real context from previous chunk - use it instead of base prompt
            # This gives Whisper actual speech patterns to continue from
            context_snippet = context.strip()[-200:] if len(context) > 200 else context.strip()
            parts.append(context_snippet)
        else:
            # First chunk - use base prompt to establish style
            if self.prompt:
                parts.append(self.prompt)
        
        # Always include custom vocabulary (user's specific terms/names)
        if self.custom_vocabulary:
            vocab_str = ", ".join(self.custom_vocabulary)
            parts.append(vocab_str)
        
        return " ".join(parts) if parts else ""
    
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
    
    def transcribe(self, audio_path: str, context: str = "") -> str:
        """
        Transcribe an audio file using whisper.cpp.
        
        Args:
            audio_path: Path to the WAV file to transcribe
            context: Optional context from previous transcription for continuity
            
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

        # Build the final prompt with custom vocabulary and context
        final_prompt = self._build_prompt(context)
        
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
            # Performance optimizations
            "--no-prints",  # Suppress progress output (faster)
            "--no-fallback",  # Don't retry with temperature (faster, slight accuracy tradeoff)
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
    
    def _build_prompt(self, context: str = "") -> str:
        """
        Build the final prompt intelligently based on available context.
        
        Strategy:
        - First chunk (no context): Use base prompt + custom vocabulary
        - Subsequent chunks: Use chunk context + custom vocabulary
        """
        parts = []
        
        if context:
            # Real context from previous chunk takes priority
            context_snippet = context.strip()[-200:] if len(context) > 200 else context.strip()
            parts.append(context_snippet)
        else:
            # First chunk - use base prompt
            if self.prompt:
                parts.append(self.prompt)
        
        # Always include custom vocabulary
        if self.custom_vocabulary:
            vocab_str = ", ".join(self.custom_vocabulary)
            parts.append(vocab_str)
        
        return " ".join(parts) if parts else ""
    
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
    
    def transcribe(self, audio_path: str, context: str = "") -> str:
        """
        Transcribe an audio file using Faster-Whisper.
        
        Args:
            audio_path: Path to the audio file to transcribe
            context: Optional context from previous transcription for continuity
            
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
            
            # Build prompt with custom vocabulary and context
            final_prompt = self._build_prompt(context)
            
            segments, info = model.transcribe(
                audio_path,
                language=language,
                initial_prompt=final_prompt,
                beam_size=self.beam_size,
                best_of=self.best_of,
                temperature=self.temperature,
                vad_filter=False,  # Disabled - was cutting off words
                # Performance optimizations
                condition_on_previous_text=True,  # Use previous output as context
                no_speech_threshold=0.5,  # Skip silence faster
                compression_ratio_threshold=2.4,  # Reject bad outputs faster
                word_timestamps=False,  # Disable if not needed (saves ~10%)
            )
            
            # Collect all segments into text
            text_parts = []
            for segment in segments:
                text_parts.append(segment.text.strip())
            
            transcription = " ".join(text_parts).strip()
            return transcription
            
        except Exception as e:
            raise TranscriptionError(f"Faster-Whisper transcription failed: {e}")


class OpenAIWhisperBackend(TranscriptionBackend):
    """
    OpenAI Whisper API backend for cloud transcription.
    Uses OpenAI's hosted Whisper model for high-quality transcription.
    """
    
    def __init__(
        self,
        api_key: str = "",
        model: str = "whisper-1",
        language: str = "en",
        prompt: str = "",
        temperature: float = 0.0,
        custom_vocabulary: list = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.language = language
        self.prompt = prompt
        self.temperature = temperature
        self.custom_vocabulary = custom_vocabulary or []
        self._client = None
    
    def _build_prompt(self, context: str = "") -> str:
        """Build the prompt with context and custom vocabulary."""
        parts = []
        
        if context:
            context_snippet = context.strip()[-200:] if len(context) > 200 else context.strip()
            parts.append(context_snippet)
        elif self.prompt:
            parts.append(self.prompt)
        
        if self.custom_vocabulary:
            vocab_str = ", ".join(self.custom_vocabulary)
            parts.append(vocab_str)
        
        return " ".join(parts) if parts else ""
    
    def get_name(self) -> str:
        return "OpenAI Whisper (Cloud)"
    
    def is_available(self) -> bool:
        """Check if openai is installed and API key is set."""
        try:
            import openai
            return bool(self.api_key)
        except ImportError:
            return False
    
    def supports_gpu(self) -> bool:
        """Cloud API - GPU is handled server-side."""
        return True  # Always "GPU" since it runs on OpenAI's servers
    
    def _get_client(self):
        """Get or create the OpenAI client."""
        if self._client is not None:
            return self._client
        
        if not self.api_key:
            raise TranscriptionError(
                "OpenAI API key not set. "
                "Set OPENAI_API_KEY environment variable or configure in settings."
            )
        
        try:
            import openai
            # Set a reasonable timeout (60 seconds for audio transcription)
            self._client = openai.OpenAI(
                api_key=self.api_key,
                timeout=60.0,  # 60 second timeout
            )
            return self._client
        except Exception as e:
            raise TranscriptionError(f"Failed to initialize OpenAI client: {e}")
    
    def transcribe(self, audio_path: str, context: str = "") -> str:
        """
        Transcribe an audio file using OpenAI's Whisper API.
        
        Args:
            audio_path: Path to the audio file to transcribe
            context: Optional context from previous transcription for continuity
            
        Returns:
            Transcribed text string
            
        Raises:
            TranscriptionError: If transcription fails
        """
        if not self.is_available():
            if not self.api_key:
                raise TranscriptionError("OpenAI API key not configured.")
            raise TranscriptionError(
                "openai package is not installed. "
                "Install with: pip install openai"
            )
        
        if not Path(audio_path).exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")
        
        try:
            client = self._get_client()
            
            # Build prompt with context and vocabulary
            prompt = self._build_prompt(context)
            
            # Log for debugging
            file_size = Path(audio_path).stat().st_size
            print(f"[OpenAI Whisper] Sending {file_size / 1024:.1f}KB audio file...")
            
            # OpenAI Whisper API call
            with open(audio_path, "rb") as audio_file:
                # Prepare API parameters
                params = {
                    "model": self.model,
                    "file": audio_file,
                    "response_format": "text",
                    "temperature": self.temperature,
                }
                
                # Add optional parameters
                if prompt:
                    params["prompt"] = prompt
                
                # Language (None = auto-detect)
                if self.language and self.language.lower() != "auto":
                    params["language"] = self.language
                
                transcription = client.audio.transcriptions.create(**params)
            
            print(f"[OpenAI Whisper] ✓ Transcription received")
            
            # Response is just text when response_format="text"
            return transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
            
        except Exception as e:
            print(f"[OpenAI Whisper] ✗ Error: {e}")
            raise TranscriptionError(f"OpenAI Whisper API call failed: {e}")


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
    elif backend_type == "openai_whisper":
        return OpenAIWhisperBackend(
            api_key="",  # Will be read from OPENAI_API_KEY env var
            model=config.get("openai_whisper_model", "whisper-1"),
            language=config.get("language", "en"),
            prompt=config.get("prompt", ""),
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


def ensure_punctuation_postprocess(text: str) -> str:
    """
    Post-process transcription to ensure proper punctuation.
    
    Fixes common issues:
    - Capitalizes first letter of text and after sentence endings
    - Adds period at end if missing punctuation
    - Cleans up spacing around punctuation
    """
    if not text or not text.strip():
        return text
    
    import re
    
    text = text.strip()
    
    # Capitalize first letter
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    
    # Capitalize after sentence-ending punctuation (. ! ?)
    def capitalize_after_punct(match):
        return match.group(1) + match.group(2).upper()
    
    text = re.sub(r'([.!?]\s+)([a-z])', capitalize_after_punct, text)
    
    # Ensure text ends with punctuation
    if text and text[-1] not in '.!?':
        # Check if it looks like a question
        question_words = ['who', 'what', 'where', 'when', 'why', 'how', 'is', 'are', 'do', 'does', 'did', 'can', 'could', 'would', 'should', 'will']
        first_word = text.split()[0].lower() if text.split() else ''
        if first_word in question_words or '?' in text:
            text += '?'
        else:
            text += '.'
    
    # Clean up spacing around punctuation
    text = re.sub(r'\s+([.,!?;:])', r'\1', text)  # Remove space before punctuation
    text = re.sub(r'([.,!?;:])([A-Za-z])', r'\1 \2', text)  # Add space after if missing
    
    # Clean up multiple spaces
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


def transcribe_with_config(audio_path: str, config: dict, context: str = "") -> str:
    """
    Transcribe using settings from config dictionary.
    This is the main entry point for transcription.

    Args:
        audio_path: Path to the WAV file to transcribe
        config: Configuration dictionary with transcription settings
        context: Optional context from previous transcription (helps continuity)

    Returns:
        Transcribed text string
    """
    # Build enhanced prompt if punctuation is enabled
    ensure_punct = config.get("ensure_punctuation", True)
    
    if ensure_punct:
        # Add punctuation hint to the prompt
        original_prompt = config.get("prompt", "")
        punct_hint = "Use proper punctuation including periods, commas, and capitalization."
        if punct_hint not in original_prompt:
            config = config.copy()  # Don't modify original
            config["prompt"] = f"{original_prompt} {punct_hint}".strip()
    
    backend = get_backend(config)
    text = backend.transcribe(audio_path, context=context)
    
    # Apply basic post-processing if punctuation is enabled
    if ensure_punct and text:
        text = ensure_punctuation_postprocess(text)
    
    # Apply LLM post-processing if enabled
    if config.get("post_processing_enabled", False) and text:
        try:
            from postprocessor import process_with_config
            import os
            backend = config.get("post_processing_backend", "llama_cpp")
            
            # Debug: Check if API key is available for cloud backends
            if backend == "openai":
                key = os.environ.get("OPENAI_API_KEY", "")
                if not key:
                    print("[Post-processing] ⚠ OPENAI_API_KEY not set in environment")
                else:
                    print(f"[Post-processing] Using OpenAI ({config.get('openai_model', 'gpt-4o-mini')})")
            elif backend == "anthropic":
                key = os.environ.get("ANTHROPIC_API_KEY", "")
                if not key:
                    print("[Post-processing] ⚠ ANTHROPIC_API_KEY not set in environment")
                else:
                    print(f"[Post-processing] Using Anthropic ({config.get('anthropic_model', 'claude-3-haiku')})")
            elif backend == "llama_cpp":
                model_path = config.get("llama_cpp_model_path", "")
                if not model_path:
                    print("[Post-processing] ⚠ No llama.cpp model selected")
                else:
                    print(f"[Post-processing] Using local model: {os.path.basename(model_path)}")
            
            original_text = text
            text = process_with_config(text, config)
            
            if text != original_text:
                print(f"[Post-processing] ✓ Text cleaned ({len(original_text)} → {len(text)} chars)")
            else:
                print("[Post-processing] Text unchanged (or processing skipped)")
                
        except ImportError as e:
            print(f"[Post-processing] ✗ Module not available: {e}")
        except Exception as e:
            print(f"[Post-processing] ✗ Error: {e}")
            # Continue with original text
    
    return text


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

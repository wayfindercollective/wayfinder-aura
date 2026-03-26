"""
Transcription module for Wayfinder Aura.
Supports multiple backends: whisper.cpp (with Vulkan GPU) and Faster-Whisper (with ROCm).
"""

import os
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


# Developer vocabulary - common terms that Whisper often mishears
# These get added to the prompt when output_tone is "dev" to improve recognition
DEV_VOCABULARY = [
    # Git terminology
    "main", "dev", "master", "branch", "branches", "merge", "commit", "commits",
    "push", "pull", "fetch", "rebase", "checkout", "stash", "cherry-pick",
    "upstream", "origin", "remote", "clone", "fork", "diff", "HEAD", "staging",
    "repo", "repository", "git", "GitHub", "GitLab", "Bitbucket",
    # Common commands
    "npm", "npx", "yarn", "pnpm", "pip", "cargo", "brew", "apt", "sudo",
    "cd", "ls", "mkdir", "rm", "mv", "cp", "grep", "sed", "awk", "curl", "wget",
    # Programming terms
    "API", "REST", "GraphQL", "JSON", "YAML", "TOML", "XML", "HTML", "CSS",
    "TypeScript", "JavaScript", "Python", "Rust", "Go", "Java", "C++",
    "function", "class", "method", "variable", "const", "let", "var",
    "async", "await", "import", "export", "module", "package",
    "frontend", "backend", "fullstack", "server", "client", "localhost",
    "Docker", "Kubernetes", "CI", "CD", "deploy", "production", "staging",
    "debug", "console", "log", "error", "exception", "stack trace",
    # File paths / extensions
    ".js", ".ts", ".py", ".rs", ".go", ".json", ".yaml", ".yml", ".md",
    "src", "lib", "bin", "node_modules", "package.json", "requirements.txt",
]

# Casual vocabulary - informal terms that Whisper tends to formalize
# These get added to the prompt when output_tone is "casual" to preserve casual speech
CASUAL_VOCABULARY = [
    # Contractions/informal speech
    "gonna", "wanna", "gotta", "kinda", "sorta", "coulda", "shoulda", "woulda",
    "dunno", "lemme", "gimme", "gotcha", "ya", "yep", "yup", "nope", "nah",
    # Casual affirmatives/responses  
    "yeah", "okay", "ok", "alright", "cool", "awesome", "nice", "sweet",
    "sure", "totally", "definitely", "absolutely", "exactly", "right",
    # Casual connectors
    "cuz", "cause", "tho", "tho", "btw", "tbh", "idk", "imo",
    # Casual expressions
    "like", "literally", "basically", "honestly", "actually", "seriously",
    "pretty much", "kind of", "sort of", "you know", "I mean", "I guess",
]


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
        prompt: str = "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next.",
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
        suppress_nst: bool = False,  # Suppress non-speech tokens (can drop words if True)
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
            # GPU device selection is handled at app startup via setup_gpu_environment()
            # which sets GGML_VK_VISIBLE_DEVICES in os.environ.
            # Subprocesses automatically inherit this - no extra logic needed here.
            
            # Log which GPU device is being used (if set)
            gpu_device = os.environ.get("GGML_VK_VISIBLE_DEVICES")
            if gpu_device and self.use_gpu:
                print(f"[Transcription] Using GPU device {gpu_device}")
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                # No env= needed - subprocess inherits GGML_VK_VISIBLE_DEVICES from parent
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
        prompt: str = "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next.",
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


class GroqWhisperBackend(TranscriptionBackend):
    """
    Groq Whisper API backend for ultra-fast cloud transcription.
    Uses Groq's LPU hardware for ~10x faster inference than standard Whisper.
    
    Note: Groq has a 896 character limit for the prompt parameter.
    Same Whisper Large-v3 model, dramatically lower latency.
    """
    
    def __init__(
        self,
        api_key: str = "",
        model: str = "whisper-large-v3",
        language: str = "en",
        prompt: str = "",
        temperature: float = 0.0,
        custom_vocabulary: list = None,
    ):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY", "")
        self.model = model
        self.language = language
        self.prompt = prompt
        self.temperature = temperature
        self.custom_vocabulary = custom_vocabulary or []
        self._client = None
    
    def _build_prompt(self, context: str = "") -> str:
        """
        Build the prompt with context and custom vocabulary.
        
        Note: Groq has a strict 896 character limit for prompts.
        We prioritize: custom vocabulary > context > base prompt
        """
        GROQ_PROMPT_LIMIT = 896
        parts = []
        
        # Start with custom vocabulary (most important for accuracy)
        vocab_str = ""
        if self.custom_vocabulary:
            vocab_str = ", ".join(self.custom_vocabulary)
            if len(vocab_str) < GROQ_PROMPT_LIMIT - 100:  # Leave room for context
                parts.append(vocab_str)
        
        # Calculate remaining space
        current_len = len(" ".join(parts)) if parts else 0
        remaining = GROQ_PROMPT_LIMIT - current_len - 10  # Buffer
        
        if context and remaining > 50:
            # Use context from previous transcription
            context_snippet = context.strip()
            if len(context_snippet) > remaining:
                context_snippet = context_snippet[-(remaining):]
            parts.insert(0, context_snippet)  # Context first, then vocab
        elif self.prompt and remaining > 50:
            # Use base prompt if no context
            prompt_snippet = self.prompt
            if len(prompt_snippet) > remaining:
                prompt_snippet = prompt_snippet[:remaining]
            parts.insert(0, prompt_snippet)
        
        result = " ".join(parts) if parts else ""
        
        # Final safety check
        if len(result) > GROQ_PROMPT_LIMIT:
            result = result[:GROQ_PROMPT_LIMIT]
            print(f"[Groq Whisper] ⚠ Prompt truncated to {GROQ_PROMPT_LIMIT} chars")
        
        return result
    
    def get_name(self) -> str:
        return "Groq Whisper (Ultra-Fast)"
    
    def is_available(self) -> bool:
        """Check if groq is installed and API key is set."""
        try:
            import groq
            return bool(self.api_key)
        except ImportError:
            return False
    
    def supports_gpu(self) -> bool:
        """Cloud API - runs on Groq's LPU hardware."""
        return True
    
    def _get_client(self):
        """Get or create the Groq client."""
        if self._client is not None:
            return self._client
        
        if not self.api_key:
            raise TranscriptionError(
                "Groq API key not set. "
                "Set GROQ_API_KEY environment variable or configure in settings."
            )
        
        try:
            import groq
            self._client = groq.Groq(
                api_key=self.api_key,
                timeout=60.0,
            )
            return self._client
        except Exception as e:
            raise TranscriptionError(f"Failed to initialize Groq client: {e}")
    
    def transcribe(self, audio_path: str, context: str = "") -> str:
        """
        Transcribe an audio file using Groq's Whisper API.
        
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
                raise TranscriptionError("Groq API key not configured.")
            raise TranscriptionError(
                "groq package is not installed. "
                "Install with: pip install groq"
            )
        
        if not Path(audio_path).exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")
        
        try:
            client = self._get_client()
            
            # Build prompt with context and vocabulary
            prompt = self._build_prompt(context)
            
            # Log for debugging
            file_size = Path(audio_path).stat().st_size
            print(f"[Groq Whisper] Sending {file_size / 1024:.1f}KB audio file...")
            
            import time
            start_time = time.time()
            
            # Groq Whisper API call
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
            
            elapsed = time.time() - start_time
            print(f"[Groq Whisper] ✓ Transcription received in {elapsed:.2f}s")
            
            # Response is just text when response_format="text"
            return transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
            
        except Exception as e:
            print(f"[Groq Whisper] ✗ Error: {e}")
            raise TranscriptionError(f"Groq Whisper API call failed: {e}")


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
            use_gpu=config.get("use_gpu", True),
            compute_type=config.get("faster_whisper_compute_type", "float16"),
            prompt=config.get("prompt", "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next."),
            language=config.get("language", "en"),
            beam_size=config.get("beam_size", 5),
            best_of=config.get("best_of", 3),
            temperature=config.get("temperature", 0.0),
            custom_vocabulary=config.get("custom_vocabulary", []),
        )
    elif backend_type == "openai_whisper":
        return OpenAIWhisperBackend(
            api_key=config.get("openai_api_key", ""),  # From config or OPENAI_API_KEY env var
            model=config.get("openai_whisper_model", "whisper-1"),
            language=config.get("language", "en"),
            prompt=config.get("prompt", ""),
            temperature=config.get("temperature", 0.0),
            custom_vocabulary=config.get("custom_vocabulary", []),
        )
    elif backend_type == "groq_whisper":
        return GroqWhisperBackend(
            api_key=config.get("groq_api_key", ""),  # From config or GROQ_API_KEY env var
            model=config.get("groq_whisper_model", "whisper-large-v3"),
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
            prompt=config.get("prompt", "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next."),
            threads=config.get("threads", 6),
            timeout=config.get("timeout", 120),
            use_gpu=config.get("use_gpu", True),
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


def _collapse_whisper_repetitions(text: str) -> str:
    """
    Detect and collapse Whisper repetition loops.

    Whisper sometimes gets stuck repeating a phrase with slight variations:
    "Loud noise Lud noise Lou noise Brd usic Bir muic Bird usi"

    This uses a sliding window to detect when consecutive word groups
    are similar to each other, keeping only the best version.
    """
    import re
    from difflib import SequenceMatcher

    words = text.split()
    if len(words) < 6:
        return text

    # Try window sizes of 2-4 words to detect repeating patterns
    for window_size in (2, 3, 4):
        if len(words) < window_size * 2:
            continue

        # Slide through words looking for consecutive similar groups
        i = 0
        result_words = []
        while i < len(words):
            # Get current window
            if i + window_size > len(words):
                result_words.extend(words[i:])
                break

            current = " ".join(words[i:i + window_size]).lower()

            # Look ahead for similar windows
            repeat_end = i + window_size
            best_version = " ".join(words[i:i + window_size])
            best_char_count = sum(len(w) for w in words[i:i + window_size])

            while repeat_end + window_size <= len(words):
                candidate = " ".join(words[repeat_end:repeat_end + window_size]).lower()
                ratio = SequenceMatcher(None, current, candidate).ratio()

                if ratio >= 0.55:
                    # This window is a repetition — keep the version with most total chars
                    # (longer words = more complete transcription)
                    candidate_chars = sum(len(w) for w in words[repeat_end:repeat_end + window_size])
                    if candidate_chars > best_char_count:
                        best_version = " ".join(words[repeat_end:repeat_end + window_size])
                        best_char_count = candidate_chars
                    repeat_end += window_size
                else:
                    break

            if repeat_end > i + window_size * 2:
                # Found 3+ occurrences (a real repetition loop) — keep only the best
                result_words.extend(best_version.split())
                i = repeat_end
            else:
                result_words.append(words[i])
                i += 1

        words = result_words

    return " ".join(words)


def clean_whisper_artifacts(text: str) -> str:
    """
    Clean up common Whisper transcription artifacts.
    
    This runs on ALL transcriptions regardless of post-processing settings.
    Removes known Whisper hallucination patterns:
    - Repeated dots/periods (. . . . . or .........)
    - Repeated punctuation patterns
    - Trailing silence artifacts
    - [BLANK_AUDIO] markers
    - Music/sound effect markers like ♪ or [Music]
    """
    if not text or not text.strip():
        return text
    
    import re
    
    original = text
    
    # Fix double-quote used as apostrophe in contractions (Whisper artifact)
    # e.g., Let"S → Let's, don"t → don't, I"m → I'm, we"ll → we'll
    text = re.sub(r'(\w)"([stmdlrv]\b)', r"\1'\2", text, flags=re.IGNORECASE)
    # Also handle "ll, "re, "ve (two-letter suffixes)
    text = re.sub(r'(\w)"(ll|re|ve)\b', r"\1'\2", text, flags=re.IGNORECASE)

    # Remove asterisk-wrapped non-speech markers (Whisper uses *text* for sounds/music)
    # e.g., *loud noise*, *bird music*, *laughing* → removed
    text = re.sub(r'\*[^*]{1,40}\*', ' ', text)
    # Clean orphaned/stray asterisks
    text = re.sub(r'\*+', '', text)

    # Remove [BLANK_AUDIO] and similar markers (case insensitive)
    # Replace with space to preserve word boundaries (will be normalized later)
    text = re.sub(r'\[BLANK_AUDIO\]', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[SILENCE\]', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[MUSIC\]', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'\[APPLAUSE\]', ' ', text, flags=re.IGNORECASE)

    # Remove music note symbols (replace with space to preserve word boundaries)
    text = re.sub(r'♪+', ' ', text)

    # Remove Whisper special token leaks: <|...|> patterns (timestamps, end-of-text, etc.)
    text = re.sub(r'<\|[^|]*\|>', ' ', text)

    # Fix angle brackets that Whisper inserts instead of punctuation
    # < often replaces commas, > often replaces periods
    text = re.sub(r'\s*<\s+', ', ', text)   # "word< next" or "word < next" -> "word, next"
    text = re.sub(r'\s*>\s*\.', '.', text)   # ">." -> "."
    text = re.sub(r'\s*>\s+', '. ', text)    # "word> next" or "word > next" -> "word. next"
    text = re.sub(r'<$', '', text)            # trailing <
    text = re.sub(r'>$', '', text)            # trailing >
    # Remove any remaining isolated angle brackets
    text = re.sub(r'<', '', text)
    text = re.sub(r'>', '', text)
    
    # Remove repeated dots with spaces: ". . . . ." or ". . ."
    # This pattern captures 2 or more dots separated by spaces
    text = re.sub(r'(\.\s*){2,}', '. ', text)
    
    # Remove repeated dots without spaces: "......" or "…"
    text = re.sub(r'\.{3,}', '...', text)  # Normalize to max 3 dots (ellipsis)
    text = re.sub(r'…+', '...', text)  # Unicode ellipsis
    
    # Remove standalone ellipsis at start/end
    text = re.sub(r'^\s*\.{1,3}\s*', '', text)
    text = re.sub(r'\s*\.{2,3}\s*$', '.', text)
    
    # Remove repeated commas, question marks, exclamation marks
    text = re.sub(r',{2,}', ',', text)
    text = re.sub(r'\?{2,}', '?', text)
    text = re.sub(r'!{2,}', '!', text)
    
    # Remove trailing/leading dots that aren't sentence endings
    # e.g., ". . ." at the end becomes nothing
    text = re.sub(r'\s+\.\s*$', '.', text)
    
    # Detect and remove Whisper repetition loops
    # Whisper sometimes gets stuck repeating a phrase with slight variations:
    # "Loud noise Lud noise Lou noise Brd usic Bir muic Bird usi Bird muic"
    # Strategy: find repeated 2-3 word phrases and keep only the first occurrence
    text = _collapse_whisper_repetitions(text)

    # Clean up multiple spaces
    text = re.sub(r'\s+', ' ', text)

    text = text.strip()

    # Log if we cleaned something
    if text != original.strip():
        removed_chars = len(original.strip()) - len(text)
        if removed_chars > 5:  # Only log if we removed substantial artifacts
            print(f"[Transcription] Cleaned {removed_chars} chars of Whisper artifacts")
    
    return text


# Acronyms and abbreviations that should stay uppercase
_PRESERVE_CAPS = {
    'I', 'OK', 'API', 'HTML', 'CSS', 'JSON', 'XML', 'YAML', 'TOML',
    'HTTP', 'HTTPS', 'URL', 'SQL', 'CPU', 'GPU', 'RAM', 'ROM', 'USB',
    'PDF', 'FAQ', 'CEO', 'CTO', 'VP', 'HR', 'PR', 'AI', 'ML',
    'LLM', 'NLP', 'OCR', 'IDE', 'CLI', 'GUI', 'SSH', 'FTP', 'DNS',
    'TCP', 'UDP', 'IP', 'VPN', 'HDMI', 'AMD', 'NVIDIA',
    'CUDA', 'GGUF', 'GGML', 'US', 'UK', 'EU', 'UN', 'NASA', 'ASAP',
    'DIY', 'ETA', 'WiFi', 'BIOS', 'SSD', 'HDD', 'NVMe', 'PCIe',
    'RGB', 'LED', 'LCD', 'OLED', 'USB', 'JPEG', 'PNG', 'GIF', 'SVG',
    'AWS', 'GCP', 'REST', 'CRUD', 'DOM', 'CSS', 'NPM',
}


def normalize_whisper_caps(text: str) -> str:
    """
    Fix random ALL CAPS and chaotic mixed-case words from Whisper.

    Whisper sometimes outputs words in ALL CAPS or with random uppercase
    letters scattered throughout (e.g., "WeLL", "cAn't", "MaiNE'S").
    This normalizes them while preserving intentional caps like acronyms.
    """
    if not text:
        return text

    import re

    words = text.split()
    result = []

    for i, word in enumerate(words):
        # Strip punctuation for checking
        clean = re.sub(r'[^A-Za-z]', '', word)

        if not clean:
            result.append(word)
            continue

        # Enforce known acronyms/words (e.g., lowercase "i" → "I")
        if clean.upper() in _PRESERVE_CAPS or clean in _PRESERVE_CAPS:
            canonical = clean.upper() if clean.upper() in _PRESERVE_CAPS else clean
            # Replace the alpha portion with canonical form, keep punctuation
            new_word = ""
            ci = 0
            for ch in word:
                if ch.isalpha() and ci < len(canonical):
                    new_word += canonical[ci]
                    ci += 1
                else:
                    new_word += ch
            result.append(new_word)
            continue

        is_sentence_start = (i == 0) or (
            i > 0 and result[-1].rstrip().endswith(('.', '!', '?'))
        )

        # Check if the word is ALL CAPS
        if clean.isupper():
            if is_sentence_start:
                new_word = word[0] + word[1:].lower()
            else:
                new_word = word.lower()
            result.append(new_word)
            continue

        # Catch any remaining chaotic case (e.g., "mE", "WeLL", "cAn't", "MaiNE'S")
        # Normal patterns: "hello" (lower), "Hello" (title) — anything else is Whisper noise
        if not clean.islower() and not clean.istitle():
            if is_sentence_start:
                new_word = word[0].upper() + word[1:].lower()
            else:
                new_word = word.lower()
            result.append(new_word)
            continue

        result.append(word)

    return ' '.join(result)


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


def transcribe_with_config(
    audio_path: str, 
    config: dict, 
    context: str = "",
    skip_post_processing: bool = False,
) -> str:
    """
    Transcribe using settings from config dictionary.
    This is the main entry point for transcription.

    Args:
        audio_path: Path to the WAV file to transcribe
        config: Configuration dictionary with transcription settings
        context: Optional context from previous transcription (helps continuity)
        skip_post_processing: If True, skip LLM post-processing (useful for chunked mode
            where post-processing should happen on the final combined text)

    Returns:
        Transcribed text string
    """
    # Build enhanced prompt if punctuation is enabled
    ensure_punct = config.get("ensure_punctuation", True)
    
    # Start with copy to avoid modifying original
    config = config.copy()
    original_prompt = config.get("prompt", "")
    
    # Add vocabulary based on selected mode
    # This helps Whisper recognize context-specific terms
    output_tone = config.get("output_tone")
    
    if output_tone == "dev":
        # Developer vocabulary: git commands, programming terms
        existing_vocab = config.get("custom_vocabulary", [])
        existing_lower = {v.lower() for v in existing_vocab}
        new_vocab = [term for term in DEV_VOCABULARY if term.lower() not in existing_lower]
        config["custom_vocabulary"] = existing_vocab + new_vocab
        print(f"[Dev Mode] Added {len(new_vocab)} developer vocabulary terms for better recognition")
    
    elif output_tone == "casual":
        # Casual vocabulary: informal speech patterns Whisper tends to formalize
        existing_vocab = config.get("custom_vocabulary", [])
        existing_lower = {v.lower() for v in existing_vocab}
        new_vocab = [term for term in CASUAL_VOCABULARY if term.lower() not in existing_lower]
        config["custom_vocabulary"] = existing_vocab + new_vocab
        print(f"[Casual Mode] Added {len(new_vocab)} casual vocabulary terms to preserve informal speech")
    
    # Add voice profile context when "personal" style is selected
    if config.get("output_tone") == "personal":
        try:
            from .voice_profile import get_voice_profile
            voice_profile = get_voice_profile(
                history_limit=config.get("voice_learning_history_limit", 100),
                regen_interval=config.get("voice_learning_regen_interval", 20),
            )
            profile_context = voice_profile.get_prompt_context()
            if profile_context:
                # Prepend voice profile context to prompt for better recognition
                original_prompt = f"{profile_context} {original_prompt}".strip()
                print(f"[Personal Style] Using voice profile ({len(profile_context)} chars)")
        except Exception as e:
            print(f"[Personal Style] ⚠ Could not load voice profile: {e}")
    
    if ensure_punct:
        # Add punctuation hint to the prompt
        punct_hint = "Use proper punctuation including periods, commas, and capitalization."
        if punct_hint not in original_prompt:
            original_prompt = f"{original_prompt} {punct_hint}".strip()
    
    config["prompt"] = original_prompt
    
    backend = get_backend(config)
    text = backend.transcribe(audio_path, context=context)
    
    # ALWAYS clean up Whisper artifacts (dots, [BLANK_AUDIO], <>, caps, etc.)
    # This runs regardless of post-processing settings
    if text:
        text = clean_whisper_artifacts(text)
        text = normalize_whisper_caps(text)

    # Apply basic post-processing if punctuation is enabled
    if ensure_punct and text:
        text = ensure_punctuation_postprocess(text)
    
    # Apply post-processing (and not skipped for chunked mode)
    # Note: For "minimal" style, regex cleanup runs even if LLM post-processing is disabled
    tone = config.get("output_tone", "professional")
    post_processing_enabled = config.get("post_processing_enabled", True)
    
    if (post_processing_enabled or tone == "minimal") and text and not skip_post_processing:
        try:
            from .postprocessor import process_with_config
            import os
            backend = config.get("post_processing_backend", "llama_cpp")
            
            # Debug: Check backend and log what's being used
            if tone == "minimal":
                print(f"[Post-processing] Using Minimal (regex-only, no LLM)")
            elif backend == "openai":
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
            else:  # llama_cpp
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

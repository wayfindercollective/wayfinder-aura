"""
Transcription module for Wayfinder Aura.
Supports multiple backends: whisper.cpp (with Vulkan GPU) and Faster-Whisper (with ROCm).
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional


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


# Machines where the GPU (Vulkan) whisper binary crashed route transcriptions to the
# sibling CPU binary, keyed by original binary path. Not persisted across sessions:
# a Mesa/driver update can fix Vulkan, and the next session retries GPU automatically.
# Within a session, a background recovery probe (see _maybe_probe_gpu_recovery) keeps
# re-testing the GPU binary on an exponential-backoff schedule and lifts the fallback
# the moment it works again — so a transient GPU reset doesn't pin a long session to CPU.
_CPU_FALLBACK_ACTIVE: dict = {}

# Per-binary retry bookkeeping: {"failures": int, "next_retry": epoch_seconds}.
# Kept (with halved failure count) after a successful restore so a flapping GPU
# climbs the backoff ladder instead of crash-looping a user's dictations.
_gpu_retry_state: dict = {}
_GPU_RETRY_BASE_SECONDS = 60.0
_GPU_RETRY_CAP_SECONDS = 1800.0

_gpu_probe_lock = threading.Lock()
_gpu_probe_inflight = False

# Optional app-level logger (the UI activity log). The transcriber works fine
# without one — events still go to stdout.
_gpu_event_logger: Optional[Callable[[str], None]] = None


def set_gpu_event_logger(callback: Optional[Callable[[str], None]]) -> None:
    """Register an app-level logger for GPU fallback/restore events.

    The app passes its activity-log method so users SEE "GPU went down / GPU
    restored" instead of it disappearing into stdout.
    """
    global _gpu_event_logger
    _gpu_event_logger = callback


def _log_gpu_event(message: str) -> None:
    print(f"[Transcription] {message}")
    callback = _gpu_event_logger
    if callback is not None:
        try:
            callback(message)
        except Exception:
            pass  # a dying UI must never break transcription


def _fmt_delay(seconds: float) -> str:
    return f"{int(seconds)}s" if seconds < 120 else f"{int(seconds // 60)}m"


def _schedule_gpu_retry(binary: str) -> float:
    """Record a GPU failure and compute the next retry time. Returns the delay."""
    state = _gpu_retry_state.setdefault(binary, {"failures": 0, "next_retry": 0.0})
    state["failures"] += 1
    delay = min(_GPU_RETRY_BASE_SECONDS * (2 ** (state["failures"] - 1)),
                _GPU_RETRY_CAP_SECONDS)
    state["next_retry"] = time.time() + delay
    return delay


def _cpu_fallback_binary(binary: str) -> Optional[str]:
    """Sibling CPU-only whisper build (e.g. /app/bin/whisper-cli-cpu), if bundled.

    The Flatpak ships whisper-cli (Vulkan) + whisper-cli-cpu (CPU baseline); from-source
    installs usually have only one binary, so this returns None there.
    """
    if binary.endswith("-cpu"):
        return None
    p = Path(binary)
    candidate = p.with_name(p.name + "-cpu")
    return str(candidate) if candidate.exists() else None


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
    
    def _supported_flags(self) -> set:
        """Long options the actual whisper-cli accepts (parsed from --help, cached).

        The Flatpak bundles whisper.cpp v1.7.2, which lacks --no-speech-thold and --suppress-nst
        that newer host builds expose. Passing an unknown flag makes whisper-cli print usage and
        transcribe NOTHING (a silent '' result -> "no dictation output"). Filtering the command
        to supported flags keeps it working across the bundled binary and a newer host one. Fails
        OPEN (empty set => no filtering) so flags are never stripped on a binary we couldn't
        introspect.
        """
        cached = getattr(self, "_supported_flags_cache", None)
        if cached is not None:
            return cached
        flags = set()
        try:
            import re
            h = subprocess.run(
                [self.whisper_binary, "--help"],
                capture_output=True, text=True, timeout=5,
            )
            flags = set(re.findall(r"--[a-z][a-z0-9-]+", (h.stdout or "") + (h.stderr or "")))
        except Exception:
            pass
        self._supported_flags_cache = flags
        return flags

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
        
        # Some accuracy/suppression flags exist only in newer whisper.cpp; the bundled v1.7.2
        # lacks --no-speech-thold / --suppress-nst, and whisper-cli aborts (transcribing nothing)
        # on the first unknown flag. Emit version-fragile flags only when --help advertises them;
        # _flag_ok fails OPEN (empty set => no filtering) so an un-introspectable binary is safe.
        _sup = self._supported_flags()
        def _flag_ok(flag):
            return (not _sup) or (flag in _sup)

        # Route to the CPU sibling if the GPU binary already crashed this session,
        # and (in the background) periodically re-test the GPU so it comes back
        # automatically once the driver recovers.
        active_binary = _CPU_FALLBACK_ACTIVE.get(self.whisper_binary, self.whisper_binary)
        if active_binary != self.whisper_binary:
            self._maybe_probe_gpu_recovery()

        cmd = [
            active_binary,
            "-m", self.model_path,
            "-f", audio_path,
            "--no-timestamps",
            "--prompt", final_prompt,
            "-t", str(self.threads),
            # Accuracy enhancement flags
            "--beam-size", str(self.beam_size),
            "--best-of", str(self.best_of),
            "--temperature", str(self.temperature),
            # Temperature fallback: if decoding fails, increment temperature and retry
            "--temperature-inc", str(self.temperature_fallback),
            # Performance optimizations
            "--no-prints",  # Suppress progress output (faster)
            "--no-fallback",  # Don't retry with temperature (faster, slight accuracy tradeoff)
        ]
        if _flag_ok("--entropy-thold"):
            cmd += ["--entropy-thold", str(self.entropy_threshold)]
        if _flag_ok("--no-speech-thold"):
            cmd += ["--no-speech-thold", str(self.no_speech_threshold)]
        
        # Add language flag (skip if auto-detect)
        if self.language and self.language.lower() != "auto":
            cmd.extend(["--language", self.language])
        
        # Hallucination suppression flags
        # Use --suppress-nst to suppress non-speech tokens (works in current whisper.cpp)
        if self.suppress_nst and _flag_ok("--suppress-nst"):
            cmd.append("--suppress-nst")
        
        # GPU is enabled by default in Vulkan builds of whisper.cpp
        # Use --no-gpu to disable it if user doesn't want GPU acceleration
        if not self.use_gpu and _flag_ok("--no-gpu"):
            cmd.append("--no-gpu")

        # GPU device selection is handled at app startup via setup_gpu_environment()
        # which sets GGML_VK_VISIBLE_DEVICES in os.environ.
        # Subprocesses automatically inherit this - no extra logic needed here.

        # Log which GPU device is being used (if set)
        gpu_device = os.environ.get("GGML_VK_VISIBLE_DEVICES")
        if gpu_device and self.use_gpu:
            print(f"[Transcription] Using GPU device {gpu_device}")

        # Attempt order: active binary, then the CPU sibling (Flatpak bundles both).
        # A Vulkan binary that crashes (signal death), hangs, or errors out with a
        # Vulkan-specific message degrades to CPU transparently — the user gets their
        # transcription either way, and the choice is memoized for this session.
        attempts = [active_binary]
        if active_binary == self.whisper_binary:
            _fb = _cpu_fallback_binary(self.whisper_binary)
            if _fb:
                attempts.append(_fb)

        def _activate_fallback(reason: str) -> None:
            _CPU_FALLBACK_ACTIVE[self.whisper_binary] = attempts[-1]
            delay = _schedule_gpu_retry(self.whisper_binary)
            _log_gpu_event(
                f"⚠️ GPU transcription {reason} — switched to CPU "
                f"(will retry GPU in {_fmt_delay(delay)})"
            )

        result = None
        for attempt_idx, binary in enumerate(attempts):
            cmd[0] = binary
            is_last = attempt_idx == len(attempts) - 1
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    # No env= needed - subprocess inherits GGML_VK_VISIBLE_DEVICES from parent
                )
            except subprocess.TimeoutExpired:
                if is_last:
                    raise TranscriptionError(f"Transcription timed out after {self.timeout} seconds")
                _activate_fallback(f"hung past {self.timeout}s")
                continue
            except FileNotFoundError:
                raise TranscriptionError(f"Could not execute whisper.cpp: {binary}")

            if not is_last:
                died_by_signal = result.returncode < 0
                vulkan_error = result.returncode != 0 and "vulkan" in (result.stderr or "").lower()
                if died_by_signal:
                    _activate_fallback(f"died with signal {-result.returncode}")
                    continue
                if vulkan_error:
                    _activate_fallback("failed with a Vulkan error")
                    continue
            break

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

    def _maybe_probe_gpu_recovery(self) -> None:
        """Spawn a background GPU recovery probe if the retry window has elapsed.

        Never blocks or risks the caller's transcription — the current dictation
        proceeds on CPU while the probe tests the GPU binary off to the side.
        """
        state = _gpu_retry_state.get(self.whisper_binary)
        if state is None or time.time() < state["next_retry"]:
            return
        global _gpu_probe_inflight
        with _gpu_probe_lock:
            if _gpu_probe_inflight:
                return
            _gpu_probe_inflight = True
        threading.Thread(
            target=self._probe_gpu_recovery, daemon=True, name="wayfinder-gpu-probe",
        ).start()

    def _probe_gpu_recovery(self) -> None:
        """Test the GPU binary on silence; restore it on success, back off on failure."""
        global _gpu_probe_inflight
        try:
            if self._gpu_probe_succeeds():
                _CPU_FALLBACK_ACTIVE.pop(self.whisper_binary, None)
                # Keep (halved) failure history so a flapping GPU climbs the
                # backoff ladder instead of crash-looping the user's dictations.
                state = _gpu_retry_state.get(self.whisper_binary)
                if state is not None:
                    state["failures"] = state["failures"] // 2
                    state["next_retry"] = 0.0
                _log_gpu_event("✅ GPU transcription restored — back on the GPU binary")
            else:
                delay = _schedule_gpu_retry(self.whisper_binary)
                _log_gpu_event(f"GPU still unavailable — next retry in {_fmt_delay(delay)}")
        finally:
            _gpu_probe_inflight = False

    def _gpu_probe_succeeds(self) -> bool:
        """Run the GPU binary over 0.1s of silence; True iff it exits cleanly."""
        probe_wav = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                probe_wav = f.name
            with wave.open(probe_wav, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\x00\x00" * 1600)
            result = subprocess.run(
                [self.whisper_binary, "-m", self.model_path, "-f", probe_wav,
                 "--no-timestamps"],
                capture_output=True,
                timeout=60,
            )
            return result.returncode == 0
        except Exception:
            return False
        finally:
            if probe_wav:
                try:
                    os.unlink(probe_wav)
                except OSError:
                    pass


class WhisperServerBackend(TranscriptionBackend):
    """
    Whisper.cpp server backend — keeps model loaded in memory for fast inference.

    Instead of spawning whisper-cli per transcription (which reloads the ~1.5GB model
    each time), this uses whisper-server as a persistent HTTP service. The server loads
    the model once and handles requests via POST /inference.

    The backend owns the full server lifecycle: lazy start on first use, health checks,
    restart on failure, and clean shutdown via atexit.
    """

    # Class-level server process shared across instances with same config
    _server_process: Optional[subprocess.Popen] = None
    _server_port: int = 0
    _server_lock = None  # Initialized lazily
    _server_model_path: str = ""
    # Set when every startup attempt failed on this machine — transcribe()
    # then delegates straight to the per-call CLI backend instead of paying
    # the failed-startup cost on every dictation.
    _server_disabled: bool = False

    def __init__(
        self,
        whisper_server_binary: str = "~/whisper.cpp/build/bin/whisper-server",
        model_path: str = "~/whisper.cpp/models/ggml-large-v3-turbo.bin",
        port: int = 8178,
        threads: int = 4,
        timeout: int = 120,
        use_gpu: bool = True,
        beam_size: int = 3,
        best_of: int = 2,
        language: str = "en",
        entropy_threshold: float = 2.6,
        no_speech_threshold: float = 0.5,
        suppress_nst: bool = False,
        prompt: str = "",
        custom_vocabulary: list = None,
        # Ignored params (kept for interface compatibility)
        temperature: float = 0.0,
        temperature_fallback: float = 0.0,
        gpu_layers: int = 0,
    ):
        self.whisper_server_binary = os.path.expanduser(whisper_server_binary)
        self.model_path = os.path.expanduser(model_path)
        self.port = port
        self.threads = threads
        self.timeout = timeout
        self.use_gpu = use_gpu
        self.beam_size = beam_size
        self.best_of = best_of
        self.language = language
        self.entropy_threshold = entropy_threshold
        self.no_speech_threshold = no_speech_threshold
        self.suppress_nst = suppress_nst
        self.prompt = prompt
        self.custom_vocabulary = custom_vocabulary or []

        # Lazy-init the class-level lock
        if WhisperServerBackend._server_lock is None:
            import threading
            WhisperServerBackend._server_lock = threading.Lock()

    def _build_prompt(self, context: str = "") -> str:
        """Build the prompt string (same logic as WhisperCppBackend)."""
        parts = []
        if self.prompt:
            parts.append(self.prompt)
        if context:
            parts.append(context)
        if self.custom_vocabulary:
            vocab_str = ", ".join(self.custom_vocabulary[:50])
            parts.append(f"Vocabulary: {vocab_str}")
        return " ".join(parts) if parts else ""

    def _is_our_server(self, port: int) -> bool:
        """Check if a whisper-server is running on the given port."""
        try:
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{port}/")
            resp = urllib.request.urlopen(req, timeout=2)
            # whisper-server returns an HTML page at root
            content = resp.read().decode("utf-8", errors="ignore")
            return "whisper" in content.lower()
        except Exception:
            return False

    def _find_available_port(self) -> int:
        """Find the configured port or the next available one."""
        import socket
        port = self.port
        for attempt in range(5):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    return port  # Port is free
                # Port in use — check if it's our server
                if self._is_our_server(port):
                    return port  # Reuse existing server
                port += 1  # Try next port
            finally:
                sock.close()
        return self.port  # Fall back to configured port

    def _server_cmd_attempts(self, port: int) -> list:
        """Ordered list of server spawn commands to try.

        whisper-server's flag set varies by release — v1.7.2 (the Flatpak's
        pinned build, whose server is examples/server installed under the
        whisper-server name) lacks -nth and -sns, and an unknown flag makes it
        print usage and exit, killing dictation entirely. And on machines with
        broken Vulkan the GPU build can die during model load. So the ladder is:
        full modern flags -> v1.7.2-safe flags -> v1.7.2-safe + no-GPU (the same
        degradation whisper-cli's binary auto-fallback provides).
        """
        base = [
            self.whisper_server_binary,
            "-m", self.model_path,
            "-t", str(self.threads),
            "--port", str(port),
            "-l", self.language if self.language and self.language.lower() != "auto" else "en",
            "-bs", str(self.beam_size),
            "-bo", str(self.best_of),
            "-et", str(self.entropy_threshold),
            "-nf",  # No fallback (faster)
        ]
        if not self.use_gpu:
            base = base + ["-ng"]

        full = list(base)
        full.extend(["-nth", str(self.no_speech_threshold)])
        if self.suppress_nst:
            full.append("-sns")

        attempts = [full, base]
        if self.use_gpu:
            attempts.append(base + ["-ng"])  # broken-Vulkan rescue: CPU server
        # A Vulkan-built server can crash at LIBRARY INIT even with -ng (ggml
        # enumerates Vulkan devices before no-gpu is honored — observed SIGSEGV
        # on the Deck's RDNA2 in-sandbox), so no flag can save it. The Flatpak
        # ships a separate CPU-only server binary for exactly this; try it last
        # so instant (model-resident) transcription survives broken Vulkan.
        cpu_server = self.whisper_server_binary.replace("whisper-server", "whisper-server-cpu")
        if cpu_server != self.whisper_server_binary and Path(cpu_server).exists():
            attempts.append([cpu_server] + base[1:])
        return attempts

    def _start_server(self) -> None:
        """Start the whisper-server process if not already running."""
        with WhisperServerBackend._server_lock:
            # Check if already running with matching model
            if (WhisperServerBackend._server_process is not None
                    and WhisperServerBackend._server_process.poll() is None
                    and WhisperServerBackend._server_model_path == self.model_path):
                return

            # Kill any existing server with different config
            self._stop_server_internal()

            port = self._find_available_port()

            # Check if an existing compatible server is already on this port
            if self._is_our_server(port):
                WhisperServerBackend._server_port = port
                WhisperServerBackend._server_model_path = self.model_path
                print(f"[Whisper Server] Reusing existing server on port {port}")
                return

            import atexit
            import time

            last_error = ""
            for attempt, cmd in enumerate(self._server_cmd_attempts(port)):
                if attempt:
                    print(f"[Whisper Server] Start failed ({last_error.strip()[-200:]}) — "
                          f"retrying with reduced flags (attempt {attempt + 1})...")
                else:
                    print(f"[Whisper Server] Starting on port {port}...")

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                WhisperServerBackend._server_process = proc
                WhisperServerBackend._server_port = port
                WhisperServerBackend._server_model_path = self.model_path

                # Register atexit handler for cleanup (idempotent: shutdown is
                # safe to call multiple times)
                atexit.register(WhisperServerBackend.shutdown)

                # Wait for server to be ready (model loading takes a few seconds)
                died = False
                for i in range(60):  # Wait up to 30 seconds
                    time.sleep(0.5)
                    if self._is_our_server(port):
                        print(f"[Whisper Server] Ready on port {port} (took {(i+1)*0.5:.1f}s)")
                        return
                    if proc.poll() is not None:
                        last_error = proc.stdout.read().decode("utf-8", errors="ignore")[-500:]
                        died = True
                        break

                if not died:
                    # Startup hang — reduced flags won't fix that; stop trying.
                    self._stop_server_internal()
                    WhisperServerBackend._server_disabled = True
                    raise TranscriptionError("whisper-server timed out during startup (30s)")

            WhisperServerBackend._server_process = None
            WhisperServerBackend._server_model_path = ""
            WhisperServerBackend._server_disabled = True
            raise TranscriptionError(f"whisper-server failed to start: {last_error}")

    @classmethod
    def _stop_server_internal(cls) -> None:
        """Internal: stop the server process without acquiring the lock."""
        if cls._server_process is not None:
            import signal, time
            try:
                cls._server_process.send_signal(signal.SIGTERM)
                try:
                    cls._server_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cls._server_process.kill()
                    cls._server_process.wait(timeout=2)
            except Exception:
                pass
            cls._server_process = None
            cls._server_model_path = ""

    @classmethod
    def shutdown(cls) -> None:
        """Stop the whisper-server process. Safe to call multiple times."""
        if cls._server_lock is not None:
            with cls._server_lock:
                cls._stop_server_internal()
        else:
            cls._stop_server_internal()

    def is_available(self) -> bool:
        """Check if the whisper-server binary exists."""
        return Path(self.whisper_server_binary).exists() and Path(self.model_path).exists()

    def get_name(self) -> str:
        return "whisper.cpp (server)"

    def supports_gpu(self) -> bool:
        return True

    def warm_up(self) -> None:
        """Pre-load the model into the server so the FIRST dictation is instant.

        Safe to call from a background thread at app startup. Swallows failures
        (a broken warm-up must never block launch — the lazy start in transcribe()
        is the fallback). No-op if the binary/model aren't present.
        """
        if not self.is_available():
            return
        try:
            self._start_server()
        except Exception as e:
            print(f"[Whisper Server] Warm-up skipped: {e}")

    def _cli_fallback(self) -> "WhisperCppBackend":
        """Per-call whisper-cli backend used when the server can't run here.

        The CLI backend has its own GPU->CPU binary auto-fallback, so the full
        degradation ladder is: server(GPU) -> server(CPU) -> cli(GPU) -> cli(CPU)
        — slower per dictation but always working.
        """
        if getattr(self, "_cli_backend", None) is None:
            self._cli_backend = WhisperCppBackend(
                whisper_binary=self.whisper_server_binary.replace("whisper-server", "whisper-cli"),
                model_path=self.model_path,
                prompt=self.prompt,
                threads=self.threads,
                timeout=self.timeout,
                use_gpu=self.use_gpu,
                beam_size=self.beam_size,
                best_of=self.best_of,
                language=self.language,
                entropy_threshold=self.entropy_threshold,
                no_speech_threshold=self.no_speech_threshold,
                custom_vocabulary=self.custom_vocabulary,
                suppress_nst=self.suppress_nst,
            )
        return self._cli_backend

    def transcribe(self, audio_path: str, context: str = "") -> str:
        """Transcribe audio via the whisper-server HTTP API."""
        if not Path(audio_path).exists():
            raise TranscriptionError(f"Audio file not found: {audio_path}")

        # Server already proved unusable on this machine — go straight to CLI.
        if WhisperServerBackend._server_disabled:
            return self._cli_fallback().transcribe(audio_path, context)

        # Ensure server is running
        try:
            self._start_server()
        except TranscriptionError as e:
            print(f"[Whisper Server] Unusable on this machine ({e}). "
                  "Falling back to whisper-cli per dictation.")
            return self._cli_fallback().transcribe(audio_path, context)
        port = WhisperServerBackend._server_port

        # Build prompt
        final_prompt = self._build_prompt(context)

        try:
            import urllib.request
            import urllib.parse

            # Build multipart form data
            boundary = "----WayfinderBoundary"
            body = b""

            # Add the audio file
            with open(audio_path, "rb") as f:
                audio_data = f.read()

            body += f"--{boundary}\r\n".encode()
            body += f'Content-Disposition: form-data; name="file"; filename="{os.path.basename(audio_path)}"\r\n'.encode()
            body += b"Content-Type: audio/wav\r\n\r\n"
            body += audio_data
            body += b"\r\n"

            # Add response_format
            body += f"--{boundary}\r\n".encode()
            body += b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            body += b"json\r\n"

            # Add prompt if provided
            if final_prompt:
                body += f"--{boundary}\r\n".encode()
                body += b'Content-Disposition: form-data; name="prompt"\r\n\r\n'
                body += final_prompt.encode("utf-8")
                body += b"\r\n"

            body += f"--{boundary}--\r\n".encode()

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/inference",
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                method="POST",
            )

            resp = urllib.request.urlopen(req, timeout=self.timeout)
            result = json.loads(resp.read().decode("utf-8"))
            text = result.get("text", "").strip()
            return text

        except (urllib.error.URLError, TimeoutError) as e:
            # Server may have died (URLError: connection refused) OR wedged its
            # inference worker while still listening (TimeoutError: the request
            # hangs past self.timeout — observed after a suspend/resume cycle,
            # where GET / still returns 200 but POST /inference never completes).
            # A hung-but-listening server raises TimeoutError, which is NOT a
            # URLError, so it must be caught here explicitly — otherwise it falls
            # through to the generic handler below and the dead server is left
            # running, hanging every subsequent dictation until manually killed.
            print(f"[Whisper Server] {type(e).__name__}: {e}. Restarting server...")
            with WhisperServerBackend._server_lock:
                self._stop_server_internal()
            self._start_server()

            # Retry once
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("text", "").strip()
            except Exception as retry_err:
                raise TranscriptionError(f"whisper-server failed after restart: {retry_err}")

        except Exception as e:
            raise TranscriptionError(f"whisper-server transcription failed: {e}")


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
        no_speech_threshold: float = 0.5,
        compression_ratio_threshold: float = 2.4,
        temperature_fallback: float = 0.0,
        suppress_nst: bool = False,
        vad_enabled: bool = True,
        vad_threshold: float = 0.3,
        gpu_device: str = "auto",
        timeout: float = 300.0,
    ):
        self.model_size = model_size
        self.use_gpu = use_gpu
        self.timeout = timeout  # wall-clock bound for the in-process transcribe+iteration
        self.compute_type = compute_type
        self.prompt = prompt
        self.language = language
        self.beam_size = beam_size
        self.best_of = best_of
        self.temperature = temperature
        self.custom_vocabulary = custom_vocabulary or []
        self.no_speech_threshold = no_speech_threshold
        self.compression_ratio_threshold = compression_ratio_threshold
        self.temperature_fallback = temperature_fallback
        self.suppress_nst = suppress_nst
        self.vad_enabled = vad_enabled
        self.vad_threshold = vad_threshold
        self.gpu_device = gpu_device

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
        """Get or create the Whisper model (cached). Falls back to CPU on GPU OOM."""
        if self._model is not None:
            return self._model

        cache_key = (self.model_size, self.use_gpu, self.compute_type)
        if cache_key in FasterWhisperBackend._model_cache:
            self._model = FasterWhisperBackend._model_cache[cache_key]
            return self._model

        from faster_whisper import WhisperModel

        device = "cuda" if self.use_gpu and self.supports_gpu() else "cpu"
        compute_type = self.compute_type if device == "cuda" else "int8"

        # GPU device selection for multi-GPU systems
        device_index = 0
        if device == "cuda" and self.gpu_device != "auto":
            try:
                device_index = int(self.gpu_device)
            except (ValueError, TypeError):
                device_index = 0

        try:
            self._model = WhisperModel(
                self.model_size,
                device=device,
                device_index=device_index,
                compute_type=compute_type,
            )
        except Exception as e:
            # Fall back to CPU on GPU memory errors or other GPU failures
            if device == "cuda":
                print(f"[Faster-Whisper] GPU failed ({e}), falling back to CPU with int8...")
                try:
                    self._model = WhisperModel(
                        self.model_size,
                        device="cpu",
                        compute_type="int8",
                    )
                except Exception as cpu_e:
                    raise TranscriptionError(f"Failed to load Faster-Whisper model on both GPU and CPU: {cpu_e}")
            else:
                raise TranscriptionError(f"Failed to load Faster-Whisper model: {e}")

        FasterWhisperBackend._model_cache[cache_key] = self._model
        return self._model
    
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
            
            # Build temperature: use fallback list if temperature_fallback > 0
            temp = self.temperature
            if self.temperature_fallback > 0:
                temp = [self.temperature, self.temperature + self.temperature_fallback,
                        self.temperature + self.temperature_fallback * 2]

            # Build VAD parameters when enabled
            vad_params = None
            if self.vad_enabled:
                vad_params = dict(
                    threshold=self.vad_threshold,
                    min_speech_duration_ms=100,
                    min_silence_duration_ms=700,
                    speech_pad_ms=200,
                )

            # faster-whisper returns a lazy generator; the actual work happens during segment
            # iteration, so the timed function must include the iteration. The in-process call
            # blocks in C and can't be killed by a thread timeout — on timeout we raise so the
            # pipeline emits TRANSCRIPTION_ERROR and resets rather than hanging forever.
            def _do_transcribe():
                segments, _info = model.transcribe(
                    audio_path,
                    language=language,
                    initial_prompt=final_prompt,
                    beam_size=self.beam_size,
                    best_of=self.best_of,
                    temperature=temp,
                    vad_filter=self.vad_enabled,
                    vad_parameters=vad_params,
                    condition_on_previous_text=True,
                    no_speech_threshold=self.no_speech_threshold,
                    compression_ratio_threshold=self.compression_ratio_threshold,
                    suppress_blank=self.suppress_nst,
                    word_timestamps=False,
                )
                return " ".join(seg.text.strip() for seg in segments).strip()

            from wayfinder.utils.timeout import run_with_timeout, CallTimeout
            try:
                return run_with_timeout(_do_transcribe, self.timeout)
            except CallTimeout:
                raise TranscriptionError(
                    f"Faster-Whisper transcription timed out after {self.timeout:.0f}s"
                )

        except TranscriptionError:
            raise
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


def warm_up_transcription(config: dict) -> None:
    """Pre-load the transcription model so the first dictation is instant.

    Only does work for backends that benefit from a persistent process — today
    that's whisper-server mode, which loads the ~1.5GB model into VRAM once and
    keeps it resident. For per-invocation backends (whisper-cli) there's nothing
    to warm, so this is a cheap no-op. Designed to be called in a daemon thread
    at app startup; never raises.
    """
    try:
        backend = get_backend(config)
        warm = getattr(backend, "warm_up", None)
        if callable(warm):
            warm()
    except Exception as e:
        print(f"[Transcription] Warm-up skipped: {e}")


def get_backend(config: dict) -> TranscriptionBackend:
    """
    Factory function to create the appropriate transcription backend.

    Args:
        config: Configuration dictionary with transcription settings

    Returns:
        A TranscriptionBackend instance
    """
    backend_type = config.get("transcription_backend", "whisper_cpp")

    # GPU acceleration is a PREMIUM feature — enforce here (the backend factory), not
    # only in the UI, so editing config.json can't unlock GPU without a license. Every
    # GPU-capable backend below uses use_gpu_effective instead of the raw config value.
    try:
        from wayfinder.license import get_feature_gate
        _gpu_allowed = get_feature_gate().has_feature("gpu_acceleration")
    except Exception:
        _gpu_allowed = False
    use_gpu_effective = bool(config.get("use_gpu", True)) and _gpu_allowed

    # Map accuracy_mode to beam_size/best_of overrides
    accuracy_mode = config.get("accuracy_mode", "balanced")
    accuracy_presets = {
        "fast": {"beam_size": 1, "best_of": 1},
        "balanced": {"beam_size": 3, "best_of": 2},
        "high": {"beam_size": 5, "best_of": 3},
    }
    if accuracy_mode in accuracy_presets:
        preset = accuracy_presets[accuracy_mode]
        config = dict(config)  # Don't mutate the original
        config["beam_size"] = preset["beam_size"]
        config["best_of"] = preset["best_of"]

    if backend_type == "faster_whisper":
        return FasterWhisperBackend(
            model_size=config.get("faster_whisper_model", "small"),
            use_gpu=use_gpu_effective,
            compute_type=config.get("faster_whisper_compute_type", "float16"),
            prompt=config.get("prompt", "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next."),
            language=config.get("language", "en"),
            beam_size=config.get("beam_size", 5),
            best_of=config.get("best_of", 3),
            temperature=config.get("temperature", 0.0),
            custom_vocabulary=config.get("custom_vocabulary", []),
            no_speech_threshold=config.get("no_speech_threshold", 0.5),
            compression_ratio_threshold=config.get("compression_ratio_threshold", 2.4),
            temperature_fallback=config.get("temperature_fallback", 0.0),
            suppress_nst=config.get("suppress_nst", False),
            vad_enabled=config.get("faster_whisper_vad_enabled", True),
            vad_threshold=config.get("faster_whisper_vad_threshold", 0.3),
            gpu_device=config.get("gpu_device", "auto"),
            # Wall-clock recovery bound for the in-process call. Generous default (CPU transcribe
            # can be slow) — honored from config if the user sets it.
            timeout=config.get("faster_whisper_timeout", 300.0),
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
        # Default to whisper.cpp — use server mode if enabled (keeps model in memory
        # for instant transcription). Server mode is the default, but it requires the
        # whisper-server binary: the Flatpak bundles it, and a from-source install may
        # only have whisper-cli built. So fall back to the per-invocation CLI backend
        # whenever the server binary is absent — instant where possible, always working.
        if config.get("whisper_server_mode", True):
            server_binary = config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli")
            # Derive server binary path from CLI binary path
            server_binary = server_binary.replace("whisper-cli", "whisper-server")
            server_backend = WhisperServerBackend(
                whisper_server_binary=server_binary,
                model_path=config.get("model_path", "~/whisper.cpp/models/ggml-small.bin"),
                port=config.get("whisper_server_port", 8178),
                threads=config.get("threads", 4),
                timeout=config.get("timeout", 120),
                use_gpu=use_gpu_effective,
                beam_size=config.get("beam_size", 5),
                best_of=config.get("best_of", 3),
                language=config.get("language", "en"),
                entropy_threshold=config.get("entropy_threshold", 2.6),
                no_speech_threshold=config.get("no_speech_threshold", 0.5),
                suppress_nst=config.get("suppress_nst", False),
                prompt=config.get("prompt", ""),
                custom_vocabulary=config.get("custom_vocabulary", []),
            )
            if server_backend.is_available():
                return server_backend
            print("[Transcription] whisper-server binary not found — using whisper-cli "
                  "(per-dictation model load). Build whisper-server for instant mode.")
        return WhisperCppBackend(
            whisper_binary=config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli"),
            model_path=config.get("model_path", "~/whisper.cpp/models/ggml-small.bin"),
            prompt=config.get("prompt", "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next."),
            threads=config.get("threads", 6),
            timeout=config.get("timeout", 120),
            use_gpu=use_gpu_effective,
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
                    print(f"[Post-processing] Using Anthropic ({config.get('anthropic_model', 'claude-3-haiku-20240307')})")
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

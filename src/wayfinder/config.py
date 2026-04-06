"""
Configuration management for Wayfinder Aura.

Handles loading, saving, and defaults for all application settings.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

# Detect runtime environment
IS_FLATPAK = os.environ.get("FLATPAK_ID") is not None or os.environ.get("WAYFINDER_FLATPAK") is not None

# APPIMAGE/APPDIR env vars can leak from parent AppImage processes (e.g. Cursor IDE),
# so verify the APPDIR actually contains our binary before treating it as our AppImage.
_appdir = os.environ.get("APPDIR", "")
IS_APPIMAGE = bool(_appdir) and os.path.exists(os.path.join(_appdir, "usr", "bin", "wayfinder-aura"))
APPDIR = _appdir if IS_APPIMAGE else ""

# Configuration paths
CONFIG_DIR = Path.home() / ".config" / "wayfinder-aura"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Get the package directory (for assets, etc.)
PACKAGE_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = PACKAGE_DIR.parent.parent  # src/wayfinder -> project root

# Socket path for IPC
SOCKET_PATH = "/tmp/wayfinder-aura.sock"

# Handle icon path for Flatpak / AppImage / regular install
if IS_FLATPAK:
    ICON_PATH = Path("/app/share/icons/hicolor/256x256/apps") / f"{os.environ.get('FLATPAK_ID', 'io.github.user.WayfinderAura')}.png"
    if not ICON_PATH.exists():
        ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
elif IS_APPIMAGE and APPDIR:
    _appimage_icon = Path(APPDIR) / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "wayfinder-aura.png"
    ICON_PATH = _appimage_icon if _appimage_icon.exists() else PROJECT_ROOT / "assets" / "icon.png"
else:
    ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"

# Default whisper paths - varies by runtime environment
if IS_FLATPAK:
    _default_whisper_binary = "/app/bin/whisper-cli"
    _default_model_dir = os.environ.get("WHISPER_MODELS_DIR", "/app/share/whisper-models")
    _default_model_path = f"{_default_model_dir}/ggml-small.en.bin"
    # LLM model for post-processing (bundled in Flatpak)
    # Prefer Qwen 3.5 if available, fall back to Qwen 2.5
    _flatpak_llm_new = "/app/share/llm-models/Qwen3.5-2B-Q4_K_M.gguf"
    _flatpak_llm_old = "/app/share/llm-models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    _default_llm_model_path = _flatpak_llm_new if os.path.exists(_flatpak_llm_new) else _flatpak_llm_old
elif IS_APPIMAGE and APPDIR:
    # AppImage uses bundled binaries if they exist, otherwise fall back to system
    _appimage_whisper = os.path.join(APPDIR, "usr", "bin", "whisper-cli")
    _default_whisper_binary = _appimage_whisper if os.path.exists(_appimage_whisper) else "~/whisper.cpp/build/bin/whisper-cli"
    _appimage_model_dir = os.path.join(APPDIR, "usr", "share", "whisper-models")
    if os.path.isdir(_appimage_model_dir):
        _default_model_path = os.path.join(_appimage_model_dir, "ggml-small.en.bin")
    else:
        _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"
    # Prefer Qwen 3.5 if available, fall back to Qwen 2.5
    _appimage_llm_new = os.path.join(APPDIR, "usr", "share", "llm-models", "Qwen3.5-2B-Q4_K_M.gguf")
    _appimage_llm_old = os.path.join(APPDIR, "usr", "share", "llm-models", "qwen2.5-1.5b-instruct-q4_k_m.gguf")
    _user_llm_dir = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models")
    if os.path.exists(_appimage_llm_new):
        _default_llm_model_path = _appimage_llm_new
    elif os.path.exists(_appimage_llm_old):
        _default_llm_model_path = _appimage_llm_old
    elif os.path.exists(os.path.join(_user_llm_dir, "Qwen3.5-2B-Q4_K_M.gguf")):
        _default_llm_model_path = os.path.join(_user_llm_dir, "Qwen3.5-2B-Q4_K_M.gguf")
    else:
        _default_llm_model_path = os.path.join(_user_llm_dir, "qwen2.5-1.5b-instruct-q4_k_m.gguf")
else:
    _default_whisper_binary = "~/whisper.cpp/build/bin/whisper-cli"
    _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"
    # LLM model for post-processing - prefer Qwen 3.5 if available, fall back to Qwen 2.5
    # Use platform-appropriate data dir (macOS: ~/Library/Application Support/, Linux: ~/.local/share/)
    if sys.platform == "darwin":
        _user_llm_dir = str(Path.home() / "Library" / "Application Support" / "wayfinder-aura" / "llm-models")
    else:
        _user_llm_dir = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models")
    _llm_new = os.path.join(_user_llm_dir, "Qwen3.5-2B-Q4_K_M.gguf")
    _llm_old = os.path.join(_user_llm_dir, "qwen2.5-1.5b-instruct-q4_k_m.gguf")
    _default_llm_model_path = _llm_new if os.path.exists(_llm_new) else _llm_old

# Default configuration values
DEFAULT_CONFIG: dict[str, Any] = {
    # Whisper settings
    "whisper_binary": _default_whisper_binary,
    "model_path": _default_model_path,
    
    # Hotkey settings
    "hotkey_key": 61,  # F3 - default (F9=67 on Linux)
    "hotkey_modifiers": [],
    
    # Style toggle hotkey (cycles Minimal → Professional → Casual → Dev → Personal)
    "style_toggle_key": 68,  # F10 default
    "style_toggle_modifiers": [],
    
    # Audio settings
    "audio_device": None,
    "sample_rate": 16000,
    
    # Transcription settings
    "prompt": "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next.",
    "threads": 4,  # Default to 4, auto-adjusted on first run based on CPU cores
    "timeout": 120,
    "min_recording_duration": 0.5,
    
    # UI settings
    "start_minimized": False,
    "enabled_input_devices": [],  # Empty = all devices; otherwise list of device names
    "typing_speed": "instant",  # instant, fast, normal, slow, very_slow
    
    # Processing mode: local (100% private, offline) or remote (cloud APIs for speed/quality)
    "processing_mode": "local",  # local | remote
    
    # Accuracy enhancement settings
    "beam_size": 5,  # Beam search size (1-5 recommended, higher is slow)
    "best_of": 3,  # Number of best candidates to consider
    "language": "en",  # Language code: "en", "auto" for auto-detect
    "entropy_threshold": 2.6,  # Filter low-confidence outputs (higher = accept more)
    "no_speech_threshold": 0.5,  # Silence detection threshold (lower = more sensitive)
    "temperature": 0.0,  # Sampling temperature (0.0 = greedy/deterministic)
    "temperature_fallback": 0.0,  # Temperature increment for retries (0 = no retries)
    "accuracy_mode": "balanced",  # fast | balanced | high
    "audio_preprocessing": "light",  # off | light | medium | heavy
    "ensure_punctuation": False,  # Additional punctuation fixes (optional, most models do this well)
    
    # Vocabulary and hallucination suppression
    "custom_vocabulary": [],  # User's personal terms appended to prompt
    "suppress_nst": False,  # Suppress non-speech tokens (can drop words if True)
    
    # Voice profile learning (auto-enabled when output_tone is "personal")
    "voice_learning_history_limit": 100,  # Max transcriptions to keep in learning history
    "voice_learning_regen_interval": 20,  # Regenerate profile summary every N transcriptions
    
    # Chunked recording settings
    "chunked_mode": True,  # Enable chunked processing for long recordings
    "chunk_duration": 15,  # Seconds per chunk (shorter = faster feedback)
    "chunk_overlap": 2,  # Overlap seconds to avoid word cuts
    "max_recording_duration": 0,  # 0 = unlimited
    
    # GPU acceleration settings
    "transcription_backend": "whisper_cpp",  # whisper_cpp | faster_whisper
    "use_gpu": True,  # Enable GPU acceleration
    "gpu_layers": 0,  # 0 = auto (all layers), or specific layer count for whisper.cpp
    "gpu_device": "auto",  # "auto" = benchmark and pick fastest, or "0", "1", "2" for manual selection
    "gpu_benchmark_cache": {},  # Cached GPU benchmark results: {"0": 0.6, "1": 7.5, "2": 52.0, "fastest": "0"}
    
    # Faster-Whisper specific settings
    "faster_whisper_model": "large-v3-turbo",  # tiny, base, small, medium, large-v3, large-v3-turbo
    "faster_whisper_compute_type": "float16",  # float16, int8, int8_float16
    "faster_whisper_vad_enabled": True,  # Silero VAD for filtering silence (tuned for dictation)
    "faster_whisper_vad_threshold": 0.3,  # VAD sensitivity (lower = more sensitive, less cutting)
    
    # Groq Whisper API settings (ultra-fast cloud transcription)
    # Get API key from: https://console.groq.com/keys
    "groq_whisper_model": "whisper-large-v3",  # whisper-large-v3 (same quality as local, 10x faster)
    "groq_api_key": "",  # Groq API key (stored encrypted, loaded into GROQ_API_KEY env var)
    
    # Floating indicator settings
    "indicator_fps": 0,  # 0 = auto-detect monitor refresh rate, or set manually (60, 120, 144, etc.)
    "overlay_mode": "persistent",  # persistent (no focus steal) | standard (shows/hides, may steal focus)
    "overlay_type": "always_on",  # always_on (PyQt6, stays visible) | disappearing (CTk, shows/hides)
    "overlay_scale": 0.7,  # Overlay scale (separate from UI scale) - 0.5 to 2.0
    "overlay_vertical_offset": 0,  # Vertical offset in pixels (negative = higher, positive = lower)
    
    # Audio ducking settings (reduce other audio while recording)
    "audio_ducking_enabled": True,  # Enable automatic volume reduction during recording
    "audio_ducking_percent": 30,  # How much to lower other audio (0-50, higher = quieter)
    
    # Style settings (5 presets that cycle via hotkey)
    "output_tone": "professional",  # minimal | professional | casual | dev | personal
    "strong_mode": False,  # When True, allows sentence restructuring. When False, preserves user's words.
    "caricature_mode": False,  # 🎭 Secret easter egg! Unlocked by typing "lol" on Style tab.
    
    # Post-processing settings (LLM cleanup)
    "post_processing_enabled": True,  # Enable LLM post-processing
    "post_processing_backend": "llama_cpp",  # llama_cpp | anthropic | openai
    "fast_filler_removal": False,  # When True, use instant regex-based filler removal (no LLM) - best for "minimal" style
    "post_processing_max_tokens": 1024,  # Max tokens for LLM response
    "post_processing_temperature": 0.1,  # LLM temperature (lower = more deterministic)
    
    # llama.cpp post-processing settings
    "llama_cpp_model_path": _default_llm_model_path,  # Path to GGUF model file
    "llama_cpp_binary": "~/llama.cpp/build/bin/llama-cli",  # Path to llama-cli binary (CLI backend)
    "llama_cpp_n_ctx": 2048,  # Context window size
    "llama_cpp_n_threads": 4,  # CPU threads
    "llama_cpp_n_gpu_layers": -1,  # -1 = auto (all layers)
    "llama_cpp_use_cli": True,  # Use CLI backend (faster, no Python bindings needed)
    
    # Cloud API settings (keys stored in config, loaded into environment on startup)
    "anthropic_api_key": "",  # Anthropic API key (for Claude post-processing)
    "anthropic_model": "claude-3-haiku-20240307",  # Claude model to use
    "openai_api_key": "",  # OpenAI API key (for GPT post-processing or Whisper transcription)
    "openai_model": "gpt-4o-mini",  # OpenAI model to use
    "openai_base_url": "",  # Custom base URL for OpenAI-compatible APIs (xAI Grok: "https://api.x.ai/v1")
    
    # Model update checking
    "check_for_model_updates": True,  # Check HuggingFace for newer models on startup (once/day)
    "dismissed_updates": [],  # List of update keys the user dismissed

    # License / Premium
    "premium_url": "https://wayfinder.dev/premium",  # URL for premium purchase page

    # Setup wizard
    "setup_completed": False,  # Set True after first-run wizard finishes (skip or complete)
    
    # Benchmark results - populated by running benchmark
    # Format: {"model_id": {"cpu_10s": 2.5, "gpu_10s": 0.8, "fastest": "gpu", "timestamp": 1234567890}}
    "benchmark_results": {},
    "benchmark_fastest_processor": None,  # "gpu" or "cpu" - auto-detected from benchmarks
    
    # API benchmark results - populated by running API latency test
    # Format: {"openai": {"latency_10s": 2.5, "timestamp": 1234567890}, "anthropic": {...}}
    "api_benchmark_results": {},
}

# Key codes for hotkey configuration
KEY_CODES: dict[str, int] = {
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "space": 57, "enter": 28, "tab": 15, "backspace": 14,
    "scrolllock": 70, "pause": 119,
    # Mouse buttons (BTN_* codes from Linux input)
    "mouse_left": 272,      # BTN_LEFT (0x110)
    "mouse_right": 273,     # BTN_RIGHT (0x111)
    "mouse_middle": 274,    # BTN_MIDDLE (0x112)
    "mouse_side": 275,      # BTN_SIDE (0x113) - often "back" button
    "mouse_extra": 276,     # BTN_EXTRA (0x114) - often "forward" button
    "mouse_forward": 277,   # BTN_FORWARD (0x115)
    "mouse_back": 278,      # BTN_BACK (0x116)
}

# Modifier key codes (left and right variants)
MODIFIER_CODES: dict[str, list[int]] = {
    "ctrl": [29, 97],
    "alt": [56, 100],
    "shift": [42, 54],
    "super": [125, 126],
}


def load_config() -> dict:
    """
    Load configuration from file, merging with defaults.
    
    Returns:
        Configuration dictionary with all settings.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
            # Merge with defaults (user config overrides defaults)
            config = DEFAULT_CONFIG.copy()
            config.update(user_config)

            # Migrate: upgrade weak default prompt to stronger one
            _old_prompt = "Hello, this is a dictation with proper punctuation and grammar."
            if config.get("prompt") == _old_prompt:
                config["prompt"] = DEFAULT_CONFIG["prompt"]

            return config
        except (json.JSONDecodeError, IOError):
            return DEFAULT_CONFIG.copy()
    else:
        # First run - save defaults
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    """
    Save configuration to file.
    
    Args:
        config: Configuration dictionary to save.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_api_keys_to_env(config: dict) -> None:
    """
    Load API keys from config into environment variables.
    
    This should be called on app startup to make API keys available
    to the transcription and post-processing backends.
    
    Args:
        config: Configuration dictionary with API key settings.
    """
    import os
    
    # Map config keys to environment variable names
    api_key_mappings = {
        "groq_api_key": "GROQ_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
    }
    
    for config_key, env_var in api_key_mappings.items():
        key_value = config.get(config_key, "")
        if key_value:
            os.environ[env_var] = key_value


# Human-readable names for mouse buttons
MOUSE_BUTTON_NAMES: dict[int, str] = {
    272: "Mouse Left",
    273: "Mouse Right",
    274: "Mouse Middle",
    275: "Mouse Side",
    276: "Mouse Extra",
    277: "Mouse Forward",
    278: "Mouse Back",
}


def get_key_name(key_code: int) -> str:
    """
    Get the display name for a key code.
    
    Args:
        key_code: evdev key code
        
    Returns:
        Human-readable key name
    """
    # Check mouse buttons first (for nicer display)
    if key_code in MOUSE_BUTTON_NAMES:
        return MOUSE_BUTTON_NAMES[key_code]
    
    for name, code in KEY_CODES.items():
        if code == key_code:
            return name.upper()
    return f"KEY_{key_code}"


def get_modifier_name(modifier: str) -> str:
    """
    Get the display name for a modifier.
    
    Args:
        modifier: Modifier identifier (ctrl, alt, shift, super)
        
    Returns:
        Human-readable modifier name
    """
    names = {
        "ctrl": "Ctrl",
        "alt": "Alt", 
        "shift": "Shift",
        "super": "Super",
    }
    return names.get(modifier, modifier.title())




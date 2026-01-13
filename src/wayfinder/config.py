"""
Configuration management for Wayfinder Aura.

Handles loading, saving, and defaults for all application settings.
"""

import json
import os
from pathlib import Path
from typing import Any

# Detect Flatpak environment
IS_FLATPAK = os.environ.get("FLATPAK_ID") is not None or os.environ.get("WAYFINDER_FLATPAK") is not None

# Configuration paths
CONFIG_DIR = Path.home() / ".config" / "wayfinder-aura"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Get the package directory (for assets, etc.)
PACKAGE_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = PACKAGE_DIR.parent.parent  # src/wayfinder -> project root

# Socket path for IPC
SOCKET_PATH = "/tmp/wayfinder-aura.sock"

# Handle icon path for Flatpak vs regular install
if IS_FLATPAK:
    ICON_PATH = Path("/app/share/icons/hicolor/256x256/apps") / f"{os.environ.get('FLATPAK_ID', 'io.github.user.WayfinderAura')}.png"
    if not ICON_PATH.exists():
        ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
else:
    ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"

# Default whisper paths - Flatpak uses bundled binary and models
if IS_FLATPAK:
    _default_whisper_binary = "/app/bin/whisper-cli"
    _default_model_dir = os.environ.get("WHISPER_MODELS_DIR", "/app/share/whisper-models")
    _default_model_path = f"{_default_model_dir}/ggml-small.en.bin"
    # LLM model for post-processing (bundled in Flatpak)
    _default_llm_model_path = "/app/share/llm-models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
else:
    _default_whisper_binary = "~/whisper.cpp/build/bin/whisper-cli"
    _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"
    # LLM model for post-processing (user's downloaded models)
    _default_llm_model_path = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf")

# Default configuration values
DEFAULT_CONFIG: dict[str, Any] = {
    # Whisper settings
    "whisper_binary": _default_whisper_binary,
    "model_path": _default_model_path,
    
    # Hotkey settings
    "hotkey_key": 67,  # F9 - works reliably on Bazzite/KDE
    "hotkey_modifiers": [],
    
    # Style toggle hotkey (cycles Minimal → Professional → Casual → Dev → Personal)
    "style_toggle_key": 68,  # F10 default
    "style_toggle_modifiers": [],
    
    # Audio settings
    "audio_device": None,
    "sample_rate": 16000,
    
    # Transcription settings
    "prompt": "Hello, this is a dictation with proper punctuation and grammar.",
    "threads": 4,  # Default to 4, auto-adjusted on first run based on CPU cores
    "timeout": 120,
    "min_recording_duration": 0.5,
    
    # UI settings
    "start_minimized": False,
    "enabled_input_devices": [],  # Empty = all devices; otherwise list of device names
    "typing_speed": "instant",  # instant, fast, normal, slow, very_slow
    
    # Processing mode: local (100% private), hybrid (local transcription + cloud post-processing), remote (cloud transcription)
    "processing_mode": "local",  # local | hybrid | remote
    
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
    
    # Floating indicator settings
    "indicator_fps": 0,  # 0 = auto-detect monitor refresh rate, or set manually (60, 120, 144, etc.)
    "overlay_mode": "persistent",  # persistent (no focus steal) | standard (shows/hides, may steal focus)
    "overlay_type": "always_on",  # always_on (PyQt6, stays visible) | disappearing (CTk, shows/hides)
    "overlay_scale": 0.7,  # Overlay scale (separate from UI scale) - 0.5 to 2.0
    
    # Style settings (5 presets that cycle via hotkey)
    "output_tone": "professional",  # minimal | professional | casual | dev | personal
    "strong_mode": False,  # When True, allows sentence restructuring. When False, preserves user's words.
    "caricature_mode": False,  # 🎭 Secret easter egg! Unlocked by typing "lol" on Style tab.
    
    # Post-processing settings (LLM cleanup)
    "post_processing_enabled": True,  # Enable LLM post-processing
    "post_processing_backend": "llama_cpp",  # llama_cpp | ollama | anthropic | openai (llama_cpp is default for self-contained distribution)
    "post_processing_max_tokens": 1024,  # Max tokens for LLM response
    "post_processing_temperature": 0.1,  # LLM temperature (lower = more deterministic)
    
    # llama.cpp post-processing settings
    "llama_cpp_model_path": _default_llm_model_path,  # Path to GGUF model file
    "llama_cpp_n_ctx": 2048,  # Context window size
    "llama_cpp_n_threads": 4,  # CPU threads
    "llama_cpp_n_gpu_layers": -1,  # -1 = auto (all layers)
    
    # Ollama post-processing settings
    "ollama_base_url": "http://localhost:11434",  # Ollama API URL
    "ollama_model": "qwen2.5:1.5b",  # Ollama model name - good balance of speed and quality (~1GB)
    
    # Cloud post-processing settings (API keys read from environment variables only)
    # Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your environment
    "anthropic_model": "claude-3-haiku-20240307",  # Claude model to use
    "openai_model": "gpt-4o-mini",  # OpenAI model to use
    
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




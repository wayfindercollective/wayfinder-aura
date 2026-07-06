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

# Socket path for IPC. In a Flatpak the sandbox has a private /tmp, so the host-side
# trigger (KDE shortcut / Steam-Deck R4 button) can't reach a socket bound there.
# $XDG_RUNTIME_DIR is bind-mounted host<->sandbox (the manifest grants
# --filesystem=xdg-run/wayfinder-aura:create), so bind the socket under it instead.
# Falls back to /tmp where no runtime dir exists (e.g. macOS) — unchanged behavior there.
_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
if _runtime_dir and os.path.isdir(_runtime_dir):
    SOCKET_PATH = os.path.join(_runtime_dir, "wayfinder-aura", "wayfinder-aura.sock")
else:
    SOCKET_PATH = "/tmp/wayfinder-aura.sock"

# Handle icon path for Flatpak / AppImage / regular install
if IS_FLATPAK:
    ICON_PATH = Path("/app/share/icons/hicolor/256x256/apps") / f"{os.environ.get('FLATPAK_ID', 'io.github.wayfindercollective.WayfinderAura')}.png"
    if not ICON_PATH.exists():
        ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"
elif IS_APPIMAGE and APPDIR:
    _appimage_icon = Path(APPDIR) / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "wayfinder-aura.png"
    ICON_PATH = _appimage_icon if _appimage_icon.exists() else PROJECT_ROOT / "assets" / "icon.png"
else:
    ICON_PATH = PROJECT_ROOT / "assets" / "icon.png"

# Preferred local post-processing models, best-first. The June 2026 tone eval
# found Gemma 3 1B the most consistent "gentle guide" cleaner — it reliably
# applies per-tone formatting (e.g. professional "oh thats tight bro" ->
# "Oh, very cool brother.") where Qwen 3.5 2B was inconsistent and LFM2.5 echoed
# the input verbatim. Keep Qwen 3.5 / 2.5 as fallbacks. New models added here are
# picked up automatically by _pick_llm (no per-environment edits needed).
_LLM_PREFERENCE = [
    "google_gemma-3-1b-it-Q4_K_M.gguf",
    "Qwen3.5-2B-Q4_K_M.gguf",
    "qwen2.5-1.5b-instruct-q4_k_m.gguf",
]


def _pick_llm(*dirs: str) -> str:
    """Return the first preferred model that exists across dirs (best-first),
    else the top-preference path in the last dir as a download target."""
    for fname in _LLM_PREFERENCE:
        for d in dirs:
            p = os.path.join(d, fname)
            if os.path.exists(p):
                return p
    return os.path.join(dirs[-1], _LLM_PREFERENCE[0])


# Default whisper paths - varies by runtime environment
if IS_FLATPAK:
    _default_whisper_binary = "/app/bin/whisper-cli"
    _default_model_dir = os.environ.get("WHISPER_MODELS_DIR", "/app/share/whisper-models")
    # base.en is the bundled model and the right Deck-class default (Issues 11/17).
    _default_model_path = f"{_default_model_dir}/ggml-base.en.bin"
    # LLM model for post-processing (bundled in Flatpak, or user-downloaded).
    # Best-first across the bundled dir and the user data dir.
    _user_llm_dir = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models")
    _default_llm_model_path = _pick_llm("/app/share/llm-models", _user_llm_dir)
    # Bundled CPU llama-simple — the subprocess fallback behind the resident
    # llama-cpp-python fast path (both ship in the Flatpak). Host paths like
    # ~/llama.cpp are invisible inside the sandbox.
    _default_llama_binary = "/app/bin/llama-simple"
elif IS_APPIMAGE and APPDIR:
    # AppImage uses bundled binaries if they exist, otherwise fall back to system
    _appimage_whisper = os.path.join(APPDIR, "usr", "bin", "whisper-cli")
    _default_whisper_binary = _appimage_whisper if os.path.exists(_appimage_whisper) else "~/whisper.cpp/build/bin/whisper-cli"
    _appimage_model_dir = os.path.join(APPDIR, "usr", "share", "whisper-models")
    if os.path.isdir(_appimage_model_dir):
        _default_model_path = os.path.join(_appimage_model_dir, "ggml-small.en.bin")
    else:
        _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"
    # Prefer the bundled model, then any user-downloaded one (best-first).
    _appimage_llm_dir = os.path.join(APPDIR, "usr", "share", "llm-models")
    _user_llm_dir = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models")
    _default_llm_model_path = _pick_llm(_appimage_llm_dir, _user_llm_dir)
    _default_llama_binary = "~/llama.cpp/build/bin/llama-cli"
else:
    _default_whisper_binary = "~/whisper.cpp/build/bin/whisper-cli"
    _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"
    # LLM model for post-processing - prefer Qwen 3.5 if available, fall back to Qwen 2.5
    # Use platform-appropriate data dir (macOS: ~/Library/Application Support/, Linux: ~/.local/share/)
    if sys.platform == "darwin":
        _user_llm_dir = str(Path.home() / "Library" / "Application Support" / "wayfinder-aura" / "llm-models")
    else:
        _user_llm_dir = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models")
    _default_llm_model_path = _pick_llm(_user_llm_dir)
    _default_llama_binary = "~/llama.cpp/build/bin/llama-cli"

# Default configuration values
DEFAULT_CONFIG: dict[str, Any] = {
    # Whisper settings
    "whisper_binary": _default_whisper_binary,
    "model_path": _default_model_path,
    
    # Hotkey settings — Super+F2 / Super+F3 by default.
    # Bare F-keys collide with countless game keybinds (e.g. DAoC qbinds);
    # Super+F* almost never does, since DEs reserve the Super key for the
    # compositor. Existing user configs keep whatever they had saved.
    "hotkey_key": 60,  # F2
    "hotkey_modifiers": ["super"],

    # Style toggle hotkey (cycles Minimal → Professional → Casual → Dev → Personal)
    "style_toggle_key": 61,  # F3
    "style_toggle_modifiers": ["super"],
    
    # Exclusive-grab devices (name substrings). Wayfinder takes these input
    # devices exclusively (EVIOCGRAB) so their keys reach ONLY the dictation
    # hotkey listener — nothing leaks to the focused app. Made for MMO-mouse
    # side grids (e.g. Corsair Scimitar's separate "Gaming Mouse Keyboard"
    # interface): the dictation button stops opening browser find bars.
    # The grab is released automatically while a GameMode game is running, so
    # the buttons return to the game, and re-acquired when the game ends.
    "grabbed_input_devices": [],

    # Audio settings
    "audio_device": None,
    "sample_rate": 16000,
    
    # Transcription settings
    "prompt": "I'm going to talk about what I've been working on today. The project is coming along well, and I don't think we'll have any issues. Let's take a look at the details and see what needs to happen next.",
    "threads": 4,  # Default to 4, auto-adjusted on first run based on CPU cores
    "timeout": 120,  # whisper-CLI fallback (per-dictation model load needs headroom)
    # whisper-SERVER request timeout. Deliberately MUCH shorter than the PROCESSING
    # watchdog (processing_timeout_secs, 120s): the server occasionally wedges its
    # inference worker (keeps answering health checks but never returns a result —
    # seen after an audio "input overflow" glitch or suspend/resume). A short request
    # timeout detects the wedge fast, so the backend can restart the server and retry
    # on a healthy one and still PASTE the dictation well before the watchdog abandons
    # the session. GPU transcribes a chunk in 1-3s, so 30s is huge headroom.
    "whisper_server_timeout": 30,
    "min_recording_duration": 0.5,

    # Whisper server mode: keep model loaded in memory for fast inference
    # Instant transcription: keep the model resident in a whisper-server process
    # instead of reloading it per dictation. On by default everywhere; get_backend
    # falls back to the whisper-cli backend automatically if the server binary
    # isn't present (e.g. a from-source install that only built whisper-cli).
    "whisper_server_mode": True,
    "whisper_server_port": 8178,
    
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
    "groq_api_key": "",  # Groq API key (stored in plaintext config.json, chmod 600; loaded into GROQ_API_KEY env var)
    
    # Floating indicator settings
    "indicator_fps": 0,  # 0 = auto-detect monitor refresh rate, or set manually (60, 120, 144, etc.)
    "overlay_mode": "persistent",  # persistent (no focus steal) | standard (shows/hides, may steal focus)
    "overlay_type": "always_on",  # always_on (PyQt6, stays visible) | disappearing (CTk, shows/hides)
    # SteamOS Game Mode dictation (audio cues, no overlay). This module is the single
    # source of DEFAULT_CONFIG — wayfinder_main.py imports it (no mirror to keep in sync).
    "game_mode_dictation": False,
    # Watchdog timeout (s) for a hung PROCESSING state; 0 disables.
    "processing_timeout_secs": 120,
    "overlay_scale": 1.0,  # Overlay scale (separate from UI scale) - 0.5 to 2.0
    "overlay_vertical_offset": 0,  # Vertical offset in pixels (negative = higher, positive = lower)
    "overlay_anchor": "bottom-center",  # {top,bottom}-{left,center,right}
    # Overlay render quality: "high" = the ambient corner wave animates continuously (smoothest
    # look); "performance" = the overlay holds still when idle to save CPU/battery on handhelds.
    # Visual quality is identical in both — only whether the idle wave keeps moving.
    "overlay_quality": "high",

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
    "llama_cpp_binary": _default_llama_binary,  # llama CLI binary (Flatpak: bundled /app/bin/llama-simple)
    "llama_cpp_n_ctx": 2048,  # Context window size
    "llama_cpp_n_threads": 4,  # CPU threads
    "llama_cpp_n_gpu_layers": -1,  # -1 = auto (all layers)
    # Force the llama-simple SUBPROCESS path even when the resident llama-cpp-python
    # wheel is importable. The resident wheel is preferred by default (it stays warm),
    # but a CPU-only / unoptimized wheel can be far slower than a locally-built,
    # GPU(Vulkan)-or-AVX2 `llama-simple`. Set True on a host whose subprocess binary
    # is faster than its installed wheel. Default False = unchanged everywhere,
    # including the Flatpak (which ships no resident wheel and already uses subprocess).
    "post_processing_force_subprocess": False,
    "llama_cpp_use_cli": True,  # Use CLI backend (faster, no Python bindings needed)
    
    # Cloud API settings (keys stored in config, loaded into environment on startup)
    "anthropic_api_key": "",  # Anthropic API key (for Claude post-processing)
    "anthropic_model": "claude-3-haiku-20240307",  # Claude model to use
    "openai_api_key": "",  # OpenAI API key (for GPT post-processing or Whisper transcription)
    "openai_model": "gpt-4o-mini",  # OpenAI model to use
    "openai_whisper_model": "whisper-1",  # OpenAI Whisper transcription model
    "openai_base_url": "",  # Custom base URL for OpenAI-compatible APIs (xAI Grok: "https://api.x.ai/v1")
    
    # Model update checking
    "check_for_model_updates": True,  # Check HuggingFace for newer models on startup (once/day)
    "dismissed_updates": [],  # List of update keys the user dismissed

    # License / Premium
    # "Buy Now" goes straight to the Ultra checkout; "More Info" goes to the landing page.
    # Both live in config so they're a one-line change without a rebuild.
    "premium_url": "https://wayfindercollective.io/checkout/m97bzwd3j9d0628vakzf94mggd8824n9",  # Buy Now / checkout
    "premium_info_url": "https://wayfindercollective.io/aura",  # More Info / landing page
    "premium_price": "$29.99",        # launch price
    "premium_price_regular": "$60",   # regular price (shown struck-through / as "reg.")
    # Free-tier GPU upsell nudge: set True once the user dismisses it for good.
    "gpu_nudge_dismissed": False,

    # Setup wizard
    "setup_completed": False,  # Set True after first-run wizard finishes (skip or complete)
    # In-window welcome tour (distinct from the dependency wizard above); shown once.
    "welcome_completed": False,  # Set True after the first-run welcome pane finishes/skips
    
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

            # Migrate: stale premium/checkout URLs must always follow the current default.
            # A config saved by an old build pins the dead wayfinder.dev URL, so "Buy Now"
            # would open the wrong page after updating. Force any known-old value forward.
            _stale_premium_urls = {
                "https://wayfinder.dev/premium",
                "https://wayfinder.dev/ultra",
                "http://wayfinder.dev/premium",
            }
            if config.get("premium_url") in _stale_premium_urls:
                config["premium_url"] = DEFAULT_CONFIG["premium_url"]

            # Hotkey defaults changed for NEW installs (bare F3/F10 → Super+F2/F3,
            # to dodge in-game F-key collisions; the GameMode pause covers games).
            # In an EXISTING config, every hotkey field the user never explicitly
            # saved must fall to its LEGACY default — never the new one. Otherwise
            # a config with a saved key but unsaved modifiers gets ["super"] merged
            # onto it, silently breaking e.g. a mouse button mapped to bare F3.
            _legacy_hotkeys = {
                "hotkey_key": 61,              # F3 — pre-change default
                "hotkey_modifiers": [],
                "style_toggle_key": 68,        # F10 — pre-change default
                "style_toggle_modifiers": [],
            }
            for _key, _legacy in _legacy_hotkeys.items():
                if _key not in user_config:
                    config[_key] = _legacy

            # Repair colliding combos (recording == style toggle). Merging new
            # default modifiers onto a partially-saved old config could land both
            # actions on one chord; style yields and returns to its legacy default.
            if (config.get("hotkey_key") == config.get("style_toggle_key")
                    and config.get("hotkey_modifiers") == config.get("style_toggle_modifiers")):
                config["style_toggle_key"] = 68
                config["style_toggle_modifiers"] = []

            # Validate critical paths — if saved path doesn't exist, fall back
            # to auto-detected default. Prevents stale paths from a previous
            # environment (e.g. Flatpak /app/bin path after switching to venv,
            # or a saved host ~/llama.cpp path inside the sandbox where only
            # the bundled /app/bin/llama-simple exists).
            _path_keys = ("whisper_binary", "model_path", "llama_cpp_model_path", "llama_cpp_binary")
            for key in _path_keys:
                saved = config.get(key, "")
                if saved and not os.path.exists(os.path.expanduser(saved)):
                    default = DEFAULT_CONFIG.get(key, "")
                    if default and os.path.exists(os.path.expanduser(default)):
                        config[key] = default

            # Audio device: a saved bare INDEX with no matching device name is unreliable.
            # PortAudio/PipeWire renumber devices between sessions (and on PipeWire restart),
            # so an old index can silently point at a different/silent mic — the "no
            # transcription" regression. Treat index-without-name as auto-select; explicit
            # picks always store audio_device_name too, so this only drops stale bare indices.
            if config.get("audio_device") is not None and not config.get("audio_device_name"):
                config["audio_device"] = None

            return config
        except (json.JSONDecodeError, IOError) as e:
            # Don't silently wipe a corrupt config — preserve it for recovery.
            # Best-effort rename to .bak, warn, then fall back to defaults.
            backup = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".bak")
            try:
                os.replace(CONFIG_FILE, backup)
                print(f"WARNING: config file was corrupt ({e}); backed up to {backup} and loaded defaults")
            except OSError as rename_err:
                print(f"WARNING: config file was corrupt ({e}) and could not be backed up ({rename_err}); loaded defaults")
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
    # Atomic write: dump to a temp file, then os.replace() onto the real path so a
    # crash mid-write can never truncate/corrupt the existing config.
    tmp_file = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    with open(tmp_file, "w") as f:
        json.dump(config, f, indent=2)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    # Config holds API keys + the license token in plaintext — restrict to owner-only.
    # Apply before the replace so the mode is in place atomically with the content.
    try:
        os.chmod(tmp_file, 0o600)
    except OSError:
        pass
    os.replace(tmp_file, CONFIG_FILE)


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




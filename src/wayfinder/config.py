"""
Configuration management for Wayfinder Aura.

Handles loading, saving, and defaults for all application settings.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from wayfinder.utils.platform import (
    WAYFINDER_FLATPAK_ID,
    get_steam_platform,
    get_wayfinder_appimage_dir,
    is_wayfinder_flatpak_env,
)

# Detect runtime environment
FLATPAK_APP_ID = WAYFINDER_FLATPAK_ID
IS_FLATPAK = is_wayfinder_flatpak_env()

_appimage_dir = get_wayfinder_appimage_dir()
IS_APPIMAGE = _appimage_dir is not None
APPDIR = str(_appimage_dir) if _appimage_dir else ""

CHUNKED_PROCESSING_MODES = ("off", "auto", "on")


def normalize_chunked_mode(value: object, default: str = "off") -> str:
    """Return the canonical Off/Auto/On recording mode.

    Releases through 1.1.7 stored a boolean.  Treat those values as explicit
    user preferences during migration: False remains Off and True remains On.
    Unknown values fail closed to ``default`` rather than enabling chunking.
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    normalized = str(value or "").strip().lower()
    if normalized in CHUNKED_PROCESSING_MODES:
        return normalized
    return default if default in CHUNKED_PROCESSING_MODES else "off"

def _default_config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wayfinder-aura"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(appdata) / "wayfinder-aura"
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg_config) / "wayfinder-aura"


# Configuration paths
CONFIG_DIR = _default_config_dir()
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
    # A tiny status breadcrumb (tab + state) written next to the socket. The control
    # socket is fire-and-forget with no reply, so this file is how an external harness
    # (tests/test_live_smoke.py) verifies the app actually acted on a command.
    STATUS_PATH = os.path.join(_runtime_dir, "wayfinder-aura", "status.json")
else:
    SOCKET_PATH = "/tmp/wayfinder-aura.sock"
    STATUS_PATH = "/tmp/wayfinder-aura-status.json"

# Handle icon path for Flatpak / AppImage / regular install
if IS_FLATPAK:
    ICON_PATH = Path("/app/share/icons/hicolor/256x256/apps") / f"{FLATPAK_APP_ID}.png"
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
    # Strong/caricature flagship — preferred over the legacy Qwen 2.5 as a
    # default, but Gemma/Qwen3.5 stay first (faster for everyday cleanup).
    "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
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
        _default_model_path = os.path.join(_appimage_model_dir, "ggml-base.en.bin")
    else:
        _default_model_path = "~/whisper.cpp/models/ggml-base.en.bin"
    # Prefer the bundled model, then any user-downloaded one (best-first).
    _appimage_llm_dir = os.path.join(APPDIR, "usr", "share", "llm-models")
    _user_llm_dir = str(Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models")
    _default_llm_model_path = _pick_llm(_appimage_llm_dir, _user_llm_dir)
    _appimage_llama = os.path.join(APPDIR, "usr", "bin", "llama-cli")
    _appimage_llama_simple = os.path.join(APPDIR, "usr", "bin", "llama-simple")
    _default_llama_binary = (
        _appimage_llama if os.path.exists(_appimage_llama)
        else _appimage_llama_simple if os.path.exists(_appimage_llama_simple)
        else "~/llama.cpp/build/bin/llama-cli"
    )
else:
    _default_whisper_binary = "~/whisper.cpp/build/bin/whisper-cli"
    _default_model_path = "~/whisper.cpp/models/ggml-base.en.bin"
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
    
    # Hotkey settings — Ctrl+Alt+Space / Ctrl+Alt+S by default.
    # Chosen 2026-07 over Super+F2: first-run users didn't know what the
    # "Super" key was (launch feedback), and every keyboard labels Ctrl/Alt/
    # Space. Still game-safe: bare F-keys collide with countless game keybinds
    # (e.g. DAoC qbinds) but Ctrl+Alt chords are as rare in games as Super+F*,
    # and the GameMode pause covers the rest. DE conflicts checked: unassigned
    # by default on KDE and GNOME. Existing user configs keep what they saved.
    "hotkey_key": 57,  # Space
    "hotkey_modifiers": ["ctrl", "alt"],

    # Style toggle hotkey (cycles Minimal → Professional → Casual → Dev → Personal).
    # Enter (not a letter): KEY_CODES/display maps carry no letter keys, and
    # Ctrl+Alt+letter chords collide with IDE bindings (e.g. Ctrl+Alt+S).
    "style_toggle_key": 28,  # Enter
    "style_toggle_modifiers": ["ctrl", "alt"],

    # Auto press Enter after dictation (opt-in): dictate → text lands → Enter
    # fires, so chat inputs submit hands-free. Off by default — implicitly
    # sending messages would surprise anyone dictating into a document.
    "press_enter_after_dictation": False,
    
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
    "prompt": "Dictation with natural speech.",
    "threads": 4,  # Default to 4, auto-adjusted on first run based on CPU cores
    "timeout": 120,  # whisper-CLI fallback (per-dictation model load needs headroom)
    # whisper-SERVER request timeout. Keep it below the 120s processing watchdog:
    # the server can wedge its inference worker after an input overflow or resume,
    # while health checks still pass. A resident Turbo request is normally far
    # faster, but can exceed the previous 5s timeout under transient GPU contention.
    # Thirty seconds leaves bounded room for restart/retry and independent CLI
    # salvage instead of turning a recoverable delay into "No speech detected."
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
    
    # Chunked recording settings. Auto keeps short dictations as one Whisper
    # request, then begins background chunks only after 30 seconds. This is the
    # first-run Ultra default; existing boolean preferences migrate to Off/On.
    # The feature gate is still enforced at the recording boundary.
    "chunked_mode": "auto",  # off | auto | on
    "chunk_auto_threshold": 30,  # First chunk boundary in Auto mode (seconds)
    "chunk_duration": 15,  # Empirical balance: fewer ASR boundary errors than 10s
    "chunk_overlap": 2,  # Context re-included at each boundary (word-cut guard)
    # Optional safety cap (seconds). 0 = unlimited. When set, auto-stops and processes
    # like a normal toggle-off. Wired via the RECORDING watchdog in WayfinderApp.
    "max_recording_duration": 0,
    
    # GPU acceleration settings
    "transcription_backend": "whisper_cpp",  # whisper_cpp | faster_whisper | cloud ids
    # When True (default), local backend is forced to whisper.cpp (safe GPU/CPU
    # path on AMD/Intel/Apple/NVIDIA). Recovers mis-set Faster-Whisper-on-CPU.
    # Manual Backend dropdown sets this False. Auto never picks Faster-Whisper
    # (CUDA load can fail closed to slow CPU-large — Manual only).
    "transcription_backend_auto": True,
    # GPU is an explicit Ultra opt-in. Even a newly activated Ultra install starts
    # on CPU until the user enables this toggle; Free is also enforced at runtime.
    "use_gpu": False,
    "gpu_layers": 0,  # 0 = auto (all layers), or specific layer count for whisper.cpp
    "gpu_device": "auto",  # "auto" = benchmark and pick fastest, or "0", "1", "2" for manual selection
    "gpu_benchmark_cache": {},  # Cached GPU benchmark results: {"0": 0.6, "1": 7.5, "2": 52.0, "fastest": "0"}
    
    # Faster-Whisper specific settings
    "faster_whisper_model": "large-v3-turbo",  # tiny, base, small, medium, large-v3, large-v3-turbo
    "faster_whisper_compute_type": "float16",  # float16, int8, int8_float16
    # CTranslate2 CUDA ordinal only (not Vulkan gpu_device). "auto" → device 0.
    "faster_whisper_cuda_device": "auto",
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
    # Master switch for the on-screen status pill. Off = no visual overlay; a
    # tray-only overlay subprocess still hosts the Qt StatusNotifier tray on Linux.
    "overlay_enabled": True,
    # SteamOS Game Mode dictation (audio cues + rumble, no overlay). This module is the
    # single source of DEFAULT_CONFIG — wayfinder_main.py imports it (no mirror to keep in sync).
    "game_mode_dictation": False,
    # When True (default) and Game Mode dictation is active, ASR uses a lighter
    # runtime profile (smaller model if available, accuracy_mode=fast, GPU off by
    # default). Does NOT rewrite the user's saved Desktop model_path on disk.
    "game_mode_light_asr": True,
    "game_mode_accuracy_mode": "fast",
    "game_mode_use_gpu": False,
    # Optional explicit light model path (empty = auto-pick tiny/base next to the
    # user's model, or Flatpak bundled base.en). Only used while GM dictation is on.
    "game_mode_model_path": "",
    "game_mode_faster_whisper_model": "base",
    # When True (default), failed type-injection in Game Mode falls back to
    # clipboard write + Ctrl+V paste. Desktop happy-path typing is unchanged.
    "game_mode_paste_fallback": True,
    # Opt-in (default False): on desktop, when focus drifted away from the
    # record-start window and the injector cannot retarget (ydotool/wtype on
    # Wayland), use clipboard + Ctrl+V instead of typing. May paste into the
    # wrong field if the user intentionally switched apps — leave off unless
    # dogfood shows systematic type-into-wrong-surface after soft-overlay.
    "desktop_paste_on_focus_drift": False,
    # Watchdog timeout (s) for a hung PROCESSING state; 0 disables.
    "processing_timeout_secs": 120,
    "overlay_scale": 1.0,  # Overlay scale (separate from UI scale) - 0.5 to 2.0
    # Negative = higher on screen, positive = lower (can sit near/over the panel).
    # UI slider spans roughly -900..+120 (asymmetric: upward travel needs more range).
    "overlay_vertical_offset": 0,
    "overlay_anchor": "bottom-center",  # {top,bottom}-{left,center,right}
    # Overlay render quality: "high" = the ambient corner wave animates continuously (smoothest
    # look); "performance" = the overlay holds still when idle to save CPU/battery on handhelds.
    # The wave still animates while recording/processing. Decks default to the battery-friendly
    # idle behavior; other platforms retain the existing high-quality default.
    "overlay_quality": "performance" if get_steam_platform() == "deck" else "high",

    # Audio ducking settings (reduce other audio while recording)
    "audio_ducking_enabled": True,  # Enable automatic volume reduction during recording
    "audio_ducking_percent": 30,  # How much to lower other audio (0-50, higher = quieter)
    
    # Style settings (5 presets that cycle via hotkey)
    # Minimal cleanup remains Free; the Style workspace/presets are Ultra.
    "output_tone": "minimal",  # minimal | professional | casual | dev | personal
    "strong_mode": False,  # When True, allows sentence restructuring. When False, preserves user's words.
    "caricature_mode": False,  # 🎭 Secret easter egg! Unlocked by typing "lol" on Style tab.
    
    # Post-processing settings (LLM cleanup)
    # First-run default is raw Whisper output: fastest and least surprising.
    # Existing installs preserve their saved cleanup preference during migration.
    "post_processing_enabled": False,  # Enable LLM post-processing
    "post_processing_backend": "llama_cpp",  # llama_cpp | anthropic | openai
    "fast_filler_removal": False,  # When True, use instant regex-based filler removal (no LLM) - best for "minimal" style
    "post_processing_max_tokens": 1024,  # Max tokens for LLM response
    "post_processing_temperature": 0.1,  # LLM temperature (lower = more deterministic)
    
    # llama.cpp post-processing settings
    "llama_cpp_model_path": _default_llm_model_path,  # Path to GGUF model file
    "llama_cpp_model_requires_feature": None,  # Catalog entitlement metadata
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
    # Chat-template mode for local GGUF cleanup. "auto" applies the model's own
    # chat template when that exact model has been measured across the tone matrix
    # (see _CHAT_TEMPLATE_MODELS); "off" forces the historical raw-completion
    # prompt. There is deliberately no "on" — a template cannot be forced onto an
    # unvalidated model, because a wrong template degrades output silently.
    "llama_cpp_chat_template": "auto",
    
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
    "premium_url": "https://wayfindercollective.io/checkout/aura-ultra",  # Stable Buy Now / checkout alias
    "premium_info_url": "https://wayfindercollective.io/aura",  # More Info / landing page
    "premium_price": "$29.99",        # launch price
    "premium_price_regular": "$60",   # regular price (shown struck-through / as "reg.")
    # Free-tier GPU upsell nudge: set True once the user dismisses it for good.
    "gpu_nudge_dismissed": False,
    # Ultra utilization launch cue ("you can still switch on GPU / better
    # models / …") — dismiss-forever flag.
    "ultra_tips_dismissed": False,

    # Setup wizard
    "setup_completed": False,  # Set True after first-run wizard finishes (skip or complete)
    # In-window welcome tour (distinct from the dependency wizard above); shown once.
    "welcome_completed": False,  # Set True after the first-run welcome pane finishes/skips
    
    # Benchmark results - populated by running benchmark
    # Format: {"model_id": {"cpu_10s": 2.5, "gpu_10s": 0.8, "fastest": "gpu", "timestamp": 1234567890}}
    "benchmark_results": {},
    "benchmark_fastest_processor": None,  # "gpu" or "cpu" - auto-detected from benchmarks

    # Post-processing (LLM cleanup) timings per installed GGUF model
    # Format: {"gemma3-1b": {"model_name": "...", "avg_time": 1.2, "is_current": True, ...}}
    "postprocessing_benchmark_results": {},
    "benchmark_fastest_postprocessor": None,  # model_id of fastest cleanup model

    # Headline pipeline numbers for the user's current setup
    # {asr_time, pp_time, total_time, asr_mode, asr_model_name, pp_model_name, ...}
    "pipeline_benchmark": {},
    
    # API benchmark results - populated by running API latency test
    # Format: {"openai": {"latency_10s": 2.5, "timestamp": 1234567890}, "anthropic": {...}}
    "api_benchmark_results": {},

    # Cloudflare R2 models CDN (Worker base URL, no trailing slash).
    # Empty string disables CDN (Hugging Face catalog fallback only).
    # Override via user config or env WAYFINDER_MODELS_CDN_BASE (preferred for
    # custom domains like https://models.wayfindercollective.io).
    "models_cdn_base": "https://wayfinder-models-cdn.peter-7b5.workers.dev",
    # Optional override for remote model catalog JSON. Empty = {models_cdn_base}/v1/catalog
    "models_catalog_url": "",
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


def _path_exists(value: object) -> bool:
    if value is None:
        return False
    path = os.path.expanduser(str(value).strip())
    if path.startswith("/app/") and not IS_FLATPAK:
        return False
    return bool(path) and os.path.exists(path)


def _runtime_path_allowed(path: str) -> bool:
    """Flatpak bundle paths are meaningful only in the Flatpak runtime."""
    return IS_FLATPAK or not path.startswith("/app/")


def _first_existing_path(candidates: list[object]) -> str | None:
    for candidate in candidates:
        if candidate is None:
            continue
        path = os.path.expanduser(str(candidate).strip())
        if path and _runtime_path_allowed(path) and os.path.exists(path):
            return path
    return None


def _which_runtime_path(name: str) -> str | None:
    """Return a PATH lookup result, ignoring Flatpak-bundled paths outside Flatpak."""
    found = shutil.which(name)
    if found and (IS_FLATPAK or not found.startswith("/app/")):
        return found
    return None


def _usable_model_candidates() -> list:
    """Existing whisper model files the CURRENT license may load, best first.

    Free may load Base/Base.en only. Every other speech model is offered only
    when `large_models` is licensed. The check is lazy and fails closed.
    """
    gate = None
    try:
        from wayfinder.license import get_feature_gate
        gate = get_feature_gate()
    except Exception:
        gate = None

    def _allowed(name: str) -> bool:
        try:
            from wayfinder.license import transcription_model_allowed
            return transcription_model_allowed(name, gate)
        except Exception:
            return name.lower() in ("ggml-base.bin", "ggml-base.en.bin")

    dirs = [Path(os.path.expanduser("~/whisper.cpp/models"))]
    if IS_APPIMAGE and APPDIR:
        dirs.append(Path(APPDIR) / "usr" / "share" / "whisper-models")
    if IS_FLATPAK:
        dirs.append(Path("/app/share/whisper-models"))

    found: list[Path] = []
    for d in dirs:
        try:
            if d.is_dir():
                found.extend(
                    p for p in d.glob("ggml-*.bin") if _allowed(p.name)
                )
        except OSError:
            continue

    # Best quality first within what the license allows.
    _order = ["large-v3-turbo", "large", "medium", "small.en", "small",
              "base.en", "base", "tiny.en", "tiny"]

    def _rank(p: Path) -> int:
        for i, k in enumerate(_order):
            if p.name == f"ggml-{k}.bin":
                return i
        return len(_order)

    return [str(p) for p in sorted(found, key=_rank)]


def enforce_license_config(config: dict, gate) -> list[str]:
    """Repair persisted settings that the current license may not execute.

    This keeps the UI honest on upgrade/downgrade and prevents stale settings
    from initializing a paid runtime path before its backend-level gate runs.
    Runtime factories still enforce every boundary independently.
    Returns the keys changed so callers can persist/log once.
    """
    changed: list[str] = []

    def _has(feature: str) -> bool:
        try:
            return bool(gate is not None and gate.has_feature(feature))
        except Exception:
            return False

    def _set(key: str, value: object) -> None:
        if config.get(key) != value:
            config[key] = value
            changed.append(key)

    if not _has("gpu_acceleration"):
        _set("use_gpu", False)
        _set("game_mode_use_gpu", False)

    if not _has("tone_system"):
        _set("output_tone", "minimal")
        _set("prompt", "Dictation with natural speech.")
        _set("strong_mode", False)
        _set("caricature_mode", False)

    # A previously installed 3B+ cleanup model must not remain executable after
    # downgrade. Prefer an existing Free model beside it; otherwise retain a
    # concrete Free download target. Runtime post-processing repeats this check.
    free_cleanup_model_fallback = None
    cleanup_path = str(config.get("llama_cpp_model_path", "") or "")
    try:
        from wayfinder.core.postprocessor import (
            cleanup_model_allowed,
            free_cleanup_model_fallback,
        )

        cleanup_allowed = cleanup_model_allowed(
            cleanup_path,
            gate,
            config.get("llama_cpp_model_requires_feature"),
        )
    except Exception:
        cleanup_allowed = False
    if not cleanup_allowed:
        default_cleanup = str(DEFAULT_CONFIG.get("llama_cpp_model_path", "") or "")
        fallback = (
            free_cleanup_model_fallback(cleanup_path, default_cleanup)
            if free_cleanup_model_fallback is not None
            else default_cleanup
        )
        _set("llama_cpp_model_path", fallback)
        _set("llama_cpp_model_requires_feature", None)

    # Free is one clear local path: Base/Base.en through whisper.cpp. A missing
    # fallback remains a concrete Base path so Setup can diagnose/download it.
    try:
        from wayfinder.license import transcription_model_allowed
        model_allowed = transcription_model_allowed(config.get("model_path", ""), gate)
    except Exception:
        model_allowed = False
    if not model_allowed:
        current = Path(os.path.expanduser(str(config.get("model_path", "") or "")))
        dirs: list[Path] = []
        if str(current.parent) not in ("", "."):
            dirs.append(current.parent)
        dirs.append(Path(os.path.expanduser("~/whisper.cpp/models")))
        if IS_APPIMAGE and APPDIR:
            dirs.append(Path(APPDIR) / "usr" / "share" / "whisper-models")
        if IS_FLATPAK:
            dirs.append(Path("/app/share/whisper-models"))

        fallback = None
        for directory in dirs:
            for filename in ("ggml-base.en.bin", "ggml-base.bin"):
                candidate = directory / filename
                try:
                    if candidate.exists():
                        fallback = str(candidate)
                        break
                except OSError:
                    continue
            if fallback:
                break
        if fallback is None:
            default = str(DEFAULT_CONFIG.get("model_path", "") or "")
            fallback = default or str(
                (current.parent if str(current.parent) not in ("", ".") else dirs[0])
                / "ggml-base.en.bin"
            )
        _set("model_path", fallback)
        _set("faster_whisper_model", "base")

    backend = str(config.get("transcription_backend", "whisper_cpp") or "whisper_cpp")
    backend_feature = {
        "faster_whisper": "faster_whisper",
        "groq_whisper": "cloud_backends",
        "openai_whisper": "cloud_backends",
    }.get(backend)
    if backend_feature and not _has(backend_feature):
        _set("transcription_backend", "whisper_cpp")
        _set("transcription_backend_auto", True)
        _set("processing_mode", "local")

    if (
        str(config.get("post_processing_backend", "llama_cpp")) in ("anthropic", "openai")
        and not _has("cloud_backends")
    ):
        _set("post_processing_backend", "llama_cpp")

    chunked_mode = normalize_chunked_mode(config.get("chunked_mode"), default="off")
    _set("chunked_mode", chunked_mode)
    if chunked_mode != "off" and not _has("chunked_recording"):
        _set("chunked_mode", "off")

    return changed


def _repair_config_path(key: str, saved: object) -> object:
    """Repair blank/stale critical paths without treating '' as cwd."""
    # AppImage FUSE mount names change on every launch. Old mounts can remain
    # stat-able even after their transport disconnects, so a plain exists()
    # check is not enough: always select this process's bundled CLI first.
    if IS_APPIMAGE and APPDIR:
        if key == "whisper_binary":
            current_appimage_cli = os.path.join(APPDIR, "usr", "bin", "whisper-cli")
            if _path_exists(current_appimage_cli):
                return current_appimage_cli
        elif key == "llama_cpp_binary":
            # LlamaCppCliBackend converts llama-cli to the preferred
            # llama-simple sibling after this repair. Starting from the current
            # AppDir is essential: a disconnected old mount can still pass
            # exists() and otherwise poison cleanup/warm-up after an update.
            current_appimage_llama = os.path.join(APPDIR, "usr", "bin", "llama-cli")
            if _path_exists(current_appimage_llama):
                return current_appimage_llama

    if _path_exists(saved):
        return saved

    default = DEFAULT_CONFIG.get(key, "")
    candidates: list[object] = [default]
    if key == "whisper_binary":
        candidates.extend([
            "~/whisper.cpp/build/bin/whisper-cli",
            "/usr/bin/whisper-cli",
            "/usr/local/bin/whisper-cli",
            "/opt/homebrew/bin/whisper-cli",
            _which_runtime_path("whisper-cli"),
        ])
        if IS_FLATPAK:
            candidates.insert(1, "/app/bin/whisper-cli")
        elif IS_APPIMAGE and APPDIR:
            candidates.insert(1, os.path.join(APPDIR, "usr", "bin", "whisper-cli"))
    elif key == "llama_cpp_binary":
        candidates.extend([
            "~/llama.cpp/build/bin/llama-cli",
            "/usr/bin/llama-cli",
            "/usr/local/bin/llama-cli",
            "/opt/homebrew/bin/llama-cli",
            _which_runtime_path("llama-cli"),
            _which_runtime_path("llama-simple"),
        ])
        if IS_FLATPAK:
            candidates[1:1] = ["/app/bin/llama-simple", "/app/bin/llama-cli"]
        elif IS_APPIMAGE and APPDIR:
            candidates[1:1] = [
                os.path.join(APPDIR, "usr", "bin", "llama-cli"),
                os.path.join(APPDIR, "usr", "bin", "llama-simple"),
            ]
    elif key == "model_path":
        # Auto-select the best INSTALLED model the current license may load.
        # Without this, a fresh free install kept pointing at the (nonexistent,
        # Ultra-gated) default weight and the user had to hand-pick the model
        # they had just downloaded (2026-07 launch feedback).
        candidates.extend(_usable_model_candidates())

    repaired = _first_existing_path(candidates)
    if repaired:
        return repaired

    # Blank paths are dangerous because Path("") points at cwd and can make
    # availability checks lie; a concrete default gives later checks a clear path.
    saved_path = os.path.expanduser(str(saved).strip()) if saved is not None else ""
    if saved is None or not str(saved).strip() or not _runtime_path_allowed(saved_path):
        return default
    return saved


def load_config() -> dict:
    """
    Load configuration from file, merging with defaults.
    
    Returns:
        Configuration dictionary with all settings.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    if CONFIG_FILE.exists():
        try:
            # Secrets may live in config and backups — repair world-readable modes.
            try:
                from wayfinder.utils.fs_security import restrict_owner_only
                for _p in CONFIG_DIR.glob("config.json*"):
                    if _p.is_file():
                        restrict_owner_only(_p)
            except Exception:
                pass
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
            # Merge with defaults (user config overrides defaults)
            config = DEFAULT_CONFIG.copy()
            config.update(user_config)

            # Migrate the old boolean Chunked Mode toggle into the new
            # Off/Auto/On selector. An existing config that somehow predates the
            # key keeps the legacy Off behavior; only a true first run gets Auto.
            if "chunked_mode" in user_config:
                config["chunked_mode"] = normalize_chunked_mode(
                    user_config.get("chunked_mode"), default="off"
                )
            else:
                config["chunked_mode"] = "off"

            # Post-processing changed to opt-in for new installs. Preserve the
            # historical enabled behavior for partial existing configs that do
            # not contain the setting, rather than silently changing an update.
            if "post_processing_enabled" not in user_config:
                config["post_processing_enabled"] = True

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
                "https://wayfindercollective.io/checkout/m97bzwd3j9d0628vakzf94mggd8824n9",
            }
            if config.get("premium_url") in _stale_premium_urls:
                config["premium_url"] = DEFAULT_CONFIG["premium_url"]

            # Migrate the old globally shipped chunk profile. There was no UI for
            # choosing these values, so 10s/1s in a saved config came from the old
            # default rather than a user selection. Golden-audio adversarial runs
            # showed materially more boundary errors at 10s/1s than 15s/2s.
            if (
                config.get("chunk_duration") == 10
                and config.get("chunk_overlap") == 1
            ):
                config["chunk_duration"] = DEFAULT_CONFIG["chunk_duration"]
                config["chunk_overlap"] = DEFAULT_CONFIG["chunk_overlap"]

            # Hotkey defaults changed for NEW installs over time (bare F3/F10 →
            # Super+F2/F3 to dodge in-game F-key collisions → Ctrl+Alt+Space/S
            # for discoverability, 2026-07; the GameMode pause covers games).
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

            # Validate critical paths — if saved path is blank or doesn't exist,
            # fall back to an existing auto-detected path/default. Prevents stale
            # paths from a previous environment (e.g. Flatpak /app/bin after switching to venv,
            # or a saved host ~/llama.cpp path inside the sandbox where only
            # the bundled /app/bin/llama-simple exists).
            _path_keys = ("whisper_binary", "model_path", "llama_cpp_model_path", "llama_cpp_binary")
            for key in _path_keys:
                config[key] = _repair_config_path(key, config.get(key, ""))

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
                try:
                    from wayfinder.utils.fs_security import restrict_owner_only
                    restrict_owner_only(backup)
                except Exception:
                    pass
                print(f"WARNING: config file was corrupt ({e}); backed up to {backup} and loaded defaults")
            except OSError as rename_err:
                print(f"WARNING: config file was corrupt ({e}) and could not be backed up ({rename_err}); loaded defaults")
            return DEFAULT_CONFIG.copy()
    else:
        # First run - save defaults, but repair critical paths first: the static
        # defaults can point at files that don't exist here (e.g. model_path names
        # the Ultra turbo weight while the user has just downloaded base.en) —
        # repair auto-selects what's actually installed and license-usable.
        config = DEFAULT_CONFIG.copy()
        for key in ("whisper_binary", "model_path", "llama_cpp_model_path", "llama_cpp_binary"):
            config[key] = _repair_config_path(key, config.get(key, ""))
        save_config(config)
        return config.copy()


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
        # "⊞ Win" over the Linux-correct "Super": launch feedback showed new
        # users don't know which key Super is; the ⊞ logo + "Win" matches
        # what's printed on nearly every physical keyboard.
        "super": "⊞ Win",
    }
    return names.get(modifier, modifier.title())

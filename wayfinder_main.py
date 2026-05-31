#!/usr/bin/env python3
"""
Wayfinder Aura - Local voice dictation for Linux (Wayland/X11)
Toggle-to-record with whisper.cpp transcription.
"""

import atexit
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import webbrowser
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

import tkinter as tk
import customtkinter as ctk
try:
    import evdev
    from evdev import InputDevice, categorize, ecodes
    HAS_EVDEV = True
except ImportError:
    evdev = None
    InputDevice = None
    HAS_EVDEV = False
from PIL import Image, ImageDraw, ImageFilter
try:
    # pystray probes a GUI tray backend at import time and can raise ValueError (not just
    # ImportError) when no AppIndicator/Ayatana typelib is present — e.g. a Flatpak or
    # SteamOS session without the GTK appindicator chain (STEAMDECK-INSTALL-LOG Issue 3).
    # Guard it so the app still runs (without a tray icon) instead of crashing at startup.
    import pystray
    HAS_PYSTRAY = True
except Exception as _pystray_err:  # backend probe can raise non-ImportError
    pystray = None
    HAS_PYSTRAY = False
    print(f"[Tray] pystray unavailable ({_pystray_err}); running without a tray icon", flush=True)

# D-Bus for Wayland GlobalShortcuts
try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

# pynput for cross-platform hotkeys (macOS/Windows)
try:
    from wayfinder.hotkeys.pynput_listener import pynput_hotkey_listener
    HAS_PYNPUT = True
except ImportError:
    HAS_PYNPUT = False

import sys as _sys
IS_MACOS = _sys.platform == 'darwin'

# SOCKET_PATH is the single source of truth in wayfinder.config (Rule #3) — it resolves
# to $XDG_RUNTIME_DIR/wayfinder-aura/wayfinder-aura.sock (host<->sandbox shared) or /tmp.
from wayfinder.config import SOCKET_PATH
from wayfinder.core.injector import inject_text, InjectionError
from wayfinder.core.recorder import AudioRecorder, ChunkedRecorder, find_best_input_device, list_input_devices, get_input_device_by_name, AudioCalibrator, is_output_device
from wayfinder.core.transcriber import transcribe_with_config, TranscriptionError
from wayfinder.core.postprocessor import process_with_config, get_available_backends, get_tone_options as get_template_names, check_settings_compatibility
from wayfinder.license import get_feature_gate, FeatureGate, PREMIUM_FEATURES, store_license, load_stored_license
from wayfinder.utils.audio_ducker import AudioDucker


# === Configuration ===

# Detect Flatpak environment
IS_FLATPAK = os.environ.get("FLATPAK_ID") is not None or os.environ.get("WAYFINDER_FLATPAK") is not None

CONFIG_DIR = Path.home() / ".config" / "wayfinder-aura"
CONFIG_FILE = CONFIG_DIR / "config.json"
SCRIPT_DIR = Path(__file__).parent.resolve()

# Handle icon path for Flatpak vs regular install
if IS_FLATPAK:
    ICON_PATH = Path("/app/share/icons/hicolor/256x256/apps") / f"{os.environ.get('FLATPAK_ID', 'io.github.user.WayfinderAura')}.png"
    if not ICON_PATH.exists():
        ICON_PATH = SCRIPT_DIR / "assets" / "icon.png"
else:
    ICON_PATH = SCRIPT_DIR / "assets" / "icon.png"

# Default whisper paths - Flatpak uses bundled binary and models
if IS_FLATPAK:
    _default_whisper_binary = "/app/bin/whisper-cli"
    _default_model_dir = os.environ.get("WHISPER_MODELS_DIR", "/app/share/whisper-models")
    # base.en is the bundled model and the right Deck-class default (Issues 11/17).
    _default_model_path = f"{_default_model_dir}/ggml-base.en.bin"
else:
    _default_whisper_binary = "~/whisper.cpp/build/bin/whisper-cli"
    _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"

DEFAULT_CONFIG = {
    "whisper_binary": _default_whisper_binary,
    "model_path": _default_model_path,
    "hotkey_key": 61,  # F3 - default (F9=67 on Linux)
    "hotkey_modifiers": [],
    # Style toggle hotkey (cycles Minimal → Professional → Casual → AI Prompt → Personal)
    "style_toggle_key": 68,  # F10 default
    "style_toggle_modifiers": [],
    "audio_device": None,
    "sample_rate": 16000,
    "prompt": "Hello, this is a dictation with proper punctuation and grammar.",
    "threads": 4,  # Default to 4, auto-adjusted on first run based on CPU cores
    "timeout": 120,
    "min_recording_duration": 0.5,
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
    "transcription_backend": "whisper_cpp",  # whisper_cpp | faster_whisper | groq_whisper | openai_whisper
    "use_gpu": True,  # Enable GPU acceleration
    "gpu_layers": 0,  # 0 = auto (all layers), or specific layer count for whisper.cpp
    "gpu_device": "auto",  # "auto" = benchmark and pick fastest, or "0", "1", "2" for manual selection
    "gpu_benchmark_cache": {},  # Cached GPU benchmark results
    # Faster-Whisper specific settings
    "faster_whisper_model": "large-v3-turbo",  # tiny, base, small, medium, large-v3, large-v3-turbo
    "faster_whisper_compute_type": "float16",  # float16, int8, int8_float16
    # Floating indicator settings
    "indicator_fps": 0,  # 0 = auto-detect monitor refresh rate, or set manually (60, 120, 144, etc.)
    "overlay_mode": "persistent",  # persistent (no focus steal) | standard (shows/hides, may steal focus)
    "overlay_type": "always_on",  # always_on (PyQt6, stays visible) | disappearing (CTk, shows/hides)
    "overlay_scale": 1.0,  # Overlay scale (separate from UI scale) - 0.5 to 2.0
    # Style settings (5 presets that cycle via hotkey)
    "output_tone": "professional",  # minimal | professional | casual | dev | personal
    "strong_mode": False,  # When True, allows sentence restructuring. When False, preserves user's words.
    # Post-processing settings (LLM cleanup)
    "post_processing_enabled": True,  # Enable LLM post-processing
    "post_processing_backend": "llama_cpp",  # llama_cpp | anthropic | openai
    "post_processing_max_tokens": 1024,  # Max tokens for LLM response
    "post_processing_temperature": 0.1,  # LLM temperature (lower = more deterministic)
    # llama.cpp post-processing settings
    "llama_cpp_model_path": "",  # Path to GGUF model file
    "llama_cpp_n_ctx": 2048,  # Context window size
    "llama_cpp_n_threads": 4,  # CPU threads
    "llama_cpp_n_gpu_layers": -1,  # -1 = auto (all layers)
    # Groq Whisper API settings (ultra-fast cloud transcription)
    "groq_whisper_model": "whisper-large-v3",  # whisper-large-v3 (same quality as local, 10x faster)
    # Cloud API settings (keys stored in config, loaded into environment on startup)
    "groq_api_key": "",  # Groq API key (for ultra-fast cloud transcription)
    "openai_api_key": "",  # OpenAI API key (for GPT post-processing or Whisper transcription)
    "openai_whisper_model": "whisper-1",  # OpenAI Whisper model
    "anthropic_api_key": "",  # Anthropic API key (for Claude post-processing)
    "anthropic_model": "claude-3-haiku-20240307",  # Claude model to use
    "openai_model": "gpt-4o-mini",  # OpenAI model to use
    "openai_base_url": "",  # Custom base URL for OpenAI-compatible APIs (xAI Grok: "https://api.x.ai/v1")
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

KEY_CODES = {
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "space": 57, "enter": 28, "tab": 15, "backspace": 14,
    "scrolllock": 70, "pause": 119,
    # Mouse buttons (BTN_* codes from Linux input)
    "mouse_left": 272, "mouse_right": 273, "mouse_middle": 274,
    "mouse_side": 275, "mouse_extra": 276, "mouse_forward": 277, "mouse_back": 278,
}

MODIFIER_CODES = {
    "ctrl": [29, 97], "alt": [56, 100], "shift": [42, 54], "super": [125, 126],
}


def _get_llm_models_dir() -> Path:
    """Get the LLM models directory (platform-aware)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wayfinder-aura" / "llm-models"
    return Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models"


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
            config = DEFAULT_CONFIG.copy()
            config.update(user_config)
            return config
        except (json.JSONDecodeError, IOError):
            return DEFAULT_CONFIG.copy()
    else:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# === GPU Detection ===

class GPUInfo:
    """Detected GPU information."""
    def __init__(self, vendor: str, name: str, driver: str = ""):
        self.vendor = vendor  # "nvidia", "amd", "intel", "apple", "unknown"
        self.name = name
        self.driver = driver

    @property
    def is_nvidia(self) -> bool:
        return self.vendor == "nvidia"

    @property
    def is_amd(self) -> bool:
        return self.vendor == "amd"

    @property
    def is_intel(self) -> bool:
        return self.vendor == "intel"

    @property
    def is_apple(self) -> bool:
        return self.vendor == "apple"

    @property
    def has_gpu(self) -> bool:
        return self.vendor != "unknown"


def detect_gpu() -> GPUInfo:
    """
    Detect the primary GPU on the system.

    Returns:
        GPUInfo with vendor, name, and driver information.
    """
    # macOS: detect Apple Silicon or Intel GPU
    if sys.platform == "darwin":
        try:
            import platform as plat
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=5,
            )
            chip = result.stdout.strip() if result.returncode == 0 else ""
            if "Apple" in chip:
                return GPUInfo("apple", f"{chip} (Metal)", "metal")
            else:
                return GPUInfo("intel", f"{chip} (Metal)", "metal")
        except Exception:
            return GPUInfo("apple", "Apple GPU (Metal)", "metal")

    # Linux: lspci and /sys detection
    try:
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                line_lower = line.lower()
                if "vga" in line_lower or "3d" in line_lower or "display" in line_lower:
                    if "nvidia" in line_lower:
                        return GPUInfo("nvidia", line.split(":")[-1].strip(), "nvidia")
                    elif "amd" in line_lower or "radeon" in line_lower or "advanced micro" in line_lower:
                        return GPUInfo("amd", line.split(":")[-1].strip(), "amdgpu")
                    elif "intel" in line_lower:
                        return GPUInfo("intel", line.split(":")[-1].strip(), "i915")

        # Fallback: Check /sys for GPU info
        gpu_path = Path("/sys/class/drm")
        if gpu_path.exists():
            for card in gpu_path.iterdir():
                if card.name.startswith("card") and not card.name.endswith("-"):
                    vendor_file = card / "device" / "vendor"
                    if vendor_file.exists():
                        vendor_id = vendor_file.read_text().strip()
                        if vendor_id == "0x10de":
                            return GPUInfo("nvidia", "NVIDIA GPU", "nvidia")
                        elif vendor_id == "0x1002":
                            return GPUInfo("amd", "AMD GPU", "amdgpu")
                        elif vendor_id == "0x8086":
                            return GPUInfo("intel", "Intel GPU", "i915")

    except Exception:
        pass

    return GPUInfo("unknown", "Unknown GPU", "")


def get_optimal_thread_count() -> int:
    """Get optimal thread count based on CPU cores.

    On Apple Silicon, uses performance core count capped at 8 (Metal handles GPU work).
    On other platforms, uses 75% of total cores (2-16 range).
    """
    try:
        import platform as _platform
        if sys.platform == "darwin" and _platform.machine() == "arm64":
            try:
                perf_cores = int(subprocess.check_output(
                    ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                    text=True, timeout=5
                ).strip())
                return max(2, min(8, perf_cores))
            except Exception:
                pass
        cpu_count = os.cpu_count() or 4
        return max(2, min(16, int(cpu_count * 0.75)))
    except Exception:
        return 4


# Cache GPU info at module load
_cached_gpu_info: GPUInfo | None = None

def get_gpu_info() -> GPUInfo:
    """Get cached GPU info."""
    global _cached_gpu_info
    if _cached_gpu_info is None:
        _cached_gpu_info = detect_gpu()
    return _cached_gpu_info


# === State Machine ===

class AppState(Enum):
    IDLE = auto()
    RECORDING = auto()
    PROCESSING = auto()
    PASTING = auto()


# === Premium Design System (2025) ===
# Pre-blended colors for depth simulation (Tkinter doesn't support alpha)
# Base: #0D1117 (Deep Ink) - all blends calculated against this

COLORS = {
    # Background layers - calculated elevation via pre-blending
    "bg_base": "#0D1117",           # Deep Ink - main background
    "bg_dark": "#0D1117",           # Alias for compatibility
    "bg_surface": "#161B22",        # Sidebar/panels - slightly elevated
    "bg_card": "#1E1E1F",           # Bento tiles - 7% white on bg
    "bg_hover": "#1E1B26",          # Hover = 10% accent glow (designer spec)
    "bg_elevated": "#2D333B",       # Pressed/elevated elements
    "bg_input": "#13171D",          # Input fields - slightly recessed
    
    # Accent - Soft Violet (desaturated to avoid "vibration")
    "accent": "#A78BFA",            # Soft Violet - primary accent
    "accent_cyan": "#A78BFA",       # Alias for compatibility  
    "accent_glow": "#1E1B26",       # Pre-blended: bg + (A78BFA - bg) * 0.10
    "accent_hover": "#BEA6FF",      # Lighter on hover
    "accent_dim": "#9580E8",        # Pressed/active state
    "accent_bright": "#C4AAFF",     # Bright version for active toggles
    
    # Rim light border - 10% violet pre-blended
    "border_rim": "#1E1B26",        # bg + (A78BFA - bg) * 0.10
    
    # State colors - muted, sophisticated palette
    "state_ready": "#7B8BD9",       # Muted indigo - calm ready state
    "state_recording": "#E8707F",   # Muted rose - warm but not aggressive
    "state_processing": "#E5AC2A",  # Muted gold - warm processing
    "state_typing": "#5DD4A8",      # Muted mint - success/typing
    
    # Legacy state color aliases for compatibility
    "accent_green": "#5DD4A8",      # Muted mint - success
    "accent_red": "#E8707F",        # Muted rose - recording
    "accent_yellow": "#E5AC2A",     # Muted gold - processing
    "accent_blue": "#7B8BD9",       # Muted indigo
    
    # Text hierarchy - calculated for dark bg readability
    "text_bright": "#F0F0F0",       # Maximum contrast (app title only)
    "text_primary": "#E8E8E8",      # Off-white - 90% brightness
    "text_secondary": "#8B8B8F",    # Section labels - muted grey
    "text_muted": "#5C5C60",        # Disabled/hints
    
    # Borders (pre-blended, no alpha)
    "border": "#2D333B",            # Standard borders
    "border_subtle": "#21262D",     # Subtle borders
    "border_light": "#1A1D24",      # Pre-blended 5% white
    "border_glow": "#1E1B26",       # Pre-blended 10% violet
}

# Corner radius design tokens for consistent rounded corners
RADIUS = {
    "sm": 8,    # Small elements (toggles, chips, badges)
    "md": 12,   # Buttons, inputs, dropdowns
    "lg": 24,   # Bento tiles - 24px for squircle feel
    "xl": 28,   # Hero sections, modals
}

# Spacing tokens for Bento grid (designer spec)
SPACING = {
    "gutter": 24,       # Between bento tiles
    "tile_pad": 20,     # Inside tile padding (20px = premium)
    "tile_pad_y": 16,   # Vertical padding inside tiles
}

STATE_COLORS = {
    AppState.IDLE: COLORS["state_ready"],        # Cyan when ready
    AppState.RECORDING: COLORS["state_recording"], # Rose when recording
    AppState.PROCESSING: COLORS["state_processing"], # Amber when processing
    AppState.PASTING: COLORS["state_typing"],    # Emerald when typing
}

STATE_LABELS = {
    AppState.IDLE: "Ready to Record",
    AppState.RECORDING: "Listening...",
    AppState.PROCESSING: "Processing...",
    AppState.PASTING: "Typing...",
}


# === Tooltip Helper ===

class ToolTip:
    """
    Modern hover tooltip for CustomTkinter widgets.
    Styled to match Nano Banana design system.
    """
    
    def __init__(self, widget, text, delay=300):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tooltip_window = None
        self.scheduled_id = None
        
        widget.bind("<Enter>", self.on_enter)
        widget.bind("<Leave>", self.on_leave)
        widget.bind("<ButtonPress>", self.on_leave)
    
    def on_enter(self, event=None):
        self.scheduled_id = self.widget.after(self.delay, self.show_tooltip)
    
    def on_leave(self, event=None):
        if self.scheduled_id:
            self.widget.after_cancel(self.scheduled_id)
            self.scheduled_id = None
        self.hide_tooltip()
    
    def show_tooltip(self):
        if self.tooltip_window:
            return
        
        # Create tooltip window
        self.tooltip_window = tw = ctk.CTkToplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)
        tw.configure(fg_color=COLORS["bg_surface"])
        
        # Modern tooltip frame with subtle styling
        frame = ctk.CTkFrame(
            tw,
            fg_color=COLORS["bg_surface"],
            corner_radius=RADIUS["sm"],
            border_width=1,
            border_color=COLORS["border"],
        )
        frame.pack(fill="both", expand=True)
        
        # Tooltip content
        label = ctk.CTkLabel(
            frame,
            text=self.text,
            font=("Inter", 12),
            text_color=COLORS["text_primary"],
            wraplength=260,
            justify="left",
        )
        label.pack(padx=12, pady=10)
        
        # Position tooltip above widget
        tw.update_idletasks()
        tw_width = tw.winfo_width()
        tw_height = tw.winfo_height()
        screen_width = tw.winfo_screenwidth()
        screen_height = tw.winfo_screenheight()
        
        # Center above widget
        widget_x = self.widget.winfo_rootx()
        widget_width = self.widget.winfo_width()
        x = widget_x + (widget_width - tw_width) // 2
        
        # Position above with gap
        y = self.widget.winfo_rooty() - tw_height - 6
        
        # Keep on screen horizontally
        if x < 10:
            x = 10
        elif x + tw_width > screen_width - 10:
            x = screen_width - tw_width - 10
        
        # If would go off top, show below
        if y < 10:
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        
        tw.wm_geometry(f"+{x}+{y}")
    
    def hide_tooltip(self):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

    def update_text(self, new_text: str):
        """Update the tooltip text dynamically."""
        self.text = new_text


# =============================================================================
# 🎭 CONFETTI ANIMATION (Easter Egg!)
# =============================================================================

class ConfettiOverlay(ctk.CTkToplevel):
    """
    Fun celebration toast for easter eggs!
    Shows as a floating notification instead of covering the whole window.
    
    Click anywhere to dismiss early.
    """
    
    CONFETTI_EMOJIS = ["🎉", "🎊", "✨", "🌟", "💫", "🎭", "🥳", "🎈"]
    
    def __init__(self, parent, num_particles=50, duration_ms=2500):
        super().__init__(parent)
        
        self._destroyed = False  # Track if already destroyed
        
        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        
        # Try to set transparency
        try:
            self.wm_attributes("-alpha", 0.95)
        except:
            pass
        
        # Toast size - compact notification style
        toast_width = 380
        toast_height = 120
        
        # Position in top-center of parent window
        parent_x = parent.winfo_rootx()
        parent_y = parent.winfo_rooty()
        parent_w = parent.winfo_width()
        
        pos_x = parent_x + (parent_w - toast_width) // 2
        pos_y = parent_y + 60  # Below title bar
        
        self.geometry(f"{toast_width}x{toast_height}+{pos_x}+{pos_y}")
        
        # Main container with celebration styling
        self.configure(fg_color="#1A0A1A")  # Deep purple-black
        
        main_frame = ctk.CTkFrame(
            self,
            fg_color="#2D1B2D",  # Dark magenta
            corner_radius=16,
            border_width=2,
            border_color="#FF6B9D",  # Hot pink border
        )
        main_frame.pack(fill="both", expand=True, padx=3, pady=3)
        
        # Animated emoji row
        emoji_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        emoji_frame.pack(fill="x", pady=(12, 4))
        
        import random
        emojis = random.sample(self.CONFETTI_EMOJIS, 5)
        self.emoji_labels = []
        for emoji in emojis:
            lbl = ctk.CTkLabel(
                emoji_frame,
                text=emoji,
                font=("Inter", 20),
                text_color="#FFFFFF",
            )
            lbl.pack(side="left", expand=True)
            self.emoji_labels.append(lbl)
        
        # Title
        ctk.CTkLabel(
            main_frame,
            text="🎭 CARICATURE MODE UNLOCKED! 🎭",
            font=("Inter", 16, "bold"),
            text_color="#FF6B9D",
        ).pack(pady=(4, 2))
        
        # Subtitle
        ctk.CTkLabel(
            main_frame,
            text="Get ready for some silly fun! (click to dismiss)",
            font=("Inter", 11),
            text_color="#B8A0B8",
        ).pack(pady=(0, 8))
        
        # Click anywhere to dismiss
        self.bind("<Button-1>", self._dismiss)
        main_frame.bind("<Button-1>", self._dismiss)
        emoji_frame.bind("<Button-1>", self._dismiss)
        # Also bind Escape key
        self.bind("<Escape>", self._dismiss)
        self.bind("<Key>", self._dismiss)
        
        # Animate emojis
        self._frame = 0
        self._animate_emojis()
        
        # Auto-destroy after duration
        self.after(duration_ms, self._safe_destroy)
        
        # Safety fallback: destroy after 5 seconds no matter what
        self.after(5000, self._force_destroy)
    
    def _animate_emojis(self):
        """Animate emoji bounce effect."""
        if self._destroyed:
            return
        
        try:
            import math
            for i, lbl in enumerate(self.emoji_labels):
                # Staggered bounce
                offset = math.sin((self._frame + i * 3) * 0.2) * 2
                lbl.configure(font=("Inter", 20 + int(offset)))
            
            self._frame += 1
            if self._frame < 150:  # ~2.5 seconds at 60fps
                self.after(16, self._animate_emojis)
        except Exception:
            pass
    
    def _dismiss(self, event=None):
        """Dismiss the toast when clicked or key pressed."""
        self._safe_destroy()
    
    def _safe_destroy(self):
        """Safely destroy the window, preventing double-destroy errors."""
        if self._destroyed:
            return
        self._destroyed = True
        try:
            self.destroy()
        except Exception:
            pass
    
    def _force_destroy(self):
        """Force destroy as a safety fallback."""
        if not self._destroyed:
            self._safe_destroy()


class CompatibilityBanner(ctk.CTkFrame):
    """
    A banner that displays model compatibility warnings and recommendations.
    
    Shows when the selected post-processing model can't handle the
    requested intensity level, with actionable upgrade suggestions.
    """
    
    # Severity colors
    SEVERITY_COLORS = {
        "ok": None,  # Hidden when ok
        "warning": "#3D3520",  # Muted amber background
        "incompatible": "#3D2020",  # Muted red background
    }
    
    SEVERITY_BORDER_COLORS = {
        "ok": COLORS["border"],
        "warning": "#E5AC2A",  # Muted gold
        "incompatible": "#E87070",  # Muted rose
    }
    
    SEVERITY_ICONS = {
        "ok": "✓",
        "warning": "⚠",
        "incompatible": "⚠",
    }
    
    def __init__(self, parent, **kwargs):
        """
        Create a compatibility banner.
        
        Args:
            parent: Parent widget
        """
        super().__init__(
            parent,
            fg_color=COLORS["bg_input"],
            corner_radius=RADIUS["sm"],
            border_width=1,
            border_color=COLORS["border"],
            **kwargs
        )
        
        self._visible = False
        
        # Main content container
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(fill="x", padx=12, pady=10)
        
        # Header row with icon and title
        self.header = ctk.CTkFrame(self.content, fg_color="transparent")
        self.header.pack(fill="x")
        
        self.icon_label = ctk.CTkLabel(
            self.header,
            text="⚠",
            font=("Inter", 14),
            text_color=COLORS["accent_yellow"],
        )
        self.icon_label.pack(side="left", padx=(0, 8))
        
        self.title_label = ctk.CTkLabel(
            self.header,
            text="Model Compatibility",
            font=("Inter", 12, "bold"),
            text_color=COLORS["text_bright"],
        )
        self.title_label.pack(side="left")
        
        # Issue description
        self.issue_label = ctk.CTkLabel(
            self.content,
            text="",
            font=("Inter", 11),
            text_color=COLORS["text_secondary"],
            wraplength=320,
            justify="left",
        )
        self.issue_label.pack(anchor="w", pady=(6, 0))
        
        # Recommendations frame
        self.recommendations_frame = ctk.CTkFrame(self.content, fg_color="transparent")
        self.recommendations_frame.pack(fill="x", pady=(8, 0))
        
        # Model suggestion with copy button
        self.suggestion_frame = ctk.CTkFrame(
            self.content,
            fg_color=COLORS["bg_elevated"],
            corner_radius=RADIUS["sm"],
        )
        self.suggestion_frame.pack(fill="x", pady=(8, 0))
        
        self.suggestion_label = ctk.CTkLabel(
            self.suggestion_frame,
            text="",
            font=("JetBrains Mono", 11),
            text_color=COLORS["accent"],
        )
        self.suggestion_label.pack(side="left", padx=10, pady=8)
        
        # Initially hidden
        self.pack_forget()
    
    def update_status(self, compatibility: dict) -> None:
        """
        Update the banner based on compatibility check results.
        
        Args:
            compatibility: Dict from check_settings_compatibility() with:
                - is_compatible: bool
                - issues: list of strings
                - recommendations: list of strings
                - upgrade_message: str or None
                - severity: "ok" | "warning" | "incompatible"
        """
        severity = compatibility.get("severity", "ok")
        
        if severity == "ok" or not compatibility.get("issues"):
            # Hide banner when everything is compatible
            self._visible = False
            self.pack_forget()
            return
        
        # Show and update banner
        self._visible = True
        
        # Update colors based on severity
        bg_color = self.SEVERITY_COLORS.get(severity, COLORS["bg_input"])
        border_color = self.SEVERITY_BORDER_COLORS.get(severity, COLORS["border"])
        icon = self.SEVERITY_ICONS.get(severity, "⚠")
        
        self.configure(fg_color=bg_color or COLORS["bg_input"], border_color=border_color)
        self.icon_label.configure(
            text=icon,
            text_color=border_color,
        )
        
        # Update issue text
        issues = compatibility.get("issues", [])
        if issues:
            issue_text = issues[0]
            if len(issues) > 1:
                issue_text += f" (+{len(issues)-1} more)"
            self.issue_label.configure(text=issue_text)
        
        # Update recommendations
        for widget in self.recommendations_frame.winfo_children():
            widget.destroy()
        
        recommendations = compatibility.get("recommendations", [])
        for rec in recommendations[:3]:  # Show max 3 recommendations
            rec_label = ctk.CTkLabel(
                self.recommendations_frame,
                text=f"• {rec}",
                font=("Inter", 10),
                text_color=COLORS["text_muted"],
                wraplength=300,
                justify="left",
            )
            rec_label.pack(anchor="w", pady=1)
        
        # Update model suggestion
        upgrade_message = compatibility.get("upgrade_message")
        if upgrade_message:
            self.suggestion_label.configure(text=f"💡 {upgrade_message[:60]}...")
            self.suggestion_frame.pack(fill="x", pady=(8, 0))
        else:
            self.suggestion_frame.pack_forget()
        
        # Show the banner
        self.pack(fill="x", pady=(8, 0))
    
    def hide(self) -> None:
        """Hide the banner."""
        self._visible = False
        self.pack_forget()
    
    def is_visible(self) -> bool:
        """Check if the banner is currently visible."""
        return self._visible


class ModeSelector(ctk.CTkFrame):
    """
    Segmented control widget for selecting processing mode.
    Two mutually exclusive options: Local | Remote
    """
    
    def __init__(
        self,
        parent,
        values: list[str] = None,
        current_value: str = "local",
        command=None,
        **kwargs
    ):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self.values = values or ["local", "remote"]
        self.current_value = current_value
        self.command = command
        self.buttons: dict[str, ctk.CTkButton] = {}
        
        # Display labels and icons for each mode
        self.display_info = {
            "local": {"label": "Local", "icon": "🔒", "desc": "100% Private"},
            "remote": {"label": "Remote", "icon": "☁️", "desc": "Cloud Processing"},
        }
        
        # Create container with pill background
        self.container = ctk.CTkFrame(
            self,
            fg_color=COLORS["bg_input"],
            corner_radius=RADIUS["md"],
            height=44,
        )
        self.container.pack(fill="x", padx=0, pady=0)
        self.container.pack_propagate(False)
        
        # Inner frame for buttons
        inner = ctk.CTkFrame(self.container, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=3, pady=3)
        
        # Configure columns
        for i in range(len(self.values)):
            inner.grid_columnconfigure(i, weight=1)
        inner.grid_rowconfigure(0, weight=1)
        
        # Create buttons
        for i, value in enumerate(self.values):
            info = self.display_info.get(value, {"label": value.title(), "icon": "", "desc": ""})
            text = f"{info['icon']}  {info['label']}"
            
            is_selected = value == self.current_value
            btn = ctk.CTkButton(
                inner,
                text=text,
                font=("Inter", 13, "bold" if is_selected else "normal"),
                fg_color=COLORS["bg_card"] if is_selected else "transparent",
                hover_color=COLORS["bg_hover"],
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                corner_radius=RADIUS["sm"],
                height=36,
                command=lambda v=value: self._on_select(v),
            )
            btn.grid(row=0, column=i, sticky="nsew", padx=1)
            self.buttons[value] = btn
    
    def _on_select(self, value: str) -> None:
        """Handle button selection."""
        if value == self.current_value:
            return
        
        old_value = self.current_value
        self.current_value = value
        
        # Update all button styles
        for v, btn in self.buttons.items():
            is_selected = v == value
            btn.configure(
                fg_color=COLORS["bg_card"] if is_selected else "transparent",
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                font=("Inter", 13, "bold" if is_selected else "normal"),
            )
        
        # Call callback
        if self.command:
            self.command(value)
    
    def get(self) -> str:
        """Get current selected value."""
        return self.current_value
    
    def set(self, value: str) -> None:
        """Set current value without triggering callback."""
        if value in self.values and value != self.current_value:
            self.current_value = value
            for v, btn in self.buttons.items():
                is_selected = v == value
                btn.configure(
                    fg_color=COLORS["bg_card"] if is_selected else "transparent",
                    text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                    font=("Inter", 13, "bold" if is_selected else "normal"),
                )


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """
    Scrollable frame wrapper that uses CTk's built-in scrolling.
    The custom smooth scrolling implementation was incompatible with 
    the current CustomTkinter version.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Just use standard CTk scrolling - works reliably


# Setting tooltip descriptions with latency indicators
# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY GUIDE: ⚡ = none, 🟢 = <10ms, 🟡 = 10-100ms, 🔴 = 100ms+, 🚀 = speedup
# ═══════════════════════════════════════════════════════════════════════════════
SETTING_TOOLTIPS = {
    # ⚡ No latency impact - UI/configuration only
    "hotkey": "The keyboard shortcut to start/stop voice recording.\n⚡ Latency: None",
    "microphone": "Select which microphone/audio input device to use.\n⚡ Latency: None",
    "hotkey_devices": "Which keyboards, mice, or keypads can trigger the hotkey.\n⚡ Latency: None",
    "benchmark": "Measure transcription speed on your hardware.\nResults customize speed estimates throughout the app.\n⏱️ Run once to get accurate timing predictions.",
    "start_minimized": "Start the app minimized to the system tray.\n⚡ Latency: None",
    "ui_scale": "Adjust the size of the user interface.\n⚡ Latency: None",
    "overlay_type": "Choose the status indicator style:\n• Always On: Stays visible, never steals focus (PyQt6)\n• Disappearing: Shows only during recording (CTk)\n⚠️ Requires restart to take effect.",
    "overlay_scale": "Adjust the size of the status overlay.\nSeparate from the main UI scale.\n⚡ Latency: None",
    "overlay_position": "Slide the overlay up or down on screen.\nNegative = higher, Positive = lower.\n⚡ Latency: None",
    "prompt": "Initial text that guides transcription style.\n⚡ Latency: None (processed at model load)",
    "language": "The language for transcription. English is most optimized.\n⚡ Latency: None",
    
    # 🟢 Minimal latency impact (<10ms per invocation)
    "typing_speed": "How fast text is typed out.\n🟢 Instant: 0ms | Fast: ~50ms | Normal: ~200ms | Slow: ~500ms per sentence",
    "ensure_punctuation": "Extra punctuation fixes if model output lacks periods/caps.\n🟢 Latency: +1-3ms (optional, most models handle this well)",
    "audio_preprocessing": "Audio signal processing before transcription.\n🟢 Off: 0ms | Light: +2ms | Medium: +5ms | Heavy: +10ms",
    
    # 🟡 Moderate latency impact (10-100ms)
    "chunked_mode": "Split long recordings into segments, transcribe each,\nand splice results together. Enables unlimited length.\n🟡 Latency: +50-100ms overhead per segment boundary",
    "chunk_duration": "Length of each audio segment (seconds).\nShorter = faster feedback but more splice points.\n🟡 15-30s recommended for best balance",
    
    # 🔴 MAJOR latency impact - These are the biggest factors
    "whisper_model": "Local on-device speech recognition model.\nProcessed entirely on your machine — no cloud API needed.\n🔴 GPU: Tiny ~0.5s | Base ~1s | Small ~1.5s | Medium ~3s | Large ~6s | Turbo ~2s\n🔴 CPU: Tiny ~2s | Base ~4s | Small ~6s | Medium ~12s | Large ~25s | Turbo ~8s",
    "accuracy_mode": "Speed vs accuracy preset - affects beam search depth.\n🔴 Fast: -40% time (beam=1) | Balanced: baseline (beam=5) | High: +60% time (beam=8)",
    "beam_size": "Search width for finding best transcription.\n🔴 1 = fastest (-50%) | 5 = balanced | 10 = slowest (+100%)",
    
    # GPU/Backend - Can dramatically change all timings
    "backend": "Transcription engine selection.\n⚙️ whisper.cpp: CPU-optimized, lower memory\n⚙️ Faster-Whisper: Better GPU utilization (up to 10x faster)",
    "gpu_acceleration": "Use GPU for transcription.\n🚀 Enabled: 3-10x faster than CPU (requires CUDA/ROCm/Vulkan)",
    "gpu_layers": "Model layers to offload to GPU.\n⚙️ Auto: Maximum speed | Fewer: Saves VRAM, slower",
}


def get_dynamic_tooltip(key: str, config: dict) -> str:
    """
    Generate dynamic tooltip text based on benchmark results.
    Falls back to static text with TBD for unbenchmarked values.
    """
    benchmark_results = config.get("benchmark_results", {})
    fastest = config.get("benchmark_fastest_processor", None)
    
    # Model-specific tooltip with actual benchmarked speeds
    if key == "whisper_model":
        base_text = "Local on-device speech recognition model.\nProcessed entirely on your machine — no cloud API needed."
        
        if not benchmark_results:
            return f"{base_text}\n\n⏱️ Run benchmark to measure speeds on your hardware."
        
        # Build speed table from benchmarks
        speed_lines = []
        processor_label = "GPU" if fastest == "gpu" else "CPU" if fastest == "cpu" else "Best"
        
        model_order = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3-turbo-q5", "large-v3-turbo", "large-v3"]
        model_names = {
            "tiny.en": "Tiny", "base.en": "Base", "small.en": "Small", 
            "medium.en": "Medium", "large-v3-turbo-q5": "Turbo Q5",
            "large-v3-turbo": "Turbo", "large-v3": "Large v3"
        }
        
        for model_id in model_order:
            if model_id in benchmark_results:
                result = benchmark_results[model_id]
                if fastest == "gpu" and result.get("gpu_10s") is not None:
                    time_str = f"{result['gpu_10s']:.1f}s"
                elif result.get("cpu_10s") is not None:
                    time_str = f"{result['cpu_10s']:.1f}s"
                else:
                    time_str = "TBD"
                name = model_names.get(model_id, model_id)
                speed_lines.append(f"{name}: ~{time_str}")
        
        if speed_lines:
            speeds = " | ".join(speed_lines)
            return f"{base_text}\n\n🚀 {processor_label} speeds (10s audio):\n{speeds}"
        
        return f"{base_text}\n\n⏱️ Run benchmark to measure speeds on your hardware."
    
    # Accuracy mode tooltip with benchmarked impact
    if key == "accuracy_mode":
        base_text = "Speed vs accuracy preset - affects beam search depth."
        if benchmark_results and fastest:
            return f"{base_text}\n🔴 Fast: ~40% faster | Balanced: baseline | High: ~60% slower\n(Based on your {fastest.upper()} benchmarks)"
        return f"{base_text}\n🔴 Fast: -40% time (beam=1) | Balanced: baseline (beam=5) | High: +60% time (beam=8)\n⏱️ Run benchmark for exact timings"
    
    # GPU acceleration tooltip with measured speedup
    if key == "gpu_acceleration":
        base_text = "Use GPU for transcription."
        if benchmark_results:
            # Calculate actual speedup from benchmark data
            speedups = []
            for model_id, result in benchmark_results.items():
                cpu_time = result.get("cpu_10s")
                gpu_time = result.get("gpu_10s")
                if cpu_time and gpu_time and gpu_time > 0:
                    speedups.append(cpu_time / gpu_time)
            if speedups:
                avg_speedup = sum(speedups) / len(speedups)
                return f"{base_text}\n🚀 Your GPU is {avg_speedup:.1f}x faster than CPU on average!"
        return f"{base_text}\n🚀 TBD — run benchmark to measure your GPU speedup"
    
    # Default to static tooltip
    return SETTING_TOOLTIPS.get(key, "")


class BenchmarkRunner:
    """
    Runs in-app benchmarks to measure transcription speed on user's hardware.
    Results are stored in config for dynamic tooltip generation.
    """
    
    # Models to benchmark (in order of speed) - model_id, filename pattern, display name
    BENCHMARK_MODELS = [
        ("tiny.en", "ggml-tiny.en.bin", "Tiny"),
        ("base.en", "ggml-base.en.bin", "Base"),
        ("small.en", "ggml-small.en.bin", "Small"),
        ("medium.en", "ggml-medium.en.bin", "Medium"),
        ("large-v3-turbo-q5", "ggml-large-v3-turbo-q5_0.bin", "Turbo Q5"),
        ("large-v3-turbo", "ggml-large-v3-turbo.bin", "Turbo"),
        ("large-v3", "ggml-large-v3.bin", "Large v3"),
    ]
    
    def __init__(self, config: dict, progress_callback=None, log_callback=None, 
                 countdown_callback=None):
        self.config = config
        self.progress_callback = progress_callback or (lambda p, msg: None)
        self.log_callback = log_callback or (lambda msg: None)
        self.countdown_callback = countdown_callback or (lambda s: None)
        self._cancel_requested = False
        self.results = {}
    
    def cancel(self):
        """Request cancellation of running benchmark."""
        self._cancel_requested = True
    
    @staticmethod
    def get_system_info() -> dict:
        """Detect system hardware: CPU, GPU, RAM."""
        info = {"cpu": "Unknown", "gpu": "Unknown", "ram": "Unknown"}

        if sys.platform == "darwin":
            # macOS: use sysctl and system_profiler
            import subprocess
            try:
                # CPU name + core count
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True, timeout=5,
                )
                cpu_name = result.stdout.strip() if result.returncode == 0 else ""
                cpu_cores = ""
                try:
                    r = subprocess.run(["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True, timeout=5)
                    total = r.stdout.strip()
                    # Get P/E core breakdown on Apple Silicon
                    rp = subprocess.run(["sysctl", "-n", "hw.perflevel0.logicalcpu"], capture_output=True, text=True, timeout=5)
                    re = subprocess.run(["sysctl", "-n", "hw.perflevel1.logicalcpu"], capture_output=True, text=True, timeout=5)
                    if rp.returncode == 0 and re.returncode == 0:
                        cpu_cores = f" ({rp.stdout.strip()}P+{re.stdout.strip()}E cores)"
                    elif total:
                        cpu_cores = f" ({total} cores)"
                except Exception:
                    pass
                if cpu_name:
                    info["cpu"] = f"{cpu_name}{cpu_cores}"
            except Exception:
                pass

            try:
                # GPU: Apple Silicon has unified GPU — get core count from system_profiler
                result = subprocess.run(
                    ["system_profiler", "SPDisplaysDataType"],
                    capture_output=True, text=True, timeout=10,
                )
                gpu_chip = ""
                gpu_cores = ""
                metal_ver = ""
                for line in result.stdout.split("\n"):
                    stripped = line.strip()
                    if "Chipset Model" in stripped or "Chip Model" in stripped:
                        gpu_chip = stripped.split(":")[-1].strip()
                    elif "Total Number of Cores" in stripped:
                        gpu_cores = stripped.split(":")[-1].strip()
                    elif "Metal Support" in stripped:
                        metal_ver = stripped.split(":")[-1].strip()
                if gpu_chip:
                    parts = [gpu_chip]
                    if gpu_cores:
                        parts.append(f"{gpu_cores} cores")
                    if metal_ver:
                        parts.append(metal_ver)
                    info["gpu"] = " · ".join(parts)
            except Exception:
                pass

            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    mem_bytes = int(result.stdout.strip())
                    info["ram"] = f"{mem_bytes / (1024**3):.0f} GB"
            except Exception:
                pass
        else:
            # Linux: /proc filesystem
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if "model name" in line:
                            info["cpu"] = line.split(":")[1].strip()
                            break
            except Exception:
                pass

            try:
                import subprocess
                result = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
                for line in result.stdout.split("\n"):
                    if "VGA" in line or "3D controller" in line:
                        if ":" in line:
                            gpu_part = line.split(":")[-1].strip()
                            for prefix in ["Advanced Micro Devices, Inc. ", "NVIDIA Corporation ", "Intel Corporation "]:
                                gpu_part = gpu_part.replace(prefix, "")
                            info["gpu"] = gpu_part[:60]
                        break
            except Exception:
                pass

            try:
                with open("/proc/meminfo") as f:
                    for line in f:
                        if "MemTotal" in line:
                            mem_kb = int(line.split()[1])
                            info["ram"] = f"{mem_kb / 1024 / 1024:.0f} GB"
                            break
            except Exception:
                pass

        return info
    
    def _find_models_dir(self) -> Path:
        """Find the whisper models directory."""
        possible_dirs = [
            Path.home() / "whisper.cpp" / "models",
            Path.home() / ".local" / "share" / "whisper.cpp",
            Path("/app/share/whisper-models"),  # Flatpak
        ]
        for d in possible_dirs:
            if d.exists():
                return d
        return possible_dirs[0]  # Default
    
    def _find_whisper_binary(self) -> str | None:
        """Find whisper-cli binary."""
        possible = [
            Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
            Path("/usr/bin/whisper-cli"),
            Path("/app/bin/whisper-cli"),
        ]
        for p in possible:
            if p.exists():
                return str(p)
        # Try system PATH
        import shutil
        return shutil.which("whisper-cli")
    
    def _create_test_audio(self, duration: int = 10) -> str:
        """Create a test audio file for benchmarking."""
        import tempfile
        import wave
        import numpy as np
        
        sample_rate = 16000
        samples = int(duration * sample_rate)
        
        # Generate pseudo-speech audio
        t = np.linspace(0, duration, samples)
        speech = np.sin(2 * np.pi * 200 * t) * 0.3
        speech += np.sin(2 * np.pi * 400 * t) * 0.2
        speech += np.random.randn(samples) * 0.1
        envelope = np.abs(np.sin(2 * np.pi * 2 * t)) ** 0.5
        speech *= envelope
        speech = speech / np.max(np.abs(speech)) * 0.7
        audio_int16 = (speech * 32767).astype(np.int16)
        
        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        with wave.open(temp_file.name, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(audio_int16.tobytes())
        
        return temp_file.name
    
    def _run_single_benchmark(self, whisper_cli: str, model_path: str, 
                               audio_file: str, use_gpu: bool, timeout: int = 60) -> float | None:
        """Run a single transcription and return time in seconds.
        
        Uses simple subprocess.run which handles timeouts correctly.
        """
        import subprocess
        
        cmd = [
            whisper_cli,
            "-m", model_path,
            "-f", audio_file,
            "-t", "6",  # threads
            "--no-timestamps",
            "--no-prints",
        ]
        
        # GPU is ON by default in Vulkan builds of whisper.cpp
        # Use --no-gpu to disable it for CPU-only testing
        if not use_gpu:
            cmd.append("--no-gpu")
        
        try:
            start = time.perf_counter()
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)
            elapsed = time.perf_counter() - start
            
            if result.returncode == 0:
                return elapsed
            else:
                stderr_text = result.stderr.decode('utf-8', errors='replace')[:200]
                self.log_callback(f"   ⚠️ Exit code {result.returncode}: {stderr_text}")
                return None
                
        except subprocess.TimeoutExpired:
            self.log_callback(f"   ⚠️ Timed out after {timeout}s")
            return None
        except Exception as e:
            self.log_callback(f"   ⚠️ Error: {e}")
            return None
    
    def run_benchmarks(self, test_gpu: bool = True, test_cpu: bool = True, 
                       quick_mode: bool = False, selected_model: str = None) -> tuple:
        """
        Run benchmarks on available models.
        
        Args:
            test_gpu: Test GPU performance
            test_cpu: Test CPU performance  
            quick_mode: Only test selected model (faster)
            selected_model: Model filename to test in quick mode
        
        Returns tuple: (results_dict, overall_fastest_processor)
        """
        self._cancel_requested = False
        self.results = {}
        
        whisper_cli = self._find_whisper_binary()
        if not whisper_cli:
            self.log_callback("❌ whisper-cli not found")
            return {}, None
        
        models_dir = self._find_models_dir()
        self.log_callback(f"📁 Models: {models_dir}")
        self.log_callback(f"🔧 Binary: {whisper_cli}")
        
        # Find available models
        available = []
        for model_id, filename, display_name in self.BENCHMARK_MODELS:
            path = models_dir / filename
            if path.exists():
                # In quick mode, only test the selected model
                if quick_mode and selected_model:
                    if filename in selected_model or selected_model in filename:
                        available.append((model_id, str(path), display_name))
                        break
                else:
                    available.append((model_id, str(path), display_name))
        
        if not available:
            self.log_callback("❌ No models found to benchmark")
            return {}, None
        
        self.log_callback(f"📦 Testing {len(available)} model(s)")
        
        # Create test audio (10 seconds)
        self.log_callback("🎤 Creating 10s test audio...")
        test_audio = self._create_test_audio(10)
        
        try:
            total_tests = len(available) * (int(test_cpu) + int(test_gpu))
            current_test = 0
            
            for model_id, model_path, display_name in available:
                if self._cancel_requested:
                    break
                
                self.results[model_id] = {"model_name": display_name}
                
                # GPU benchmark first (usually faster)
                if test_gpu and not self._cancel_requested:
                    current_test += 1
                    progress = current_test / total_tests
                    self.progress_callback(progress, f"Testing {display_name} (GPU)...")
                    self.log_callback(f"⏱️ {display_name} GPU...")
                    
                    gpu_time = self._run_single_benchmark(
                        whisper_cli, model_path, test_audio, use_gpu=True, timeout=60
                    )
                    if gpu_time:
                        self.results[model_id]["gpu_10s"] = round(gpu_time, 2)
                        self.log_callback(f"   ✅ GPU: {gpu_time:.1f}s")
                    else:
                        self.log_callback(f"   ⚠️ GPU test failed")
                
                # CPU benchmark
                if test_cpu and not self._cancel_requested:
                    current_test += 1
                    progress = current_test / total_tests
                    self.progress_callback(progress, f"Testing {display_name} (CPU)...")
                    self.log_callback(f"⏱️ {display_name} CPU...")
                    
                    cpu_time = self._run_single_benchmark(
                        whisper_cli, model_path, test_audio, use_gpu=False, timeout=120
                    )
                    if cpu_time:
                        self.results[model_id]["cpu_10s"] = round(cpu_time, 2)
                        self.log_callback(f"   ✅ CPU: {cpu_time:.1f}s")
                    else:
                        self.log_callback(f"   ⚠️ CPU test failed")
                
                # Determine fastest processor for this model
                cpu = self.results[model_id].get("cpu_10s")
                gpu = self.results[model_id].get("gpu_10s")
                if cpu and gpu:
                    self.results[model_id]["fastest"] = "gpu" if gpu < cpu else "cpu"
                    speedup = cpu / gpu if gpu > 0 else 1.0
                    self.log_callback(f"   🚀 GPU is {speedup:.1f}x faster")
                elif gpu:
                    self.results[model_id]["fastest"] = "gpu"
                elif cpu:
                    self.results[model_id]["fastest"] = "cpu"
                
                self.results[model_id]["timestamp"] = int(time.time())
        
        finally:
            # Cleanup test audio
            try:
                os.unlink(test_audio)
            except:
                pass
        
        # Determine overall fastest processor
        gpu_wins = sum(1 for r in self.results.values() if r.get("fastest") == "gpu")
        cpu_wins = sum(1 for r in self.results.values() if r.get("fastest") == "cpu")
        overall_fastest = "gpu" if gpu_wins > cpu_wins else "cpu" if cpu_wins > 0 else None
        
        self.log_callback(f"\n🏁 Benchmark complete!")
        self.log_callback(f"🚀 Fastest processor: {overall_fastest.upper() if overall_fastest else 'Unknown'}")
        
        return self.results, overall_fastest


# === Model Download Definitions ===

# whisper.cpp GGML models (from Hugging Face)
WHISPER_CPP_MODELS = {
    "tiny.en": {
        "name": "Tiny (English)",
        "size": "75 MB",
        "size_bytes": 75_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin",
        "filename": "ggml-tiny.en.bin",
        "speed": "⚡ Fastest • ~0.5s GPU | ~2s CPU",
    },
    "base.en": {
        "name": "Base (English)",
        "size": "142 MB",
        "size_bytes": 142_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
        "filename": "ggml-base.en.bin",
        "speed": "⚡ Fast • ~1s GPU | ~4s CPU",
    },
    "small.en": {
        "name": "Small (English)",
        "size": "466 MB",
        "size_bytes": 466_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin",
        "filename": "ggml-small.en.bin",
        "speed": "🟡 Good • ~1.5s GPU | ~6s CPU",
    },
    "medium.en": {
        "name": "Medium (English)",
        "size": "1.5 GB",
        "size_bytes": 1_500_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.en.bin",
        "filename": "ggml-medium.en.bin",
        "speed": "🔴 Slow • ~3s GPU | ~12s CPU",
    },
    "large-v3-turbo": {
        "name": "Large v3 Turbo ⭐",
        "size": "1.6 GB",
        "size_bytes": 1_600_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
        "filename": "ggml-large-v3-turbo.bin",
        "speed": "🚀 Best balance • ~2s GPU | ~8s CPU",
        "recommended": True,
    },
    "large-v3-turbo-q5_0": {
        "name": "Large v3 Turbo Q5 (Quantized)",
        "size": "547 MB",
        "size_bytes": 547_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        "filename": "ggml-large-v3-turbo-q5_0.bin",
        "speed": "🚀 Fast+Small • ~2s GPU | ~6s CPU",
    },
    "tiny": {
        "name": "Tiny (Multi-lang)",
        "size": "75 MB",
        "size_bytes": 75_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
        "filename": "ggml-tiny.bin",
        "speed": "⚡ Fastest • Multi-language",
    },
    "base": {
        "name": "Base (Multi-lang)",
        "size": "142 MB",
        "size_bytes": 142_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        "filename": "ggml-base.bin",
        "speed": "⚡ Fast • Multi-language",
    },
    "small": {
        "name": "Small (Multi-lang)",
        "size": "466 MB",
        "size_bytes": 466_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        "filename": "ggml-small.bin",
        "speed": "🟡 Good • Multi-language",
    },
    "medium": {
        "name": "Medium (Multi-lang)",
        "size": "1.5 GB",
        "size_bytes": 1_500_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
        "filename": "ggml-medium.bin",
        "speed": "🔴 Slow • Multi-language",
    },
    "large-v3": {
        "name": "Large v3 (Best Quality)",
        "size": "3.0 GB",
        "size_bytes": 3_000_000_000,
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3.bin",
        "filename": "ggml-large-v3.bin",
        "speed": "🔴 Slowest • Best accuracy",
    },
}

# Recommended GGUF models for post-processing (llama.cpp)
LLM_GGUF_MODELS = {
    "qwen3.5-2b": {
        "name": "Qwen 3.5 2B ⭐",
        "size": "1.3 GB",
        "size_bytes": 1_390_000_000,
        "url": "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf",
        "filename": "Qwen3.5-2B-Q4_K_M.gguf",
        "description": "Top recommendation. Best balance of speed and quality for dictation.",
        "speed": "Very Fast",
        "accuracy": "Excellent",
        "recommended": True,
    },
    "phi-3-mini": {
        "name": "Phi-3 Mini (3.8B)",
        "size": "2.3 GB",
        "size_bytes": 2_393_231_072,
        "url": "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct-gguf/resolve/main/Phi-3-mini-4k-instruct-q4.gguf",
        "filename": "Phi-3-mini-4k-instruct-q4.gguf",
        "description": "Microsoft's compact powerhouse. Excellent at text cleanup and formatting.",
        "speed": "Fast",
        "accuracy": "High",
    },
    "qwen2.5-1.5b": {
        "name": "Qwen2.5 1.5B",
        "size": "1.0 GB",
        "size_bytes": 1_117_320_736,
        "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "description": "Previous default, still works well.",
        "speed": "Very Fast",
        "accuracy": "Good",
    },
    "smollm2-360m": {
        "name": "SmolLM2 360M",
        "size": "369 MB",
        "size_bytes": 386_404_992,
        "url": "https://huggingface.co/HuggingFaceTB/SmolLM2-360M-Instruct-GGUF/resolve/main/smollm2-360m-instruct-q8_0.gguf",
        "filename": "smollm2-360m-instruct-q8_0.gguf",
        "description": "HuggingFace's tiny model. Blazing fast, best for simple cleanup.",
        "speed": "Instant",
        "accuracy": "Basic",
    },
    "llama3.2-1b": {
        "name": "Llama 3.2 1B",
        "size": "770 MB",
        "size_bytes": 807_694_464,
        "url": "https://huggingface.co/bartowski/Llama-3.2-1B-Instruct-GGUF/resolve/main/Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "filename": "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "description": "Meta's latest efficient model. Good all-rounder for text tasks.",
        "speed": "Very Fast",
        "accuracy": "Good",
    },
}

# Faster-Whisper models (auto-downloaded by library, but we can list them)
FASTER_WHISPER_MODELS = {
    "tiny": {"name": "Tiny", "size": "~75 MB", "speed": "⚡ Fastest"},
    "base": {"name": "Base", "size": "~142 MB", "speed": "⚡ Fast"},
    "small": {"name": "Small", "size": "~466 MB", "speed": "🟡 Good"},
    "medium": {"name": "Medium", "size": "~1.5 GB", "speed": "🔴 Slow"},
    "large-v3-turbo": {"name": "Large v3 Turbo ⭐", "size": "~1.6 GB", "speed": "🚀 Best", "recommended": True},
    "large-v3": {"name": "Large v3", "size": "~3 GB", "speed": "🔴 Slowest"},
}


class ModelDownloader:
    """Downloads whisper models with progress tracking."""
    
    def __init__(self, models_dir: Path = None):
        self.models_dir = models_dir or (Path.home() / "whisper.cpp" / "models")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._cancel_requested = False
        self._current_download = None
    
    def get_installed_models(self) -> list[str]:
        """Get list of installed model IDs."""
        installed = []
        for model_id, info in WHISPER_CPP_MODELS.items():
            if (self.models_dir / info["filename"]).exists():
                installed.append(model_id)
        return installed
    
    def is_installed(self, model_id: str) -> bool:
        """Check if a model is already downloaded."""
        if model_id not in WHISPER_CPP_MODELS:
            return False
        return (self.models_dir / WHISPER_CPP_MODELS[model_id]["filename"]).exists()
    
    def get_model_path(self, model_id: str) -> Path | None:
        """Get the path to an installed model."""
        if model_id not in WHISPER_CPP_MODELS:
            return None
        path = self.models_dir / WHISPER_CPP_MODELS[model_id]["filename"]
        return path if path.exists() else None
    
    def download_model(
        self, 
        model_id: str, 
        progress_callback: callable = None,
        complete_callback: callable = None,
        error_callback: callable = None,
    ) -> None:
        """Download a model in a background thread."""
        if model_id not in WHISPER_CPP_MODELS:
            if error_callback:
                error_callback(f"Unknown model: {model_id}")
            return
        
        self._cancel_requested = False
        model_info = WHISPER_CPP_MODELS[model_id]
        
        def download_thread():
            try:
                url = model_info["url"]
                filename = model_info["filename"]
                dest_path = self.models_dir / filename
                temp_path = self.models_dir / f"{filename}.downloading"
                
                # Create request with headers
                request = urllib.request.Request(url)
                request.add_header("User-Agent", "Wayfinder-Voice/1.0")
                
                with urllib.request.urlopen(request, timeout=30) as response:
                    total_size = int(response.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk_size = 1024 * 1024  # 1MB chunks
                    
                    with open(temp_path, "wb") as f:
                        while True:
                            if self._cancel_requested:
                                temp_path.unlink(missing_ok=True)
                                if error_callback:
                                    error_callback("Download cancelled")
                                return
                            
                            chunk = response.read(chunk_size)
                            if not chunk:
                                break
                            
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if progress_callback and total_size > 0:
                                progress = downloaded / total_size
                                progress_callback(progress, downloaded, total_size)
                
                # Move temp file to final destination
                temp_path.rename(dest_path)
                
                if complete_callback:
                    complete_callback(str(dest_path))
                    
            except urllib.error.URLError as e:
                if error_callback:
                    error_callback(f"Network error: {e.reason}")
            except Exception as e:
                if error_callback:
                    error_callback(f"Download failed: {str(e)}")
        
        self._current_download = threading.Thread(target=download_thread, daemon=True)
        self._current_download.start()
    
    def cancel_download(self):
        """Cancel the current download."""
        self._cancel_requested = True
    
    def delete_model(self, model_id: str) -> bool:
        """Delete a downloaded model."""
        if model_id not in WHISPER_CPP_MODELS:
            return False
        path = self.models_dir / WHISPER_CPP_MODELS[model_id]["filename"]
        if path.exists():
            path.unlink()
            return True
        return False


def get_audio_input_devices() -> list[dict]:
    """Get all available audio input devices with smart recommendations."""
    import sounddevice as sd
    
    # Use the enhanced device listing from recorder module
    devices_info = list_input_devices()
    
    devices = []
    try:
        default_input = sd.default.device[0] if sd.default.device else None
        
        for dev in devices_info:
            # Skip excluded devices (monitors, HDMI, etc.) from the UI list
            if dev.get('excluded', False):
                continue
            
            devices.append({
                "index": dev["index"],
                "name": dev["name"],
                "channels": dev["channels"],
                "sample_rate": 16000,  # We resample anyway
                "is_default": dev["index"] == default_input,
                "recommended": dev.get("recommended", False),
            })
        
        # Sort: recommended first, then by name
        devices.sort(key=lambda d: (not d.get("recommended", False), d["name"]))
        
    except Exception as e:
        print(f"Error querying audio devices: {e}")
    
    return devices


def resolve_audio_device(config: dict) -> int | None:
    """
    Resolve the audio device to use based on config.
    
    If audio_device is None, intelligently selects the best device.
    If audio_device_name is set, finds the device by name.
    
    Args:
        config: The application config dict
        
    Returns:
        Device index to use, or None for system default
    """
    device_id = config.get("audio_device")
    device_name = config.get("audio_device_name")
    
    # If we have a saved device name, try to find it by name first
    # (device IDs can change when USB devices are reconnected)
    if device_name:
        found_id = get_input_device_by_name(device_name)
        if found_id is not None:
            return found_id
        print(f"Saved device '{device_name}' not found, using auto-selection")
    
    # If explicit device ID is set and valid, use it
    if device_id is not None:
        try:
            import sounddevice as sd
            dev_info = sd.query_devices(device_id)
            dev_name = dev_info.get('name', '')
            if dev_info.get('max_input_channels', 0) > 0:
                if is_output_device(dev_name):
                    print(f"Configured device '{dev_name}' is an output, not a microphone — using auto-selection")
                else:
                    return device_id
        except Exception:
            print(f"Configured device {device_id} not available, using auto-selection")
    
    # Auto-select the best device
    return find_best_input_device(preferred_name=device_name)


# Single source of truth for EventType — shared with hotkey listeners
from wayfinder.hotkeys.types import EventType


# === Tray Icon ===

def create_status_icon(color: str, size: int = 64) -> Image.Image:
    """Create a glowing status indicator icon."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Parse color
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    
    # Draw glow effect (outer rings with decreasing opacity)
    for i in range(3, 0, -1):
        alpha = int(60 / i)
        glow_color = (r, g, b, alpha)
        offset = i * 4
        draw.ellipse([offset, offset, size - offset, size - offset], fill=glow_color)
    
    # Draw main circle
    draw.ellipse([12, 12, size - 12, size - 12], fill=color)
    
    # Draw highlight
    draw.ellipse([20, 16, 28, 24], fill="#ffffff80")
    
    return image


# === Hotkey Listener ===

def get_all_input_devices() -> list[dict]:
    """Get all input devices that could potentially send hotkeys."""
    if not HAS_EVDEV:
        return []
    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    result = []
    
    for device in devices:
        capabilities = device.capabilities()
        if ecodes.EV_KEY in capabilities:
            key_caps = capabilities[ecodes.EV_KEY]
            has_fkeys = ecodes.KEY_F1 in key_caps and ecodes.KEY_F12 in key_caps
            
            name_lower = device.name.lower()
            is_virtual = "virtual" in name_lower or "ydotool" in name_lower
            
            if has_fkeys and not is_virtual:
                # Determine device type for display
                device_type = "keyboard"
                if "mouse" in name_lower:
                    device_type = "mouse"
                elif "gamepad" in name_lower or "controller" in name_lower:
                    device_type = "gamepad"
                
                result.append({
                    "name": device.name,
                    "path": device.path,
                    "type": device_type,
                    "device": device,
                })
    
    return result


def find_keyboard_devices(enabled_devices: list[str] = None) -> list:
    """Find input devices to listen to based on config."""
    all_devices = get_all_input_devices()
    
    if enabled_devices:
        # Only use explicitly enabled devices
        return [d["device"] for d in all_devices if d["name"] in enabled_devices]
    else:
        # Default: use all available devices
        return [d["device"] for d in all_devices]


def hotkey_listener(event_queue, hotkey_key, hotkey_modifiers, stop_event, enabled_devices=None, log_callback=None,
                    style_toggle_key=None, style_toggle_modifiers=None):
    import select
    
    def log(msg):
        if log_callback:
            try:
                log_callback(msg)
            except:
                pass
    
    log(f"🔧 Hotkey listener starting (record={hotkey_key}, style={style_toggle_key})...")
    print(f"[DEBUG] Hotkey listener thread started: record={hotkey_key}, style={style_toggle_key}", flush=True)
    
    devices = find_keyboard_devices(enabled_devices)
    print(f"[DEBUG] Found {len(devices)} devices to monitor:", flush=True)
    for d in devices:
        print(f"[DEBUG]   - {d.name} ({d.path})", flush=True)
    if not devices:
        # Don't give up: the loop below keeps rescanning, so a keyboard that is asleep
        # or disconnected at startup recovers automatically instead of killing hotkeys.
        log("⚠️ No input devices found yet — will keep scanning (check: sudo usermod -aG input $USER)")
    else:
        log(f"🎮 Monitoring {len(devices)} input device(s):")
        for dev in devices:
            short_name = dev.name[:40] + "..." if len(dev.name) > 40 else dev.name
            log(f"   • {short_name}")
    
    # Build set of required modifier key codes for recording hotkey
    required_modifiers = set()
    for name in hotkey_modifiers:
        if name.lower() in MODIFIER_CODES:
            required_modifiers.update(MODIFIER_CODES[name.lower()])
    
    # Build set of required modifier key codes for style toggle hotkey
    style_toggle_modifiers = style_toggle_modifiers or []
    required_style_modifiers = set()
    for name in style_toggle_modifiers:
        if name.lower() in MODIFIER_CODES:
            required_style_modifiers.update(MODIFIER_CODES[name.lower()])
    
    pressed_modifiers = set()
    all_modifier_codes = set()
    for codes in MODIFIER_CODES.values():
        all_modifier_codes.update(codes)

    # Map file descriptors to devices
    fd_to_device = {dev.fd: dev for dev in devices}
    
    def check_modifiers(required_mods, modifier_names):
        """Check if all required modifiers are currently pressed."""
        if not required_mods:
            return True
        return all(
            pressed_modifiers & set(MODIFIER_CODES.get(mod.lower(), []))
            for mod in modifier_names
        )

    RESCAN_BACKOFF = 2.0  # seconds between device rescans when none available (Rule #1: >=1s)

    while not stop_event.is_set():
        try:
            # No devices: wait (honoring shutdown) and rescan instead of dying — a slept
            # wireless keyboard or USB re-enumeration then recovers automatically.
            if not fd_to_device:
                if stop_event.wait(RESCAN_BACKOFF):
                    break
                pressed_modifiers.clear()  # avoid a stuck-modifier state across device loss
                rescanned = find_keyboard_devices(enabled_devices)
                fd_to_device = {dev.fd: dev for dev in rescanned}
                if fd_to_device:
                    log(f"🔌 Input device(s) available ({len(fd_to_device)}) — resuming hotkey monitoring")
                continue
            
            try:
                r, _, _ = select.select(list(fd_to_device.keys()), [], [], 0.5)
            except (ValueError, OSError) as e:
                # Invalid file descriptor - a device was disconnected
                # Rebuild fd_to_device by checking which devices are still valid
                log(f"⚠️ Device disconnected, rebuilding device list: {e}")
                new_fd_to_device = {}
                for fd, dev in list(fd_to_device.items()):
                    try:
                        # Test if device is still accessible
                        dev.capabilities()
                        new_fd_to_device[fd] = dev
                    except (OSError, IOError):
                        log(f"   Removed: {dev.name}")
                fd_to_device = new_fd_to_device
                continue
            
            for fd in r:
                device = fd_to_device.get(fd)
                if device is None:
                    continue
                
                try:
                    for event in device.read():
                        if event.type == ecodes.EV_KEY:
                            key_event = categorize(event)
                            keycode = key_event.scancode
                            
                            if keycode in all_modifier_codes:
                                if key_event.keystate == 1:
                                    pressed_modifiers.add(keycode)
                                elif key_event.keystate == 0:
                                    pressed_modifiers.discard(keycode)
                            
                            # Debug: log all key presses (F-keys only to reduce noise)
                            if key_event.keystate == 1 and 59 <= keycode <= 88:
                                print(f"[DEBUG] Key pressed: code={keycode} from {device.name[:30]}", flush=True)
                            
                            # Check for recording hotkey press
                            if keycode == hotkey_key and key_event.keystate == 1:
                                if check_modifiers(required_modifiers, hotkey_modifiers):
                                    event_queue.put((EventType.HOTKEY_PRESSED, None))
                            
                            # Check for style toggle hotkey press
                            if style_toggle_key and key_event.keystate == 1:
                                # Debug: log when we see the style toggle key
                                if keycode == style_toggle_key:
                                    print(f"[DEBUG] Style toggle key {keycode} detected!", flush=True)
                                    log(f"🎯 Style toggle key {keycode} pressed!")
                                    if check_modifiers(required_style_modifiers, style_toggle_modifiers):
                                        event_queue.put((EventType.STYLE_TOGGLE, None))
                                    else:
                                        log(f"   (modifiers not matched)")
                except (OSError, IOError) as e:
                    # Device read failed - remove it from monitoring
                    log(f"⚠️ Device read failed, removing: {device.name} ({e})")
                    del fd_to_device[fd]
        except Exception as e:
            # Last-resort guard: never let one unexpected error permanently kill the hotkey
            # listener (that strands the app in RECORDING). Log, brief backoff, and continue.
            log(f"⚠️ Hotkey loop error (recovering): {e}")
            if stop_event.wait(0.2):
                break

    log("🛑 Hotkey listener stopped")


def socket_listener(event_queue, stop_event, log_callback=None):
    """
    Listen for toggle commands on a Unix socket.
    This allows KDE shortcuts to trigger recording via a simple script.
    """
    def log(msg):
        if log_callback:
            try:
                log_callback(msg)
            except:
                pass
    
    print("[DEBUG] Socket listener thread starting...", flush=True)
    
    # Ensure the socket's parent dir exists (e.g. $XDG_RUNTIME_DIR/wayfinder-aura). The
    # Flatpak manifest also creates it via --filesystem=xdg-run/...:create, but do it here
    # too so a freshly-booted runtime dir and the non-Flatpak path both work.
    try:
        os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    except Exception:
        pass

    # Remove old socket if exists
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(1)
        server.settimeout(1.0)
        
        log(f"📡 Socket listener ready: {SOCKET_PATH}")
        
        while not stop_event.is_set():
            try:
                conn, _ = server.accept()
                data = conn.recv(64)
                data_str = data.decode("utf-8").strip() if data else ""
                
                if data_str == "toggle":
                    log("🎯 Toggle received via socket")
                    event_queue.put((EventType.HOTKEY_PRESSED, None))
                elif data_str == "style":
                    # Cycle to next style
                    log("✎ Style toggle received via socket")
                    event_queue.put((EventType.STYLE_TOGGLE, None))
                elif data_str.startswith("style:"):
                    # Set specific style (style:professional, style:dev, style:casual, style:personal)
                    style = data_str.split(":", 1)[1]
                    log(f"✎ Style set to '{style}' via socket")
                    event_queue.put((EventType.STYLE_TOGGLE, style))
                elif data_str == "show":
                    # Tray "Open" — raise/restore the main window
                    log("🪟 Show window received via socket")
                    event_queue.put((EventType.SHOW_WINDOW, None))
                elif data_str == "reset":
                    # Tray "Reset" — abort any stuck/in-flight dictation, return to idle
                    log("🔄 Reset received via socket")
                    event_queue.put((EventType.FORCE_RESET, None))
                elif data_str == "quit":
                    # Tray "Quit" — clean full shutdown
                    log("👋 Quit received via socket")
                    event_queue.put((EventType.QUIT_APP, None))
                conn.close()
            except socket.timeout:
                continue
            except Exception as e:
                if not stop_event.is_set():
                    log(f"⚠️ Socket error: {e}")
        
        server.close()
        os.unlink(SOCKET_PATH)
    except Exception as e:
        log(f"⚠️ Socket listener failed: {e}")


def wayland_hotkey_listener(event_queue, hotkey_display, stop_event, log_callback=None):
    """Global hotkey listener via the XDG GlobalShortcuts portal (Wayland / Flatpak).

    This is the sandbox-correct path: in a Flatpak, evdev can't read /dev/input and pynput
    can't see Wayland global keys, so the portal is the only option in the bundle.

    Implements the proper portal request/response lifecycle. The previous version called
    CreateSession then slept 0.5s and *guessed* the session path, which is unreliable
    (Codex review). The portal returns results asynchronously via a Response signal on a
    per-call Request object whose path is derived from the caller's unique bus name +
    handle_token — so we subscribe BEFORE issuing the call and avoid the race:
        CreateSession -> Request.Response (real session_handle)
                      -> BindShortcuts -> Request.Response
                      -> Activated signals -> HOTKEY_PRESSED

    NOTE: needs validation against the live KDE portal on the Steam Deck — portal presence and
    the bind UX vary by compositor. Falls through cleanly (returns False) when unavailable.
    """
    def log(msg):
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    if not DBUS_AVAILABLE:
        log("⚠️ D-Bus/GLib not available — portal hotkeys disabled")
        return False

    app_id = os.environ.get("FLATPAK_ID", "wayfinder-aura")

    try:
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        portal = bus.get_object("org.freedesktop.portal.Desktop",
                                "/org/freedesktop/portal/desktop")
        shortcuts_iface = dbus.Interface(portal, "org.freedesktop.portal.GlobalShortcuts")

        # Request object paths are /request/<sender>/<token>, where <sender> is our unique
        # bus name without the leading ':' and with '.'->'_'. Subscribe before the call.
        unique = bus.get_unique_name().lstrip(":").replace(".", "_")
        token = app_id.replace(".", "_").replace("-", "_")
        sess_token = f"{token}_sess"
        create_req = f"/org/freedesktop/portal/desktop/request/{unique}/{sess_token}"

        def _on_create_response(response, results):
            if response != 0:
                log(f"⚠️ Portal CreateSession denied (code {response})")
                return
            session_handle = results.get("session_handle")
            if not session_handle:
                log("⚠️ Portal returned no session_handle")
                return
            log("✓ Portal session established")

            shortcuts = dbus.Array([
                dbus.Struct([
                    dbus.String("record-toggle"),
                    dbus.Dictionary({
                        "description": dbus.String("Toggle voice recording"),
                        "preferred_trigger": dbus.String(hotkey_display or ""),
                    }, signature="sv"),
                ], signature="(sa{sv})"),
            ], signature="(sa{sv})")
            bind_token = f"{token}_bind"
            bind_req = f"/org/freedesktop/portal/desktop/request/{unique}/{bind_token}"

            def _on_bind_response(bresp, bresults):
                if bresp == 0:
                    log(f"✓ Shortcut registered (default trigger: {hotkey_display or 'unset'})")
                else:
                    log("⚠️ Shortcut bind cancelled — set it in System Settings → Shortcuts")

            bus.add_signal_receiver(
                _on_bind_response, signal_name="Response",
                dbus_interface="org.freedesktop.portal.Request", path=bind_req,
            )
            try:
                shortcuts_iface.BindShortcuts(
                    dbus.ObjectPath(session_handle), shortcuts, "",
                    dbus.Dictionary({"handle_token": dbus.String(bind_token)}, signature="sv"),
                )
            except dbus.exceptions.DBusException as e:
                log(f"⚠️ BindShortcuts failed: {e}")

        bus.add_signal_receiver(
            _on_create_response, signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request", path=create_req,
        )

        def _on_activated(session_handle, shortcut_id, timestamp, options):
            if shortcut_id == "record-toggle":
                event_queue.put((EventType.HOTKEY_PRESSED, None))

        bus.add_signal_receiver(
            _on_activated, signal_name="Activated",
            dbus_interface="org.freedesktop.portal.GlobalShortcuts",
            path="/org/freedesktop/portal/desktop",
        )

        log(f"\U0001f517 Requesting GlobalShortcuts portal session as '{app_id}'...")
        shortcuts_iface.CreateSession(dbus.Dictionary({
            "handle_token": dbus.String(sess_token),
            "session_handle_token": dbus.String(sess_token),
        }, signature="sv"))

        loop = GLib.MainLoop()

        def check_stop():
            if stop_event.is_set():
                loop.quit()
                return False
            return True

        GLib.timeout_add(500, check_stop)
        log("\U0001f3a7 Listening for Wayland global shortcuts (portal)...")
        loop.run()
        return True

    except Exception as e:
        log(f"⚠️ Wayland portal hotkey setup failed: {e}")
        return False


# === Floating Status Indicator ===

def get_monitor_refresh_rate() -> int:
    """
    Detect the monitor's refresh rate.
    Returns the refresh rate in Hz, or 60 as fallback.
    """
    # Try xrandr first (works on X11 and XWayland)
    try:
        result = subprocess.run(
            ["xrandr", "--current"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            # Parse xrandr output for current refresh rate
            # Look for lines with "*" which indicates current mode
            for line in result.stdout.split('\n'):
                if '*' in line:
                    # Extract refresh rate - format like "1920x1080 60.00*+"
                    import re
                    match = re.search(r'(\d+\.?\d*)\*', line)
                    if match:
                        rate = float(match.group(1))
                        return int(round(rate))
    except:
        pass
    
    # Try kscreen-doctor for KDE Plasma
    try:
        result = subprocess.run(
            ["kscreen-doctor", "-o"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            import re
            # Look for refresh rate in output
            match = re.search(r'@(\d+)(?:Hz)?', result.stdout)
            if match:
                return int(match.group(1))
    except:
        pass
    
    # Fallback to 60Hz
    return 60


class FloatingIndicator:
    """
    A floating status indicator that appears near the cursor.
    Shows "Listening..." during recording and "Processing..." during transcription.
    
    STABILITY FIX: Uses pre-created canvas items updated via coords()/itemconfig()
    instead of delete/recreate cycle which caused memory corruption segfaults.
    """
    
    # STABILITY: Fixed 15fps for animations - prevents canvas memory issues
    STABLE_FPS = 15
    
    def __init__(self, parent: ctk.CTk, target_fps: int = 0, audio_level_callback=None):
        self.parent = parent
        self.window: ctk.CTkToplevel | None = None
        self.label: ctk.CTkLabel | None = None
        self.pulse_job: str | None = None
        self.follow_job: str | None = None
        self.pulse_state = 0
        self._visible = False
        self._window_width = 0
        self._window_height = 0
        
        # Callback to get audio level for voice-reactive waveform
        self._audio_level_callback = audio_level_callback
        self._current_audio_level = 0.0  # Smoothed audio level
        
        # UI element references
        self.dot_canvas = None
        self.wave_canvas = None
        self.glow_frame = None
        self.main_frame = None
        
        # Track cursor position for stuck detection
        self._last_cursor_x = 0
        self._last_cursor_y = 0
        self._stuck_count = 0
        self._is_centered = False
        
        # Waveform animation state
        self._wave_time = 0.0  # Animation time for phase shifting
        self._wave_breath = 0.0  # Breathing amplitude modulation
        
        # Ring animation state for enhanced pulsing dot
        self._ring_phase = 0.0
        
        # STABILITY FIX: Pre-created canvas item IDs (avoids delete/recreate cycle)
        self._dot_items_created = False
        self._dot_glow_ids = []  # List of oval IDs for glow layers
        self._dot_core_id = None  # Core dot oval ID
        self._dot_ring_id = None  # Expanding ring oval ID
        
        self._wave_items_created = False
        self._wave_bar_ids = []  # List of rectangle IDs for waveform bars
        
        # STABILITY: Fixed 15fps regardless of monitor - prevents memory issues
        self._target_fps = self.STABLE_FPS
        self._fps_auto_detected = False
        
        # Calculate frame interval in milliseconds
        self._frame_interval_ms = max(1, int(1000 / self._target_fps))
    
    def set_audio_level_callback(self, callback):
        """Set the callback function for getting audio levels."""
        self._audio_level_callback = callback
    
    def get_fps_info(self) -> str:
        """Return a string describing the current FPS setting."""
        if self._fps_auto_detected:
            return f"{self._target_fps} Hz (auto-detected)"
        else:
            return f"{self._target_fps} Hz (manual)"
    
    def _font_exists(self, font_name: str) -> bool:
        """Check if a font exists on the system."""
        try:
            import tkinter.font as tkfont
            return font_name in tkfont.families()
        except:
            return False
        
    def show(self, text: str = "Listening...", color: str = COLORS["accent_red"]) -> None:
        """Show the floating indicator with the given text."""
        if self._visible and self.window:
            self.update(text, color)
            return
        
        # Reset tracking state
        self._stuck_count = 0
        self._is_centered = False
            
        # Get cursor position (works via XWayland on Wayland)
        try:
            x = self.parent.winfo_pointerx()
            y = self.parent.winfo_pointery()
            self._last_cursor_x = x
            self._last_cursor_y = y
        except:
            # Fallback: center of screen
            x = self.parent.winfo_screenwidth() // 2
            y = 100
        
        # Create the floating window
        self.window = ctk.CTkToplevel(self.parent)
        self.window.title("Wayfinder Status")
        self.window.overrideredirect(True)  # Borderless
        self.window.attributes("-topmost", True)  # Always on top
        
        # Try to set transparency (may not work on all compositors)
        try:
            self.window.attributes("-alpha", 0.95)
        except:
            pass
        
        # Additional hints for Wayland/KDE to keep window on top
        try:
            # Set window type hint for better stacking on KDE
            self.window.attributes("-type", "notification")
        except:
            pass
        
        try:
            # Lift window and make it stay on top
            self.window.lift()
            self.window.focus_force()
        except:
            pass
        
        # GitHub Dark palette - reduces halation/eye strain
        pill_bg = "#161B22"  # GitHub Dark surface
        shadow_color = "#0D1117"  # GitHub Dark base
        
        # Parse accent color for "glow from within" effect
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        # Glassmorphism simulation: 5% accent color blend
        glow_r = int(22 + r * 0.05)
        glow_g = int(27 + g * 0.05)
        glow_b = int(34 + b * 0.05)
        inner_glow_color = f"#{glow_r:02x}{glow_g:02x}{glow_b:02x}"
        
        self.window.configure(fg_color=shadow_color)
        
        # Outer frame with violet rim light (pre-blended, no alpha)
        self.glow_frame = ctk.CTkFrame(
            self.window,
            fg_color=shadow_color,
            corner_radius=24,  # Squircle feel
            border_width=1,
            border_color="#1E1B26",  # Pre-blended 10% violet
        )
        self.glow_frame.pack(padx=0, pady=0)
        
        # Main pill with glassmorphism simulation (inner glow from accent)
        self.main_frame = ctk.CTkFrame(
            self.glow_frame,
            fg_color=inner_glow_color,  # Subtle accent-tinted background
            corner_radius=20,  # Squircle feel
            border_width=1,
            border_color=color,
        )
        self.main_frame.pack(padx=2, pady=2)
        
        # Content container with waveform as background
        content = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        content.pack(padx=10, pady=6)
        
        # Single row: dot + label + waveform all together
        row = ctk.CTkFrame(content, fg_color="transparent")
        row.pack()
        
        # Glowing status indicator dot - bigger for visibility
        self.dot_canvas = ctk.CTkCanvas(
            row,
            width=28,
            height=28,
            bg=inner_glow_color,  # Match glassmorphism background
            highlightthickness=0,
        )
        self.dot_canvas.pack(side="left", padx=(0, 8))
        self._draw_dot(color)
        
        # Label with status text - refined mono for pro-tool aesthetic
        self.label = ctk.CTkLabel(
            row,
            text=text,
            font=("Geist Mono", 11) if self._font_exists("Geist Mono") else ("JetBrains Mono", 11),
            text_color=COLORS["text_bright"],
        )
        self.label.pack(side="left", padx=(0, 6))
        
        # Voice-reactive waveform - right next to text, fills remaining space
        self.wave_canvas = ctk.CTkCanvas(
            row,
            width=200,  # Much wider for dramatic effect
            height=32,  # Taller for bigger waves
            bg=inner_glow_color,  # Match glassmorphism background
            highlightthickness=0,
        )
        self.wave_canvas.pack(side="left")
        self._draw_waveform(color)
        
        self.current_color = color
        
        # Position near cursor (offset slightly so it doesn't cover click target)
        self.window.update_idletasks()
        self._window_width = self.window.winfo_width()
        self._window_height = self.window.winfo_height()
        
        # Initial position
        self._update_position(x, y)
        
        self._visible = True
        self._start_pulse()
        self._start_follow()
        
    def update(self, text: str, color: str | None = None) -> None:
        """Update the indicator text and optionally color."""
        if not self._visible or not self.window:
            self.show(text, color or COLORS["accent_yellow"])
            return
            
        if self.label:
            self.label.configure(text=text)
            
        if color and color != self.current_color:
            self.current_color = color
            self._draw_dot(color)
            self._draw_waveform(color)
            
            # Calculate new outer glow color
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            glow_r = int(r * 0.3 + 10)
            glow_g = int(g * 0.3 + 10)
            glow_b = int(b * 0.3 + 10)
            outer_glow_color = f"#{glow_r:02x}{glow_g:02x}{glow_b:02x}"
            
            # Update border colors on both frames
            if hasattr(self, 'glow_frame') and self.glow_frame:
                try:
                    self.glow_frame.configure(border_color=outer_glow_color)
                except:
                    pass
            if hasattr(self, 'main_frame') and self.main_frame:
                try:
                    self.main_frame.configure(border_color=color)
                except:
                    pass
                    
    def hide(self) -> None:
        """Hide and destroy the floating indicator."""
        self._stop_pulse()
        self._stop_follow()
        self._visible = False
        
        # Reset animation state
        self._wave_time = 0.0
        self._wave_breath = 0.0
        self._ring_phase = 0.0
        
        # STABILITY FIX: Reset canvas item flags so they get recreated next show()
        self._dot_items_created = False
        self._dot_glow_ids = []
        self._dot_core_id = None
        self._dot_ring_id = None
        self._wave_items_created = False
        self._wave_bar_ids = []
        
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            self.window = None
            self.label = None
            self.dot_canvas = None
            self.wave_canvas = None
            self.glow_frame = None
            self.main_frame = None
    
    def _update_position(self, x: int, y: int) -> None:
        """Update window position near the given cursor coordinates."""
        if not self.window:
            return
            
        # Position to the right and below cursor
        pos_x = x + 20
        pos_y = y + 20
        
        # Keep on screen
        screen_w = self.parent.winfo_screenwidth()
        screen_h = self.parent.winfo_screenheight()
        if pos_x + self._window_width > screen_w:
            pos_x = x - self._window_width - 20
        if pos_y + self._window_height > screen_h:
            pos_y = y - self._window_height - 20
            
        self.window.geometry(f"+{pos_x}+{pos_y}")
        self._is_centered = False
    
    def _center_above_taskbar(self) -> None:
        """Center the indicator above the taskbar (fallback for native Wayland apps)."""
        if not self.window:
            return
        
        screen_w = self.parent.winfo_screenwidth()
        screen_h = self.parent.winfo_screenheight()
        
        # Center horizontally, position just above taskbar
        pos_x = (screen_w - self._window_width) // 2
        pos_y = screen_h - self._window_height - 52  # Resting just on top of taskbar (~48px)
        
        self.window.geometry(f"+{pos_x}+{pos_y}")
        self._is_centered = True
            
    def _init_dot_items(self, color: str) -> None:
        """Create dot canvas items ONCE - called during show()."""
        if not self.dot_canvas or self._dot_items_created:
            return
        
        canvas_size = 28  # Bigger for visibility
        cx, cy = canvas_size // 2, canvas_size // 2

        try:
            # Create ring (initially hidden) - larger for dramatic effect
            self._dot_ring_id = self.dot_canvas.create_oval(
                cx - 6, cy - 6, cx + 6, cy + 6,
                outline=color, width=2, fill="", state="hidden"
            )

            # Create 3 glow layers - larger radii for bigger dot
            glow_radii = [10, 7, 4]
            self._dot_glow_ids = []
            for radius in glow_radii:
                item_id = self.dot_canvas.create_oval(
                    cx - radius, cy - radius, cx + radius, cy + radius,
                    fill=color, outline=""
                )
                self._dot_glow_ids.append(item_id)

            # Create core dot - bigger
            self._dot_core_id = self.dot_canvas.create_oval(
                cx - 4, cy - 4, cx + 4, cy + 4,
                fill=color, outline=""
            )
        except tk.TclError:
            pass
        
        self._dot_items_created = True
    
    def _draw_dot(self, color: str, scale: float = 1.0) -> None:
        """Update dot canvas items - STABLE version using coords/itemconfig."""
        if not self.dot_canvas:
            return
        
        # Initialize items on first call
        if not self._dot_items_created:
            self._init_dot_items(color)
            return
        
        # Wrap all canvas operations in try/except - Tk 9.0 can crash on coords
        try:
            # Validate scale to avoid NaN/Inf crashes
            if not isinstance(scale, (int, float)) or scale != scale or abs(scale) > 100:
                scale = 1.0
            
            # Parse color
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            
            canvas_size = 28  # Bigger for visibility
            cx, cy = canvas_size // 2, canvas_size // 2
            bg_r, bg_g, bg_b = 24, 24, 31  # pill_bg
            
            # Update ring animation - larger for bigger dot
            ring_progress = self._ring_phase % 1.0
            if ring_progress > 0.05 and ring_progress < 0.95:
                ring_radius = 6 + ring_progress * 8  # Bigger ring expansion
                ring_alpha = 0.6 * (1 - ring_progress)
                ring_r = int(r * ring_alpha + bg_r * (1 - ring_alpha))
                ring_g = int(g * ring_alpha + bg_g * (1 - ring_alpha))
                ring_b = int(b * ring_alpha + bg_b * (1 - ring_alpha))
                ring_color = f"#{ring_r:02x}{ring_g:02x}{ring_b:02x}"
                
                self.dot_canvas.coords(self._dot_ring_id,
                    cx - ring_radius, cy - ring_radius, cx + ring_radius, cy + ring_radius)
                self.dot_canvas.itemconfig(self._dot_ring_id, outline=ring_color, state="normal")
            else:
                self.dot_canvas.itemconfig(self._dot_ring_id, state="hidden")
            
            # Update glow layers - larger radii for bigger dot
            glow_configs = [(10, 0.25), (7, 0.5), (4, 0.75)]
            for i, (base_radius, alpha) in enumerate(glow_configs):
                if i < len(self._dot_glow_ids):
                    glow_radius = base_radius * scale
                    gr = int(r * alpha + bg_r * (1 - alpha))
                    gg = int(g * alpha + bg_g * (1 - alpha))
                    gb = int(b * alpha + bg_b * (1 - alpha))
                    glow_color = f"#{gr:02x}{gg:02x}{gb:02x}"
                    
                    self.dot_canvas.coords(self._dot_glow_ids[i],
                        cx - glow_radius, cy - glow_radius, cx + glow_radius, cy + glow_radius)
                    self.dot_canvas.itemconfig(self._dot_glow_ids[i], fill=glow_color)
            
            # Update core - bigger for visibility
            core_radius = 4 * scale
            self.dot_canvas.coords(self._dot_core_id,
                cx - core_radius, cy - core_radius, cx + core_radius, cy + core_radius)
            self.dot_canvas.itemconfig(self._dot_core_id, fill=color)
        except Exception:
            pass  # Tk 9.0 canvas coord bug - ignore to prevent crash
    
    def _init_wave_items(self, color: str) -> None:
        """Create waveform bar items ONCE - called during show()."""
        if not self.wave_canvas or self._wave_items_created:
            return
        
        # Use vertical bars - BIG and dramatic
        width = 200  # Match canvas width
        height = 32  # Match canvas height
        center_y = height // 2
        bar_width = 3
        bar_gap = 4
        num_bars = width // (bar_width + bar_gap)
        
        self._wave_bar_ids = []
        try:
            for i in range(num_bars):
                x = i * (bar_width + bar_gap) + bar_gap // 2
                # Create bar centered vertically (will be updated in _draw_waveform)
                bar_id = self.wave_canvas.create_rectangle(
                    x, center_y - 1, x + bar_width, center_y + 1,
                    fill=color, outline=""
                )
                self._wave_bar_ids.append(bar_id)
        except tk.TclError:
            pass
        
        self._wave_items_created = True
    
    def _draw_waveform(self, color: str) -> None:
        """Update waveform bars - HYPER voice-reactive version!"""
        if not self.wave_canvas:
            return
        
        # Initialize items on first call
        if not self._wave_items_created:
            self._init_wave_items(color)
            return
        
        # Wrap all canvas operations in try/except - Tk 9.0 can crash on coords
        try:
            import math
            
            width = 200  # Match canvas width
            height = 32  # Match canvas height
            center_y = height // 2
            max_amp = height // 2  # Full height available
            bar_width = 3
            bar_gap = 4
            
            # Parse color
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            bg_r, bg_g, bg_b = 14, 14, 16  # Match new deep charcoal
            
            # HYPER voice-reactive amplitude - FREAK OUT when speaking!
            audio_level = self._current_audio_level
            # Validate audio_level to avoid NaN
            if not isinstance(audio_level, (int, float)) or audio_level != audio_level:
                audio_level = 0.0
            # Small base motion when quiet
            base_breath = 0.15 + 0.1 * (0.5 + 0.5 * math.sin(self._wave_breath))
            # MASSIVE exponential voice boost - fills space FAST when speaking
            voice_boost = (audio_level ** 0.3) * 3.0  # Very sensitive, huge multiplier
            amplitude_factor = min(1.0, base_breath + voice_boost)
            
            # Update each bar
            for i, bar_id in enumerate(self._wave_bar_ids):
                x = i * (bar_width + bar_gap) + bar_gap // 2
                
                # Calculate bar height - chaotic multi-frequency waves
                phase = self._wave_time + i * 0.4
                # Multiple overlapping frequencies for organic chaos
                wave_val = (math.sin(phase * 1.2) * 0.35 + 
                           math.sin(phase * 2.1 + 0.7) * 0.35 + 
                           math.sin(phase * 0.7 + 1.2) * 0.3)
                # Bars fill the space when voice is detected
                bar_height = max(1, (0.2 + abs(wave_val) * 0.8) * max_amp * amplitude_factor)
                
                # Intense color variation
                alpha = 0.4 + 0.6 * abs(wave_val) * amplitude_factor
                br = int(r * alpha + bg_r * (1 - alpha))
                bg = int(g * alpha + bg_g * (1 - alpha))
                bb = int(b * alpha + bg_b * (1 - alpha))
                bar_color = f"#{br:02x}{bg:02x}{bb:02x}"
                
                # Update bar position and color
                self.wave_canvas.coords(bar_id,
                    x, center_y - bar_height, x + bar_width, center_y + bar_height)
                self.wave_canvas.itemconfig(bar_id, fill=bar_color)
        except Exception:
            pass  # Tk 9.0 canvas coord bug - ignore to prevent crash
        
    def _start_pulse(self) -> None:
        """Start the pulsing animation."""
        self._pulse_step()
        
    def _pulse_step(self) -> None:
        """Animate one pulse step - STABLE version at fixed 15fps."""
        if not self._visible or not self.window:
            return
        
        import math
        
        fps_scale = 4.0  # 60/15 = 4
        
        # Cycle through pulse states (2 second cycle at 15fps = 30 steps)
        self.pulse_state = (self.pulse_state + 1) % 30
        
        # Get audio level from callback - INSTANT attack, moderate decay
        if self._audio_level_callback:
            try:
                raw_level = self._audio_level_callback()
                # Instant attack - respond IMMEDIATELY to voice!
                if raw_level > self._current_audio_level:
                    self._current_audio_level = raw_level
                else:
                    # Slower decay to maintain energy
                    self._current_audio_level = self._current_audio_level * 0.7 + raw_level * 0.3
            except:
                self._current_audio_level *= 0.8
        else:
            self._current_audio_level *= 0.8
        
        # Calculate scale with sinusoidal easing
        t = self.pulse_state / 30.0 * 2 * math.pi
        ease = 0.5 + 0.5 * math.sin(t)
        voice_scale_boost = self._current_audio_level * 0.5
        scale = 0.7 + ease * 0.6 + voice_scale_boost
        
        # Update animation phases
        self._ring_phase = (self._ring_phase + 0.022 * fps_scale) % 1.0
        self._wave_time += 0.08 * fps_scale
        self._wave_breath += 0.04 * fps_scale
        
        # Update canvas items (STABLE - no delete/recreate)
        self._draw_dot(self.current_color, scale)
        self._draw_waveform(self.current_color)
        
        # Schedule next step at fixed 66ms (15fps)
        self.pulse_job = self.window.after(66, self._pulse_step)
            
    def _stop_pulse(self) -> None:
        """Stop the pulsing animation."""
        if self.pulse_job and self.window:
            try:
                self.window.after_cancel(self.pulse_job)
            except:
                pass
        self.pulse_job = None
    
    def _start_follow(self) -> None:
        """Start following the cursor."""
        self._follow_step()
        
    def _follow_step(self) -> None:
        """Update position - currently centered, mouse follow code preserved for future."""
        if not self._visible or not self.window:
            return
        
        # For now, just keep centered above taskbar (more reliable on Wayland)
        # Mouse following code preserved below for future use
        if not self._is_centered:
            self._center_above_taskbar()
        
        # Keep window on top
        try:
            self.window.attributes("-topmost", True)
            self.window.lift()
        except:
            pass
        
        # Schedule next update (slower rate since we're not following mouse)
        if self.window:
            self.follow_job = self.window.after(100, self._follow_step)
        
        # === MOUSE FOLLOWING CODE (disabled for now, enable later if needed) ===
        # try:
        #     x = self.parent.winfo_pointerx()
        #     y = self.parent.winfo_pointery()
        #     
        #     # Check if cursor position is "stuck" (not changing = likely over native Wayland app)
        #     if x == self._last_cursor_x and y == self._last_cursor_y:
        #         self._stuck_count += 1
        #     else:
        #         self._stuck_count = 0
        #         self._last_cursor_x = x
        #         self._last_cursor_y = y
        #     
        #     # If stuck for ~0.5 seconds, center above taskbar
        #     if self._stuck_count > 15:
        #         if not self._is_centered:
        #             self._center_above_taskbar()
        #     else:
        #         self._update_position(x, y)
        #     
        #     self.window.attributes("-topmost", True)
        #     self.window.lift()
        # except:
        #     if not self._is_centered:
        #         self._center_above_taskbar()
            
    def _stop_follow(self) -> None:
        """Stop following the cursor."""
        if self.follow_job and self.window:
            try:
                self.window.after_cancel(self.follow_job)
            except:
                pass
        self.follow_job = None


# === PyQt6 Glassmorphic Overlay Controller ===

class OverlayController:
    """
    Controls the PyQt6 glassmorphic status overlay subprocess.
    
    The overlay runs in a separate process to avoid event loop conflicts
    with the main CustomTkinter application. Communication is via JSON
    messages over stdin/stdout.
    """
    
    def __init__(self, audio_level_callback=None, mode: str = "persistent", initial_style: str = "professional", config: dict | None = None, log_callback=None, want_tray: bool = False):
        self._process: subprocess.Popen | None = None
        # When True, the overlay subprocess hosts a QSystemTrayIcon (used when the main
        # process has no in-process pystray tray — the Flatpak). tray_available is re-set
        # live from the overlay's ready handshake on each (re)start, so it never goes stale.
        self.want_tray = want_tray
        self.tray_available = False
        self._audio_level_callback = audio_level_callback
        self._audio_poll_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_state = "hidden"
        self._lock = threading.Lock()
        self._mode = mode  # "persistent" or "standard"
        self._initial_style = initial_style  # "professional", "dev", "casual", or "personal"
        self._config_ref = config  # Reference to app config for refresh
        self._log_callback = log_callback  # route rare failures to the app/UI log (diagnostics)

    def _log(self, msg: str):
        """Surface a diagnostic message to the app/UI log if available, else stderr."""
        if self._log_callback:
            try:
                self._log_callback(msg)
                return
            except Exception:
                pass
        print(msg, flush=True)
    
    def _start_process(self) -> bool:
        """Start the overlay subprocess if not already running."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return True  # Already running
            
            # Fresh start: clear stale tray availability — it's re-set from the ready
            # handshake below, so it can't go stale across a refresh()/health-check restart.
            self.tray_available = False

            # Clean up any stale overlay processes before starting a new one
            try:
                subprocess.run(["pkill", "-9", "-f", "overlay.py"], 
                              capture_output=True, timeout=1)
            except:
                pass
            
            try:
                # In frozen builds, sys.executable is the app binary — not Python.
                # Launching it would relaunch the entire app, causing a fork bomb.
                if getattr(sys, 'frozen', False):
                    print("[Overlay] Subprocess disabled in frozen builds")
                    return False

                # Find the overlay script (prefer new location in src/)
                script_path = Path(__file__).parent / "src" / "wayfinder" / "ui" / "overlay.py"
                if not script_path.exists():
                    # Fallback to old location
                    script_path = Path(__file__).parent / "status_overlay.py"
                if not script_path.exists():
                    print(f"Overlay script not found: {script_path}")
                    return False
                
                # Use the same Python interpreter that's running this script
                python_exe = sys.executable
                
                # Start subprocess with mode, style, scale, and offset arguments
                cmd_args = [
                    python_exe, str(script_path),
                    f"--mode={self._mode}",
                    f"--style={self._initial_style}",
                ]
                if self.want_tray:
                    cmd_args.append("--tray")
                if self._config_ref:
                    scale = self._config_ref.get("overlay_scale", 0.7)
                    offset = self._config_ref.get("overlay_vertical_offset", 0)
                    cmd_args.append(f"--scale={scale}")
                    cmd_args.append(f"--offset={offset}")
                self._process = subprocess.Popen(
                    cmd_args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,  # Capture errors for debugging
                    text=True,
                    bufsize=1,  # Line buffered
                )
                
                # Wait for ready signal with timeout
                import select
                ready, _, _ = select.select([self._process.stdout], [], [], 2.0)
                if ready:
                    response = self._process.stdout.readline()
                    if response:
                        try:
                            data = json.loads(response)
                            if data.get("status") == "ready":
                                # The overlay reports whether it created a QSystemTrayIcon
                                # (tray mode + SNI available). hide_to_tray reads this live.
                                self.tray_available = bool(data.get("tray_available", False))
                                return True
                        except json.JSONDecodeError:
                            pass
                
                return True
            except Exception as e:
                print(f"Failed to start overlay: {e}")
                return False
    
    def _send_command(self, cmd: dict, critical: bool = False) -> bool:
        """Send a command to the overlay subprocess.

        Args:
            cmd: JSON-serializable command dict.
            critical: If True, use longer timeout and retry (for state changes).
                      If False, best-effort with short timeout (for audio levels).
        """
        attempts = 3 if critical else 1
        lock_timeout = 0.5 if critical else 0.05

        for attempt in range(attempts):
            acquired = self._lock.acquire(timeout=lock_timeout)
            if not acquired:
                continue

            try:
                if self._process is None or self._process.poll() is not None:
                    return False

                try:
                    line = json.dumps(cmd) + "\n"
                    import select
                    _, ready, _ = select.select([], [self._process.stdin], [], 0.05)
                    if not ready:
                        if critical:
                            continue  # Retry critical commands
                        return False
                    self._process.stdin.write(line)
                    self._process.stdin.flush()
                    return True
                except (BrokenPipeError, OSError) as e:
                    self._log(f"⚠ Overlay subprocess died: {e}")
                    self._process = None
                    return False
                except Exception as e:
                    print(f"Failed to send overlay command: {e}")
                    return False
            finally:
                self._lock.release()

        if critical:
            self._log(f"⚠ Overlay command not delivered after {attempts} attempts: {cmd.get('cmd')} {cmd.get('state', '')}")
        return False
    
    def send_command(self, cmd: dict) -> bool:
        """Public method to send a command to the overlay subprocess."""
        return self._send_command(cmd)
    
    def _start_audio_polling(self):
        """Start background thread to poll audio levels."""
        if self._audio_poll_thread is not None and self._audio_poll_thread.is_alive():
            return
        
        self._stop_event.clear()
        self._audio_poll_thread = threading.Thread(target=self._audio_poll_loop, daemon=True)
        self._audio_poll_thread.start()
    
    def _stop_audio_polling(self):
        """Stop audio level polling and wait for thread to release the lock."""
        self._stop_event.set()
        thread = self._audio_poll_thread
        self._audio_poll_thread = None
        # Brief wait for audio thread to finish its current _send_command cycle
        # so it releases the lock before we try to send a state change
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)
    
    def _audio_poll_loop(self):
        """Background loop to send audio levels to overlay."""
        while not self._stop_event.is_set():
            if self._current_state == "listening" and self._audio_level_callback:
                try:
                    level = self._audio_level_callback()
                    # Convert numpy float32 to Python float for JSON serialization
                    level = float(level) if level is not None else 0.0
                    self._send_command({"cmd": "level", "value": level})
                except:
                    pass
            self._stop_event.wait(0.033)  # ~30 Hz (reduced to minimize command flooding)
    
    def show(self, state: str = "listening"):
        """
        Show the overlay with the specified state.
        
        Args:
            state: One of "listening", "processing", "ready"
        """
        if not self._start_process():
            return False

        self._current_state = state
        ok = self._send_command({"cmd": "show", "state": state}, critical=True)

        if state == "listening":
            self._start_audio_polling()
        return ok

    def update(self, state: str):
        """Update the overlay to a new state. Returns the send result (True/False)."""
        if self._process is None or self._process.poll() is not None:
            return self.show(state)

        old_state = self._current_state
        self._current_state = state

        # Stop audio polling BEFORE sending state command to free the lock
        if state != "listening" and old_state == "listening":
            self._stop_audio_polling()

        ok = self._send_command({"cmd": "show", "state": state}, critical=True)

        if state == "listening" and old_state != "listening":
            self._start_audio_polling()
        return ok
    
    def hide(self):
        """Hide the overlay."""
        self._stop_audio_polling()
        self._current_state = "hidden"
        self._send_command({"cmd": "hide"}, critical=True)
    
    def set_scale(self, scale: float):
        """Set the overlay scale factor."""
        self._send_command({"cmd": "scale", "value": scale})

    def set_vertical_offset(self, offset: int):
        """Set the overlay vertical position offset."""
        self._send_command({"cmd": "offset", "value": offset})

    def quit(self):
        """Shut down the overlay subprocess."""
        self._stop_audio_polling()
        self._send_command({"cmd": "quit"})
        
        # Use timeout on lock to avoid deadlock
        acquired = self._lock.acquire(timeout=1.0)
        if not acquired:
            # Lock stuck - force kill via pkill
            subprocess.run(["pkill", "-9", "-f", "overlay.py"], capture_output=True, timeout=1)
            return
        
        try:
            if self._process is not None:
                try:
                    self._process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    try:
                        self._process.wait(timeout=0.5)
                    except:
                        pass
                self._process = None
        finally:
            self._lock.release()
    
    def stop(self):
        """Stop and clean up the overlay subprocess forcefully."""
        self._stop_audio_polling()
        
        # Use timeout on lock to avoid deadlock
        acquired = self._lock.acquire(timeout=0.5)
        if not acquired:
            # Lock stuck - force kill via pkill
            subprocess.run(["pkill", "-9", "-f", "overlay.py"], capture_output=True, timeout=1)
            return
        
        try:
            if self._process is not None:
                pid = self._process.pid
                # First try graceful quit
                self._send_command({"cmd": "quit"})
                try:
                    self._process.wait(timeout=0.3)
                except subprocess.TimeoutExpired:
                    # Force kill if it doesn't respond
                    try:
                        self._process.kill()
                        self._process.wait(timeout=0.3)
                    except:
                        # Last resort: use pkill
                        import os
                        try:
                            os.kill(pid, 9)
                        except:
                            pass
                self._process = None
        finally:
            self._lock.release()
    
    def set_audio_level_callback(self, callback):
        """Set the callback function for getting audio levels."""
        self._audio_level_callback = callback
    
    def is_healthy(self) -> bool:
        """Check if the overlay subprocess is running and responsive."""
        # Use non-blocking lock check to avoid deadlock
        acquired = self._lock.acquire(timeout=0.1)
        if not acquired:
            # Lock held for too long - subprocess may be stuck
            return False
        
        try:
            if self._process is None or self._process.poll() is not None:
                return False
            return True
        finally:
            self._lock.release()
    
    def refresh(self) -> bool:
        """
        Refresh the overlay after display wake-up or other disruptions.
        
        This kills the existing overlay and restarts it, restoring
        the previous state.
        """
        # Remember current state
        previous_state = self._current_state
        
        # Stop audio polling while refreshing
        self._stop_audio_polling()
        
        # Kill existing process (use timeout on lock to avoid deadlock)
        acquired = self._lock.acquire(timeout=0.5)
        if acquired:
            try:
                if self._process is not None:
                    try:
                        self._process.kill()
                        self._process.wait(timeout=0.5)
                    except:
                        pass
                    self._process = None
            finally:
                self._lock.release()
        else:
            # Lock stuck - force kill via pkill
            subprocess.run(["pkill", "-9", "-f", "overlay.py"], capture_output=True, timeout=1)
        
        # Give compositor a moment to clean up the old window
        time.sleep(0.2)
        
        # Restart the process
        if not self._start_process():
            return False

        # Re-apply saved scale and position (new process has defaults)
        if self._config_ref:
            scale = self._config_ref.get("overlay_scale", 1.0)
            if scale != 1.0:
                self._send_command({"cmd": "scale", "value": scale})
            offset = self._config_ref.get("overlay_vertical_offset", 0)
            if offset != 0:
                self._send_command({"cmd": "offset", "value": offset})

        # Restore previous state (if not hidden)
        if previous_state != "hidden":
            self._send_command({"cmd": "show", "state": previous_state})
            self._current_state = previous_state
            if previous_state == "listening":
                self._start_audio_polling()

        return True


# === Main Application ===

class WayfinderApp(ctk.CTk):
    def __init__(self):
        # Disable CustomTkinter's automatic DPI scaling BEFORE the Tk root is created (CTk's
        # DPI tracker hooks in at root construction). Otherwise CTk's auto-detected scale
        # multiplies with our explicit ui_scale and the UI double-scales — notably in the
        # Flatpak sandbox on the Steam Deck's 1280x800 panel. _get_recommended_scale() +
        # set_widget_scaling(ui_scale) then governs scaling alone. (CustomTkinter >=5.2 API.)
        try:
            ctk.deactivate_automatic_dpi_awareness()
        except Exception:
            pass  # older CustomTkinter without the API — harmless
        super().__init__()

        # Set WM_CLASS so Linux desktop environments show the correct icon
        # This must match the .desktop file's StartupWMClass
        self.tk.call('tk', 'appname', 'wayfinder-aura')
        
        self.config = load_config()
        
        # Load API keys from config into environment variables
        # This makes them available to transcription and post-processing backends
        from wayfinder.config import load_api_keys_to_env
        load_api_keys_to_env(self.config)
        
        self.app_state = AppState.IDLE
        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()            # long-lived: socket listener + global shutdown
        self._evdev_stop_event = threading.Event()     # dedicated: evdev listener (restartable on its own)
        self._hotkey_thread = None                     # live evdev listener thread (for supervision)
        self._socket_thread = None                     # socket listener thread (supervised for liveness)
        self._hotkey_restart_lock = threading.Lock()   # serialize evdev restarts (config change + supervisor)
        self.session_generation = 0                    # bumped per recording / force_reset; discards stale work
        self._finish_injection_job = None              # pending after() id for the delayed overlay reset

        # Start the hotkey listeners NOW, before the heavy UI build, so the evdev thread opens the
        # input devices during setup_window/tray/ui rather than after it. A press made while the
        # app is still loading then lands in the thread-safe event_queue (instead of being lost to
        # a not-yet-listening thread) and is handled once poll_events() runs at the end of __init__.
        self.start_hotkey_listener()

        # Store tooltips that need dynamic updates (keyed by tooltip type)
        self.dynamic_tooltips: dict[str, list[ToolTip]] = {}
        
        # License/feature gate for premium features
        self.feature_gate = get_feature_gate()
        
        # Resolve audio device (intelligent selection if not explicitly set)
        self._resolved_audio_device = resolve_audio_device(self.config)
        
        # Sync the config's audio_device index with the resolved device
        # This handles cases where the device was found by name but indices shifted
        if self._resolved_audio_device is not None:
            if self.config.get("audio_device") != self._resolved_audio_device:
                self.config["audio_device"] = self._resolved_audio_device
                save_config(self.config)
        
        
        # Standard recorder for short recordings
        self.recorder = AudioRecorder(
            sample_rate=self.config["sample_rate"],
            device=self._resolved_audio_device,
            preprocessing=self.config.get("audio_preprocessing", "light"),
        )
        
        # Chunked recorder for indefinite recording
        self.chunked_recorder: ChunkedRecorder | None = None
        self.chunk_transcriptions: list[str] = []
        self.transcription_executor = ThreadPoolExecutor(max_workers=2)
        self.chunk_transcription_lock = threading.Lock()
        
        # Audio ducker for reducing other audio during recording
        duck_percent = self.config.get("audio_ducking_percent", 30)
        self.audio_ducker = AudioDucker(duck_percent=duck_percent)
        
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.logs = []
        
        # UI scaling for high-DPI screens (saved in config)
        self.ui_scale = self.config.get("ui_scale", 1.0)
        
        # Recording duration tracking
        self._recording_start_time: float | None = None
        self._duration_update_job: str | None = None
        
        # Floating status indicator (legacy CTk fallback)
        self.indicator: FloatingIndicator | None = None
        
        # PyQt6 glassmorphic overlay controller (preferred)
        self.overlay_controller: OverlayController | None = None
        self._use_pyqt_overlay = False  # Will be set based on PyQt6 availability
        
        self.setup_window()
        
        # Create indicator with voice-reactive callback
        def get_audio_level():
            """Get current audio level from the active recorder."""
            try:
                if hasattr(self, 'chunked_recorder') and self.chunked_recorder and self.chunked_recorder.is_recording():
                    return self.chunked_recorder.get_audio_level()
                elif hasattr(self, 'recorder') and self.recorder and self.recorder.is_recording():
                    return self.recorder.get_audio_level()
            except:
                pass
            return 0.0
        
        # Check which overlay type to use
        overlay_type = self.config.get("overlay_type", "always_on")

        # In frozen builds, sys.executable is the app binary, not Python —
        # launching overlay.py this way would relaunch the entire app.
        # Use CTk indicator fallback instead.
        if getattr(sys, 'frozen', False):
            overlay_type = "disappearing"

        if overlay_type == "always_on":
            # Use PyQt6 overlay (always on, no focus steal)
            try:
                import PyQt6
                initial_style = self.config.get("output_tone", "professional")
                self.overlay_controller = OverlayController(
                    audio_level_callback=get_audio_level,
                    mode="persistent",  # Always use persistent mode for always_on
                    initial_style=initial_style,
                    config=self.config,
                    log_callback=self.log,
                    # Host the tray in the overlay subprocess only when there's no in-process
                    # pystray tray (the Flatpak case) and the user hasn't disabled it — avoids
                    # a double tray on desktop Linux where pystray works.
                    want_tray=(not HAS_PYSTRAY and self.config.get("enable_tray_icon", True)),
                )
                self._use_pyqt_overlay = True
                self.log(f"✨ Using Always On indicator (PyQt6)")
                # Pre-start the subprocess to avoid focus steal
                self.overlay_controller._start_process()
                # Apply saved overlay scale and position
                overlay_scale = self.config.get("overlay_scale", 1.0)
                if overlay_scale != 1.0:
                    self.overlay_controller.set_scale(overlay_scale)
                overlay_offset = self.config.get("overlay_vertical_offset", 0)
                if overlay_offset != 0:
                    self.overlay_controller.set_vertical_offset(overlay_offset)
            except ImportError:
                self._use_pyqt_overlay = False
                self.log("⚠ PyQt6 not available, using Disappearing indicator")
        else:
            # Use CTk FloatingIndicator (disappearing)
            self._use_pyqt_overlay = False
            self.log(f"✨ Using Disappearing indicator (CTk)")
        
        # Only create CTk indicator if NOT using PyQt overlay
        if not self._use_pyqt_overlay:
            self.indicator = FloatingIndicator(
                self, 
                target_fps=self.config.get("indicator_fps", 0),
                audio_level_callback=get_audio_level
            )
        else:
            self.indicator = None
        self.setup_tray()
        self.setup_ui()
        self.setup_scaling_shortcuts()
        # Hotkey listeners were started early (see top of __init__); just supervise + poll now.
        self._start_hotkey_supervisor()
        self.poll_events()
        
        # Log animation refresh rate info
        if self._use_pyqt_overlay:
            self.log("🎯 Overlay: PyQt6 glassmorphic (60 Hz)")
        elif self.indicator:
            self.log(f"🎯 Indicator refresh: {self.indicator.get_fps_info()}")
        self.log(f"🎬 Animation refresh: {self._target_fps} Hz (monitor sync)")
        
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        
        # Only start minimized to tray if the user has enabled this option
        if self.config.get("start_minimized", False):
            self.after(100, self.hide_to_tray)
        
        # Run startup dependency checks
        self.after(300, self._check_startup_dependencies)

        # Check for model updates in background (non-blocking, once per day)
        self.after(2000, self._check_model_updates_background)

        # Start display wake-up listener for overlay recovery
        if self._use_pyqt_overlay:
            self._start_display_wake_listener()
            # Also start periodic health check for the overlay
            self._start_overlay_health_check()

    # Consistent base dimensions for the window
    BASE_WINDOW_WIDTH = 480
    BASE_WINDOW_HEIGHT = 720
    
    def setup_window(self) -> None:
        self.title("Wayfinder Aura")
        
        # Ensure window is resizable
        self.resizable(True, True)
        
        # Check if this is first run (no saved scale) - use recommended scale
        if "ui_scale" not in self.config:
            # First run: calculate optimal scale for this display
            self.ui_scale = self._get_recommended_scale()
            self.config["ui_scale"] = self.ui_scale
            save_config(self.config)
        
        # Get screen dimensions
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        
        # Account for taskbar and panels
        taskbar_height = 56
        top_panel = 32
        usable_h = screen_h - taskbar_height - top_panel
        
        # Try to restore saved window geometry
        saved_geom = self.config.get("window_geometry")
        if saved_geom:
            # Restore saved position and size
            window_w = saved_geom.get("width", 520)
            window_h = saved_geom.get("height", 780)
            window_x = saved_geom.get("x", None)
            window_y = saved_geom.get("y", None)
            
            # Validate the saved geometry is still on-screen
            if window_x is not None and window_y is not None:
                # Check if window is mostly visible
                if (window_x + window_w > 50 and window_x < screen_w - 50 and
                    window_y + window_h > 50 and window_y < screen_h - 50):
                    # Saved position is valid, use it
                    pass
                else:
                    # Window would be off-screen, center it instead
                    window_x = (screen_w - window_w) // 2
                    window_y = top_panel + (usable_h - window_h) // 2
            else:
                window_x = (screen_w - window_w) // 2
                window_y = top_panel + (usable_h - window_h) // 2
        else:
            # First run: fill the right half of the screen
            window_w = screen_w // 2
            window_h = usable_h
            
            window_x = screen_w // 2
            window_y = top_panel
        
        # Clamp to usable area
        window_h = min(window_h, usable_h)
        window_w = min(window_w, screen_w - 40)
        
        # Apply geometry
        self.geometry(f"{window_w}x{window_h}+{window_x}+{window_y}")
        
        # Set reasonable minimum size
        self.minsize(360, 500)
        self.configure(fg_color=COLORS["bg_dark"])
        
        # Apply widget scaling only - this controls content size
        # NOT window scaling - that would fight with manual window resizing
        ctk.set_widget_scaling(self.ui_scale)
        
        # Save geometry when window is moved or resized (debounced)
        self._geometry_save_pending = False
        self.bind("<Configure>", self._on_window_configure)
        
        if ICON_PATH.exists():
            try:
                from PIL import ImageTk
                icon_img = Image.open(ICON_PATH)
                self.icon_photo = ImageTk.PhotoImage(icon_img.resize((64, 64)))
                self.iconphoto(True, self.icon_photo)
            except:
                pass
        
        ctk.set_appearance_mode("dark")
    
    def _on_window_configure(self, event):
        """Handle window move/resize events - save geometry with debouncing."""
        # Only handle events for the main window, not child widgets
        if event.widget != self:
            return
        
        # Debounce: schedule a save after 500ms of no changes
        if self._geometry_save_pending:
            self.after_cancel(self._geometry_save_id)
        
        self._geometry_save_pending = True
        self._geometry_save_id = self.after(500, self._save_window_geometry)
    
    def _save_window_geometry(self):
        """Save current window geometry to config."""
        self._geometry_save_pending = False
        
        try:
            geometry = {
                "width": self.winfo_width(),
                "height": self.winfo_height(),
                "x": self.winfo_x(),
                "y": self.winfo_y(),
            }
            
            # Only save if values are reasonable (window is visible)
            if geometry["width"] > 100 and geometry["height"] > 100:
                self.config["window_geometry"] = geometry
                save_config(self.config)
        except Exception:
            pass  # Ignore errors during geometry save
    
    def setup_scaling_shortcuts(self):
        """Bind Ctrl+Plus/Minus for UI scaling."""
        self.bind("<Control-plus>", lambda e: self.scale_ui(1.1))
        self.bind("<Control-equal>", lambda e: self.scale_ui(1.1))  # Ctrl+= (plus without shift)
        self.bind("<Control-minus>", lambda e: self.scale_ui(0.9))
        self.bind("<Control-0>", lambda e: self.reset_scale())
        self.bind("<Control-r>", lambda e: self.rescue_window())  # Emergency rescue
    
    def _get_recommended_scale(self) -> float:
        """Calculate recommended UI scale based on screen resolution for READABILITY.
        
        Focus: Make text readable on high-DPI screens out of the box.
        
        Scale targets (content scale, not window size):
        - 4K (3840x2160): 200% - big readable text
        - 1440p (2560x1440): 150% 
        - 1080p (1920x1080): 100% - baseline
        - Lower: 100% minimum
        """
        screen_h = self.winfo_screenheight()
        
        # Scale based on vertical resolution (most reliable metric)
        # These are optimized for readability, not for "fitting" 
        if screen_h >= 2160:  # 4K
            recommended = 2.0
        elif screen_h >= 1440:  # 1440p
            recommended = 1.5
        elif screen_h >= 1080:  # 1080p
            recommended = 1.0
        else:  # Lower resolutions
            recommended = 1.0
        
        # Snap to 5% increments
        recommended = round(recommended * 20) / 20
        
        return recommended
    
    def rescue_window(self):
        """Emergency rescue: reset window to right half of screen at a usable size.
        
        Use this if the window gets lost off-screen or under the taskbar.
        Bound to Ctrl+R as a keyboard shortcut.
        """
        # Reset to a good readable scale
        optimal_scale = self._get_recommended_scale()
        self.ui_scale = optimal_scale
        self.config["ui_scale"] = optimal_scale
        save_config(self.config)
        
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        taskbar_height = 56
        top_panel = 32
        usable_h = screen_h - taskbar_height - top_panel
        
        new_w = screen_w // 2
        new_h = usable_h
        new_x = screen_w // 2
        new_y = top_panel
        
        # Update widget scaling only (not window scaling)
        ctk.set_widget_scaling(optimal_scale)
        
        # Apply geometry
        self.geometry(f"{new_w}x{new_h}+{new_x}+{new_y}")
        self.minsize(360, 500)
        self.update_idletasks()
        
        # Update UI indicators
        if hasattr(self, 'scale_slider_var'):
            self.scale_slider_var.set(optimal_scale)
        if hasattr(self, 'scale_value_label'):
            self.scale_value_label.configure(text=f"{int(optimal_scale * 100)}%")
        if hasattr(self, 'header_scale_label'):
            self.header_scale_label.configure(text=f"{int(optimal_scale * 100)}%")
        
        self.log(f"🛟 Window rescued! Right half at {new_w}x{new_h}, scale {int(optimal_scale*100)}%")
    
    def scale_ui(self, factor: float):
        """Scale the UI by the given factor."""
        new_scale = self.ui_scale * factor
        # Clamp between 0.7 and 2.5 (supports up to 4K displays)
        new_scale = max(0.7, min(2.5, new_scale))
        # Snap to 5% increments for cleaner values
        new_scale = round(new_scale * 20) / 20
        
        if abs(new_scale - self.ui_scale) > 0.01:
            self.ui_scale = new_scale
            self.config["ui_scale"] = new_scale
            save_config(self.config)
            self._apply_scale()
            self.log(f"⚙ UI Scale: {int(new_scale * 100)}%")
    
    def reset_scale(self):
        """Reset UI scale to optimal for current display."""
        optimal = self._get_recommended_scale()
        if abs(self.ui_scale - optimal) > 0.01:
            self.ui_scale = optimal
            self.config["ui_scale"] = optimal
            save_config(self.config)
            self._apply_scale()
            self.log(f"⚙ UI Scale: {int(optimal * 100)}% (optimal for display)")
    
    def _apply_scale(self):
        """Apply the current scale - scales content WITHOUT changing window size.
        
        This allows big readable text in a small window that scrolls.
        """
        # Only update widget scaling - this makes content bigger
        # DON'T change window size - let user control that manually
        # DON'T use set_window_scaling - that fights with manual window sizing
        ctk.set_widget_scaling(self.ui_scale)
        
        # Force layout recalculation so scrollable frames update
        self.update_idletasks()
        
        # Update all scale indicators
        if hasattr(self, 'header_scale_label'):
            self.header_scale_label.configure(text=f"{int(self.ui_scale * 100)}%")
        if hasattr(self, 'scale_value_label'):
            self.scale_value_label.configure(text=f"{int(self.ui_scale * 100)}%")
    
    def _scaled_font(self, base_size: int) -> int:
        """Return a scaled font size."""
        return int(base_size * self.ui_scale)

    def setup_ui(self) -> None:
        # === Premium Typography System (2025) ===
        # Inter & Segoe UI Variable: designed for screen legibility at all weights
        # Light text on dark BG looks thinner - use Semi-Bold/Bold for headers
        self.font_display = ("Inter", "Segoe UI Variable", "SF Pro Display", "Ubuntu")
        self.font_header = ("Inter", "Segoe UI Variable", "SF Pro Display", "Ubuntu")
        self.font_body = ("Inter", "Segoe UI Variable", "SF Pro Text", "system-ui")
        self.font_mono = ("JetBrains Mono", "Cascadia Code", "SF Mono", "monospace")

        # The UI references these as font_*[0]. In the Flatpak the design fonts (Inter /
        # JetBrains Mono) aren't installed, so Tk 8.6 substitutes a font lacking the UI's
        # symbol glyphs (sidebar ∿ ⚓ ✎ ◷, etc.) and — unlike the host's Tk — does NOT fall
        # back, rendering them as literal \uXXXX. DejaVu Sans / DejaVu Sans Mono ship in the
        # runtime and cover both Latin and those symbols, so make them the primary family.
        # Gated to IS_FLATPAK — the from-source build resolves the design fonts on the host.
        if IS_FLATPAK:
            self.font_display = ("DejaVu Sans",) + self.font_display
            self.font_header = ("DejaVu Sans",) + self.font_header
            self.font_body = ("DejaVu Sans",) + self.font_body
            self.font_mono = ("DejaVu Sans Mono",) + self.font_mono
        
        # Font size tokens - optimized for dark mode readability
        self.font_sizes = {
            "display": 18,      # Main title
            "title": 15,        # Section titles - Semi-Bold
            "heading": 13,      # Card headings - Semi-Bold
            "body": 13,         # Body text, labels
            "small": 11,        # Secondary text
            "caption": 10,      # Captions, hints
        }
        
        # Spacing tokens - 24px gutter for Bento breathing room
        self.spacing = {
            "xs": 4,
            "sm": 8,
            "md": 12,
            "lg": 16,
            "xl": 24,       # Bento gutter
            "2xl": 32,
        }
        
        # Track last transcription for Dictate tab
        self.last_transcription = ""
        
        # Main container with gradient background
        self.main_container = ctk.CTkFrame(self, fg_color=COLORS["bg_base"])
        self.main_container.pack(fill="both", expand=True)
        
        # Create gradient background canvas
        self.bg_canvas = ctk.CTkCanvas(
            self.main_container,
            highlightthickness=0,
            bg=COLORS["bg_base"],
        )
        self.bg_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.main_container.bind("<Configure>", self._draw_gradient_bg)
        
        # Content frame (non-scrollable for header + hero, tabs handle their own scrolling)
        content = ctk.CTkFrame(self.main_container, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=16)
        
        # === Header ===
        self._create_header(content)
        
        # === Hero Section (Always visible) ===
        self._create_hero_section(content)
        
        # === Main Body: Sidebar + Content ===
        body_container = ctk.CTkFrame(content, fg_color="transparent")
        body_container.pack(fill="both", expand=True)
        
        # Configure grid for sidebar layout
        body_container.grid_columnconfigure(0, weight=0, minsize=180)  # Sidebar fixed width
        body_container.grid_columnconfigure(1, weight=1)  # Content expands
        body_container.grid_rowconfigure(0, weight=1)
        
        # === Sidebar Navigation ===
        self._create_sidebar(body_container)
        
        # === Tab Content Container ===
        self.tab_content_container = ctk.CTkFrame(body_container, fg_color="transparent")
        self.tab_content_container.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        
        # Create tab frames
        self.tab_frames = {}
        self.active_tab = "settings"
        
        self._create_dictate_tab()
        self._create_settings_tab()
        self._create_style_tab()
        self._create_history_tab()
        
        # Show initial tab (Settings shows content, Dictate is clean)
        self._switch_tab("settings")
        
        # Initial log entries
        self.log("✓ Wayfinder Aura started")
        self.log(f"⌨ Hotkey: {self.get_hotkey_display()}")
        
        # Log detected hardware
        gpu_info = get_gpu_info()
        cpu_count = os.cpu_count() or 4
        if gpu_info.is_nvidia:
            self.log(f"🟢 GPU: NVIDIA detected (use Faster-Whisper + CUDA)")
        elif gpu_info.is_amd:
            self.log(f"🔴 GPU: AMD detected (Vulkan ready)")
        elif gpu_info.is_apple:
            self.log(f"🍎 GPU: {gpu_info.name}")
        elif gpu_info.is_intel:
            self.log(f"🔵 GPU: Intel detected (CPU mode recommended)")
        else:
            self.log(f"⚪ GPU: Not detected (using CPU)")
        
        threads_config = self.config.get("threads", 4)
        if cpu_count < threads_config:
            optimal = get_optimal_thread_count()
            self.log(f"💡 Tip: {cpu_count} CPU cores detected, consider setting threads to {optimal}")
    
    def _create_header(self, parent) -> None:
        """Create the app header with refined, minimal branding and scale controls."""
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))
        
        # Title container - compact and elegant
        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left")
        
        # Logo icon - navigation arrow matching brand
        logo_size = 24
        try:
            logo_img = Image.open(ICON_PATH).resize((logo_size, logo_size), Image.LANCZOS)
            self._header_logo_img = ctk.CTkImage(light_image=logo_img, dark_image=logo_img, size=(logo_size, logo_size))
            ctk.CTkLabel(
                title_frame,
                image=self._header_logo_img,
                text="",
            ).pack(side="left", padx=(0, 8))
        except Exception:
            pass  # Skip logo if icon not found
        
        # Smaller, refined logo wordmark
        ctk.CTkLabel(
            title_frame,
            text="wayfinder",
            font=(self.font_mono[0], self.font_sizes["body"]),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        # Accent dot separator
        ctk.CTkLabel(
            title_frame,
            text=" · ",
            font=(self.font_mono[0], self.font_sizes["body"]),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        # "aura" in accent - lowercase for modern feel
        ctk.CTkLabel(
            title_frame,
            text="aura",
            font=(self.font_mono[0], self.font_sizes["body"]),
            text_color=COLORS["accent"],
        ).pack(side="left")
        
        # Right side controls container
        right_controls = ctk.CTkFrame(header, fg_color="transparent")
        right_controls.pack(side="right")
        
        # Close button - minimal
        ctk.CTkButton(
            right_controls,
            text="×",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            font=(self.font_body[0], 18),
            corner_radius=RADIUS["sm"],
            command=self.hide_to_tray,
        ).pack(side="right")
        
        # === Quick Scale Controls (always visible for accessibility) ===
        scale_frame = ctk.CTkFrame(right_controls, fg_color="transparent")
        scale_frame.pack(side="right", padx=(0, 12))
        
        # Rescue button (emergency reset) - icon only
        rescue_btn = ctk.CTkButton(
            scale_frame,
            text="⟲",  # Reset/rescue icon
            width=24,
            height=24,
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            font=(self.font_body[0], 14),
            corner_radius=4,
            command=self.rescue_window,
        )
        rescue_btn.pack(side="left", padx=(0, 4))
        ToolTip(rescue_btn, "Rescue Window (Ctrl+R)\nReset size and position if window is lost")
        
        # Minus button
        minus_btn = ctk.CTkButton(
            scale_frame,
            text="−",
            width=24,
            height=24,
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
            font=(self.font_mono[0], 14),
            corner_radius=4,
            command=lambda: self.scale_ui(0.9),
        )
        minus_btn.pack(side="left", padx=1)
        ToolTip(minus_btn, "Decrease UI Scale (Ctrl+-)")
        
        # Scale percentage label
        self.header_scale_label = ctk.CTkLabel(
            scale_frame,
            text=f"{int(self.ui_scale * 100)}%",
            font=(self.font_mono[0], self.font_sizes["small"]),
            text_color=COLORS["text_secondary"],
            width=40,
        )
        self.header_scale_label.pack(side="left", padx=2)
        ToolTip(self.header_scale_label, "Current UI Scale\nCtrl+0 to reset to 100%")
        
        # Plus button
        plus_btn = ctk.CTkButton(
            scale_frame,
            text="+",
            width=24,
            height=24,
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
            font=(self.font_mono[0], 14),
            corner_radius=4,
            command=lambda: self.scale_ui(1.1),
        )
        plus_btn.pack(side="left", padx=1)
        ToolTip(plus_btn, "Increase UI Scale (Ctrl++)")
    
    def _create_hero_section(self, parent) -> None:
        """Create the hero section with visualizer and mic button."""
        # Hero card with layered depth effect
        self.hero_frame = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border"],
        )
        self.hero_frame.pack(fill="x", pady=(0, 12))
        
        hero_inner = ctk.CTkFrame(self.hero_frame, fg_color="transparent")
        hero_inner.pack(fill="x", padx=20, pady=16)
        
        # === Waveform Visualizer Canvas - elegant silk ribbon ===
        self.hero_canvas = ctk.CTkCanvas(
            hero_inner,
            height=64,  # Compact, elegant height
            bg=COLORS["bg_card"],
            highlightthickness=0,
        )
        self.hero_canvas.pack(fill="x", pady=(0, 8))
        
        # Initialize animation state
        self._hero_wave_time = 0.0
        self._hero_audio_level = 0.0
        self._hero_animation_job = None
        self._idle_breath_job = None

        # PIL-based waveform rendering (single image blit instead of 80+ Tk calls)
        self._hero_wave_items_created = False
        self._hero_wave_bar_ids = []  # Legacy compat (unused with PIL path)
        self._hero_canvas_width = 0
        self._hero_wave_image_id = None  # Canvas image item ID
        self._hero_wave_photo = None    # Keep reference to prevent GC

        # Bind resize to reinitialize
        self.hero_canvas.bind("<Configure>", self._on_hero_canvas_resize)
        
        # STABILITY FIX: Fixed 15fps for animations to prevent memory issues
        self._target_fps = 15
        self._frame_interval_ms = 66  # ~15fps
        self._fps_scale = 60.0 / self._target_fps
        
        # Schedule idle animation to start after window is shown (gives time for layout)
        self.after(200, self._start_idle_breath)
        
        # === Mic Button Container (centered) ===
        mic_container = ctk.CTkFrame(hero_inner, fg_color="transparent")
        mic_container.pack()
        
        # Glowing mic button canvas
        self.mic_button_canvas = ctk.CTkCanvas(
            mic_container,
            width=80,
            height=80,
            bg=COLORS["bg_card"],
            highlightthickness=0,
            cursor="hand2",
        )
        self.mic_button_canvas.pack()
        self.mic_button_canvas.bind("<Button-1>", lambda e: self.on_record_button())
        
        # STABILITY FIX: Pre-created mic button item tracking
        self._mic_items_created = False
        self._mic_glow_ids = []  # List of oval IDs for glow layers
        self._mic_button_id = None  # Main button circle
        self._mic_icon_ids = []  # Mic icon elements
        
        # Draw the mic button
        self._draw_mic_button(STATE_COLORS[AppState.IDLE])
        
        # Status text below button
        self.status_label = ctk.CTkLabel(
            hero_inner,
            text=STATE_LABELS[AppState.IDLE],
            font=(self.font_header[0], self.font_sizes["heading"], "bold"),
            text_color=STATE_COLORS[AppState.IDLE],
        )
        self.status_label.pack(pady=(12, 0))
        
        # Hotkey hint
        self.hotkey_label = ctk.CTkLabel(
            hero_inner,
            text=f"Press {self.get_hotkey_display()} to toggle",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_muted"],
        )
        self.hotkey_label.pack(pady=(4, 0))
    
    def _draw_mic_button(self, color: str, pressed: bool = False) -> None:
        """Draw premium tactile mic button with depth and glow."""
        canvas = self.mic_button_canvas

        # Reset item tracking since we're recreating
        self._mic_items_created = False
        self._mic_glow_ids = []
        self._mic_button_id = None

        size = 80
        cx, cy = size // 2, size // 2

        # Parse color for glow
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)

        # Background color for blending
        bg_r = int(COLORS["bg_card"][1:3], 16)
        bg_g = int(COLORS["bg_card"][3:5], 16)
        bg_b = int(COLORS["bg_card"][5:7], 16)

        # Check if this is an active state (recording or processing)
        is_active = self.app_state in [AppState.RECORDING, AppState.PROCESSING, AppState.PASTING]

        try:
            canvas.delete("all")

            if is_active:
                # Active state: outer glow effect (soft ambient light)
                glow_layers = [
                    (38, 0.06),  # Outermost - barely visible
                    (34, 0.12),
                    (30, 0.20),
                    (27, 0.30),  # Inner glow
                ]

                for radius, intensity in glow_layers:
                    gr = int(bg_r + (r - bg_r) * intensity)
                    gg = int(bg_g + (g - bg_g) * intensity)
                    gb = int(bg_b + (b - bg_b) * intensity)
                    glow_color = f"#{gr:02x}{gg:02x}{gb:02x}"
                    canvas.create_oval(
                        cx - radius, cy - radius,
                        cx + radius, cy + radius,
                        fill=glow_color, outline="",
                    )

            # Button bezel - creates tactile depth
            bezel_radius = 26

            # Outer shadow (button sits in a depression)
            shadow_color = "#050506"
            canvas.create_oval(
                cx - bezel_radius - 2, cy - bezel_radius - 1,
                cx + bezel_radius + 2, cy + bezel_radius + 3,
                fill=shadow_color, outline="",
            )

            # Button face with subtle gradient simulation
            # Darker bottom edge (3D effect)
            dr = max(0, int(r * 0.7))
            dg = max(0, int(g * 0.7))
            db = max(0, int(b * 0.7))
            dark_color = f"#{dr:02x}{dg:02x}{db:02x}"

            canvas.create_oval(
                cx - bezel_radius, cy - bezel_radius + 2,
                cx + bezel_radius, cy + bezel_radius + 2,
                fill=dark_color, outline="",
            )

            # Main button face
            main_radius = 24
            canvas.create_oval(
                cx - main_radius, cy - main_radius,
                cx + main_radius, cy + main_radius,
                fill=color, outline="",
            )

            # Top highlight (gives convex/tactile feel)
            hr = min(255, int(r * 1.2 + 40))
            hg = min(255, int(g * 1.2 + 40))
            hb = min(255, int(b * 1.2 + 40))
            highlight_color = f"#{hr:02x}{hg:02x}{hb:02x}"

            # Small arc highlight at top
            canvas.create_arc(
                cx - 18, cy - 22,
                cx + 18, cy - 6,
                start=30, extent=120,
                style="arc", outline=highlight_color,
                width=2,
            )

            # Inner shadow when pressed (concave effect)
            if pressed or is_active:
                # Subtle inner shadow ring
                inner_shadow = f"#{max(0,r-60):02x}{max(0,g-60):02x}{max(0,b-60):02x}"
                canvas.create_oval(
                    cx - main_radius + 3, cy - main_radius + 3,
                    cx + main_radius - 3, cy + main_radius - 3,
                    fill="", outline=inner_shadow, width=2,
                )

            # Microphone icon - refined, minimal
            icon_color = COLORS["bg_base"]

            # Mic body (rounded rectangle simulated with oval + rect)
            mic_w = 5
            canvas.create_oval(
                cx - mic_w, cy - 14,
                cx + mic_w, cy - 6,
                fill=icon_color, outline="",
            )
            canvas.create_rectangle(
                cx - mic_w, cy - 10,
                cx + mic_w, cy + 2,
                fill=icon_color, outline="",
            )
            canvas.create_oval(
                cx - mic_w, cy - 2,
                cx + mic_w, cy + 6,
                fill=icon_color, outline="",
            )

            # Mic cradle arc
            canvas.create_arc(
                cx - 10, cy - 2,
                cx + 10, cy + 14,
                start=180, extent=180,
                style="arc", outline=icon_color, width=2,
            )

            # Mic stand
            canvas.create_line(
                cx, cy + 13, cx, cy + 18,
                fill=icon_color, width=2, capstyle="round",
            )
        except tk.TclError:
            pass
    
    def _on_hero_canvas_resize(self, event=None):
        """Handle canvas resize - reset image item to fill new width."""
        if not self.hero_canvas:
            return
        new_width = self.hero_canvas.winfo_width()
        if new_width > 100 and abs(new_width - self._hero_canvas_width) > 20:
            self._hero_canvas_width = new_width
            # Delete old image item if any
            if self._hero_wave_image_id is not None:
                try:
                    self.hero_canvas.delete(self._hero_wave_image_id)
                except Exception:
                    pass
                self._hero_wave_image_id = None
            self._hero_wave_items_created = False
            self._init_hero_wave_items()

    def _init_hero_wave_items(self) -> None:
        """Create a single canvas image item for PIL-rendered waveform."""
        if not self.hero_canvas or self._hero_wave_items_created:
            return

        canvas = self.hero_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()

        if w <= 1 or h <= 1:
            return  # Canvas not ready yet

        # Create a single image item anchored at top-left
        from PIL import ImageTk
        # Create initial blank image
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        self._hero_wave_photo = ImageTk.PhotoImage(img)
        self._hero_wave_image_id = canvas.create_image(
            0, 0, anchor="nw", image=self._hero_wave_photo
        )
        self._hero_wave_items_created = True
    
    def _draw_hero_waveform(self) -> None:
        """Render waveform to PIL Image and blit as single canvas image (1 Tk call)."""
        if not self.hero_canvas:
            return

        # Initialize on first call
        if not self._hero_wave_items_created:
            self._init_hero_wave_items()
            return

        try:
            import math
            from PIL import ImageTk

            canvas = self.hero_canvas
            w = canvas.winfo_width()
            h = canvas.winfo_height()

            if w <= 1 or h <= 1:
                return

            center_y = h // 2
            max_amp = (h // 2) - 2
            bar_width = 3
            bar_gap = 2
            num_bars = w // (bar_width + bar_gap)

            # Parse colors
            color = STATE_COLORS.get(self.app_state, COLORS["accent"])
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)

            bg_hex = COLORS["bg_card"]
            bg_r = int(bg_hex[1:3], 16)
            bg_g = int(bg_hex[3:5], 16)
            bg_b = int(bg_hex[5:7], 16)

            # Amplitude calculation
            audio_level = self._hero_audio_level
            if not isinstance(audio_level, (int, float)) or audio_level != audio_level:
                audio_level = 0.0
            base_breath = 0.6 + 0.3 * (0.5 + 0.5 * math.sin(self._hero_wave_time * 0.3))
            voice_boost = (audio_level ** 0.4) * 2.0
            amplitude_factor = min(1.0, base_breath + voice_boost)

            _sin = math.sin
            t = self._hero_wave_time
            is_idle = (self.app_state == AppState.IDLE)

            # Pre-compute color palette (10 levels for smooth gradients)
            color_lut = []
            for a_idx in range(10):
                alpha = 0.5 + 0.05 * a_idx  # 0.5 to 0.95
                cr = int(r * alpha + bg_r * (1 - alpha))
                cg = int(g * alpha + bg_g * (1 - alpha))
                cb = int(b * alpha + bg_b * (1 - alpha))
                color_lut.append((cr, cg, cb))

            # Render to PIL Image (pure C, no Tk bridge calls)
            img = Image.new("RGB", (w, h), (bg_r, bg_g, bg_b))
            draw = ImageDraw.Draw(img)

            for i in range(num_bars):
                x = i * (bar_width + bar_gap) + bar_gap // 2

                phase = t + i * 0.2
                wave_val = (_sin(phase) * 0.4 +
                            _sin(phase * 1.7 + 0.5) * 0.35 +
                            _sin(phase * 0.6 + 1.0) * 0.25)
                abs_wave = abs(wave_val)
                bar_height = max(8, (0.3 + abs_wave * 0.7) * max_amp * amplitude_factor)

                bar_color = color_lut[min(9, int(abs_wave * 10))]

                y1 = int(center_y - bar_height)
                y2 = int(center_y + bar_height)
                draw.rectangle([x, y1, x + bar_width, y2], fill=bar_color)

            # Single Tk call: update the canvas image
            self._hero_wave_photo = ImageTk.PhotoImage(img)
            canvas.itemconfig(self._hero_wave_image_id, image=self._hero_wave_photo)
        except Exception:
            pass
    
    def _create_sidebar(self, parent) -> None:
        """Create the vertical sidebar navigation."""
        sidebar = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_surface"],
            corner_radius=RADIUS["lg"],
            width=180,
            height=380,  # Fixed height to match content area
        )
        sidebar.grid(row=0, column=0, sticky="nw")  # Don't stretch vertically
        sidebar.grid_propagate(False)
        sidebar.pack_propagate(False)
        
        # Nav items container - fills the sidebar height
        nav_container = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav_container.pack(fill="both", expand=True, padx=8, pady=12)
        
        self.tab_buttons = {}
        self.tab_colors = {}
        
        # Each tab has unique color accent (matching premium palette)
        tabs = [
            ("dictate", "∿", "Dictate", "#C4B5FD"),     # Lavender - sine wave
            ("settings", "⚙", "Settings", "#A1A1AA"),   # Zinc - neutral
            ("style", "✎", "Style", "#F9A8D4"),         # Pink - creative/tone
            ("history", "◷", "History", "#6EE7B7"),     # Soft mint
        ]
        
        for tab_id, icon, label, color in tabs:
            # Big clickable button - entire area is clickable
            btn = ctk.CTkButton(
                nav_container,
                text=f"  {icon}   {label}",
                font=(self.font_body[0], self.font_sizes["heading"]),
                fg_color="transparent",
                hover_color=COLORS["bg_hover"],
                text_color=COLORS["text_secondary"],
                corner_radius=RADIUS["md"],
                anchor="w",
                command=lambda t=tab_id: self._switch_tab(t),
            )
            btn.pack(fill="both", expand=True, pady=4)
            
            self.tab_buttons[tab_id] = btn
            self.tab_colors[tab_id] = color
    
    def _switch_tab(self, tab_id: str) -> None:
        """Switch to the specified tab."""
        # Update button styles for sidebar
        for tid, btn in self.tab_buttons.items():
            color = self.tab_colors[tid]
            
            if tid == tab_id:
                # Selected state - show colored background indent
                btn.configure(
                    fg_color=COLORS["bg_card"],
                    hover_color=COLORS["bg_card"],
                    text_color=color,  # Use the tab's unique color
                )
            else:
                # Unselected state
                btn.configure(
                    fg_color="transparent",
                    hover_color=COLORS["bg_hover"],
                    text_color=COLORS["text_secondary"],
                )
        
        # Hide all tabs, show selected
        for tid, frame in self.tab_frames.items():
            if tid == tab_id:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        
        self.active_tab = tab_id
    
    def _create_dictate_tab(self) -> None:
        """Create the Dictate tab content."""
        frame = ctk.CTkFrame(self.tab_content_container, fg_color="transparent")
        self.tab_frames["dictate"] = frame
        
        # Scrollable content
        scroll = SmoothScrollableFrame(
            frame,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
        )
        scroll.pack(fill="both", expand=True)
        
        # Last transcription card
        trans_card = ctk.CTkFrame(
            scroll,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_subtle"],
        )
        trans_card.pack(fill="x", pady=(0, 12))
        
        # Card header
        header = ctk.CTkFrame(trans_card, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 8))
        
        ctk.CTkLabel(
            header,
            text="LAST TRANSCRIPTION",
            font=(self.font_body[0], self.font_sizes["caption"], "bold"),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        # Copy button
        self.copy_btn = ctk.CTkButton(
            header,
            text="Copy",
            font=(self.font_body[0], self.font_sizes["small"]),
            width=60,
            height=24,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            corner_radius=RADIUS["sm"],
            command=self._copy_last_transcription,
        )
        self.copy_btn.pack(side="right")
        
        # Transcription text
        self.transcription_label = ctk.CTkLabel(
            trans_card,
            text="No transcription yet. Press the mic button or use your hotkey to start recording.",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["text_secondary"],
            wraplength=380,
            justify="left",
            anchor="w",
        )
        self.transcription_label.pack(fill="x", padx=16, pady=(0, 16))
        
        # Quick tips card
        tips_card = ctk.CTkFrame(
            scroll,
            fg_color=COLORS["bg_surface"],
            corner_radius=RADIUS["lg"],
        )
        tips_card.pack(fill="x")
        
        tips_inner = ctk.CTkFrame(tips_card, fg_color="transparent")
        tips_inner.pack(fill="x", padx=16, pady=12)
        
        ctk.CTkLabel(
            tips_inner,
            text="💡 Quick Tips",
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w")
        
        tips = [
            f"• Press {self.get_hotkey_display()} to start/stop recording",
            "• Speak clearly and pause at punctuation",
            "• Text is automatically typed into any focused field",
        ]
        
        for tip in tips:
            ctk.CTkLabel(
                tips_inner,
                text=tip,
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w", pady=(4, 0))
    
    def _copy_last_transcription(self) -> None:
        """Copy the last transcription to clipboard."""
        if self.last_transcription:
            self.clipboard_clear()
            self.clipboard_append(self.last_transcription)
            self.log("📋 Transcription copied to clipboard")
    
    def _create_bento_tile(self, parent, title: str, icon: str = "") -> ctk.CTkFrame:
        """Create a Bento-style tile with header and thin border."""
        tile = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_light"],  # 5% white "catches light"
        )
        
        # Tile header
        header = ctk.CTkFrame(tile, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(14, 8))
        
        header_text = f"{icon}  {title}" if icon else title
        ctk.CTkLabel(
            header,
            text=header_text.upper(),
            font=(self.font_mono[0], self.font_sizes["caption"], "bold"),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        # Content container with increased padding
        content = ctk.CTkFrame(tile, fg_color="transparent")
        content.pack(fill="x", padx=16, pady=(0, 14))
        
        return content
    
    def _create_settings_tab(self) -> None:
        """Create the Settings tab with Local/Remote processing modes."""
        frame = ctk.CTkFrame(self.tab_content_container, fg_color="transparent")
        self.tab_frames["settings"] = frame
        
        # Scrollable content
        scroll = SmoothScrollableFrame(
            frame,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
        )
        scroll.pack(fill="both", expand=True)
        
        # === BENTO TILE 1: Audio ===
        audio_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        audio_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        audio_header = ctk.CTkFrame(audio_tile, fg_color="transparent")
        audio_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            audio_header, text="🎤   A U D I O",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        audio_content = ctk.CTkFrame(audio_tile, fg_color="transparent")
        audio_content.pack(fill="x", padx=4, pady=(0, SPACING["tile_pad_y"]))
        
        mic_options, mic_current = self._get_microphone_dropdown_options()
        self.mic_var = ctk.StringVar(value=mic_current)
        self.mic_dropdown = self.create_dropdown_row(
            audio_content, "Microphone", mic_options, self.mic_var,
            self._on_microphone_selected, tooltip=SETTING_TOOLTIPS["microphone"], width=180,
        )
        
        # Audio Processing dropdown
        preprocess_options = ["Off", "Light", "Medium", "Heavy"]
        current_preprocess = self.config.get("audio_preprocessing", "light").capitalize()
        self.preprocess_var = ctk.StringVar(value=current_preprocess)
        self.preprocess_dropdown = self.create_dropdown_row(
            audio_content, "Audio Processing", preprocess_options, self.preprocess_var,
            self._on_audio_processing_selected, tooltip=SETTING_TOOLTIPS["audio_preprocessing"], width=180,
        )
        
        # Audio Calibration inline section
        self._build_audio_calibration_section(audio_content)
        
        # Audio Ducking controls
        self._build_audio_ducking_section(audio_content)
        
        # === BENTO TILE 2: Processing Mode ===
        mode_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        mode_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        mode_header = ctk.CTkFrame(mode_tile, fg_color="transparent")
        mode_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            mode_header, text="🧠   P R O C E S S I N G   M O D E",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        # Mode selector (Local | Remote)
        mode_selector_frame = ctk.CTkFrame(mode_tile, fg_color="transparent")
        mode_selector_frame.pack(fill="x", padx=SPACING["tile_pad"], pady=(0, 12))
        
        current_mode = self.config.get("processing_mode", "local")
        self.mode_selector = ModeSelector(
            mode_selector_frame,
            values=["local", "remote"],
            current_value=current_mode,
            command=self._on_processing_mode_changed,
        )
        self.mode_selector.pack(fill="x")
        
        # Dynamic content container for mode-specific settings
        self.mode_settings_container = ctk.CTkFrame(mode_tile, fg_color="transparent")
        self.mode_settings_container.pack(fill="x", padx=4, pady=(0, SPACING["tile_pad_y"]))
        
        # Build initial mode settings
        self._build_mode_settings(current_mode)
        
        # === BENTO TILE 3: System (expanded with Hotkey and Typing Speed) ===
        system_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        system_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        system_header = ctk.CTkFrame(system_tile, fg_color="transparent")
        system_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            system_header, text="⚙️   S Y S T E M",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        system_content = ctk.CTkFrame(system_tile, fg_color="transparent")
        system_content.pack(fill="x", padx=4, pady=(0, SPACING["tile_pad_y"]))
        
        # Hotkey key dropdown (inline, no popup)
        self._hotkey_key_codes = {
            "F9": 67, "F10": 68, "F8": 66, "F7": 65, "F6": 64, "F5": 63,
            "F4": 62, "F3": 61, "F2": 60, "F1": 59, "F11": 87, "F12": 88,
            "ScrollLock": 70, "Pause": 119,
            "Mouse Middle": 274, "Mouse Side": 275, "Mouse Extra": 276,
            "Mouse Forward": 277, "Mouse Back": 278,
        }
        self._hotkey_code_to_name = {v: k for k, v in self._hotkey_key_codes.items()}
        current_key_code = self.config.get("hotkey_key", 67)
        current_key_name = self._hotkey_code_to_name.get(current_key_code, "F9")
        self._hotkey_key_var = ctk.StringVar(value=current_key_name)
        self.hotkey_dropdown = self.create_dropdown_row(
            system_content, "Hotkey",
            list(self._hotkey_key_codes.keys()),
            self._hotkey_key_var, self._on_hotkey_key_changed,
            tooltip=SETTING_TOOLTIPS["hotkey"], width=160,
        )
        
        # Hotkey modifier checkboxes (inline row)
        mod_row = ctk.CTkFrame(system_content, fg_color="transparent")
        mod_row.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(
            mod_row, text="Modifiers",
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        current_mods = self.config.get("hotkey_modifiers", [])
        self._hotkey_mod_vars = {}
        for mod in ["ctrl", "alt", "shift"]:
            var = ctk.BooleanVar(value=mod in current_mods)
            self._hotkey_mod_vars[mod] = var
            ctk.CTkCheckBox(
                mod_row, text=mod.capitalize(), variable=var,
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["text_primary"],
                fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"],
                checkmark_color="#000000", width=24,
                command=self._on_hotkey_mod_changed,
            ).pack(side="right", padx=(8, 0))
        
        # UI Scale slider
        self._create_scale_slider_row(system_content)
        
        # Note: "Start minimized" toggle removed - app always starts to tray
        # Access UI via tray icon -> "Open Settings"
        
        # Hotkey devices dropdown (inline)
        # On macOS, pynput listens globally — no per-device filtering
        if sys.platform == "darwin":
            all_devices = []
            device_names = []
            device_options = ["All Devices (Global)"]
        else:
            all_devices = get_all_input_devices()
            device_names = [d["name"] for d in all_devices]
            device_options = ["All Devices"] + device_names
        enabled = self.config.get("enabled_input_devices", [])
        if not enabled or len(enabled) == len(device_names):
            current_device_selection = "All Devices"
        elif len(enabled) == 1:
            current_device_selection = enabled[0] if enabled[0] in device_names else "All Devices"
        else:
            current_device_selection = "All Devices"
        self._device_var = ctk.StringVar(value=current_device_selection)
        self._all_device_names = device_names
        self.devices_dropdown = self.create_dropdown_row(
            system_content, "Hotkey Devices", device_options,
            self._device_var, self._on_device_selected,
            tooltip=SETTING_TOOLTIPS["hotkey_devices"], width=200,
        )
        
        # === BENTO TILE: Status Overlay ===
        overlay_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        overlay_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        overlay_header = ctk.CTkFrame(overlay_tile, fg_color="transparent")
        overlay_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            overlay_header, text="🔔   S T A T U S   O V E R L A Y",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        overlay_content = ctk.CTkFrame(overlay_tile, fg_color="transparent")
        overlay_content.pack(fill="x", padx=4, pady=(0, SPACING["tile_pad_y"]))
        
        # Overlay Type dropdown
        overlay_type = self.config.get("overlay_type", "always_on")
        self.overlay_type_var = ctk.StringVar(value=overlay_type)
        overlay_type_labels = {
            "always_on": "Always On (PyQt6)",
            "disappearing": "Disappearing (CTk)",
        }
        self.overlay_type_dropdown = self.create_dropdown_row(
            overlay_content, "Indicator Style",
            list(overlay_type_labels.keys()),
            self.overlay_type_var, self.on_overlay_type_changed,
            tooltip=SETTING_TOOLTIPS.get("overlay_type", ""),
            width=180,
        )
        # Set display value
        self.overlay_type_dropdown.set(overlay_type)
        
        # Overlay Scale slider
        self._create_overlay_scale_slider_row(overlay_content)

        # Overlay Position slider
        self._create_overlay_position_slider_row(overlay_content)

        # === BENTO TILE 4: Benchmark (inline, no popup) ===
        # This tile is for local mode only - hidden in remote mode
        self.local_benchmark_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        self.local_benchmark_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        benchmark_tile = self.local_benchmark_tile  # Keep local reference for below
        
        benchmark_header = ctk.CTkFrame(benchmark_tile, fg_color="transparent")
        benchmark_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            benchmark_header, text="⏱️   B E N C H M A R K",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        benchmark_content = ctk.CTkFrame(benchmark_tile, fg_color="transparent")
        benchmark_content.pack(fill="x", padx=SPACING["tile_pad"], pady=(0, SPACING["tile_pad_y"]))
        
        # Hardware info (inline)
        sys_info = BenchmarkRunner.get_system_info()
        hw_frame = ctk.CTkFrame(benchmark_content, fg_color=COLORS["bg_hover"], corner_radius=8)
        hw_frame.pack(fill="x", pady=(0, 10))
        
        hw_text = f"CPU: {sys_info['cpu'][:45]}{'...' if len(sys_info['cpu']) > 45 else ''}\n"
        hw_text += f"GPU: {sys_info['gpu'][:45]}{'...' if len(sys_info['gpu']) > 45 else ''}\n"
        hw_text += f"RAM: {sys_info['ram']}"
        
        ctk.CTkLabel(
            hw_frame, text=hw_text,
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", padx=12, pady=10)
        
        # Results display (inline)
        self.benchmark_results_frame = ctk.CTkFrame(benchmark_content, fg_color="transparent")
        self.benchmark_results_frame.pack(fill="x", pady=(0, 10))
        self._update_benchmark_results_display()
        
        # Test button and status (inline)
        btn_row = ctk.CTkFrame(benchmark_content, fg_color="transparent")
        btn_row.pack(fill="x")
        
        self.benchmark_test_btn = ctk.CTkButton(
            btn_row,
            text="⏱️ Test Current Model",
            font=(self.font_body[0], 13, "bold"),
            height=40,
            width=180,
            corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=self._run_inline_benchmark,
        )
        self.benchmark_test_btn.pack(side="left")
        
        self.benchmark_status_label = ctk.CTkLabel(
            btn_row,
            text="",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        )
        self.benchmark_status_label.pack(side="left", padx=(15, 0))
        
        # Hide local benchmark tile if starting in Remote mode
        current_mode = self.config.get("processing_mode", "local")
        if current_mode == "remote":
            self.local_benchmark_tile.pack_forget()

        # === BENTO TILE: License ===
        license_tile = ctk.CTkFrame(scroll, fg_color=COLORS["bg_card"], corner_radius=RADIUS["md"])
        license_tile.pack(fill="x", pady=(0, SPACING["gutter"]))

        ctk.CTkLabel(
            license_tile, text="License",
            font=(self.font_header[0], self.font_sizes["caption"], "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 4))

        license_info = self.feature_gate.license_info
        is_premium = license_info.is_premium

        # Status badge
        status_text = "Premium" if is_premium else "Free"
        status_color = COLORS["accent"] if is_premium else COLORS["text_muted"]
        self._license_status_label = ctk.CTkLabel(
            license_tile, text=status_text,
            font=(self.font_body[0], 14, "bold"),
            text_color=status_color,
        )
        self._license_status_label.pack(anchor="w", padx=SPACING["tile_pad"], pady=(0, 8))

        # License key input row
        key_row = ctk.CTkFrame(license_tile, fg_color="transparent")
        key_row.pack(fill="x", padx=SPACING["tile_pad"], pady=(0, 4))

        self._license_key_entry = ctk.CTkEntry(
            key_row, placeholder_text="WV-XXXX-XXXX-XXXX-XXXX",
            font=(self.font_body[0], 12), height=34, corner_radius=8,
            fg_color=COLORS["bg_input"], text_color=COLORS["text_primary"],
            border_color=COLORS["border_subtle"],
        )
        self._license_key_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        if license_info.license_key:
            self._license_key_entry.insert(0, license_info.license_key)

        ctk.CTkButton(
            key_row, text="Activate", font=(self.font_body[0], 12, "bold"),
            width=90, height=34, corner_radius=8,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_dim"],
            text_color="#FFFFFF",
            command=self._activate_license,
        ).pack(side="left")

        # Feedback label
        self._license_feedback = ctk.CTkLabel(
            license_tile, text="",
            font=(self.font_body[0], 11), text_color=COLORS["text_muted"],
        )
        self._license_feedback.pack(anchor="w", padx=SPACING["tile_pad"], pady=(0, 4))

        # Get Premium / Deactivate buttons
        action_row = ctk.CTkFrame(license_tile, fg_color="transparent")
        action_row.pack(fill="x", padx=SPACING["tile_pad"], pady=(0, SPACING["tile_pad_y"]))

        if not is_premium:
            import webbrowser
            ctk.CTkButton(
                action_row, text="Get Premium — $20",
                font=(self.font_body[0], 12, "bold"),
                height=32, corner_radius=8,
                fg_color=COLORS["accent"], hover_color=COLORS["accent_dim"],
                text_color="#FFFFFF",
                command=lambda: webbrowser.open(self.config.get("premium_url", "https://wayfinder.dev/premium")),
            ).pack(side="left")
        else:
            ctk.CTkButton(
                action_row, text="Deactivate",
                font=(self.font_body[0], 11),
                height=28, corner_radius=6,
                fg_color=COLORS["bg_hover"], hover_color=COLORS["bg_elevated"],
                text_color=COLORS["text_muted"],
                command=self._deactivate_license,
            ).pack(side="left")

        self._settings_scroll = scroll
    
    def _update_benchmark_results_display(self):
        """Update the inline benchmark results display."""
        # Clear existing
        for widget in self.benchmark_results_frame.winfo_children():
            widget.destroy()
        
        benchmark_results = self.config.get("benchmark_results", {})
        fastest = self.config.get("benchmark_fastest_processor", None)
        
        if benchmark_results:
            # Show results in a simple format
            for model_id, result in benchmark_results.items():
                model_name = result.get("model_name", model_id)
                gpu_time = result.get("gpu_10s")
                cpu_time = result.get("cpu_10s")
                model_fastest = result.get("fastest", "")
                
                gpu_str = f"GPU: {gpu_time:.1f}s" if gpu_time else "GPU: —"
                cpu_str = f"CPU: {cpu_time:.1f}s" if cpu_time else "CPU: —"
                
                # Highlight the faster one
                if model_fastest == "gpu":
                    result_text = f"{model_name}: {gpu_str} ✓  |  {cpu_str}"
                elif model_fastest == "cpu":
                    result_text = f"{model_name}: {gpu_str}  |  {cpu_str} ✓"
                else:
                    result_text = f"{model_name}: {gpu_str}  |  {cpu_str}"
                
                ctk.CTkLabel(
                    self.benchmark_results_frame,
                    text=result_text,
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_primary"],
                ).pack(anchor="w", pady=2)
            
            # Show timestamp
            timestamps = [r.get("timestamp", 0) for r in benchmark_results.values()]
            if timestamps and max(timestamps) > 0:
                from datetime import datetime
                last_run = datetime.fromtimestamp(max(timestamps)).strftime("%b %d, %H:%M")
                ctk.CTkLabel(
                    self.benchmark_results_frame,
                    text=f"Last tested: {last_run}",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                ).pack(anchor="w", pady=(5, 0))
        else:
            ctk.CTkLabel(
                self.benchmark_results_frame,
                text="No benchmark results yet. Click 'Test Current Model' to measure speed.",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            ).pack(anchor="w")
    
    def _refresh_benchmark_tooltips(self):
        """Refresh all dynamic tooltips that depend on benchmark results."""
        tooltip_keys = ["whisper_model", "gpu_acceleration", "accuracy_mode"]
        
        for key in tooltip_keys:
            if key in self.dynamic_tooltips:
                new_text = get_dynamic_tooltip(key, self.config)
                for tooltip in self.dynamic_tooltips[key]:
                    tooltip.update_text(new_text)
        
        self.log("   📊 Updated tooltips with new benchmark data")
    
    def _run_inline_benchmark(self):
        """Run a quick benchmark on the current model with live timer feedback."""
        import subprocess
        import queue
        
        # Result queue for thread-safe communication
        result_queue = queue.Queue()
        
        # Debug logging to file
        log_file = Path.home() / ".cache" / "wayfinder-benchmark.log"
        def debug_log(msg):
            try:
                with open(log_file, "a") as f:
                    f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            except:
                pass
        
        # Clear previous log
        try:
            log_file.unlink(missing_ok=True)
        except:
            pass
        
        # Get current model with proper display name
        # Read from "model_path" config key (where UI saves the selection)
        model_path_config = self.config.get("model_path", "~/whisper.cpp/models/ggml-large-v3-turbo.bin")
        # Extract just the filename from the full path
        selected_model = Path(os.path.expanduser(model_path_config)).name
        
        # Create proper display name
        model_id = selected_model.replace("ggml-", "").replace(".bin", "")
        if model_id in WHISPER_CPP_MODELS:
            model_name = WHISPER_CPP_MODELS[model_id]["name"]
        else:
            model_name = model_id.replace("-", " ").replace("_", " ").title()
        
        self.log(f"⏱️ BENCHMARK: Starting test of {model_name}")
        debug_log(f"Starting benchmark for {model_name}")
        
        # Timer state for live feedback
        timer_state = {"running": True, "seconds": 0, "phase": "Starting", "timer_id": None}
        
        def update_timer():
            if not timer_state["running"]:
                return
            
            # Check if benchmark completed (poll the queue)
            try:
                gpu_time, cpu_time, error = result_queue.get_nowait()
                debug_log("Result retrieved from queue in timer")
                timer_state["running"] = False
                on_complete(gpu_time, cpu_time, error)
                return
            except queue.Empty:
                pass  # No result yet, continue timer
            
            timer_state["seconds"] += 1
            phase = timer_state["phase"]
            try:
                self.benchmark_test_btn.configure(text=f"⏳ {phase} {timer_state['seconds']}s")
                timer_state["timer_id"] = self.after(1000, update_timer)
            except:
                pass
        
        def stop_timer():
            timer_state["running"] = False
            if timer_state["timer_id"]:
                try:
                    self.after_cancel(timer_state["timer_id"])
                except:
                    pass
        
        # Disable button and start timer
        self.benchmark_test_btn.configure(state="disabled", text="⏳ Starting 0s", fg_color=COLORS["accent_green"])
        self.benchmark_status_label.configure(text=f"Preparing {model_name}...")
        timer_state["timer_id"] = self.after(1000, update_timer)
        
        def run_benchmark_thread():
            """Background thread to run benchmarks."""
            debug_log("Thread started")
            import tempfile
            import wave
            import numpy as np
            
            gpu_time = None
            cpu_time = None
            error = None
            
            try:
                # Find whisper-cli
                debug_log("Finding whisper-cli...")
                whisper_cli = None
                for path in [
                    Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
                    Path("/usr/bin/whisper-cli"),
                    Path("/app/bin/whisper-cli"),
                ]:
                    if path.exists():
                        whisper_cli = str(path)
                        break
                
                if not whisper_cli:
                    error = "whisper-cli not found"
                    debug_log(f"Error: {error}")
                    return
                
                # Find model
                models_dir = Path.home() / "whisper.cpp" / "models"
                if not models_dir.exists():
                    models_dir = Path("/app/share/whisper-models")
                model_path = models_dir / selected_model
                
                if not model_path.exists():
                    error = f"Model not found: {selected_model}"
                    debug_log(f"Error: {error}")
                    return
                
                debug_log(f"Binary: {whisper_cli}")
                debug_log(f"Model: {model_path}")
                self.after(0, lambda: self.log(f"   Binary: {whisper_cli}"))
                self.after(0, lambda: self.log(f"   Model: {model_path}"))
                
                # Create 10s test audio
                debug_log("Creating test audio...")
                timer_state["phase"] = "Audio"
                self.after(0, lambda: self.benchmark_status_label.configure(text="Creating test audio..."))
                
                sample_rate = 16000
                duration = 10
                samples = duration * sample_rate
                t = np.linspace(0, duration, samples)
                speech = np.sin(2 * np.pi * 200 * t) * 0.3 + np.sin(2 * np.pi * 400 * t) * 0.2
                speech += np.random.randn(samples) * 0.1
                speech = (speech / np.max(np.abs(speech)) * 0.7 * 32767).astype(np.int16)
                
                test_audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
                with wave.open(test_audio.name, "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(sample_rate)
                    wav.writeframes(speech.tobytes())
                
                debug_log(f"Audio created: {test_audio.name}")
                
                try:
                    # GPU TEST
                    debug_log("Starting GPU test...")
                    timer_state["phase"] = "GPU"
                    self.after(0, lambda: self.benchmark_status_label.configure(text="Testing GPU..."))
                    self.after(0, lambda: self.log("   🔥 GPU test starting..."))
                    
                    cmd_gpu = [whisper_cli, "-m", str(model_path), "-f", test_audio.name, 
                               "-t", "6", "--no-timestamps", "--no-prints"]
                    
                    start = time.perf_counter()
                    result = subprocess.run(cmd_gpu, capture_output=True, timeout=60)
                    gpu_elapsed = time.perf_counter() - start
                    debug_log(f"GPU done: {gpu_elapsed:.2f}s, exit={result.returncode}")
                    
                    if result.returncode == 0:
                        gpu_time = gpu_elapsed
                        self.after(0, lambda t=gpu_time: self.log(f"   ✅ GPU: {t:.2f}s"))
                    else:
                        stderr = result.stderr.decode('utf-8', errors='replace')[:200]
                        debug_log(f"GPU stderr: {stderr}")
                        self.after(0, lambda: self.log(f"   ⚠️ GPU failed: exit {result.returncode}"))
                    
                    # CPU TEST  
                    debug_log("Starting CPU test...")
                    timer_state["phase"] = "CPU"
                    self.after(0, lambda: self.benchmark_status_label.configure(text="Testing CPU..."))
                    self.after(0, lambda: self.log("   🧠 CPU test starting..."))
                    
                    cmd_cpu = cmd_gpu + ["--no-gpu"]
                    
                    start = time.perf_counter()
                    result = subprocess.run(cmd_cpu, capture_output=True, timeout=120)
                    cpu_elapsed = time.perf_counter() - start
                    debug_log(f"CPU done: {cpu_elapsed:.2f}s, exit={result.returncode}")
                    
                    if result.returncode == 0:
                        cpu_time = cpu_elapsed
                        self.after(0, lambda t=cpu_time: self.log(f"   ✅ CPU: {t:.2f}s"))
                    else:
                        stderr = result.stderr.decode('utf-8', errors='replace')[:200]
                        debug_log(f"CPU stderr: {stderr}")
                        self.after(0, lambda: self.log(f"   ⚠️ CPU failed: exit {result.returncode}"))
                        
                except subprocess.TimeoutExpired as e:
                    error = f"Test timed out: {e}"
                    debug_log(f"Timeout: {e}")
                    self.after(0, lambda: self.log(f"   ⚠️ {error}"))
                finally:
                    try:
                        os.unlink(test_audio.name)
                    except:
                        pass
                        
            except Exception as e:
                error = str(e)
                import traceback
                debug_log(f"Exception: {e}\n{traceback.format_exc()}")
                self.after(0, lambda: self.log(f"   ❌ Error: {error}"))
            
            debug_log(f"Benchmark complete: GPU={gpu_time}, CPU={cpu_time}, error={error}")
            # Put result in queue for main thread to pick up
            result_queue.put((gpu_time, cpu_time, error))
            debug_log("Result placed in queue")
        
        def on_complete(gpu_time, cpu_time, error):
            """Handle benchmark completion on main thread."""
            try:
                debug_log(f"on_complete EXECUTING: GPU={gpu_time}, CPU={cpu_time}, error={error}")
                stop_timer()
                
                # Reset button
                self.benchmark_test_btn.configure(
                    state="normal",
                    text="⏱️ Test Current Model",
                    fg_color=COLORS["accent"],
                )
                debug_log("Button reset done")
                
                if error:
                    self.benchmark_status_label.configure(text=f"Error: {error}")
                    self.log(f"   ❌ BENCHMARK FAILED: {error}")
                    debug_log(f"Error case handled: {error}")
                    return
                
                # Log summary
                self.log(f"   🏁 BENCHMARK COMPLETE")
                if gpu_time and cpu_time:
                    faster = "GPU" if gpu_time < cpu_time else "CPU"
                    speedup = max(gpu_time, cpu_time) / min(gpu_time, cpu_time)
                    self.log(f"      🚀 {faster} is {speedup:.1f}x faster!")
                debug_log("Summary logged")
            except Exception as ex:
                import traceback
                debug_log(f"EXCEPTION in on_complete: {ex}\n{traceback.format_exc()}")
            
            # Determine model_id from filename
            model_id = selected_model.replace("ggml-", "").replace(".bin", "")
            
            # Save results
            existing = self.config.get("benchmark_results", {})
            existing[model_id] = {
                "model_name": model_name,
                "gpu_10s": round(gpu_time, 2) if gpu_time else None,
                "cpu_10s": round(cpu_time, 2) if cpu_time else None,
                "fastest": "gpu" if (gpu_time and cpu_time and gpu_time < cpu_time) else "cpu" if cpu_time else None,
                "timestamp": int(time.time()),
            }
            self.config["benchmark_results"] = existing
            
            # Determine overall fastest
            gpu_wins = sum(1 for r in existing.values() if r.get("fastest") == "gpu")
            cpu_wins = sum(1 for r in existing.values() if r.get("fastest") == "cpu")
            self.config["benchmark_fastest_processor"] = "gpu" if gpu_wins > cpu_wins else "cpu"
            save_config(self.config)
            
            # Update display and refresh tooltips with new benchmark data
            self._update_benchmark_results_display()
            self._refresh_benchmark_tooltips()
            
            # Show completion message
            if gpu_time and cpu_time:
                faster = "GPU" if gpu_time < cpu_time else "CPU"
                speedup = max(gpu_time, cpu_time) / min(gpu_time, cpu_time)
                self.benchmark_status_label.configure(
                    text=f"✓ {faster} is {speedup:.1f}x faster (GPU:{gpu_time:.1f}s CPU:{cpu_time:.1f}s)"
                )
            elif gpu_time:
                self.benchmark_status_label.configure(text=f"✓ GPU: {gpu_time:.1f}s (CPU failed)")
            elif cpu_time:
                self.benchmark_status_label.configure(text=f"✓ CPU: {cpu_time:.1f}s (GPU failed)")
            else:
                self.benchmark_status_label.configure(text="Both tests failed")
        
        # Start benchmark in background thread
        threading.Thread(target=run_benchmark_thread, daemon=True).start()
    
    def _run_api_benchmark(self):
        """Run an API latency benchmark for remote transcription (Groq or OpenAI Whisper API)."""
        import subprocess
        import tempfile
        import wave
        import numpy as np
        
        # Determine which provider is selected
        backend = self.config.get("transcription_backend", "groq_whisper")
        is_groq = backend == "groq_whisper"
        provider_name = "Groq" if is_groq else "OpenAI"
        
        # Check if API key is configured (from config first, then environment)
        if is_groq:
            api_key = self.config.get("groq_api_key", "") or os.environ.get("GROQ_API_KEY", "")
        else:
            api_key = self.config.get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        
        if not api_key:
            self.api_benchmark_status_label.configure(text=f"❌ {provider_name} API key not configured")
            return
        
        # Disable button and show progress
        self.api_benchmark_btn.configure(state="disabled", text="Testing...", fg_color=COLORS["bg_surface"])
        self.api_benchmark_status_label.configure(text="Creating test audio...")
        self.log(f"🌐 API BENCHMARK: Testing {provider_name} Whisper latency...")
        
        # Timer display
        elapsed = [0]
        timer_running = [True]
        
        def update_timer():
            if timer_running[0]:
                elapsed[0] += 1
                try:
                    # Check if widget still exists before updating
                    if hasattr(self, 'api_benchmark_status_label') and self.api_benchmark_status_label.winfo_exists():
                        self.api_benchmark_status_label.configure(text=f"Testing... {elapsed[0]}s")
                        self.after(1000, update_timer)
                    else:
                        # Widget destroyed, stop timer
                        timer_running[0] = False
                except Exception:
                    # Widget destroyed or other error, stop timer
                    timer_running[0] = False
        
        def stop_timer():
            timer_running[0] = False
        
        self.after(1000, update_timer)
        
        def create_test_audio(duration: int = 10) -> str:
            """Create a 10-second test audio file with speech-like noise."""
            sample_rate = 16000
            samples = int(duration * sample_rate)
            
            # Create pseudo-speech: modulated noise with pauses
            t = np.linspace(0, duration, samples)
            
            # Base speech-like signal
            speech = np.sin(2 * np.pi * 200 * t) * 0.3  # Base tone
            speech += np.sin(2 * np.pi * 400 * t) * 0.2  # Harmonic
            speech += np.random.randn(samples) * 0.1    # Noise
            
            # Add envelope to simulate words
            envelope = np.abs(np.sin(2 * np.pi * 2 * t)) ** 0.5
            speech *= envelope
            
            # Normalize
            speech = speech / np.max(np.abs(speech)) * 0.7
            
            # Convert to int16
            audio_int16 = (speech * 32767).astype(np.int16)
            
            # Save to temp file
            temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            with wave.open(temp_file.name, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(audio_int16.tobytes())
            
            return temp_file.name
        
        def run_api_test():
            """Run the actual API latency test."""
            audio_file = None
            try:
                # Create test audio (use shorter 5s for faster testing)
                audio_file = create_test_audio(5)
                self.after(0, lambda: self.log("   📁 Created 5s test audio"))
                
                # Time the API call based on provider
                if is_groq:
                    # Import Groq client
                    try:
                        import groq
                    except ImportError:
                        return None, "groq package not installed"
                    
                    self.after(0, lambda: self.log("   ⚡ Creating Groq client..."))
                    
                    # Create client with shorter timeout
                    client = groq.Groq(api_key=api_key, timeout=30.0)
                    
                    self.after(0, lambda: self.log("   ⚡ Sending to Groq..."))
                    start_time = time.perf_counter()
                    
                    with open(audio_file, "rb") as f:
                        transcription = client.audio.transcriptions.create(
                            model="whisper-large-v3",
                            file=f,
                            response_format="text",
                            language="en",
                        )
                    
                    latency = time.perf_counter() - start_time
                    self.after(0, lambda l=latency: self.log(f"   ✓ Groq response: {l:.2f}s"))
                else:
                    # Import OpenAI client
                    try:
                        import openai
                        import httpx
                    except ImportError as ie:
                        missing = "openai" if "openai" not in str(ie) else "httpx"
                        return None, f"{missing} package not installed"
                    
                    # Create client with explicit timeout configuration for file uploads
                    # Use httpx.Timeout for proper control over connect/read/write timeouts
                    timeout_config = httpx.Timeout(
                        connect=10.0,   # Connection timeout
                        read=60.0,      # Read timeout (waiting for response)
                        write=30.0,     # Write timeout (uploading file)
                        pool=10.0,      # Pool timeout
                    )
                    client = openai.OpenAI(api_key=api_key, timeout=timeout_config)
                    
                    self.after(0, lambda: self.log("   ☁️ Sending to OpenAI..."))
                    start_time = time.perf_counter()
                    
                    with open(audio_file, "rb") as f:
                        transcription = client.audio.transcriptions.create(
                            model="whisper-1",
                            file=f,
                            response_format="text",
                            language="en",
                        )
                    
                    latency = time.perf_counter() - start_time
                    self.after(0, lambda l=latency: self.log(f"   ✓ OpenAI response: {l:.2f}s"))
                
                return latency, None
                
            except Exception as e:
                import traceback
                error_msg = f"{type(e).__name__}: {str(e)}"
                self.after(0, lambda msg=error_msg: self.log(f"   ❌ Error: {msg}"))
                traceback.print_exc()
                return None, error_msg
            finally:
                # Cleanup
                if audio_file and os.path.exists(audio_file):
                    try:
                        os.unlink(audio_file)
                    except:
                        pass
        
        def on_complete(latency, error):
            # Stop timer FIRST
            stop_timer()
            
            # Log completion
            self.log(f"   📊 API test completed")
            
            # Reset button (with widget existence check)
            try:
                if hasattr(self, 'api_benchmark_btn') and self.api_benchmark_btn.winfo_exists():
                    self.api_benchmark_btn.configure(
                        state="normal",
                        text="☁️ Test API Latency",
                        fg_color=COLORS["accent"],
                    )
            except Exception as e:
                self.log(f"   ⚠ UI update error: {e}")
            
            if error:
                try:
                    if hasattr(self, 'api_benchmark_status_label') and self.api_benchmark_status_label.winfo_exists():
                        self.api_benchmark_status_label.configure(text=f"❌ {error[:40]}")
                except Exception:
                    pass
                self.log(f"   ❌ API BENCHMARK FAILED: {error}")
                return
            
            # Log results
            self.log(f"   🏁 API BENCHMARK COMPLETE: {latency:.2f}s latency")
            
            # Save results
            api_results = self.config.get("api_benchmark_results", {})
            result_key = "groq" if is_groq else "openai"
            api_results[result_key] = {
                "latency_5s": round(latency, 2),
                "timestamp": int(time.time()),
            }
            self.config["api_benchmark_results"] = api_results
            save_config(self.config)
            
            # Update display
            self._update_api_benchmark_display()
            
            # Show completion message (with widget existence check)
            try:
                if hasattr(self, 'api_benchmark_status_label') and self.api_benchmark_status_label.winfo_exists():
                    self.api_benchmark_status_label.configure(text=f"✓ Done! Latency: {latency:.1f}s")
            except Exception:
                pass
        
        def background_thread():
            # Use a hard timeout failsafe (90 seconds max) in case the API call hangs
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
            
            hard_timeout = 90  # seconds
            
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_api_test)
                try:
                    latency, error = future.result(timeout=hard_timeout)
                except FuturesTimeoutError:
                    latency, error = None, f"Timeout: API call exceeded {hard_timeout}s"
                    self.after(0, lambda: self.log(f"   ⏱️ Hard timeout triggered after {hard_timeout}s"))
                except Exception as e:
                    latency, error = None, f"Unexpected error: {e}"
            
            self.after(0, lambda: on_complete(latency, error))
        
        threading.Thread(target=background_thread, daemon=True).start()
    
    def _update_api_benchmark_display(self):
        """Update the API benchmark results display in Remote mode settings."""
        if not hasattr(self, 'api_benchmark_results_frame') or not self.api_benchmark_results_frame.winfo_exists():
            return
        
        # Clear existing
        for widget in self.api_benchmark_results_frame.winfo_children():
            widget.destroy()
        
        api_results = self.config.get("api_benchmark_results", {})
        
        # Determine current provider
        backend = self.config.get("transcription_backend", "groq_whisper")
        is_groq = backend == "groq_whisper"
        result_key = "groq" if is_groq else "openai"
        provider_name = "Groq" if is_groq else "OpenAI"
        
        if api_results and result_key in api_results:
            result = api_results[result_key]
            # Support both old 10s and new 5s results
            latency = result.get("latency_5s") or result.get("latency_10s")
            audio_duration = "5s" if result.get("latency_5s") else "10s"
            timestamp = result.get("timestamp", 0)
            
            if latency:
                ctk.CTkLabel(
                    self.api_benchmark_results_frame,
                    text=f"{provider_name} Whisper: {latency:.1f}s ({audio_duration} audio)",
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_primary"],
                ).pack(anchor="w", pady=2)
            
            # Show timestamp
            if timestamp > 0:
                from datetime import datetime
                last_run = datetime.fromtimestamp(timestamp).strftime("%b %d, %H:%M")
                ctk.CTkLabel(
                    self.api_benchmark_results_frame,
                    text=f"Last tested: {last_run}",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                ).pack(anchor="w", pady=(5, 0))
        else:
            ctk.CTkLabel(
                self.api_benchmark_results_frame,
                text="No API benchmark results yet.",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            ).pack(anchor="w")
    
    def _build_mode_settings(self, mode: str) -> None:
        """Build the settings panel for the selected processing mode."""
        # Validate and reset post-processing backend based on mode
        current_backend = self.config.get("post_processing_backend", "llama_cpp")
        if mode == "local":
            # Local mode: only allow llama_cpp backend
            if current_backend != "llama_cpp":
                self.config["post_processing_backend"] = "llama_cpp"
                save_config(self.config)
        # Remote mode uses cloud backends
        elif mode == "remote":
            if current_backend not in ["openai", "anthropic"]:
                self.config["post_processing_backend"] = "openai"
                save_config(self.config)
        
        # Show/hide local benchmark tile based on mode
        # Local uses local whisper, Remote doesn't
        if hasattr(self, 'local_benchmark_tile') and self.local_benchmark_tile.winfo_exists():
            if mode == "remote":
                self.local_benchmark_tile.pack_forget()
            else:
                # Re-pack if it was hidden (after the settings scroll content)
                self.local_benchmark_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        # Clear existing content
        for widget in self.mode_settings_container.winfo_children():
            widget.destroy()
        
        if mode == "local":
            self._build_local_mode_settings(self.mode_settings_container)
        elif mode == "remote":
            self._build_remote_mode_settings(self.mode_settings_container)
    
    def _build_local_mode_settings(self, parent) -> None:
        """Build settings panel for Local mode (100% private, on-device processing)."""
        # Privacy indicator
        privacy_frame = ctk.CTkFrame(parent, fg_color="#1A2A1A", corner_radius=RADIUS["sm"])
        privacy_frame.pack(fill="x", padx=SPACING["tile_pad"]-4, pady=(0, 12))
        
        ctk.CTkLabel(
            privacy_frame,
            text="🔒  100% Private — All processing happens on your device",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color="#7BCF7B",
        ).pack(padx=12, pady=8)
        
        # Whisper Model
        model_display = self.get_model_display()
        self.model_btn = self.create_setting_row(
            parent, "Whisper Model", model_display,
            self.open_model_settings, tooltip=get_dynamic_tooltip("whisper_model", self.config),
            tooltip_key="whisper_model",
        )
        
        # Note: Prompt button removed - now configured via Style tab
        
        # GPU Acceleration toggle
        self.gpu_var = ctk.BooleanVar(value=self.config.get("use_gpu", True))
        self.create_toggle_row(
            parent, "GPU Acceleration",
            self.gpu_var, self.toggle_gpu,
            tooltip=get_dynamic_tooltip("gpu_acceleration", self.config),
            tooltip_key="gpu_acceleration",
        )
        
        # Accuracy Mode
        accuracy_mode = self.config.get("accuracy_mode", "balanced")
        self.accuracy_mode_var = ctk.StringVar(value=accuracy_mode)
        self.accuracy_mode_dropdown = self.create_dropdown_row(
            parent, "Accuracy Mode", ["fast", "balanced", "high"],
            self.accuracy_mode_var, self.on_accuracy_mode_changed,
            tooltip=get_dynamic_tooltip("accuracy_mode", self.config), width=140,
            tooltip_key="accuracy_mode",
        )
        
        # Language
        language = self.config.get("language", "en")
        self.language_var = ctk.StringVar(value=language)
        self.language_dropdown = self.create_dropdown_row(
            parent, "Language",
            ["en", "auto", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "zh", "ja", "ko"],
            self.language_var, self.on_language_changed,
            tooltip=SETTING_TOOLTIPS["language"], width=100,
        )
        
        # Backend (whisper_cpp or faster_whisper only for local)
        backend = self.config.get("transcription_backend", "whisper_cpp")
        if backend in ("openai_whisper", "groq_whisper"):
            backend = "whisper_cpp"  # Default to local backend
        self.backend_var = ctk.StringVar(value=backend)
        self.backend_dropdown = self.create_dropdown_row(
            parent, "Backend", ["whisper_cpp", "faster_whisper"],
            self.backend_var, self.on_backend_changed,
            tooltip=SETTING_TOOLTIPS["backend"], width=160,
        )
        
        # Chunked Mode toggle (for unlimited recording length)
        self.chunked_var = ctk.BooleanVar(value=self.config.get("chunked_mode", True))
        self.create_toggle_row(
            parent, "Chunked Mode",
            self.chunked_var, self.toggle_chunked_mode,
            tooltip="Split long recordings into segments for unlimited duration.\n\n✅ Enables recordings of any length\n✅ Transcribes while you speak\n⚠️ Small overhead at chunk boundaries\n\nRecommended for recordings >30 seconds.",
        )
        
        # === Post-Processing Section ===
        self._create_mode_section_header(parent, "Post-Processing (LLM Cleanup)")
        
        # Post-processing toggle
        postproc_enabled = self.config.get("post_processing_enabled", True)
        self.postproc_enabled_var = ctk.BooleanVar(value=postproc_enabled)
        self.create_toggle_row(
            parent, "Enable Post-Processing",
            self.postproc_enabled_var, self.toggle_post_processing,
            tooltip="Clean up transcriptions using a local LLM.\nRemoves filler words (um, uh, like) and fixes punctuation.\nRuns 100% on your device — no data sent anywhere.",
        )
        
        # Post-processing backend and options (only show if enabled)
        if postproc_enabled:
            postproc_backend = self.config.get("post_processing_backend", "llama_cpp")
            # Ensure we're using llama_cpp for local mode
            if postproc_backend != "llama_cpp":
                postproc_backend = "llama_cpp"
                self.config["post_processing_backend"] = postproc_backend
                save_config(self.config)
            
            # Inline model management section (no popups)
            self._build_inline_model_section(parent, postproc_backend)
            
            # Note: Format template removed - now uses Style tab settings (output_tone + smart_formatting)
    
    def _build_remote_mode_settings(self, parent) -> None:
        """Build settings panel for Remote mode (full cloud transcription)."""
        # Cloud warning
        warning_frame = ctk.CTkFrame(parent, fg_color="#2A1A1A", corner_radius=RADIUS["sm"])
        warning_frame.pack(fill="x", padx=SPACING["tile_pad"]-4, pady=(0, 12))
        
        ctk.CTkLabel(
            warning_frame,
            text="☁️  Audio recordings will be sent to cloud for transcription",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color="#CF7B7B",
        ).pack(padx=12, pady=8)
        
        # Provider selector (Groq vs OpenAI Whisper)
        current_backend = self.config.get("transcription_backend", "groq_whisper")
        if current_backend not in ("groq_whisper", "openai_whisper"):
            current_backend = "groq_whisper"  # Default to Groq for speed
        
        # Map backend IDs to display names
        self._remote_provider_map = {
            "⚡ Groq (Fast)": "groq_whisper",
            "☁️ OpenAI": "openai_whisper",
        }
        self._remote_provider_reverse = {v: k for k, v in self._remote_provider_map.items()}
        display_value = self._remote_provider_reverse.get(current_backend, "⚡ Groq (Fast)")
        
        self.remote_provider_var = ctk.StringVar(value=display_value)
        self.remote_provider_dropdown = self.create_dropdown_row(
            parent, "Provider",
            ["⚡ Groq (Fast)", "☁️ OpenAI"],
            self.remote_provider_var, self._on_remote_provider_changed,
            tooltip="⚡ Groq: Ultra-fast (~0.3s for 10s audio), free tier available\n☁️ OpenAI: Reliable, per-minute billing",
            width=160,
        )
        
        # Language
        language = self.config.get("language", "en")
        self.language_var = ctk.StringVar(value=language)
        self.language_dropdown = self.create_dropdown_row(
            parent, "Language",
            ["en", "auto", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "zh", "ja", "ko"],
            self.language_var, self.on_language_changed,
            tooltip=SETTING_TOOLTIPS["language"], width=100,
        )
        
        # API Configuration - dynamic based on provider
        backend = self.config.get("transcription_backend", "groq_whisper")
        if backend == "groq_whisper":
            api_key = self.config.get("groq_api_key", "") or os.environ.get("GROQ_API_KEY", "")
            status = "Configured ✓" if api_key else "Not configured"
            tooltip = "Configure Groq API key for ultra-fast cloud transcription\nGet free key at: console.groq.com/keys"
        else:
            api_key = self.config.get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
            status = "Configured ✓" if api_key else "Not configured"
            tooltip = "Configure OpenAI API key for cloud transcription\nGet key at: platform.openai.com/api-keys"
        
        self.remote_api_btn = self.create_setting_row(
            parent, "API Configuration", status,
            self.open_remote_api_settings,
            tooltip=tooltip,
        )
        
        # Info about no local model needed
        self.remote_info_frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_input"], corner_radius=RADIUS["sm"])
        self.remote_info_frame.pack(fill="x", padx=SPACING["tile_pad"]-4, pady=(12, 0))
        
        provider_name = "Groq" if current_backend == "groq_whisper" else "OpenAI"
        self.remote_info_label = ctk.CTkLabel(
            self.remote_info_frame,
            text=f"ℹ️  No local model needed — transcription runs on {provider_name} servers",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_muted"],
        )
        self.remote_info_label.pack(padx=12, pady=8)
        
        # === API Benchmark Section ===
        benchmark_header = ctk.CTkFrame(parent, fg_color="transparent")
        benchmark_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(16, 8))
        ctk.CTkLabel(
            benchmark_header, text="⏱️   B E N C H M A R K",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        # Results display
        self.api_benchmark_results_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.api_benchmark_results_frame.pack(fill="x", padx=SPACING["tile_pad"], pady=(0, 10))
        self._update_api_benchmark_display()
        
        # Test button and status
        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=SPACING["tile_pad"])
        
        self.api_benchmark_btn = ctk.CTkButton(
            btn_row,
            text="☁️ Test API Latency",
            font=(self.font_body[0], 13, "bold"),
            height=40,
            width=180,
            corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=self._run_api_benchmark,
        )
        self.api_benchmark_btn.pack(side="left")
        
        self.api_benchmark_status_label = ctk.CTkLabel(
            btn_row,
            text="",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        )
        self.api_benchmark_status_label.pack(side="left", padx=(15, 0))
        
        # === Post-Processing Section (LLM text cleanup for caricature mode, etc.) ===
        postproc_header = ctk.CTkFrame(parent, fg_color="transparent")
        postproc_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(16, 8))
        ctk.CTkLabel(
            postproc_header, text="🎨   P O S T - P R O C E S S I N G",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        # Post-processing toggle
        postproc_enabled = self.config.get("post_processing_enabled", True)
        self.postproc_enabled_var = ctk.BooleanVar(value=postproc_enabled)
        self.create_toggle_row(
            parent, "Enable Text Cleanup",
            self.postproc_enabled_var, self.toggle_post_processing,
            tooltip="Use cloud LLM (GPT-4o-mini or Claude) to clean up transcriptions.\nRequired for Caricature mode, Strong mode, and advanced style options.",
        )
        
        # Post-processing backend options (only show if enabled)
        if postproc_enabled:
            postproc_backend = self.config.get("post_processing_backend", "openai")
            # Ensure we're using a cloud backend for remote mode
            if postproc_backend not in ["openai", "anthropic"]:
                postproc_backend = "openai"
                self.config["post_processing_backend"] = postproc_backend
                save_config(self.config)
            self.postproc_backend_var = ctk.StringVar(value=postproc_backend)
            
            # Provider dropdown (OpenAI vs Anthropic)
            self.postproc_backend_dropdown = self.create_dropdown_row(
                parent, "LLM Provider", ["openai", "anthropic"],
                self.postproc_backend_var, self.on_postproc_backend_changed,
                tooltip="openai: GPT-4o-mini - Fast and affordable\nanthropic: Claude Haiku - High quality",
                width=140,
            )
            
            # API Configuration button
            if postproc_backend == "openai":
                api_key = self.config.get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
            else:
                api_key = self.config.get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
            
            postproc_api_status = "Configured ✓" if api_key else "Not configured"
            self.postproc_config_btn = self.create_setting_row(
                parent, "LLM API Settings", postproc_api_status,
                self.open_postproc_settings,
                tooltip="Configure API key and model for LLM post-processing",
            )
    
    def _create_mode_section_header(self, parent, text: str) -> None:
        """Create a section header within mode settings."""
        ctk.CTkLabel(
            parent,
            text=text.upper(),
            font=(self.font_body[0], self.font_sizes["caption"], "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=SPACING["tile_pad"], pady=(8, 8))
    
    def _on_remote_provider_changed(self, display_value: str) -> None:
        """Handle remote provider selection change."""
        backend_id = self._remote_provider_map.get(display_value, "groq_whisper")
        self.config["transcription_backend"] = backend_id
        save_config(self.config)
        
        # Update the info label
        provider_name = "Groq" if backend_id == "groq_whisper" else "OpenAI"
        if hasattr(self, "remote_info_label"):
            self.remote_info_label.configure(
                text=f"ℹ️  No local model needed — transcription runs on {provider_name} servers"
            )
        
        # Update API status
        self._update_remote_api_status()
        
        self.log(f"⚙ Remote provider changed to: {provider_name}")
    
    def _update_remote_api_status(self) -> None:
        """Update the API configuration status based on selected provider."""
        backend = self.config.get("transcription_backend", "groq_whisper")
        
        if backend == "groq_whisper":
            # Check config first, then environment
            api_key = self.config.get("groq_api_key", "") or os.environ.get("GROQ_API_KEY", "")
            status = "Configured ✓" if api_key else "Not configured"
        else:
            api_key = self.config.get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
            status = "Configured ✓" if api_key else "Not configured"
        
        # Update existing button if it exists (button is returned directly from create_setting_row)
        if hasattr(self, "remote_api_btn") and self.remote_api_btn:
            self.remote_api_btn.configure(text=status)
    
    def open_remote_api_settings(self) -> None:
        """Open dialog to configure remote API settings (Groq or OpenAI)."""
        self._open_remote_api_dialog()
    
    def _open_remote_api_dialog(self) -> None:
        """Open dialog to configure the selected remote transcription API (Groq or OpenAI)."""
        backend = self.config.get("transcription_backend", "groq_whisper")
        is_groq = backend == "groq_whisper"
        
        dialog = ctk.CTkToplevel(self)
        dialog.title("Groq API Configuration" if is_groq else "OpenAI API Configuration")
        dialog.transient(self)
        dialog.grab_set()
        self._setup_dialog(dialog, 520, 440)
        
        # Header
        ctk.CTkLabel(
            dialog,
            text="⚡ Groq API Configuration" if is_groq else "☁️ OpenAI API Configuration",
            font=(self.font_header[0], self.font_sizes["title"], "bold"),
            text_color=COLORS["text_bright"],
        ).pack(padx=24, pady=(24, 4))
        
        # Description
        if is_groq:
            desc = "Ultra-fast cloud transcription using Groq's LPU hardware.\n~10x faster than local GPU • Free tier: 14,400 requests/day"
        else:
            desc = "Reliable cloud transcription using OpenAI's Whisper API.\nPer-minute billing • High accuracy"
        
        ctk.CTkLabel(
            dialog,
            text=desc,
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_muted"],
            justify="center",
        ).pack(padx=24, pady=(0, 16))
        
        # API Key input
        form_frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=RADIUS["md"])
        form_frame.pack(fill="x", padx=24, pady=8)
        
        env_var = "GROQ_API_KEY" if is_groq else "OPENAI_API_KEY"
        config_key = "groq_api_key" if is_groq else "openai_api_key"
        # Try config first, then environment
        current_key = self.config.get(config_key, "") or os.environ.get(env_var, "")
        
        ctk.CTkLabel(
            form_frame,
            text="API Key",
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=16, pady=(16, 4))
        
        key_var = ctk.StringVar(value=current_key)
        key_entry = ctk.CTkEntry(
            form_frame,
            textvariable=key_var,
            font=(self.font_mono[0], self.font_sizes["small"]),
            fg_color=COLORS["bg_input"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            width=380,
            height=40,
            show="•",
        )
        key_entry.pack(padx=16, pady=(0, 8))
        
        # Show/hide toggle
        show_var = ctk.BooleanVar(value=False)
        def toggle_show():
            key_entry.configure(show="" if show_var.get() else "•")
        
        ctk.CTkCheckBox(
            form_frame,
            text="Show key",
            variable=show_var,
            command=toggle_show,
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_secondary"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_dim"],
        ).pack(anchor="w", padx=16, pady=(0, 16))
        
        # Link to get API key
        if is_groq:
            link_text = "Get your free API key at: console.groq.com/keys"
            link_url = "https://console.groq.com/keys"
        else:
            link_text = "Get your API key at: platform.openai.com/api-keys"
            link_url = "https://platform.openai.com/api-keys"
        
        link = ctk.CTkLabel(
            dialog,
            text=link_text,
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["accent"],
            cursor="hand2",
        )
        link.pack(pady=(8, 16))
        link.bind("<Button-1>", lambda e: webbrowser.open(link_url))
        
        # Save button
        def save_and_close():
            key = key_var.get().strip()
            if key:
                # Save to environment (for current session)
                os.environ[env_var] = key
                
                # Save to config (for persistence across restarts)
                config_key = "groq_api_key" if is_groq else "openai_api_key"
                self.config[config_key] = key
                save_config(self.config)
                
                self.log(f"⚙ {env_var} configured and saved")
            
            # Update the status in the main UI
            self._update_remote_api_status()
            
            dialog.destroy()
        
        ctk.CTkButton(
            dialog,
            text="Save",
            font=(self.font_body[0], 14, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            width=200,
            height=44,
            corner_radius=RADIUS["md"],
            command=save_and_close,
        ).pack(pady=(8, 24))
    
    def _create_advanced_section_header(self, parent, text: str) -> None:
        """Create a section header for advanced settings."""
        ctk.CTkLabel(
            parent,
            text=text.upper(),
            font=(self.font_body[0], self.font_sizes["caption"], "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", pady=(0, 8))
    
    def _create_style_tab(self) -> None:
        """Create the Style tab with 5 preset options and Strong mode toggle."""
        frame = ctk.CTkFrame(self.tab_content_container, fg_color="transparent")
        self.tab_frames["style"] = frame
        
        # Scrollable content
        scroll = SmoothScrollableFrame(
            frame,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
        )
        scroll.pack(fill="both", expand=True)
        
        # === SECTION: Output Style ===
        tone_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        tone_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        tone_header = ctk.CTkFrame(tone_tile, fg_color="transparent")
        tone_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            tone_header, text="✎   O U T P U T   S T Y L E",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        # Description
        ctk.CTkLabel(
            tone_tile,
            text="Choose how your speech is cleaned up. Use the hotkey to cycle through styles.",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_muted"],
            wraplength=350,
        ).pack(anchor="w", padx=SPACING["tile_pad"], pady=(0, 12))
        
        # Style selection cards
        tone_container = ctk.CTkFrame(tone_tile, fg_color="transparent")
        tone_container.pack(fill="x", padx=SPACING["tile_pad"], pady=(0, SPACING["tile_pad_y"]))
        
        self.tone_buttons = {}
        self.tone_checkmarks = {}  # Store checkmark label references
        self.tone_title_labels = {}  # Store title label references
        current_tone = self.config.get("output_tone", "professional")
        
        # 5 style presets - each with distinct purpose
        tones = [
            ("minimal", "🎤", "Minimal", "Just removes um/uh. Your exact words, nothing changed."),
            ("professional", "💼", "Professional", "Clean + business-appropriate tone"),
            ("casual", "💬", "Casual", "Clean + relaxed texting style"),
            ("dev", "🖥️", "Dev", "Developer mode - recognizes git & code terms"),
            ("personal", "✨", "Personal", "Clean + your learned speech patterns"),
        ]
        
        for i, (tone_id, icon, label, desc) in enumerate(tones):
            is_selected = tone_id == current_tone
            
            card = ctk.CTkFrame(
                tone_container,
                fg_color=COLORS["bg_card"] if is_selected else COLORS["bg_input"],
                corner_radius=RADIUS["md"],
                border_width=2 if is_selected else 1,
                border_color=COLORS["accent"] if is_selected else COLORS["border_subtle"],
            )
            card.pack(fill="x", pady=4)
            
            # Make the card clickable
            card_inner = ctk.CTkFrame(card, fg_color="transparent")
            card_inner.pack(fill="x", padx=12, pady=10)
            
            # Icon and title row
            title_row = ctk.CTkFrame(card_inner, fg_color="transparent")
            title_row.pack(fill="x")
            
            title_label = ctk.CTkLabel(
                title_row,
                text=f"{icon}  {label}",
                font=(self.font_body[0], self.font_sizes["body"], "bold"),
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_primary"],
            )
            title_label.pack(side="left")
            self.tone_title_labels[tone_id] = title_label
            
            # Always create checkmark, but only show if selected
            checkmark = ctk.CTkLabel(
                title_row,
                text="✓",
                font=(self.font_body[0], self.font_sizes["body"]),
                text_color=COLORS["accent"],
            )
            if is_selected:
                checkmark.pack(side="right")
            self.tone_checkmarks[tone_id] = checkmark
            
            ctk.CTkLabel(
                card_inner,
                text=desc,
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["text_muted"],
            ).pack(anchor="w", pady=(4, 0))
            
            # Bind click for style selection
            for widget in [card, card_inner, title_row]:
                widget.bind("<Button-1>", lambda e, t=tone_id: self._on_tone_selected(t))
                for child in widget.winfo_children():
                    child.bind("<Button-1>", lambda e, t=tone_id: self._on_tone_selected(t))
            
            self.tone_buttons[tone_id] = card
        
        # === Model Compatibility Banner ===
        self.compatibility_banner_frame = ctk.CTkFrame(tone_container, fg_color="transparent")
        self.compatibility_banner_frame.pack(fill="x", pady=(4, 0))
        
        self.compatibility_banner = CompatibilityBanner(self.compatibility_banner_frame)
        self._update_compatibility_banner()
        
        # === Voice Profile Section (only shown when Personal style is selected) ===
        if current_tone == "personal":
            self._build_voice_profile_section(tone_container)
        
        # === Mode Toggles (Strong + Caricature) - inline for easy access ===
        modes_frame = ctk.CTkFrame(tone_tile, fg_color="transparent")
        modes_frame.pack(fill="x", padx=SPACING["tile_pad"], pady=(8, SPACING["tile_pad_y"]))
        
        # Horizontal container for both toggles
        toggles_row = ctk.CTkFrame(modes_frame, fg_color="transparent")
        toggles_row.pack(fill="x")
        
        # Strong mode toggle (left side)
        strong_container = ctk.CTkFrame(toggles_row, fg_color=COLORS["bg_input"], corner_radius=RADIUS["sm"])
        strong_container.pack(side="left", fill="x", expand=True, padx=(0, 6))
        
        strong_inner = ctk.CTkFrame(strong_container, fg_color="transparent")
        strong_inner.pack(fill="x", padx=12, pady=10)
        
        self.strong_mode_var = ctk.BooleanVar(value=self.config.get("strong_mode", False))
        
        strong_label = ctk.CTkLabel(
            strong_inner,
            text="💪 Strong Mode",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["text_primary"],
        )
        strong_label.pack(side="left")
        
        strong_toggle = ctk.CTkSwitch(
            strong_inner,
            text="",
            variable=self.strong_mode_var,
            command=self._on_strong_mode_toggled,
            width=40,
            height=22,
            switch_width=36,
            switch_height=18,
            corner_radius=9,
            fg_color=COLORS["bg_elevated"],
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_bright"],
        )
        strong_toggle.pack(side="right")
        
        # Add tooltip for strong mode
        ToolTip(strong_container, "Restructures sentences for clarity. Off = keeps your exact words.")
        
        # Caricature mode toggle (right side) - always visible now!
        caricature_container = ctk.CTkFrame(toggles_row, fg_color=COLORS["bg_input"], corner_radius=RADIUS["sm"])
        caricature_container.pack(side="left", fill="x", expand=True, padx=(6, 0))
        
        caricature_inner = ctk.CTkFrame(caricature_container, fg_color="transparent")
        caricature_inner.pack(fill="x", padx=12, pady=10)
        
        self.caricature_mode_var = ctk.BooleanVar(value=self.config.get("caricature_mode", False))
        
        caricature_label = ctk.CTkLabel(
            caricature_inner,
            text="🎭 Caricature",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["accent"] if self.config.get("caricature_mode", False) else COLORS["text_primary"],
        )
        caricature_label.pack(side="left")
        self._caricature_label = caricature_label  # Store reference for updating
        
        caricature_toggle = ctk.CTkSwitch(
            caricature_inner,
            text="",
            variable=self.caricature_mode_var,
            command=self._on_caricature_mode_toggled,
            width=40,
            height=22,
            switch_width=36,
            switch_height=18,
            corner_radius=9,
            fg_color=COLORS["bg_elevated"],
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_bright"],
        )
        caricature_toggle.pack(side="right")
        
        # Add tooltip for caricature mode
        ToolTip(caricature_container, "Maximum parody mode! Exaggerates your style hilariously.")
        
        # === SECTION: Style Toggle Hotkey ===
        hotkey_tile = ctk.CTkFrame(
            scroll, fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["border_rim"],
        )
        hotkey_tile.pack(fill="x", pady=(0, SPACING["gutter"]))
        
        hotkey_header = ctk.CTkFrame(hotkey_tile, fg_color="transparent")
        hotkey_header.pack(fill="x", padx=SPACING["tile_pad"], pady=(SPACING["tile_pad_y"], 8))
        ctk.CTkLabel(
            hotkey_header, text="⌨   S T Y L E   T O G G L E   H O T K E Y",
            font=(self.font_header[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left")
        
        hotkey_content = ctk.CTkFrame(hotkey_tile, fg_color="transparent")
        hotkey_content.pack(fill="x", padx=4, pady=(0, SPACING["tile_pad_y"]))
        
        ctk.CTkLabel(
            hotkey_content,
            text="Press this key to cycle: 🎤 → 💼 → 💬 → 🖥️ → ✨",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=16, pady=(0, 4))
        
        # Style hotkey key dropdown (inline)
        current_style_key_code = self.config.get("style_toggle_key", 68)
        current_style_key_name = self._hotkey_code_to_name.get(current_style_key_code, "F10")
        self._style_hotkey_key_var = ctk.StringVar(value=current_style_key_name)
        self.style_hotkey_dropdown = self.create_dropdown_row(
            hotkey_content, "Toggle Style",
            list(self._hotkey_key_codes.keys()),
            self._style_hotkey_key_var, self._on_style_hotkey_key_changed,
            width=160,
        )
        
        # Style hotkey modifier checkboxes (inline row)
        style_mod_row = ctk.CTkFrame(hotkey_content, fg_color="transparent")
        style_mod_row.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(
            style_mod_row, text="Modifiers",
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        current_style_mods = self.config.get("style_toggle_modifiers", [])
        self._style_hotkey_mod_vars = {}
        for mod in ["ctrl", "alt", "shift"]:
            var = ctk.BooleanVar(value=mod in current_style_mods)
            self._style_hotkey_mod_vars[mod] = var
            ctk.CTkCheckBox(
                style_mod_row, text=mod.capitalize(), variable=var,
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["text_primary"],
                fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"],
                checkmark_color="#000000", width=24,
                command=self._on_style_hotkey_mod_changed,
            ).pack(side="right", padx=(8, 0))
        
        # === 🎭 SECRET: Caricature Mode Keyboard Detection ===
        # Track keystrokes to detect "lol" or "haha" (easter egg still works!)
        if not hasattr(self, '_secret_key_buffer'):
            self._secret_key_buffer = ""
            # Bind keyboard to root window for easter egg detection
            self.bind("<Key>", self._on_style_tab_key)
    
    def _on_style_tab_key(self, event):
        """Handle keystrokes for easter egg detection (only on Style tab)."""
        # Only trigger on Style tab
        if not hasattr(self, 'active_tab') or self.active_tab != "style":
            return
        
        if not hasattr(self, '_secret_key_buffer'):
            self._secret_key_buffer = ""
        
        # Only track letter keys
        char = event.char.lower() if event.char else ""
        if char.isalpha():
            self._secret_key_buffer += char
            # Keep only last 5 characters
            self._secret_key_buffer = self._secret_key_buffer[-5:]
            
            # Check for secret codes
            if "lol" in self._secret_key_buffer or "haha" in self._secret_key_buffer:
                self._toggle_caricature_mode()
                self._secret_key_buffer = ""  # Reset buffer
    
    def _toggle_caricature_mode(self):
        """Toggle caricature mode on/off with celebration effects! (Used by easter egg)"""
        current = self.config.get("caricature_mode", False)
        new_state = not current
        self._set_caricature_mode(new_state, from_easter_egg=True)
    
    def _on_caricature_mode_toggled(self):
        """Handle caricature mode toggle from the UI switch."""
        new_state = self.caricature_mode_var.get()
        self._set_caricature_mode(new_state, from_easter_egg=False)
    
    def _set_caricature_mode(self, enabled: bool, from_easter_egg: bool = False):
        """Set caricature mode state with optional celebration effects."""
        self.config["caricature_mode"] = enabled
        save_config(self.config)
        
        # Sync the toggle variable if it exists
        if hasattr(self, 'caricature_mode_var'):
            self.caricature_mode_var.set(enabled)
        
        # Update label color
        if hasattr(self, '_caricature_label'):
            self._caricature_label.configure(
                text_color=COLORS["accent"] if enabled else COLORS["text_primary"]
            )
        
        if enabled:
            # 🎉 Celebration effects (always for easter egg, subtle for toggle)
            if from_easter_egg:
                self._play_silly_sound()
                self._show_confetti()
            self.log("🎭 CARICATURE MODE ACTIVATED! Things are about to get silly...")
        else:
            # Deactivated - clean up any lingering confetti
            self._cleanup_confetti()
            self.log("🎭 Caricature mode deactivated. Back to normal!")
        
        # Update compatibility check - caricature requires 3B+ model
        self._update_compatibility_banner()
    
    def _play_silly_sound(self):
        """Play a silly celebration sound."""
        try:
            import subprocess
            import os
            
            # Try different sound methods
            # Method 1: paplay (PulseAudio)
            # Method 2: aplay (ALSA)
            # Method 3: play (sox)
            # Method 4: Python beep (fallback)
            
            # Try to find a system sound
            sound_paths = [
                "/usr/share/sounds/freedesktop/stereo/complete.oga",
                "/usr/share/sounds/freedesktop/stereo/bell.oga",
                "/usr/share/sounds/Yaru/stereo/complete.oga",
                "/usr/share/sounds/gnome/default/alerts/glass.ogg",
            ]
            
            for sound_path in sound_paths:
                if os.path.exists(sound_path):
                    try:
                        subprocess.Popen(
                            ["paplay", sound_path],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        return
                    except:
                        pass
            
            # Fallback: Try system bell
            try:
                print("\a", end="", flush=True)  # Terminal bell
            except:
                pass
                
        except Exception as e:
            print(f"[Easter Egg] Couldn't play sound: {e}")
    
    def _show_confetti(self):
        """Show confetti celebration animation."""
        try:
            # Clean up any existing confetti first
            if hasattr(self, '_confetti_overlay') and self._confetti_overlay:
                try:
                    self._confetti_overlay._safe_destroy()
                except Exception:
                    pass
            
            # Create confetti overlay
            self._confetti_overlay = ConfettiOverlay(self, num_particles=60, duration_ms=3000)
            self._confetti_overlay.focus_set()
        except Exception as e:
            print(f"[Easter Egg] Couldn't show confetti: {e}")
    
    def _cleanup_confetti(self):
        """Force cleanup of any lingering confetti overlay."""
        if hasattr(self, '_confetti_overlay') and self._confetti_overlay:
            try:
                self._confetti_overlay._safe_destroy()
            except Exception:
                pass
            self._confetti_overlay = None
    
    
    def _on_tone_selected(self, tone_id: str) -> None:
        """Handle tone selection from Style tab."""
        # Gate non-minimal tones behind premium
        if tone_id != "minimal" and not self.feature_gate.has_feature("tone_system"):
            self._show_premium_prompt("tone_system")
            return

        current_tone = self.config.get("output_tone", "professional")
        if tone_id == current_tone:
            return

        self.config["output_tone"] = tone_id
        
        # Also update the whisper prompt to match the tone
        # Dev mode includes vocabulary hints to help recognize git/coding terms
        tone_prompts = {
            "minimal": "Dictation with natural speech.",
            "professional": "This is a professional dictation with formal language, proper punctuation, and business-appropriate terminology.",
            "casual": "This is a casual conversation with natural, relaxed language and everyday expressions.",
            "dev": "Developer dictation for coding and git. Terms: main, dev, branch, pull, push, commit, merge, rebase, checkout, stash, diff, log, fetch, clone, fork, repo, PR, issue, Cursor, VS Code, npm, pip, yarn, pnpm, API, JSON, TypeScript, Python, JavaScript, React, function, class, variable, const, let, import, export, async, await, promise, callback, component, props, state, hook, useState, useEffect.",
            "personal": "Natural dictation in the user's personal speaking style.",
        }
        self.config["prompt"] = tone_prompts.get(tone_id, tone_prompts["professional"])
        
        save_config(self.config)
        
        # Update card styles
        for tid, card in self.tone_buttons.items():
            is_selected = tid == tone_id
            card.configure(
                fg_color=COLORS["bg_card"] if is_selected else COLORS["bg_input"],
                border_width=2 if is_selected else 1,
                border_color=COLORS["accent"] if is_selected else COLORS["border_subtle"],
            )
        
        # Rebuild the style tab to update checkmarks and show/hide voice profile section
        self._rebuild_style_tab()
        
        tone_labels = {
            "minimal": "🎤 Minimal",
            "professional": "💼 Professional",
            "casual": "💬 Casual",
            "dev": "🖥️ Dev",
            "personal": "✨ Personal"
        }
        self.log(f"✎ Style: {tone_labels.get(tone_id, tone_id)}")
    
    def _on_intensity_changed(self, tone_id: str, intensity_id: str) -> None:
        """Handle intensity change for a specific tone."""
        intensity_key = f"{tone_id}_intensity"
        current_intensity = self.config.get(intensity_key, "standard")
        
        if intensity_id == current_intensity:
            return
        
        # Update config
        self.config[intensity_key] = intensity_id
        save_config(self.config)
        
        # Update button styles for this tone's intensity buttons
        if tone_id in self.intensity_buttons:
            for int_id, btn in self.intensity_buttons[tone_id].items():
                is_selected = int_id == intensity_id
                btn.configure(
                    fg_color=COLORS["accent_dim"] if is_selected else COLORS["bg_elevated"],
                    hover_color=COLORS["accent_dim"] if is_selected else COLORS["bg_hover"],
                    text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                )
        
        # Log the change
        tone_labels = {"professional": "Professional", "casual": "Casual", "dev": "Dev", "personal": "Personal"}
        intensity_labels = {"light": "Light", "standard": "Standard", "strong": "Strong"}
        self.log(f"✎ {tone_labels.get(tone_id, tone_id)} intensity: {intensity_labels.get(intensity_id, intensity_id)}")
        
        # Update compatibility check
        self._update_compatibility_banner()
    
    def _update_style_selection(self, new_tone: str) -> None:
        """
        Update style card visuals without rebuilding the entire tab.
        This provides a smooth, flicker-free update when cycling styles via hotkey.
        """
        if not hasattr(self, 'tone_buttons') or not self.tone_buttons:
            return
        
        # Update card styles (border and background)
        for tid, card in self.tone_buttons.items():
            is_selected = tid == new_tone
            try:
                card.configure(
                    fg_color=COLORS["bg_card"] if is_selected else COLORS["bg_input"],
                    border_width=2 if is_selected else 1,
                    border_color=COLORS["accent"] if is_selected else COLORS["border_subtle"],
                )
            except Exception:
                pass  # Widget might not exist
        
        # Update title label colors
        if hasattr(self, 'tone_title_labels'):
            for tid, label in self.tone_title_labels.items():
                is_selected = tid == new_tone
                try:
                    label.configure(
                        text_color=COLORS["text_bright"] if is_selected else COLORS["text_primary"]
                    )
                except Exception:
                    pass
        
        # Update checkmark visibility
        if hasattr(self, 'tone_checkmarks'):
            for tid, checkmark in self.tone_checkmarks.items():
                is_selected = tid == new_tone
                try:
                    if is_selected:
                        checkmark.pack(side="right")
                    else:
                        checkmark.pack_forget()
                except Exception:
                    pass
    
    def _rebuild_style_tab(self) -> None:
        """Rebuild the style tab to reflect current settings."""
        # Destroy current content
        if "style" in self.tab_frames:
            self.tab_frames["style"].destroy()
        
        # Recreate
        self._create_style_tab()
        
        # Re-show if it's the active tab
        if self.active_tab == "style":
            self.tab_frames["style"].pack(fill="both", expand=True)
    
    def _on_smart_formatting_toggled(self) -> None:
        """Handle smart formatting toggle (legacy - kept for compatibility)."""
        enabled = self.smart_format_var.get()
        self.config["smart_formatting"] = enabled
        save_config(self.config)
        
        status = "enabled" if enabled else "disabled"
        self.log(f"✨ Smart formatting {status}")
        
        # Update compatibility check
        self._update_compatibility_banner()
    
    def _on_strong_mode_toggled(self) -> None:
        """Handle strong mode toggle."""
        enabled = self.strong_mode_var.get()
        self.config["strong_mode"] = enabled
        save_config(self.config)
        
        status = "enabled" if enabled else "disabled"
        self.log(f"💪 Strong mode {status}")
        
        # Update compatibility check
        self._update_compatibility_banner()
    
    def _update_compatibility_banner(self) -> None:
        """
        Check current settings compatibility and update the banner.
        
        Called whenever tone, intensity, model, or smart formatting changes.
        """
        if not hasattr(self, 'compatibility_banner'):
            return
        
        try:
            compatibility = check_settings_compatibility(self.config)
            self.compatibility_banner.update_status(compatibility)
            
            # Log if there are issues
            if not compatibility.get("is_compatible", True):
                model = compatibility.get("current_model", "unknown")
                requested = compatibility.get("requested_intensity", "standard")
                effective = compatibility.get("effective_intensity", requested)
                if requested != effective:
                    self.log(f"⚠ Model '{model}' limited to '{effective}' intensity (requested: '{requested}')")
        except Exception as e:
            # Don't crash if compatibility check fails
            print(f"[Compatibility] Error checking compatibility: {e}")
    
    def _create_history_tab(self) -> None:
        """Create the History tab content."""
        frame = ctk.CTkFrame(self.tab_content_container, fg_color="transparent")
        self.tab_frames["history"] = frame
        
        # Header with clear button
        header = ctk.CTkFrame(frame, fg_color="transparent")
        header.pack(fill="x", pady=(0, 8))
        
        ctk.CTkLabel(
            header,
            text="ACTIVITY LOG",
            font=(self.font_body[0], self.font_sizes["caption"], "bold"),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        ctk.CTkButton(
            header,
            text="Clear",
            font=(self.font_body[0], self.font_sizes["small"]),
            width=60,
            height=24,
            fg_color=COLORS["bg_surface"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_secondary"],
            corner_radius=RADIUS["sm"],
            command=self.clear_log,
        ).pack(side="right")
        
        # Log card
        log_card = ctk.CTkFrame(
            frame,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_subtle"],
        )
        log_card.pack(fill="both", expand=True)
        
        self.log_textbox = ctk.CTkTextbox(
            log_card,
            font=(self.font_mono[0], self.font_sizes["small"]),
            fg_color="transparent",
            text_color=COLORS["text_secondary"],
            wrap="word",
            activate_scrollbars=True,
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
        )
        self.log_textbox.pack(fill="both", expand=True, padx=12, pady=12)
        self.log_textbox.configure(state="disabled")
        
        # Legacy compatibility - these are needed for old code references
        self.log_expanded = True
        self.log_container = log_card
        self.log_frame = log_card
        self.log_header_frame = header
        
        # Note: advanced_toggle_btn and advanced_container are created in _create_settings_tab
        # Do NOT overwrite them here!
    
    def _draw_gradient_bg(self, event=None):
        """Draw ambient gradient - GitHub Dark base with violet warmth."""
        # Skip when window is not visible
        try:
            if self.state() in ("iconic", "withdrawn"):
                return
        except Exception:
            pass
        
        canvas = self.bg_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        
        if w <= 1 or h <= 1:
            return
        
        try:
            canvas.delete("gradient")

            # Base color - GitHub Dark (#0D1117)
            base_r, base_g, base_b = 13, 17, 23

            # Subtle violet glow - 10% accent blend for depth
            glow_r, glow_g, glow_b = 25, 22, 35

            # Create ultra-subtle radial gradient (ambient light, not spotlight)
            cx, cy = w // 2, h // 3  # Upper third for hero section
            max_radius = int(max(w, h) * 0.9)

            steps = 20
            for i in range(steps, 0, -1):
                ratio = i / steps
                radius = int(max_radius * ratio)

                # Very gentle blend - almost imperceptible depth
                blend = (1 - ratio) ** 3.0  # Smooth falloff
                intensity = blend * 0.10  # Very subtle
                r = int(base_r + (glow_r - base_r) * intensity)
                g = int(base_g + (glow_g - base_g) * intensity)
                b = int(base_b + (glow_b - base_b) * intensity)

                color = f"#{r:02x}{g:02x}{b:02x}"
                canvas.create_oval(
                    cx - radius, cy - radius,
                    cx + radius, cy + radius,
                    fill=color,
                    outline="",
                    tags="gradient",
                )

            # Make sure gradient is behind everything
            canvas.tag_lower("gradient")
        except tk.TclError:
            pass
    
    
    def toggle_activity_log(self):
        """Toggle the activity log expanded/collapsed state (legacy - now in History tab)."""
        # Activity log is now always visible in the History tab
        pass
    
    def toggle_advanced_settings(self):
        """Toggle the advanced settings expanded/collapsed state (legacy method)."""
        self._toggle_advanced_inline()
    
    def _get_preprocess_desc(self, level: str) -> str:
        """Get brief description for audio processing level with latency."""
        descs = {
            "off": "Raw audio • 0ms",
            "light": "Volume only • +2ms",
            "medium": "Filter noise • +5ms",
            "heavy": "Full cleanup • +10ms",
        }
        return descs.get(level, "")
    
    def on_preprocessing_changed(self, value: str):
        """Handle audio preprocessing level change."""
        self.config["audio_preprocessing"] = value
        save_config(self.config)
        # Update description label
        if hasattr(self, 'preprocess_desc_label'):
            self.preprocess_desc_label.configure(text=self._get_preprocess_desc(value))
        self.log(f"⚙ Audio processing: {value}")
    
    def on_accuracy_mode_changed(self, value: str):
        """Handle accuracy mode change from dropdown."""
        self.config["accuracy_mode"] = value
        # Apply preset settings
        presets = {
            "fast": {"beam_size": 1, "best_of": 1},
            "balanced": {"beam_size": 5, "best_of": 3},
            "high": {"beam_size": 8, "best_of": 5},
        }
        if value in presets:
            for key, val in presets[value].items():
                self.config[key] = val
            # Update beam size dropdown to reflect preset
            if hasattr(self, 'beam_size_var'):
                self.beam_size_var.set(str(presets[value]["beam_size"]))
        save_config(self.config)
        self.log(f"⚙ Accuracy mode: {value}")
    
    def on_beam_size_changed(self, value: str):
        """Handle beam size change from dropdown."""
        self.config["beam_size"] = int(value)
        save_config(self.config)
        self.log(f"⚙ Beam size: {value}")
    
    def on_language_changed(self, value: str):
        """Handle language change from dropdown."""
        self.config["language"] = value
        save_config(self.config)
        lang_name = "English" if value == "en" else "Auto-detect" if value == "auto" else value.upper()
        self.log(f"⚙ Language: {lang_name}")
    
    def on_chunk_duration_changed(self, value: str):
        """Handle segment length change from dropdown."""
        self.config["chunk_duration"] = int(value)
        save_config(self.config)
        self.log(f"⚙ Segment length: {value}s")
    
    def on_backend_changed(self, value: str):
        """Handle transcription backend change from dropdown."""
        # Gate Faster-Whisper behind premium
        if value == "faster_whisper" and not self.feature_gate.has_feature("faster_whisper"):
            self._show_premium_prompt("faster_whisper")
            self.backend_var.set(self.config.get("transcription_backend", "whisper_cpp"))
            return
        self.config["transcription_backend"] = value
        save_config(self.config)
        display_names = {
            "whisper_cpp": "whisper.cpp",
            "faster_whisper": "Faster-Whisper",
            "openai_whisper": "OpenAI Whisper (Cloud)",
        }
        display = display_names.get(value, value)
        self.log(f"⚙ Backend: {display}")
    
    def on_gpu_layers_changed(self, value: str):
        """Handle GPU layers change from dropdown."""
        if value == "auto":
            self.config["gpu_layers"] = 0
        else:
            self.config["gpu_layers"] = int(value)
        save_config(self.config)
        display = "Auto (all)" if value == "auto" else value
        self.log(f"⚙ GPU layers: {display}")
    
    def _on_processing_mode_changed(self, mode: str) -> None:
        """Handle processing mode change from the mode selector."""
        old_mode = self.config.get("processing_mode", "local")
        if mode == old_mode:
            return

        # Gate cloud backends behind premium
        if mode == "remote" and not self.feature_gate.has_feature("cloud_backends"):
            self._show_premium_prompt("cloud_backends")
            # Reset the selector back to local
            if hasattr(self, 'mode_selector'):
                self.mode_selector.set("local")
            return

        # Save mode to config
        self.config["processing_mode"] = mode
        
        # Update related settings based on mode
        if mode == "local":
            # Local mode: use local transcription backend
            # Post-processing stays as configured - llama.cpp is local and private
            if self.config.get("transcription_backend") in ("openai_whisper", "groq_whisper"):
                self.config["transcription_backend"] = "whisper_cpp"
            # Ensure post-processing uses llama_cpp if enabled
            if self.config.get("post_processing_enabled", True):
                backend = self.config.get("post_processing_backend", "llama_cpp")
                if backend != "llama_cpp":
                    self.config["post_processing_backend"] = "llama_cpp"
            self.log("🔒 Mode: Local (100% private)")
            
        elif mode == "remote":
            # Remote mode: cloud transcription via Groq (fastest) or OpenAI Whisper
            current_backend = self.config.get("transcription_backend", "")
            # Keep existing cloud backend if already set, otherwise default to Groq (faster)
            if current_backend not in ("groq_whisper", "openai_whisper"):
                self.config["transcription_backend"] = "groq_whisper"  # Default to Groq for speed
            # Note: Don't disable post-processing - user may want cloud LLM cleanup (caricature mode, etc.)
            # Post-processing backend will be switched to cloud (openai/anthropic) by _build_mode_settings
            backend_name = "Groq" if self.config["transcription_backend"] == "groq_whisper" else "OpenAI"
            self.log(f"☁️ Mode: Remote ({backend_name} cloud transcription)")
        
        save_config(self.config)
        
        # Rebuild the mode-specific settings panel
        self._build_mode_settings(mode)
    
    def show_preprocessing_help(self):
        """Show help dialog explaining audio processing levels."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Audio Processing Help")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.transient(self)
        dialog.grab_set()
        self._setup_dialog(dialog, 480, 360)
        
        ctk.CTkLabel(
            dialog,
            text="Audio Processing Levels",
            font=(self.font_body[0], 16, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(pady=(20, 15))
        
        levels = [
            ("off", "Off — 0ms latency", "Raw audio passed directly to Whisper. Use if words are being cut off."),
            ("light", "Light (Recommended) — +2ms", "Only adjusts volume levels. Preserves all speech including soft sounds."),
            ("medium", "Medium — +5ms", "Adds a filter to remove low rumble/hum. Good for noisy environments."),
            ("heavy", "Heavy — +10ms", "Adds noise gate that reduces quiet sounds. May cut off soft consonants."),
        ]
        
        for value, title, desc in levels:
            frame = ctk.CTkFrame(dialog, fg_color=COLORS["bg_card"], corner_radius=8)
            frame.pack(fill="x", padx=20, pady=4)
            
            # Highlight current selection
            current = self.config.get("audio_preprocessing", "light")
            if current == value:
                frame.configure(border_width=1, border_color=COLORS["accent"])
            
            ctk.CTkLabel(
                frame,
                text=title,
                font=(self.font_body[0], 13, "bold"),
                text_color=COLORS["accent"] if current == value else COLORS["text_primary"],
            ).pack(anchor="w", padx=12, pady=(8, 2))
            
            ctk.CTkLabel(
                frame,
                text=desc,
                font=(self.font_body[0], 11),
                text_color=COLORS["text_secondary"],
                wraplength=360,
            ).pack(anchor="w", padx=12, pady=(0, 8))
        
        ctk.CTkButton(
            dialog,
            text="Close",
            command=dialog.destroy,
            fg_color=COLORS["bg_elevated"],
            hover_color=COLORS["bg_hover"],
            font=(self.font_body[0], 12),
            height=32,
            width=80,
        ).pack(pady=15)
        # Update recorder
        self.recorder.preprocessing = self.preprocess_var.get()
    
    def toggle_chunked_mode(self):
        """Toggle chunked recording mode."""
        if self.chunked_var.get() and not self.feature_gate.has_feature("chunked_recording"):
            self.chunked_var.set(False)
            self._show_premium_prompt("chunked_recording")
            return
        self.config["chunked_mode"] = self.chunked_var.get()
        save_config(self.config)
        mode = "chunked (unlimited)" if self.chunked_var.get() else "simple"
        self.log(f"⚙ Recording mode: {mode}")
    
    def toggle_punctuation(self):
        """Toggle punctuation enforcement."""
        self.config["ensure_punctuation"] = self.punctuation_var.get()
        save_config(self.config)
        status = "on" if self.punctuation_var.get() else "off"
        self.log(f"⚙ Punctuation: {status}")

    def toggle_gpu(self):
        """Toggle GPU acceleration."""
        if hasattr(self, 'gpu_var'):
            self.config["use_gpu"] = self.gpu_var.get()
            save_config(self.config)
            status = "enabled" if self.gpu_var.get() else "disabled"
            self.log(f"⚙ GPU acceleration: {status}")

    # === Post-Processing Handlers ===
    
    def toggle_post_processing(self):
        """Toggle LLM post-processing."""
        enabled = self.postproc_enabled_var.get() if hasattr(self, 'postproc_enabled_var') else self.postproc_var.get() if hasattr(self, 'postproc_var') else False
        self.config["post_processing_enabled"] = enabled
        save_config(self.config)
        status = "enabled" if enabled else "disabled"
        self.log(f"⚙ LLM Post-processing: {status}")
        # Rebuild mode settings to show/hide post-processing options
        current_mode = self.config.get("processing_mode", "local")
        self._build_mode_settings(current_mode)
        # Update compatibility check
        self._update_compatibility_banner()
    
    def _build_voice_profile_section(self, parent):
        """Build voice profile section for the Style tab when Personal is selected."""
        # Gate voice profiles behind premium
        if not self.feature_gate.has_feature("voice_profiles"):
            locked_frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_elevated"], corner_radius=RADIUS["sm"])
            locked_frame.pack(fill="x", pady=(8, 0))
            ctk.CTkLabel(
                locked_frame, text="Voice Profiles require Wayfinder Aura Premium",
                font=(self.font_body[0], 12), text_color=COLORS["text_muted"],
            ).pack(padx=12, pady=8)
            ctk.CTkButton(
                locked_frame, text="Upgrade", font=(self.font_body[0], 11),
                fg_color=COLORS["accent"], hover_color=COLORS["accent_dim"],
                text_color="#FFFFFF", height=28, corner_radius=6,
                command=lambda: self._show_premium_prompt("voice_profiles"),
            ).pack(padx=12, pady=(0, 8))
            return

        profile_frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_elevated"], corner_radius=RADIUS["sm"])
        profile_frame.pack(fill="x", pady=(8, 0))

        try:
            from wayfinder.core.voice_profile import get_voice_profile
            voice_profile = get_voice_profile(
                history_limit=self.config.get("voice_learning_history_limit", 100),
                regen_interval=self.config.get("voice_learning_regen_interval", 20),
            )
            stats = voice_profile.get_stats()
        except Exception as e:
            print(f"[Voice Profile] Error loading stats: {e}")
            stats = {"history_count": 0, "vocabulary_count": 0, "has_summary": False}
        
        history_count = stats.get("history_count", 0)
        vocab_count = stats.get("vocabulary_count", 0)
        has_summary = stats.get("has_summary", False)
        
        # Info row
        info_row = ctk.CTkFrame(profile_frame, fg_color="transparent")
        info_row.pack(fill="x", padx=12, pady=(10, 5))
        
        if history_count == 0:
            ctk.CTkLabel(
                info_row,
                text="🎤 Start speaking to build your personal voice profile!",
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["text_muted"],
            ).pack(side="left")
        else:
            status_parts = [f"📚 Learning from {history_count} transcriptions"]
            if vocab_count > 0:
                status_parts.append(f"📝 {vocab_count} terms")
            if has_summary:
                status_parts.append("✨ Profile ready")
            
            ctk.CTkLabel(
                info_row,
                text=" • ".join(status_parts),
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["accent"] if has_summary else COLORS["text_muted"],
            ).pack(side="left")
        
        # Action buttons
        btn_row = ctk.CTkFrame(profile_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        
        ctk.CTkButton(
            btn_row,
            text="View Profile",
            font=(self.font_body[0], 11),
            height=28,
            width=90,
            corner_radius=6,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_card"],
            text_color=COLORS["text_primary"],
            command=self.open_voice_profile_dialog,
        ).pack(side="left", padx=(0, 8))
        
        if history_count > 0:
            ctk.CTkButton(
                btn_row,
                text="Clear",
                font=(self.font_body[0], 11),
                height=28,
                width=60,
                corner_radius=6,
                fg_color=COLORS["bg_hover"],
                hover_color="#3A2020",
                text_color=COLORS["text_muted"],
                command=self.clear_voice_profile,
            ).pack(side="left")
    
    def _build_voice_profile_status(self, parent):
        """Build voice profile status display with action buttons."""
        try:
            from wayfinder.core.voice_profile import get_voice_profile
            voice_profile = get_voice_profile(
                history_limit=self.config.get("voice_learning_history_limit", 100),
                regen_interval=self.config.get("voice_learning_regen_interval", 20),
            )
            stats = voice_profile.get_stats()
        except Exception as e:
            print(f"[Voice Profile] Error loading stats: {e}")
            stats = {"history_count": 0, "vocabulary_count": 0, "has_summary": False}
        
        # Status container
        status_frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_input"], corner_radius=RADIUS["sm"])
        status_frame.pack(fill="x", padx=SPACING["tile_pad"]-4, pady=(8, 0))
        
        # Stats row
        stats_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        stats_row.pack(fill="x", padx=12, pady=(10, 5))
        
        # Learning count
        history_count = stats.get("history_count", 0)
        vocab_count = stats.get("vocabulary_count", 0)
        has_summary = stats.get("has_summary", False)
        
        status_text = f"📚 {history_count} transcriptions"
        if vocab_count > 0:
            status_text += f" • 📝 {vocab_count} terms learned"
        if has_summary:
            status_text += " • ✨ Profile active"
        
        ctk.CTkLabel(
            stats_row,
            text=status_text,
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        # Action buttons row
        btn_row = ctk.CTkFrame(status_frame, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(5, 10))
        
        # View Profile button
        ctk.CTkButton(
            btn_row,
            text="View Profile",
            font=(self.font_body[0], 11),
            height=28,
            width=90,
            corner_radius=6,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_primary"],
            command=self.open_voice_profile_dialog,
        ).pack(side="left", padx=(0, 8))
        
        # Clear Data button
        ctk.CTkButton(
            btn_row,
            text="Clear Data",
            font=(self.font_body[0], 11),
            height=28,
            width=80,
            corner_radius=6,
            fg_color=COLORS["bg_hover"],
            hover_color="#3A2020",  # Reddish hover
            text_color=COLORS["text_muted"],
            command=self.clear_voice_profile,
        ).pack(side="left")
    
    def open_voice_profile_dialog(self):
        """Open dialog to view and edit voice profile."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Voice Profile")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 600, 680)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Voice Profile",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Your personal voice profile, learned from transcriptions.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 20))
        
        # Load profile data
        try:
            from wayfinder.core.voice_profile import get_voice_profile
            voice_profile = get_voice_profile()
            stats = voice_profile.get_stats()
        except Exception as e:
            ctk.CTkLabel(
                inner,
                text=f"Error loading profile: {e}",
                font=(self.font_body[0], 12),
                text_color="#CF7B7B",
            ).pack(anchor="w")
            return
        
        # Stats section
        stats_frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_input"], corner_radius=10)
        stats_frame.pack(fill="x", pady=(0, 15))
        
        history_count = stats.get("history_count", 0)
        vocab_count = stats.get("vocabulary_count", 0)
        total_words = stats.get("total_words", 0)
        
        ctk.CTkLabel(
            stats_frame,
            text=f"📊 Statistics: {history_count} transcriptions • {total_words:,} words • {vocab_count} unique terms",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_primary"],
        ).pack(padx=15, pady=12)
        
        # Profile Summary section
        ctk.CTkLabel(
            inner,
            text="Profile Summary",
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(10, 5))
        
        summary = stats.get("summary", "")
        summary_text = ctk.CTkTextbox(
            inner,
            font=(self.font_body[0], 12),
            fg_color=COLORS["bg_card"],
            text_color=COLORS["text_primary"],
            corner_radius=10,
            height=100,
            wrap="word",
        )
        summary_text.pack(fill="x", pady=(0, 5))
        if summary:
            summary_text.insert("1.0", summary)
        else:
            summary_text.insert("1.0", "(No profile summary yet — keep talking to build one!)")
            summary_text.configure(text_color=COLORS["text_muted"])
        
        # Learned Vocabulary section
        ctk.CTkLabel(
            inner,
            text="Learned Vocabulary",
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(15, 5))
        
        vocabulary = stats.get("vocabulary", [])
        vocab_display = ", ".join(vocabulary[:30]) if vocabulary else "(No vocabulary learned yet)"
        
        vocab_label = ctk.CTkLabel(
            inner,
            text=vocab_display,
            font=(self.font_body[0], 11),
            text_color=COLORS["accent"] if vocabulary else COLORS["text_muted"],
            wraplength=490,
            justify="left",
        )
        vocab_label.pack(anchor="w", pady=(0, 15))
        
        # Buttons
        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(10, 0))
        
        def save_summary():
            new_summary = summary_text.get("1.0", "end").strip()
            try:
                voice_profile.set_summary(new_summary)
                self.log("✓ Voice profile summary saved")
                dialog.destroy()
            except Exception as e:
                self.log(f"⚠ Error saving profile: {e}")
        
        def regenerate_profile():
            if not self.config.get("post_processing_enabled", True):
                self.log("⚠ Enable Post-Processing to regenerate profile")
                return
            
            llm_callback = self._get_llm_callback_for_voice_learning()
            if voice_profile.regenerate_profile(llm_callback):
                self.log("🔄 Regenerating voice profile...")
                dialog.destroy()
            else:
                self.log("⚠ Profile regeneration already in progress or not enough data")
        
        ctk.CTkButton(
            btn_frame,
            text="Save Changes",
            font=(self.font_body[0], 13, "bold"),
            height=40,
            corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save_summary,
        ).pack(side="left", padx=(0, 10))
        
        ctk.CTkButton(
            btn_frame,
            text="Regenerate Profile",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_primary"],
            command=regenerate_profile,
        ).pack(side="left", padx=(0, 10))
        
        ctk.CTkButton(
            btn_frame,
            text="Close",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(side="left")
    
    def clear_voice_profile(self):
        """Clear all voice profile data after confirmation."""
        # Simple confirmation via a popup
        confirm_dialog = ctk.CTkToplevel(self)
        confirm_dialog.title("Clear Voice Profile")
        confirm_dialog.configure(fg_color=COLORS["bg_base"])
        confirm_dialog.transient(self)
        confirm_dialog.after(100, confirm_dialog.lift)
        self._setup_dialog(confirm_dialog, 400, 200)
        
        ctk.CTkLabel(
            confirm_dialog,
            text="Clear all voice learning data?\n\nThis cannot be undone.",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_primary"],
            justify="center",
        ).pack(pady=20)
        
        btn_frame = ctk.CTkFrame(confirm_dialog, fg_color="transparent")
        btn_frame.pack(pady=10)
        
        def do_clear():
            try:
                from wayfinder.core.voice_profile import get_voice_profile
                voice_profile = get_voice_profile()
                voice_profile.clear()
                self.log("🗑️ Voice profile data cleared")
                confirm_dialog.destroy()
                # Rebuild settings to update status
                current_mode = self.config.get("processing_mode", "local")
                self._build_mode_settings(current_mode)
            except Exception as e:
                self.log(f"⚠ Error clearing profile: {e}")
                confirm_dialog.destroy()
        
        ctk.CTkButton(
            btn_frame,
            text="Clear Data",
            font=(self.font_body[0], 13, "bold"),
            height=36,
            width=100,
            corner_radius=8,
            fg_color="#8B3030",
            hover_color="#A03030",
            text_color="#FFFFFF",
            command=do_clear,
        ).pack(side="left", padx=10)
        
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=36,
            width=80,
            corner_radius=8,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=confirm_dialog.destroy,
        ).pack(side="left", padx=10)
    
    def on_postproc_backend_changed(self, value: str):
        """Handle post-processing backend change."""
        self.config["post_processing_backend"] = value
        save_config(self.config)
        display_map = {
            "llama_cpp": "Local (llama.cpp)",
            "anthropic": "Cloud (Anthropic Claude)",
            "openai": "Cloud (OpenAI GPT)",
        }
        display = display_map.get(value, value)
        self.log(f"⚙ Post-processing backend: {display}")
        # Update config button text
        if hasattr(self, 'postproc_config_btn'):
            self.postproc_config_btn.configure(text=self._get_postproc_config_display())
        # Rebuild mode settings to show/hide model selection
        current_mode = self.config.get("processing_mode", "local")
        self._build_mode_settings(current_mode)
        # Update compatibility check
        self._update_compatibility_banner()
    
    def on_postproc_template_changed(self, value: str):
        """Legacy handler - templates replaced by Style tab's tone + smart formatting."""
        # Kept for backwards compatibility with old config values
        pass
    
    def _build_inline_model_section(self, parent, backend: str) -> None:
        """Build inline model selection with download capability (no popups)."""
        # Container for the model section
        model_section = ctk.CTkFrame(parent, fg_color="transparent")
        model_section.pack(fill="x", padx=16, pady=(0, 10))
        
        # Store reference for updates
        self._inline_model_section = model_section
        self._inline_download_active = False
        self._cancel_download = False
        
        if backend == "llama_cpp":
            self._build_llamacpp_inline_section(model_section)
    
    def _build_llamacpp_inline_section(self, parent) -> None:
        """Build inline llama.cpp model selection with download."""
        models_dir = _get_llm_models_dir()
        models_dir.mkdir(parents=True, exist_ok=True)
        
        current_model_path = self.config.get("llama_cpp_model_path", "")
        
        # Build model options with install status
        model_options = []
        model_data = {}  # model_display -> {id, info, path, installed}
        
        for model_id, model_info in LLM_GGUF_MODELS.items():
            model_file = models_dir / model_info["filename"]
            is_installed = model_file.exists()
            is_selected = str(model_file) == current_model_path
            
            status_icon = "✓ " if is_installed else ""
            display_name = f"{status_icon}{model_info['name']}"
            model_options.append(display_name)
            model_data[display_name] = {
                "id": model_id,
                "info": model_info,
                "path": str(model_file),
                "installed": is_installed,
                "selected": is_selected,
            }
        
        # Determine current selection display
        current_display = None
        first_installed = None
        for display_name, data in model_data.items():
            if data["selected"]:
                current_display = display_name
                break
            # Track the first installed model as fallback
            if data["installed"] and first_installed is None:
                first_installed = display_name
        
        if not current_display:
            if first_installed:
                # Auto-select the first installed model and save to config
                current_display = first_installed
                installed_data = model_data[first_installed]
                self.config["llama_cpp_model_path"] = installed_data["path"]
                save_config(self.config)
                print(f"[Config] Auto-selected installed model: {installed_data['info']['name']}")
            elif model_options:
                # Default to first option if nothing installed
                current_display = model_options[0]
        
        # Row 1: Label + Browse on left, Dropdown + Download on right
        row1 = ctk.CTkFrame(parent, fg_color="transparent")
        row1.pack(fill="x", pady=(0, 4))
        row1.grid_columnconfigure(0, weight=0)
        row1.grid_columnconfigure(1, weight=1)
        row1.grid_columnconfigure(2, weight=0)
        
        # Left side: Label + Browse
        left_frame = ctk.CTkFrame(row1, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w")
        
        label_widget = ctk.CTkLabel(
            left_frame,
            text="LLM Model",
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        )
        label_widget.pack(side="left")
        
        # Info icon
        info_icon = ctk.CTkLabel(
            left_frame,
            text="ⓘ",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        )
        info_icon.pack(side="left", padx=(6, 0))
        llamacpp_tooltip = f"GGUF models run locally via llama.cpp.\nModels are downloaded to {_get_llm_models_dir()}/\nGPU acceleration is automatic (Metal on macOS, CUDA/ROCm on Linux)."
        ToolTip(label_widget, llamacpp_tooltip)
        ToolTip(info_icon, llamacpp_tooltip)
        
        # Browse button on left (subtle)
        browse_btn = ctk.CTkButton(
            left_frame,
            text="Browse...",
            font=(self.font_body[0], self.font_sizes["caption"]),
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            width=60,
            height=22,
            command=self._browse_custom_gguf,
        )
        browse_btn.pack(side="left", padx=(12, 0))
        ToolTip(browse_btn, "Load a custom GGUF model from anywhere on your system.\nSupports any llama.cpp compatible GGUF file.")
        
        # Right side container for dropdown and button
        right_frame = ctk.CTkFrame(row1, fg_color="transparent")
        right_frame.grid(row=0, column=2, sticky="e")
        
        # Model dropdown
        self._llamacpp_model_var = ctk.StringVar(value=current_display or "Select model")
        self._llamacpp_model_data = model_data
        
        model_dropdown = ctk.CTkOptionMenu(
            right_frame,
            values=model_options if model_options else ["No models available"],
            variable=self._llamacpp_model_var,
            command=self._on_llamacpp_model_selected,
            fg_color=COLORS["bg_input"],
            button_color=COLORS["bg_hover"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
            font=(self.font_mono[0], self.font_sizes["small"]),
            width=180,
            height=32,
        )
        model_dropdown.pack(side="left", padx=(0, 8))
        self._llamacpp_model_dropdown = model_dropdown
        
        # Download button (will show size)
        self._llamacpp_download_btn = ctk.CTkButton(
            right_frame,
            text="Download",
            font=(self.font_body[0], self.font_sizes["small"]),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_bright"],
            width=120,
            height=32,
            command=self._download_selected_llamacpp_model,
        )
        self._llamacpp_download_btn.pack(side="left")
        
        # Row 2: Model info panel (shows details about selected model)
        self._llamacpp_info_frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_hover"], corner_radius=6)
        self._llamacpp_info_frame.pack(fill="x", pady=(4, 6))
        
        info_inner = ctk.CTkFrame(self._llamacpp_info_frame, fg_color="transparent")
        info_inner.pack(fill="x", padx=10, pady=8)
        
        # Description label
        self._llamacpp_desc_label = ctk.CTkLabel(
            info_inner,
            text="",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_secondary"],
            anchor="w",
            justify="left",
        )
        self._llamacpp_desc_label.pack(anchor="w")
        
        # Stats row (size, speed, accuracy)
        stats_frame = ctk.CTkFrame(info_inner, fg_color="transparent")
        stats_frame.pack(fill="x", pady=(4, 0))
        
        self._llamacpp_size_label = ctk.CTkLabel(
            stats_frame,
            text="",
            font=(self.font_mono[0], self.font_sizes["caption"]),
            text_color=COLORS["text_muted"],
        )
        self._llamacpp_size_label.pack(side="left")
        
        self._llamacpp_speed_label = ctk.CTkLabel(
            stats_frame,
            text="",
            font=(self.font_mono[0], self.font_sizes["caption"]),
            text_color=COLORS["text_muted"],
        )
        self._llamacpp_speed_label.pack(side="left", padx=(16, 0))
        
        self._llamacpp_accuracy_label = ctk.CTkLabel(
            stats_frame,
            text="",
            font=(self.font_mono[0], self.font_sizes["caption"]),
            text_color=COLORS["text_muted"],
        )
        self._llamacpp_accuracy_label.pack(side="left", padx=(16, 0))
        
        # Update info panel and button state
        self._update_llamacpp_info_panel()
        self._update_llamacpp_download_button()
        
        # Row 3: Progress bar (hidden by default)
        self._llamacpp_progress_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._llamacpp_progress_frame.pack(fill="x", pady=(0, 6))
        self._llamacpp_progress_frame.pack_forget()  # Hide initially
        
        self._llamacpp_progress_bar = ctk.CTkProgressBar(
            self._llamacpp_progress_frame,
            progress_color=COLORS["accent"],
            fg_color=COLORS["bg_hover"],
            height=8,
        )
        self._llamacpp_progress_bar.pack(fill="x", pady=(0, 4))
        self._llamacpp_progress_bar.set(0)
        
        self._llamacpp_status_label = ctk.CTkLabel(
            self._llamacpp_progress_frame,
            text="",
            font=(self.font_body[0], self.font_sizes["caption"]),
            text_color=COLORS["text_secondary"],
        )
        self._llamacpp_status_label.pack(anchor="w")
    
    def _on_llamacpp_model_selected(self, selection: str) -> None:
        """Handle llama.cpp model selection from dropdown."""
        if selection not in self._llamacpp_model_data:
            return
        
        data = self._llamacpp_model_data[selection]
        if data["installed"]:
            # Select and save the model
            self.config["llama_cpp_model_path"] = data["path"]
            save_config(self.config)
            self.log(f"⚙ LLM Model: {data['info']['name']}")
        
        self._update_llamacpp_download_button()
        self._update_llamacpp_info_panel()
        
        # Update compatibility check when model changes
        self._update_compatibility_banner()
    
    def _update_llamacpp_download_button(self) -> None:
        """Update download button state based on selected model."""
        if not hasattr(self, '_llamacpp_model_var'):
            return
        
        selection = self._llamacpp_model_var.get()
        if selection in self._llamacpp_model_data:
            data = self._llamacpp_model_data[selection]
            size = data["info"].get("size", "")
            if data["installed"]:
                self._llamacpp_download_btn.configure(
                    text="✓ Installed",
                    fg_color=COLORS["bg_hover"],
                    hover_color=COLORS["bg_hover"],
                    state="disabled",
                )
            else:
                btn_text = f"Download ({size})" if size else "Download"
                self._llamacpp_download_btn.configure(
                    text=btn_text,
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_hover"],
                    state="normal",
                )
    
    def _update_llamacpp_info_panel(self) -> None:
        """Update the model info panel based on selected model."""
        if not hasattr(self, '_llamacpp_model_var') or not hasattr(self, '_llamacpp_desc_label'):
            return
        
        selection = self._llamacpp_model_var.get()
        if selection in self._llamacpp_model_data:
            data = self._llamacpp_model_data[selection]
            info = data["info"]
            
            # Update description
            desc = info.get("description", "")
            if info.get("recommended"):
                desc = "⭐ " + desc
            self._llamacpp_desc_label.configure(text=desc)
            
            # Update stats
            size = info.get("size", "")
            speed = info.get("speed", "")
            accuracy = info.get("accuracy", "")
            
            self._llamacpp_size_label.configure(text=f"📦 {size}" if size else "")
            self._llamacpp_speed_label.configure(text=f"⚡ {speed}" if speed else "")
            self._llamacpp_accuracy_label.configure(text=f"🎯 {accuracy}" if accuracy else "")
    
    def _download_selected_llamacpp_model(self) -> None:
        """Download the currently selected llama.cpp model."""
        if self._inline_download_active:
            return
        
        selection = self._llamacpp_model_var.get()
        if selection not in self._llamacpp_model_data:
            return
        
        data = self._llamacpp_model_data[selection]
        if data["installed"]:
            return
        
        model_id = data["id"]
        model_info = data["info"]
        models_dir = _get_llm_models_dir()
        models_dir.mkdir(parents=True, exist_ok=True)
        
        self._inline_download_active = True
        self._cancel_download = False
        
        # Update button to show Cancel option (white text on accent color for readability)
        self._llamacpp_download_btn.configure(
            text="✕ Cancel",
            fg_color="#CF6679",  # Reddish cancel color
            hover_color="#B55566",
            text_color="#FFFFFF",  # White text for readability
            state="normal",
            command=self._cancel_llamacpp_download,
        )
        
        # Show progress bar
        self._llamacpp_progress_frame.pack(fill="x", pady=(0, 6))
        self._llamacpp_progress_bar.set(0)
        self._llamacpp_status_label.configure(text="Starting download...", text_color=COLORS["text_secondary"])
        
        def format_size(size_bytes):
            if size_bytes >= 1024 * 1024 * 1024:
                return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
            elif size_bytes >= 1024 * 1024:
                return f"{size_bytes / (1024 * 1024):.1f} MB"
            else:
                return f"{size_bytes / 1024:.0f} KB"
        
        def format_speed(bytes_per_sec):
            if bytes_per_sec >= 1024 * 1024:
                return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
            elif bytes_per_sec >= 1024:
                return f"{bytes_per_sec / 1024:.0f} KB/s"
            else:
                return f"{bytes_per_sec:.0f} B/s"
        
        def format_eta(seconds):
            if seconds < 60:
                return f"{int(seconds)}s"
            elif seconds < 3600:
                mins = int(seconds // 60)
                secs = int(seconds % 60)
                return f"{mins}m {secs}s"
            else:
                hours = int(seconds // 3600)
                mins = int((seconds % 3600) // 60)
                return f"{hours}h {mins}m"
        
        def update_progress(progress, status_text):
            try:
                if hasattr(self, '_llamacpp_progress_bar') and self._llamacpp_progress_bar.winfo_exists():
                    self._llamacpp_progress_bar.set(progress)
                    self._llamacpp_status_label.configure(text=status_text)
                    self.update_idletasks()  # Force UI update
            except:
                pass
        
        def download_thread():
            import time as time_module
            temp_path = None
            response = None
            try:
                # Log that thread started (this should appear in log panel)
                self.after(0, lambda: self.log("📦 Download thread started..."))
                
                import requests
                
                url = model_info["url"]
                filename = model_info["filename"]
                model_file = models_dir / filename
                temp_path = model_file.with_suffix('.tmp')
                
                # Check for cancel before starting
                if self._cancel_download:
                    raise Exception("Download cancelled by user")
                
                self.log(f"⬇️ Downloading {model_info['name']} from {url[:50]}...")
                
                # Update status to show we're connecting
                def show_connecting():
                    self._llamacpp_status_label.configure(text="Connecting to server...")
                    self.update_idletasks()
                self.after(0, show_connecting)
                
                # Set up session with proper headers (some CDNs block requests without User-Agent)
                session = requests.Session()
                session.headers.update({
                    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Wayfinder-Voice/1.0',
                    'Accept': '*/*',
                })
                
                # Start download with reasonable timeouts
                # (connect timeout, read timeout) - read timeout per chunk, not total
                response = session.get(url, stream=True, timeout=(15, 30), allow_redirects=True)
                response.raise_for_status()
                
                # Check for cancel after connection
                if self._cancel_download:
                    raise Exception("Download cancelled by user")
                
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                start_time = time_module.time()
                last_update_time = start_time
                
                with open(temp_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024 * 256):  # 256KB chunks for smoother progress
                        # Check for cancel
                        if self._cancel_download:
                            raise Exception("Download cancelled by user")
                        
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            current_time = time_module.time()
                            if current_time - last_update_time >= 0.3:  # Update every 300ms
                                elapsed = current_time - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                
                                if total_size > 0:
                                    progress = downloaded / total_size
                                    percentage = progress * 100
                                    remaining = total_size - downloaded
                                    eta = remaining / speed if speed > 0 else 0
                                    
                                    status = f"{format_size(downloaded)} / {format_size(total_size)} ({percentage:.0f}%) • {format_speed(speed)}"
                                    if eta > 0 and eta < 86400:  # Less than a day
                                        status += f" • ETA {format_eta(eta)}"
                                else:
                                    progress = min(0.9, downloaded / (500 * 1024 * 1024))  # Assume ~500MB if unknown
                                    status = f"{format_size(downloaded)} • {format_speed(speed)}"
                                
                                # Schedule UI update
                                self.after(10, lambda p=progress, s=status: update_progress(p, s))
                                last_update_time = current_time
                
                # Move temp to final
                if temp_path.exists():
                    if model_file.exists():
                        model_file.unlink()
                    temp_path.rename(model_file)
                
                # Calculate final stats
                total_time = time_module.time() - start_time
                avg_speed = downloaded / total_time if total_time > 0 else 0
                
                # Success
                def on_success():
                    self._inline_download_active = False
                    try:
                        self._llamacpp_progress_bar.set(1.0)
                        self._llamacpp_status_label.configure(
                            text=f"✓ Downloaded {model_info['name']} ({format_size(downloaded)} in {format_eta(total_time)})",
                            text_color=COLORS["accent"]
                        )
                        self.update_idletasks()  # Force UI refresh
                    except:
                        pass
                    self.log(f"✓ Downloaded: {model_info['name']} ({format_size(downloaded)}, avg {format_speed(avg_speed)})")
                    
                    # Auto-select the downloaded model
                    self.config["llama_cpp_model_path"] = str(model_file)
                    save_config(self.config)
                    
                    # Hide progress after 2 seconds and rebuild
                    self.after(2000, self._rebuild_postproc_section)
                
                self.after(0, on_success)
                
            except Exception as e:
                error_msg = str(e)
                is_cancelled = "cancelled" in error_msg.lower()
                
                # Close response if it exists (important for cleanup)
                try:
                    if response is not None:
                        response.close()
                except:
                    pass
                
                def on_error():
                    self._inline_download_active = False
                    if is_cancelled:
                        self._llamacpp_status_label.configure(
                            text="Download cancelled",
                            text_color=COLORS["text_muted"]
                        )
                        self.log("⚠️ Download cancelled")
                        # Rebuild UI after short delay
                        self.after(1000, self._rebuild_postproc_section)
                    else:
                        self._llamacpp_status_label.configure(
                            text=f"✗ Error: {error_msg[:60]}",
                            text_color="#CF7B7B"
                        )
                        self.log(f"❌ Download error: {error_msg}")
                        # Reset download button for retry
                        self._llamacpp_download_btn.configure(
                            text="Retry",
                            fg_color=COLORS["accent"],
                            hover_color=COLORS["accent_hover"],
                            text_color=COLORS["text_bright"],
                            state="normal",
                            command=self._download_selected_llamacpp_model,
                        )
                        self.log(f"⚠️ Download failed: {error_msg}")
                
                self.after(0, on_error)
                
                # Cleanup temp file
                try:
                    if temp_path and temp_path.exists():
                        temp_path.unlink()
                except:
                    pass
        
        self.log(f"🚀 Starting download of {model_info['name']}...")
        threading.Thread(target=download_thread, daemon=True).start()
    
    def _cancel_llamacpp_download(self) -> None:
        """Cancel the current llama.cpp model download."""
        self._cancel_download = True
        self._llamacpp_status_label.configure(text="Cancelling...", text_color=COLORS["text_muted"])
        self._llamacpp_download_btn.configure(state="disabled", text="Cancelling...")
    
    def _check_startup_dependencies(self) -> None:
        """Check critical dependencies on startup and warn/adjust as needed."""
        import shutil
        from pathlib import Path
        
        # Check 1: Verify transcription backend is available
        backend = self.config.get("transcription_backend", "whisper_cpp")
        
        if backend == "whisper_cpp":
            whisper_binary = self.config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli")
            whisper_binary = os.path.expanduser(whisper_binary)
            
            if not Path(whisper_binary).exists():
                # Try to find it elsewhere
                found = shutil.which("whisper-cli")
                if found:
                    self.config["whisper_binary"] = found
                    save_config(self.config)
                    self.log(f"✓ Found whisper-cli at {found}")
                else:
                    self.log("⚠️ whisper-cli not found - transcription won't work")
                    self.log("💡 Install: git clone https://github.com/ggerganov/whisper.cpp && cd whisper.cpp && make")
        
        # Check 2: Adjust thread count on first run
        if "threads_auto_adjusted" not in self.config:
            optimal_threads = get_optimal_thread_count()
            if self.config.get("threads", 4) != optimal_threads:
                self.config["threads"] = optimal_threads
                self.config["threads_auto_adjusted"] = True
                save_config(self.config)
                self.log(f"⚙️ Threads auto-set to {optimal_threads} (based on CPU cores)")
        
        # Check 3: Verify text injection is available
        if sys.platform == "darwin":
            try:
                import pyautogui
                self.log("✓ Text injection: pyautogui (macOS)")
            except ImportError:
                self.log("⚠️ pyautogui not installed - text injection won't work")
                self.log("💡 Install: pip install pyautogui")
        else:
            # Package-manager-aware hints — the old hardcoded `dnf`/`ydotoold` strings were
            # Fedora-only and broke on Arch/SteamOS, where the unit is the USER-level
            # ydotool.service (STEAMDECK-INSTALL-LOG Issue 4 + 2026-05-08 post-mortem).
            from wayfinder.core.setup import _detect_package_manager, _get_install_hint
            _svc = ("systemctl --user enable --now ydotool.service"
                    if _detect_package_manager() == "pacman"
                    else "sudo systemctl enable --now ydotoold")
            if not shutil.which("ydotool"):
                self.log("⚠️ ydotool not found - text injection won't work")
                self.log(f"💡 Install: {_get_install_hint('ydotool')}")
                self.log(f"💡 Then enable the daemon: {_svc}")
            else:
                from wayfinder.core.injector import check_ydotool_ready
                ready, msg = check_ydotool_ready()
                if ready:
                    self.log(f"✓ {msg}")
                else:
                    self.log(f"⚠️ {msg}")
                    self.log(f"💡 Start daemon: {_svc}")
    
    def _check_model_updates_background(self) -> None:
        """Check for model updates in a background thread (non-blocking)."""
        if not self.config.get("check_for_model_updates", True):
            return

        def _check():
            try:
                from wayfinder.core.model_updates import check_for_updates, get_available_upgrades

                # Check for repo updates
                results = check_for_updates()

                # Also check if user could upgrade their current model
                model_path = self.config.get("llama_cpp_model_path", "")
                upgrade = get_available_upgrades(model_path)

                if upgrade:
                    dismissed = self.config.get("dismissed_updates", [])
                    if upgrade["download_key"] not in dismissed:
                        self.event_queue.put((
                            EventType.LOG_MESSAGE,
                            f"💡 {upgrade['message']}",
                        ))
            except Exception:
                pass  # Silent failure - don't annoy user

        threading.Thread(target=_check, daemon=True).start()

    def _start_display_wake_listener(self) -> None:
        """Start D-Bus listener for system wake-up events to refresh the overlay.

        Uses org.freedesktop.login1.Manager.PrepareForSleep signal which fires:
        - With True before sleep
        - With False after wake-up
        """
        if not DBUS_AVAILABLE:
            self.log("⚠️ D-Bus not available - overlay won't auto-recover after sleep")
            return
        
        def listen_for_wake():
            """Background thread to listen for wake-up events."""
            try:
                DBusGMainLoop(set_as_default=True)
                bus = dbus.SystemBus()
                
                def on_prepare_for_sleep(sleeping):
                    """Called when system prepares for sleep (True) or wakes up (False)."""
                    if not sleeping:
                        # System just woke up - refresh the overlay after a short delay
                        # Delay gives the display time to fully initialize
                        self.after(1500, self._on_display_wake)
                
                # Subscribe to the PrepareForSleep signal
                bus.add_signal_receiver(
                    on_prepare_for_sleep,
                    signal_name="PrepareForSleep",
                    dbus_interface="org.freedesktop.login1.Manager",
                    bus_name="org.freedesktop.login1"
                )
                
                # Run the GLib main loop (blocks)
                loop = GLib.MainLoop()
                loop.run()
                
            except Exception as e:
                self.after(0, lambda: self.log(f"⚠️ Wake listener failed: {e}"))
        
        # Start listener in background thread
        wake_thread = threading.Thread(target=listen_for_wake, daemon=True, name="WakeListener")
        wake_thread.start()
    
    def _start_overlay_health_check(self) -> None:
        """Start periodic health check for the overlay subprocess."""
        def check_health():
            if self._use_pyqt_overlay and self.overlay_controller:
                if not self.overlay_controller.is_healthy():
                    self.log("🔄 Overlay process died - restarting...")
                    self.overlay_controller.refresh()
            # Check again in 10 seconds
            self.after(10000, check_health)
        
        # Start checking after 30 seconds (give app time to fully initialize)
        self.after(30000, check_health)

    def _start_hotkey_supervisor(self) -> None:
        """Periodically ensure the evdev hotkey listener is alive; restart it if it died.

        Runs regardless of overlay mode (the overlay health check above is PyQt-only). With the
        self-healing listener (device rescan + per-iteration guard) the thread should rarely die,
        so this is a backstop that prevents the app from being stranded in RECORDING when a
        dropped stop-press can no longer be delivered.
        """
        def check_hotkey():
            try:
                # Don't spawn evdev inside a Flatpak: evdev imports there (HAS_EVDEV is True)
                # but /dev/input is inaccessible (0 devices), so a "restart" only creates a
                # useless thread. The portal (below) is the Flatpak hotkey path; evdev
                # supervision is for non-sandboxed installs that actually use it.
                if HAS_EVDEV and not IS_FLATPAK and (self._hotkey_thread is None or not self._hotkey_thread.is_alive()):
                    self.log("🔄 Hotkey listener not running - restarting...")
                    self.restart_evdev_listener("supervisor: listener was not alive")
                # The socket listener can die on a transient bind failure; bring it back if so.
                if self._socket_thread is None or not self._socket_thread.is_alive():
                    self.log("🔄 Socket listener not running - restarting...")
                    self._ensure_socket_listener()
                # In a Flatpak the GlobalShortcuts portal is the only in-app global-hotkey
                # path; if its listener exited (D-Bus briefly unavailable, or setup returned
                # False), bring it back. Rides this same 10s tick — no new timer (Rule #1).
                if IS_FLATPAK and not getattr(self, '_portal_listener_started', False):
                    self.log("🔄 Portal hotkey listener not running - restarting...")
                    self._start_portal_listener()
            except Exception as e:
                self.log(f"⚠️ Hotkey supervisor error: {e}")
            # Re-check every 10 seconds (Rule #1: >=100ms, no busy polling)
            self.after(10000, check_hotkey)

        # Start checking after 30 seconds (give app time to bind input devices first)
        self.after(30000, check_hotkey)

    def _on_display_wake(self) -> None:
        """Called when the display wakes up from sleep.
        
        Refreshes the overlay to ensure it's visible and properly positioned.
        """
        if self._use_pyqt_overlay and self.overlay_controller:
            self.log("🌅 Display woke up - refreshing overlay...")
            if self.overlay_controller.refresh():
                self.log("✓ Overlay refreshed successfully")
            else:
                self.log("⚠️ Overlay refresh failed")
    
    def _browse_custom_gguf(self) -> None:
        """Browse for a custom GGUF file."""
        from tkinter import filedialog
        
        current_path = self.config.get("llama_cpp_model_path", "")
        if current_path:
            initial_dir = str(Path(current_path).parent)
        else:
            initial_dir = str(Path.home())
        
        file_path = filedialog.askopenfilename(
            title="Select GGUF Model File",
            initialdir=initial_dir,
            filetypes=[("GGUF models", "*.gguf"), ("All files", "*.*")],
        )
        
        if file_path:
            self.config["llama_cpp_model_path"] = file_path
            save_config(self.config)
            model_name = Path(file_path).name
            self.log(f"⚙ LLM Model: {model_name}")
            self._rebuild_postproc_section()
    
    def _rebuild_postproc_section(self) -> None:
        """Rebuild the post-processing section to refresh model list."""
        current_mode = self.config.get("processing_mode", "local")
        self._build_mode_settings(current_mode)
    
    def open_postproc_model_settings(self):
        """Open dialog to select or download llama.cpp GGUF models for post-processing."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("LLM Models for Post-Processing")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 680, 780)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        # Title
        ctk.CTkLabel(
            inner,
            text="LLM Models for Post-Processing",
            font=(self.font_header[0], 20, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 8))
        
        ctk.CTkLabel(
            inner,
            text="Select a GGUF model or download a recommended one.",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 16))
        
        # Models directory
        models_dir = _get_llm_models_dir()
        models_dir.mkdir(parents=True, exist_ok=True)
        
        # Content container
        content_container = ctk.CTkFrame(inner, fg_color="transparent")
        content_container.pack(fill="both", expand=True)
        
        # Recommended models scrollable list
        recommended_scroll = SmoothScrollableFrame(
            content_container,
            fg_color=COLORS["bg_surface"],
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
            height=400,
        )
        recommended_scroll.pack(fill="both", expand=True)
        
        current_model_path = self.config.get("llama_cpp_model_path", "")
        
        # Show recommended models
        for model_id, model_info in LLM_GGUF_MODELS.items():
            model_file = models_dir / model_info["filename"]
            is_installed = model_file.exists()
            is_selected = str(model_file) == current_model_path if is_installed else False
            
            frame = ctk.CTkFrame(
                recommended_scroll,
                fg_color=COLORS["bg_card"] if is_selected else COLORS["bg_surface"],
                corner_radius=RADIUS["sm"],
            )
            frame.pack(fill="x", padx=4, pady=4)
            
            # Model info
            info_frame = ctk.CTkFrame(frame, fg_color="transparent")
            info_frame.pack(fill="x", padx=12, pady=8)
            
            name_label = ctk.CTkLabel(
                info_frame,
                text=model_info["name"] + (" ⭐" if model_info.get("recommended") else ""),
                font=(self.font_body[0], self.font_sizes["body"], "bold"),
                text_color=COLORS["text_bright"],
            )
            name_label.pack(anchor="w")
            
            desc_label = ctk.CTkLabel(
                info_frame,
                text=model_info["description"],
                font=(self.font_body[0], self.font_sizes["small"]),
                text_color=COLORS["text_secondary"],
            )
            desc_label.pack(anchor="w", pady=(2, 0))
            
            size_label = ctk.CTkLabel(
                info_frame,
                text=f"Size: {model_info['size']}",
                font=(self.font_mono[0], self.font_sizes["caption"]),
                text_color=COLORS["text_muted"],
            )
            size_label.pack(anchor="w", pady=(2, 0))
            
            # Actions
            actions_frame = ctk.CTkFrame(frame, fg_color="transparent")
            actions_frame.pack(fill="x", padx=12, pady=(0, 8))
            
            if is_installed:
                if is_selected:
                    status_btn = ctk.CTkButton(
                        actions_frame,
                        text="✓ Selected",
                        font=(self.font_body[0], self.font_sizes["small"]),
                        fg_color=COLORS["accent"],
                        hover_color=COLORS["accent_hover"],
                        text_color=COLORS["text_bright"],
                        command=lambda m=model_file: self._select_llm_model(str(m), dialog),
                    )
                    status_btn.pack(side="left", padx=(0, 8))
                else:
                    select_btn = ctk.CTkButton(
                        actions_frame,
                        text="Select",
                        font=(self.font_body[0], self.font_sizes["small"]),
                        fg_color=COLORS["bg_hover"],
                        hover_color=COLORS["bg_elevated"],
                        text_color=COLORS["text_primary"],
                        command=lambda m=model_file: self._select_llm_model(str(m), dialog),
                    )
                    select_btn.pack(side="left", padx=(0, 8))
                
                delete_btn = ctk.CTkButton(
                    actions_frame,
                    text="Delete",
                    font=(self.font_body[0], self.font_sizes["small"]),
                    fg_color="transparent",
                    hover_color=COLORS["bg_hover"],
                    text_color=COLORS["text_secondary"],
                    command=lambda m=model_file: self._delete_llm_model(m, dialog),
                )
                delete_btn.pack(side="left")
            else:
                download_btn = ctk.CTkButton(
                    actions_frame,
                    text="⬇️ Download",
                    font=(self.font_body[0], self.font_sizes["small"]),
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_hover"],
                    text_color=COLORS["text_bright"],
                    command=lambda mid=model_id, info=model_info: self._download_llm_model(mid, info, models_dir, dialog),
                )
                download_btn.pack(side="left")
        
        # Browse files button
        browse_files_btn = ctk.CTkButton(
            inner,
            text="Browse for GGUF File...",
            font=(self.font_body[0], self.font_sizes["body"]),
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_primary"],
            command=lambda: self._browse_llm_file(dialog),
        )
        browse_files_btn.pack(fill="x", pady=(12, 8))
        
        # Close button
        close_btn = ctk.CTkButton(
            inner,
            text="Close",
            font=(self.font_body[0], self.font_sizes["body"]),
            fg_color=COLORS["bg_card"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
            command=dialog.destroy,
        )
        close_btn.pack(fill="x")
    
    def _select_llm_model(self, model_path: str, dialog):
        """Select an LLM model."""
        self.config["llama_cpp_model_path"] = model_path
        save_config(self.config)
        model_name = Path(model_path).name
        self.log(f"⚙ Post-processing model: {model_name}")
        dialog.destroy()
        # Rebuild settings to refresh UI
        current_mode = self.config.get("processing_mode", "local")
        self._build_mode_settings(current_mode)
    
    def _delete_llm_model(self, model_path: Path, dialog):
        """Delete an LLM model file."""
        try:
            model_path.unlink()
            self.log(f"🗑️ Deleted model: {model_path.name}")
            dialog.destroy()
            self.open_postproc_model_settings()  # Refresh dialog
        except Exception as e:
            self.log(f"⚠️ Failed to delete model: {e}")
    
    def _browse_llm_file(self, dialog):
        """Browse for a GGUF file."""
        from tkinter import filedialog
        
        current_path = self.config.get("llama_cpp_model_path", "")
        if current_path:
            initial_dir = str(Path(current_path).parent)
        else:
            initial_dir = str(Path.home())
        
        file_path = filedialog.askopenfilename(
            title="Select GGUF Model",
            initialdir=initial_dir,
            filetypes=[("GGUF files", "*.gguf"), ("All files", "*.*")],
        )
        
        if file_path:
            self._select_llm_model(file_path, dialog)
    
    def _download_llm_model(self, model_id: str, model_info: dict, models_dir: Path, dialog):
        """Download an LLM model with detailed progress."""
        import time as time_module
        
        model_file = models_dir / model_info["filename"]
        model_name = model_info["name"]
        model_url = model_info["url"]
        model_filename = model_info["filename"]
        
        # Create progress dialog
        progress_dialog = ctk.CTkToplevel(dialog)
        progress_dialog.title(f"Downloading {model_name}")
        progress_dialog.configure(fg_color=COLORS["bg_base"])
        progress_dialog.transient(dialog)
        self._setup_dialog(progress_dialog, 500, 320)
        
        inner = ctk.CTkFrame(progress_dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=20, pady=20)
        
        ctk.CTkLabel(
            inner,
            text=f"Downloading {model_name}",
            font=(self.font_header[0], 16, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(pady=(0, 12))
        
        progress_bar = ctk.CTkProgressBar(inner)
        progress_bar.pack(fill="x", pady=(0, 8))
        progress_bar.set(0)
        
        # Status label (bytes and percentage)
        status_label = ctk.CTkLabel(
            inner,
            text="Connecting...",
            font=(self.font_body[0], self.font_sizes["small"]),
            text_color=COLORS["text_secondary"],
        )
        status_label.pack(pady=(0, 4))
        
        # Speed and time label
        speed_label = ctk.CTkLabel(
            inner,
            text="",
            font=(self.font_mono[0], self.font_sizes["caption"]),
            text_color=COLORS["text_muted"],
        )
        speed_label.pack(pady=(0, 12))
        
        self._cancel_llm_download = False
        
        cancel_btn = ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], self.font_sizes["small"]),
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_primary"],
            command=lambda: setattr(self, '_cancel_llm_download', True),
        )
        cancel_btn.pack(pady=(8, 0))
        
        # Helper functions (defined outside thread for stability)
        def format_size(bytes_val):
            for unit in ['B', 'KB', 'MB', 'GB']:
                if bytes_val < 1024.0:
                    return f"{bytes_val:.1f} {unit}"
                bytes_val /= 1024.0
            return f"{bytes_val:.1f} TB"
        
        def format_speed(bytes_per_sec):
            if bytes_per_sec < 1024:
                return f"{bytes_per_sec:.0f} B/s"
            elif bytes_per_sec < 1024 * 1024:
                return f"{bytes_per_sec / 1024:.1f} KB/s"
            else:
                return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
        
        def format_time(seconds):
            if seconds < 60:
                return f"{int(seconds)}s"
            elif seconds < 3600:
                return f"{int(seconds // 60)}m {int(seconds % 60)}s"
            else:
                hours = int(seconds // 3600)
                minutes = int((seconds % 3600) // 60)
                return f"{hours}h {minutes}m"
        
        def update_progress(prog, status_text, speed_text):
            """Safe UI update function."""
            try:
                if progress_dialog.winfo_exists():
                    progress_bar.set(prog)
                    status_label.configure(text=status_text)
                    if speed_text:
                        speed_label.configure(text=speed_text)
            except Exception:
                pass
        
        def download_thread():
            temp_path = None
            try:
                temp_path = models_dir / f"{model_filename}.downloading"
                
                request = urllib.request.Request(model_url)
                request.add_header("User-Agent", "Wayfinder-Voice/1.0")
                
                start_time = time_module.time()
                last_update_time = start_time
                
                with urllib.request.urlopen(request, timeout=60) as response:
                    total_size = int(response.headers.get("Content-Length", 0))
                    downloaded = 0
                    chunk_size = 64 * 1024  # 64KB chunks
                    
                    with open(temp_path, "wb") as f:
                        while True:
                            if self._cancel_llm_download:
                                try:
                                    f.close()
                                    temp_path.unlink(missing_ok=True)
                                except:
                                    pass
                                self.after(0, lambda: progress_dialog.destroy() if progress_dialog.winfo_exists() else None)
                                return
                            
                            chunk = response.read(chunk_size)
                            if not chunk:
                                break
                            
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Update UI every 0.3 seconds
                            current_time = time_module.time()
                            if current_time - last_update_time >= 0.3:
                                elapsed = current_time - start_time
                                speed = downloaded / elapsed if elapsed > 0 else 0
                                
                                if total_size > 0:
                                    progress = downloaded / total_size
                                    percentage = progress * 100
                                    remaining = total_size - downloaded
                                    eta = remaining / speed if speed > 0 else 0
                                    
                                    status_text = f"{format_size(downloaded)} / {format_size(total_size)} ({percentage:.1f}%)"
                                    speed_text = f"{format_speed(speed)} • {format_time(elapsed)} elapsed"
                                    if eta > 0 and eta < 86400:
                                        speed_text += f" • {format_time(eta)} remaining"
                                    
                                    self.after(0, lambda p=progress, s=status_text, sp=speed_text: update_progress(p, s, sp))
                                else:
                                    status_text = f"{format_size(downloaded)} downloaded"
                                    speed_text = f"{format_speed(speed)}" if speed > 0 else ""
                                    self.after(0, lambda s=status_text, sp=speed_text: update_progress(0.5, s, sp))
                                
                                last_update_time = current_time
                    
                    # Download complete - move temp to final
                    if temp_path.exists():
                        if model_file.exists():
                            model_file.unlink()
                        temp_path.rename(model_file)
                        
                        total_time = time_module.time() - start_time
                        final_speed = downloaded / total_time if total_time > 0 else 0
                        
                        def on_success():
                            try:
                                self.log(f"✓ Downloaded: {model_name} ({format_size(downloaded)} in {format_time(total_time)}, avg {format_speed(final_speed)})")
                                if progress_dialog.winfo_exists():
                                    progress_dialog.destroy()
                                self.open_postproc_model_settings()
                            except Exception as e:
                                self.log(f"⚠️ Error in success handler: {e}")
                        
                        self.after(0, on_success)
                    else:
                        raise Exception("Download file missing after completion")
                    
            except Exception as e:
                error_msg = str(e)[:100]  # Truncate long errors
                self.log(f"⚠️ Download failed: {error_msg}")
                
                def on_error():
                    try:
                        if progress_dialog.winfo_exists():
                            status_label.configure(text=f"Error: {error_msg}", text_color="#CF7B7B")
                            speed_label.configure(text="")
                            cancel_btn.configure(text="Close")
                    except:
                        pass
                
                self.after(0, on_error)
                
                # Cleanup
                try:
                    if temp_path and temp_path.exists():
                        temp_path.unlink()
                except:
                    pass
        
        threading.Thread(target=download_thread, daemon=True).start()
    
    def _get_postproc_config_display(self) -> str:
        """Get display text for post-processing configuration button."""
        import os
        backend = self.config.get("post_processing_backend", "llama_cpp")
        if backend == "llama_cpp":
            model_path = self.config.get("llama_cpp_model_path", "")
            if model_path:
                # Show just the filename
                from pathlib import Path
                name = Path(model_path).name
                return name[:25] + "..." if len(name) > 25 else name
            return "No model selected"
        elif backend == "anthropic":
            # API keys are read from environment variables only for security
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            model = self.config.get("anthropic_model", "claude-3-haiku-20240307")
            if api_key:
                return f"{model.split('-')[1].title()}: ✓ Key set"
            return "⚠ Set ANTHROPIC_API_KEY env var"
        elif backend == "openai":
            # API keys are read from environment variables only for security
            api_key = os.environ.get("OPENAI_API_KEY", "")
            model = self.config.get("openai_model", "gpt-4o-mini")
            if api_key:
                return f"{model}: ✓ Key set"
            return "⚠ Set OPENAI_API_KEY env var"
        return "Not configured"
    
    def open_postproc_settings(self):
        """Open post-processing configuration dialog with provider switching."""
        import os
        
        dialog = ctk.CTkToplevel(self)
        dialog.title("Cloud API Configuration")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 640, 600)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        # Title
        ctk.CTkLabel(
            inner,
            text="Cloud API Configuration",
            font=(self.font_header[0], 20, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 16))
        
        # Get current provider from config
        current_backend = self.config.get("post_processing_backend", "openai")
        if current_backend == "llama_cpp":
            current_backend = "openai"  # Default to OpenAI for cloud config
        
        # Provider selector (OpenAI vs Anthropic)
        provider_frame = ctk.CTkFrame(inner, fg_color="transparent")
        provider_frame.pack(fill="x", pady=(0, 16))
        
        ctk.CTkLabel(
            provider_frame,
            text="PROVIDER",
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", pady=(0, 8))
        
        # Provider segmented control
        provider_var = ctk.StringVar(value=current_backend)
        provider_buttons_frame = ctk.CTkFrame(provider_frame, fg_color=COLORS["bg_input"], corner_radius=RADIUS["md"], height=44)
        provider_buttons_frame.pack(fill="x")
        provider_buttons_frame.pack_propagate(False)
        
        provider_inner = ctk.CTkFrame(provider_buttons_frame, fg_color="transparent")
        provider_inner.pack(fill="both", expand=True, padx=3, pady=3)
        provider_inner.grid_columnconfigure(0, weight=1)
        provider_inner.grid_columnconfigure(1, weight=1)
        provider_inner.grid_rowconfigure(0, weight=1)
        
        provider_btns = {}
        
        def update_provider_buttons():
            selected = provider_var.get()
            for name, btn in provider_btns.items():
                is_selected = name == selected
                btn.configure(
                    fg_color=COLORS["bg_card"] if is_selected else "transparent",
                    text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                    font=(self.font_body[0], 13, "bold" if is_selected else "normal"),
                )
        
        def select_provider(name):
            provider_var.set(name)
            update_provider_buttons()
            rebuild_provider_settings()
        
        openai_btn = ctk.CTkButton(
            provider_inner,
            text="OpenAI",
            font=(self.font_body[0], 13, "bold" if current_backend == "openai" else "normal"),
            fg_color=COLORS["bg_card"] if current_backend == "openai" else "transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_bright"] if current_backend == "openai" else COLORS["text_secondary"],
            corner_radius=RADIUS["sm"],
            height=36,
            command=lambda: select_provider("openai"),
        )
        openai_btn.grid(row=0, column=0, sticky="nsew", padx=1)
        provider_btns["openai"] = openai_btn
        
        anthropic_btn = ctk.CTkButton(
            provider_inner,
            text="Anthropic",
            font=(self.font_body[0], 13, "bold" if current_backend == "anthropic" else "normal"),
            fg_color=COLORS["bg_card"] if current_backend == "anthropic" else "transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_bright"] if current_backend == "anthropic" else COLORS["text_secondary"],
            corner_radius=RADIUS["sm"],
            height=36,
            command=lambda: select_provider("anthropic"),
        )
        anthropic_btn.grid(row=0, column=1, sticky="nsew", padx=1)
        provider_btns["anthropic"] = anthropic_btn
        
        # Dynamic settings container
        settings_container = ctk.CTkFrame(inner, fg_color="transparent")
        settings_container.pack(fill="both", expand=True)
        
        # Variables to store form data (persist across provider switches)
        # Note: API keys are read from environment variables only for security
        form_data = {
            "openai_key": ctk.StringVar(value=self.config.get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")),
            "openai_model": ctk.StringVar(value=self.config.get("openai_model", "gpt-4o-mini")),
            "anthropic_key": ctk.StringVar(value=self.config.get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")),
            "anthropic_model": ctk.StringVar(value=self.config.get("anthropic_model", "claude-3-haiku-20240307")),
        }
        
        def rebuild_provider_settings():
            # Clear existing settings
            for widget in settings_container.winfo_children():
                widget.destroy()
            
            provider = provider_var.get()
            
            if provider == "openai":
                # OpenAI settings
                api_frame = ctk.CTkFrame(settings_container, fg_color=COLORS["bg_card"], corner_radius=12)
                api_frame.pack(fill="x", pady=(0, 12))
                
                ctk.CTkLabel(
                    api_frame,
                    text="API Key",
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_secondary"],
                ).pack(anchor="w", padx=16, pady=(12, 4))
                
                key_entry = ctk.CTkEntry(
                    api_frame,
                    textvariable=form_data["openai_key"],
                    font=(self.font_mono[0], 11),
                    fg_color=COLORS["bg_input"],
                    border_color=COLORS["border_subtle"],
                    text_color=COLORS["text_primary"],
                    placeholder_text="sk-...",
                    width=350,
                    show="•",
                )
                key_entry.pack(anchor="w", padx=16, pady=(0, 4))
                
                show_key_var = ctk.BooleanVar(value=False)
                def toggle_visibility():
                    key_entry.configure(show="" if show_key_var.get() else "•")
                
                ctk.CTkCheckBox(
                    api_frame,
                    text="Show key",
                    variable=show_key_var,
                    command=toggle_visibility,
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_dim"],
                ).pack(anchor="w", padx=16, pady=(0, 12))
                
                ctk.CTkLabel(
                    api_frame,
                    text="OpenAI Model",
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_secondary"],
                ).pack(anchor="w", padx=16, pady=(0, 4))
                
                ctk.CTkOptionMenu(
                    api_frame,
                    variable=form_data["openai_model"],
                    values=["gpt-4o-mini", "gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"],
                    font=(self.font_body[0], 12),
                    fg_color=COLORS["bg_input"],
                    button_color=COLORS["bg_surface"],
                    button_hover_color=COLORS["bg_hover"],
                    dropdown_fg_color=COLORS["bg_card"],
                    dropdown_hover_color=COLORS["bg_hover"],
                    text_color=COLORS["text_primary"],
                    width=280,
                ).pack(anchor="w", padx=16, pady=(0, 12))
                
                ctk.CTkLabel(
                    settings_container,
                    text="GPT-4o-mini is fast and affordable. GPT-4o is higher quality but more expensive.",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                    wraplength=460,
                ).pack(anchor="w", pady=(0, 8))
                
                link = ctk.CTkLabel(
                    settings_container,
                    text="Get your API key at: platform.openai.com/api-keys",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["accent"],
                    cursor="hand2",
                )
                link.pack(anchor="w", pady=(0, 8))
                link.bind("<Button-1>", lambda e: webbrowser.open("https://platform.openai.com/api-keys"))
                
            else:  # anthropic
                api_frame = ctk.CTkFrame(settings_container, fg_color=COLORS["bg_card"], corner_radius=12)
                api_frame.pack(fill="x", pady=(0, 12))
                
                ctk.CTkLabel(
                    api_frame,
                    text="API Key",
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_secondary"],
                ).pack(anchor="w", padx=16, pady=(12, 4))
                
                key_entry = ctk.CTkEntry(
                    api_frame,
                    textvariable=form_data["anthropic_key"],
                    font=(self.font_mono[0], 11),
                    fg_color=COLORS["bg_input"],
                    border_color=COLORS["border_subtle"],
                    text_color=COLORS["text_primary"],
                    placeholder_text="sk-ant-api03-...",
                    width=350,
                    show="•",
                )
                key_entry.pack(anchor="w", padx=16, pady=(0, 4))
                
                show_key_var = ctk.BooleanVar(value=False)
                def toggle_visibility():
                    key_entry.configure(show="" if show_key_var.get() else "•")
                
                ctk.CTkCheckBox(
                    api_frame,
                    text="Show key",
                    variable=show_key_var,
                    command=toggle_visibility,
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_dim"],
                ).pack(anchor="w", padx=16, pady=(0, 12))
                
                ctk.CTkLabel(
                    api_frame,
                    text="Claude Model",
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_secondary"],
                ).pack(anchor="w", padx=16, pady=(0, 4))
                
                ctk.CTkOptionMenu(
                    api_frame,
                    variable=form_data["anthropic_model"],
                    values=[
                        "claude-3-haiku-20240307",
                        "claude-3-5-haiku-20241022",
                        "claude-3-sonnet-20240229",
                        "claude-3-5-sonnet-20241022",
                    ],
                    font=(self.font_body[0], 12),
                    fg_color=COLORS["bg_input"],
                    button_color=COLORS["bg_surface"],
                    button_hover_color=COLORS["bg_hover"],
                    dropdown_fg_color=COLORS["bg_card"],
                    dropdown_hover_color=COLORS["bg_hover"],
                    text_color=COLORS["text_primary"],
                    width=280,
                ).pack(anchor="w", padx=16, pady=(0, 12))
                
                ctk.CTkLabel(
                    settings_container,
                    text="Claude Haiku is fast and cheap (~$0.25/1M tokens). Sonnet is higher quality but slower.",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                    wraplength=460,
                ).pack(anchor="w", pady=(0, 8))
                
                link = ctk.CTkLabel(
                    settings_container,
                    text="Get your API key at: console.anthropic.com",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["accent"],
                    cursor="hand2",
                )
                link.pack(anchor="w", pady=(0, 8))
                link.bind("<Button-1>", lambda e: webbrowser.open("https://console.anthropic.com"))
        
        # Build initial settings
        rebuild_provider_settings()
        
        def save_settings():
            provider = provider_var.get()
            
            # Save the selected provider as the post-processing backend
            self.config["post_processing_backend"] = provider
            
            # Save OpenAI settings (to both env and config for persistence)
            openai_key = form_data["openai_key"].get().strip()
            if openai_key:
                os.environ["OPENAI_API_KEY"] = openai_key
                self.config["openai_api_key"] = openai_key  # Persist to config
            self.config["openai_model"] = form_data["openai_model"].get()
            
            # Save Anthropic settings (to both env and config for persistence)
            anthropic_key = form_data["anthropic_key"].get().strip()
            if anthropic_key:
                os.environ["ANTHROPIC_API_KEY"] = anthropic_key
                self.config["anthropic_api_key"] = anthropic_key  # Persist to config
            self.config["anthropic_model"] = form_data["anthropic_model"].get()
            
            save_config(self.config)
            
            # Update UI
            if hasattr(self, 'postproc_config_btn'):
                self.postproc_config_btn.configure(text=self._get_postproc_config_display())
            if hasattr(self, 'postproc_backend_var'):
                self.postproc_backend_var.set(provider)
            if hasattr(self, 'remote_api_btn'):
                status = "Configured ✓" if (openai_key or anthropic_key) else "Not configured"
                self.remote_api_btn.configure(text=status)
            
            self.log(f"⚙ Cloud API settings saved ({provider.title()})")
            dialog.destroy()
        
        # Buttons
        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(20, 0), side="bottom")
        
        ctk.CTkButton(
            btn_frame,
            text="Cancel",
            font=(self.font_body[0], 13),
            fg_color=COLORS["bg_surface"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_secondary"],
            height=40,
            width=100,
            corner_radius=8,
            command=dialog.destroy,
        ).pack(side="left")
        
        ctk.CTkButton(
            btn_frame,
            text="Save",
            font=(self.font_body[0], 13, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_dim"],
            text_color=COLORS["bg_base"],
            height=40,
            width=100,
            corner_radius=8,
            command=save_settings,
        ).pack(side="right")

    def _setup_dialog(self, dialog, base_w, base_h):
        """Center a dialog on the main window with proper sizing."""
        dialog.update_idletasks()
        parent_x = self.winfo_x()
        parent_y = self.winfo_y()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        x = parent_x + (parent_w - base_w) // 2
        y = parent_y + (parent_h - base_h) // 2
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(20, min(x, screen_w - base_w - 20))
        y = max(20, min(y, screen_h - base_h - 20))
        dialog.geometry(f"{base_w}x{base_h}+{x}+{y}")
        dialog.minsize(base_w, base_h)

    def _show_inline_panel(self, container, title: str, build_fn):
        """Show an inline settings panel, hiding the container's current children.

        Args:
            container: The parent frame whose children will be hidden
            title: Panel title shown in the header
            build_fn: Callable(content_frame) that builds the panel content
        """
        # Store hidden children so we can restore them
        hidden = []
        for child in container.winfo_children():
            info = child.pack_info() if child.winfo_manager() == "pack" else None
            child.pack_forget()
            hidden.append((child, info))

        # Panel wrapper
        panel = ctk.CTkFrame(container, fg_color="transparent")
        panel.pack(fill="both", expand=True)
        panel._inline_hidden = hidden  # stash for restore

        # Header with back button
        header = ctk.CTkFrame(panel, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(8, 4))

        def close():
            self._close_inline_panel(container, panel)

        back_btn = ctk.CTkButton(
            header, text="←", width=32, height=32,
            font=(self.font_body[0], 16),
            fg_color=COLORS["bg_hover"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_primary"], corner_radius=RADIUS["sm"],
            command=close,
        )
        back_btn.pack(side="left")

        ctk.CTkLabel(
            header, text=title,
            font=(self.font_header[0], 16, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(side="left", padx=(10, 0))

        # Content area
        content = ctk.CTkFrame(panel, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=8, pady=(4, 8))

        build_fn(content, close)

    @staticmethod
    def _trap_scroll(scrollable_frame):
        """Prevent mouse wheel events from bubbling to parent scroll containers.

        Uses bind_all with a tag so that any widget inside the scrollable frame
        has its scroll events handled locally and stopped from propagating.
        """
        try:
            canvas = scrollable_frame._parent_canvas
        except AttributeError:
            return

        def _on_enter(event):
            # When mouse enters, bind scroll to this canvas
            canvas.bind_all("<Button-4>", _scroll_up)
            canvas.bind_all("<Button-5>", _scroll_down)
            canvas.bind_all("<MouseWheel>", _scroll_wheel)

        def _on_leave(event):
            # When mouse leaves, unbind
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")
            canvas.unbind_all("<MouseWheel>")

        def _scroll_up(event):
            canvas.yview_scroll(-3, "units")
            return "break"

        def _scroll_down(event):
            canvas.yview_scroll(3, "units")
            return "break"

        def _scroll_wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

        scrollable_frame.bind("<Enter>", _on_enter)
        scrollable_frame.bind("<Leave>", _on_leave)

    def _close_inline_panel(self, container, panel):
        """Close an inline panel and restore the container's original children."""
        hidden = getattr(panel, '_inline_hidden', [])
        panel.destroy()

        for child, pack_info in hidden:
            if pack_info:
                child.pack(**pack_info)
            else:
                child.pack()

    def create_setting_row(self, parent, label, value, command, tooltip=None, tooltip_key=None):
        """Create a premium setting row with improved typography and spacing.
        
        Args:
            tooltip_key: If provided, stores tooltips for dynamic updates (e.g., after benchmarks)
        """
        # Row container with increased padding (20% more breathing room)
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=10)  # Increased vertical padding
        
        row.grid_columnconfigure(0, weight=1)  # Label column - grows
        row.grid_columnconfigure(1, weight=0)  # Button column - fixed
        
        # Label container for text + info icon
        label_container = ctk.CTkFrame(row, fg_color="transparent")
        label_container.grid(row=0, column=0, sticky="w")
        
        # Label text - MEDIUM weight, improved contrast
        label_widget = ctk.CTkLabel(
            label_container,
            text=label,
            font=(self.font_body[0], self.font_sizes["body"], "bold"),  # Medium/bold weight
            text_color=COLORS["text_primary"],  # Better contrast
        )
        label_widget.pack(side="left")
        
        # Info icon
        if tooltip:
            info_icon = ctk.CTkLabel(
                label_container,
                text="ⓘ",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            )
            info_icon.pack(side="left", padx=(8, 0))
            tt1 = ToolTip(label_widget, tooltip)
            tt2 = ToolTip(info_icon, tooltip)
            # Store for dynamic updates if key provided
            if tooltip_key:
                if tooltip_key not in self.dynamic_tooltips:
                    self.dynamic_tooltips[tooltip_key] = []
                self.dynamic_tooltips[tooltip_key].extend([tt1, tt2])
        
        # Premium button with thin border
        btn = ctk.CTkButton(
            row,
            text=value,
            font=(self.font_mono[0], self.font_sizes["small"]),  # Mono for values
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["accent"],
            height=36,
            corner_radius=RADIUS["sm"],
            border_width=1,
            border_color=COLORS["border"],
            command=command,
        )
        btn.grid(row=0, column=1, sticky="e", padx=(16, 0))
        
        return btn

    def _create_scale_slider_row(self, parent):
        """Create inline UI scale slider with real-time adjustment."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=8)
        
        row.grid_columnconfigure(0, weight=0)  # Label column
        row.grid_columnconfigure(1, weight=1)  # Slider column - grows
        row.grid_columnconfigure(2, weight=0)  # Value display column
        
        # Label
        label_widget = ctk.CTkLabel(
            row,
            text="UI Scale",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["text_secondary"],
        )
        label_widget.grid(row=0, column=0, sticky="w")
        
        tooltip_text = SETTING_TOOLTIPS.get("ui_scale", "")
        if tooltip_text:
            ToolTip(label_widget, tooltip_text)
        
        # Value display (right side, shows percentage)
        self.scale_value_label = ctk.CTkLabel(
            row,
            text=f"{int(self.ui_scale * 100)}%",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["accent"],
            width=60,
        )
        self.scale_value_label.grid(row=0, column=2, sticky="e", padx=(10, 0))
        
        # Slider in the middle
        self.scale_slider_var = ctk.DoubleVar(value=self.ui_scale)
        
        # Debounce timer for applying scale
        self._scale_apply_timer = None
        
        def on_slider_change(value):
            """Update display and schedule scale application."""
            value = float(value)
            # Snap to nearest 5%
            snapped = round(value * 20) / 20
            self.scale_value_label.configure(text=f"{int(snapped * 100)}%")
            
            # Cancel any pending apply
            if self._scale_apply_timer is not None:
                try:
                    self.after_cancel(self._scale_apply_timer)
                except:
                    pass
            
            # Schedule apply after a short delay (debounce)
            self._scale_apply_timer = self.after(150, lambda: self._apply_live_scale(snapped))
        
        self.scale_slider = ctk.CTkSlider(
            row,
            from_=0.7,
            to=2.5,
            variable=self.scale_slider_var,
            command=on_slider_change,
            height=18,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            fg_color=COLORS["bg_input"],
        )
        self.scale_slider.grid(row=0, column=1, sticky="ew", padx=(20, 10))
        
        if tooltip_text:
            ToolTip(self.scale_slider, tooltip_text)
    
    def _apply_live_scale(self, new_scale):
        """Apply UI scale change in real-time."""
        if abs(new_scale - self.ui_scale) < 0.01:
            return  # No significant change
        
        self.ui_scale = new_scale
        self.config["ui_scale"] = new_scale
        save_config(self.config)
        self._apply_scale()
        self.log(f"⚙ UI Scale: {int(new_scale * 100)}%")

    def _create_overlay_scale_slider_row(self, parent):
        """Create inline overlay scale slider (separate from UI scale)."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=8)
        
        row.grid_columnconfigure(0, weight=0)  # Label column
        row.grid_columnconfigure(1, weight=1)  # Slider column - grows
        row.grid_columnconfigure(2, weight=0)  # Value display column
        
        # Label
        label_widget = ctk.CTkLabel(
            row,
            text="Overlay Scale",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["text_secondary"],
        )
        label_widget.grid(row=0, column=0, sticky="w")
        
        tooltip_text = SETTING_TOOLTIPS.get("overlay_scale", "")
        if tooltip_text:
            ToolTip(label_widget, tooltip_text)
        
        # Get current overlay scale
        overlay_scale = self.config.get("overlay_scale", 1.0)
        
        # Value display (right side, shows percentage)
        self.overlay_scale_value_label = ctk.CTkLabel(
            row,
            text=f"{int(overlay_scale * 100)}%",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["accent"],
            width=60,
        )
        self.overlay_scale_value_label.grid(row=0, column=2, sticky="e", padx=(10, 0))
        
        # Slider in the middle
        self.overlay_scale_slider_var = ctk.DoubleVar(value=overlay_scale)
        
        # Debounce timer for applying scale
        self._overlay_scale_apply_timer = None
        
        def on_overlay_slider_change(value):
            """Update display and schedule overlay scale application."""
            value = float(value)
            # Snap to nearest 10%
            snapped = round(value * 10) / 10
            self.overlay_scale_value_label.configure(text=f"{int(snapped * 100)}%")
            
            # Cancel any pending apply
            if self._overlay_scale_apply_timer is not None:
                try:
                    self.after_cancel(self._overlay_scale_apply_timer)
                except:
                    pass
            
            # Schedule apply after a short delay (debounce)
            self._overlay_scale_apply_timer = self.after(150, lambda: self._apply_overlay_scale(snapped))
        
        self.overlay_scale_slider = ctk.CTkSlider(
            row,
            from_=0.5,
            to=2.0,
            variable=self.overlay_scale_slider_var,
            command=on_overlay_slider_change,
            height=18,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            fg_color=COLORS["bg_input"],
        )
        self.overlay_scale_slider.grid(row=0, column=1, sticky="ew", padx=(20, 10))
        
        if tooltip_text:
            ToolTip(self.overlay_scale_slider, tooltip_text)
    
    def _apply_overlay_scale(self, new_scale):
        """Apply overlay scale change - sends command to overlay process."""
        current = self.config.get("overlay_scale", 1.0)
        if abs(new_scale - current) < 0.01:
            return  # No significant change
        
        self.config["overlay_scale"] = new_scale
        save_config(self.config)
        
        # Send scale command to overlay if running
        if self.overlay_controller and self._use_pyqt_overlay:
            self.overlay_controller.set_scale(new_scale)
        
        self.log(f"⚙ Overlay Scale: {int(new_scale * 100)}%")

    def _create_overlay_position_slider_row(self, parent):
        """Create overlay vertical position slider."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=8)

        row.grid_columnconfigure(0, weight=0)
        row.grid_columnconfigure(1, weight=1)
        row.grid_columnconfigure(2, weight=0)

        label_widget = ctk.CTkLabel(
            row,
            text="Overlay Position",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["text_secondary"],
        )
        label_widget.grid(row=0, column=0, sticky="w")

        tooltip_text = SETTING_TOOLTIPS.get("overlay_position", "")
        if tooltip_text:
            ToolTip(label_widget, tooltip_text)

        current_offset = self.config.get("overlay_vertical_offset", 0)

        # Value display
        self.overlay_position_value_label = ctk.CTkLabel(
            row,
            text=f"{current_offset}px",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["accent"],
            width=60,
        )
        self.overlay_position_value_label.grid(row=0, column=2, sticky="e", padx=(10, 0))

        self.overlay_position_slider_var = ctk.DoubleVar(value=current_offset)
        self._overlay_position_apply_timer = None

        def on_position_slider_change(value):
            value = int(round(float(value)))
            self.overlay_position_value_label.configure(text=f"{value}px")

            if self._overlay_position_apply_timer is not None:
                try:
                    self.after_cancel(self._overlay_position_apply_timer)
                except:
                    pass

            self._overlay_position_apply_timer = self.after(
                150, lambda: self._apply_overlay_position(value)
            )

        self.overlay_position_slider = ctk.CTkSlider(
            row,
            from_=-200,
            to=200,
            variable=self.overlay_position_slider_var,
            command=on_position_slider_change,
            height=18,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            fg_color=COLORS["bg_input"],
        )
        self.overlay_position_slider.grid(row=0, column=1, sticky="ew", padx=(20, 10))

        if tooltip_text:
            ToolTip(self.overlay_position_slider, tooltip_text)

    def _apply_overlay_position(self, new_offset):
        """Apply overlay vertical position change."""
        new_offset = int(new_offset)
        current = self.config.get("overlay_vertical_offset", 0)
        if new_offset == current:
            return

        self.config["overlay_vertical_offset"] = new_offset
        save_config(self.config)

        if self.overlay_controller and self._use_pyqt_overlay:
            self.overlay_controller.set_vertical_offset(new_offset)

        self.log(f"⚙ Overlay Position: {new_offset}px")

    def create_toggle_row(self, parent, label, variable, command, tooltip=None, tooltip_key=None):
        """Create a premium toggle row with improved typography.
        
        Args:
            tooltip_key: If provided, stores tooltips for dynamic updates (e.g., after benchmarks)
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=10)  # Increased padding
        
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=0)
        
        # Label container
        label_container = ctk.CTkFrame(row, fg_color="transparent")
        label_container.grid(row=0, column=0, sticky="w")
        
        # Label text - MEDIUM weight
        label_widget = ctk.CTkLabel(
            label_container,
            text=label,
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        )
        label_widget.pack(side="left")
        
        if tooltip:
            info_icon = ctk.CTkLabel(
                label_container,
                text="ⓘ",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            )
            info_icon.pack(side="left", padx=(8, 0))
            tt1 = ToolTip(label_widget, tooltip)
            tt2 = ToolTip(info_icon, tooltip)
            # Store for dynamic updates if key provided
            if tooltip_key:
                if tooltip_key not in self.dynamic_tooltips:
                    self.dynamic_tooltips[tooltip_key] = []
                self.dynamic_tooltips[tooltip_key].extend([tt1, tt2])
        
        # Premium toggle - uses bright accent for "on" state
        switch = ctk.CTkSwitch(
            row,
            text="",
            variable=variable,
            command=command,
            progress_color=COLORS["accent_bright"] if "accent_bright" in COLORS else COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            fg_color=COLORS["bg_input"],
            switch_width=44,
            switch_height=24,
            corner_radius=12,
        )
        switch.grid(row=0, column=1, sticky="e", padx=(16, 0))

    def create_dropdown_row(self, parent, label, values, variable, command, tooltip=None, width=140, tooltip_key=None):
        """Create a premium dropdown row with improved typography.
        
        Args:
            tooltip_key: If provided, stores tooltips for dynamic updates (e.g., after benchmarks)
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=10)  # Increased padding
        
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=0)
        
        # Label container
        label_container = ctk.CTkFrame(row, fg_color="transparent")
        label_container.grid(row=0, column=0, sticky="w")
        
        # Label text - MEDIUM weight
        label_widget = ctk.CTkLabel(
            label_container,
            text=label,
            font=(self.font_body[0], self.font_sizes["body"], "bold"),
            text_color=COLORS["text_primary"],
        )
        label_widget.pack(side="left")
        
        if tooltip:
            info_icon = ctk.CTkLabel(
                label_container,
                text="ⓘ",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            )
            info_icon.pack(side="left", padx=(8, 0))
            tt1 = ToolTip(label_widget, tooltip)
            tt2 = ToolTip(info_icon, tooltip)
            # Store for dynamic updates if key provided
            if tooltip_key:
                if tooltip_key not in self.dynamic_tooltips:
                    self.dynamic_tooltips[tooltip_key] = []
                self.dynamic_tooltips[tooltip_key].extend([tt1, tt2])
        
        # Premium dropdown with thin border
        dropdown = ctk.CTkOptionMenu(
            row,
            values=values,
            variable=variable,
            command=command,
            fg_color=COLORS["bg_input"],
            button_color=COLORS["bg_hover"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_surface"],
            dropdown_hover_color=COLORS["bg_hover"],
            dropdown_text_color=COLORS["text_primary"],
            text_color=COLORS["accent"],
            font=(self.font_mono[0], self.font_sizes["small"]),  # Mono for values
            dropdown_font=(self.font_body[0], self.font_sizes["body"]),
            width=max(width, 120),
            height=36,
            corner_radius=RADIUS["sm"],
        )
        dropdown.grid(row=0, column=1, sticky="e", padx=(16, 0))
        return dropdown

    def toggle_start_minimized(self):
        self.config["start_minimized"] = self.start_min_var.get()
        save_config(self.config)
    
    def on_overlay_type_changed(self, value: str):
        """Handle overlay type change - requires restart for clean switch."""
        old_value = self.config.get("overlay_type", "always_on")
        self.config["overlay_type"] = value
        save_config(self.config)
        
        type_names = {
            "always_on": "Always On (PyQt6)",
            "disappearing": "Disappearing (CTk)",
        }
        self.log(f"⚙ Status indicator: {type_names.get(value, value)}")
        
        if value == old_value:
            return  # No change
        
        # Kill any running overlay processes
        import subprocess as sp
        try:
            sp.run(["pkill", "-9", "-f", "overlay.py"], 
                   capture_output=True, timeout=2)
        except:
            pass
        
        self.log("  ℹ️ Restart the app to apply the change")
        
        # Show restart dialog
        try:
            self._show_restart_dialog()
        except Exception as e:
            self.log(f"  ⚠ Dialog error: {e}")
    
    def _show_restart_dialog(self):
        """Show a dialog prompting user to restart the app."""
        self.log("  📋 Opening restart dialog...")
        dialog = ctk.CTkToplevel(self)
        dialog.title("Restart Required")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.focus_force()
        dialog.lift()
        dialog.attributes("-topmost", True)
        dialog.after(200, lambda: dialog.attributes("-topmost", False))
        self._setup_dialog(dialog, 460, 220)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Restart Required",
            font=(self.font_header[0], 18, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(pady=(0, 12))
        
        ctk.CTkLabel(
            inner,
            text="The status indicator change requires a restart to take effect.",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_secondary"],
            wraplength=340,
        ).pack(pady=(0, 20))
        
        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x")
        
        ctk.CTkButton(
            btn_frame,
            text="Later",
            font=(self.font_body[0], 13),
            fg_color=COLORS["bg_surface"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_secondary"],
            height=36,
            width=100,
            corner_radius=8,
            command=dialog.destroy,
        ).pack(side="left", expand=True)
        
        def restart_now():
            dialog.destroy()
            # Kill overlay and restart
            import subprocess as sp
            import sys
            try:
                sp.run(["pkill", "-9", "-f", "overlay.py"], capture_output=True, timeout=1)
            except:
                pass
            # Restart the app
            import os
            os.execv(sys.executable, [sys.executable] + sys.argv)
        
        ctk.CTkButton(
            btn_frame,
            text="Restart Now",
            font=(self.font_body[0], 13, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_dim"],
            text_color=COLORS["bg_base"],
            height=36,
            width=120,
            corner_radius=8,
            command=restart_now,
        ).pack(side="right", expand=True)

    def clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        
        # Use event queue for thread-safe logging (avoids Tk threading crash)
        # The actual UI update happens in _do_log() called from handle_event()
        try:
            self.event_queue.put((EventType.LOG_MESSAGE, log_line))
        except:
            pass
    
    def _do_log(self, log_line: str):
        """Actually update the log textbox (must be called from main thread)."""
        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", log_line)
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        except:
            pass

    def _show_premium_prompt(self, feature_id: str) -> None:
        """Show an inline premium upgrade banner within the main window."""
        import webbrowser

        # Remove any existing premium banner first
        if hasattr(self, '_premium_banner') and self._premium_banner is not None:
            try:
                self._premium_banner.destroy()
            except Exception:
                pass

        msg = self.feature_gate.get_upgrade_message(feature_id)

        # Create inline banner at top of window
        banner = ctk.CTkFrame(
            self, fg_color=COLORS["bg_elevated"],
            corner_radius=RADIUS["lg"], border_width=1,
            border_color=COLORS["accent_dim"],
        )
        banner.place(relx=0.5, rely=0.0, anchor="n", relwidth=0.9, y=10)
        self._premium_banner = banner

        inner = ctk.CTkFrame(banner, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=12)

        ctk.CTkLabel(
            inner, text="✨ Premium Feature",
            font=(self.font_body[0], 14, "bold"),
            text_color=COLORS["accent"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            inner, text=msg, font=(self.font_body[0], 12),
            text_color=COLORS["text_primary"], wraplength=380, justify="left",
        ).pack(fill="x", pady=(4, 12))

        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(fill="x")

        def _dismiss():
            banner.place_forget()
            banner.destroy()
            self._premium_banner = None

        ctk.CTkButton(
            btn_row, text="Get Premium — $20", font=(self.font_body[0], 13, "bold"),
            fg_color=COLORS["accent"], hover_color=COLORS["accent_dim"],
            text_color="#FFFFFF", height=34, corner_radius=8,
            command=lambda: [webbrowser.open(self.config.get("premium_url", "https://wayfinder.dev/premium")), _dismiss()],
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="Dismiss", font=(self.font_body[0], 12),
            fg_color=COLORS["bg_hover"], hover_color=COLORS["bg_card"],
            text_color=COLORS["text_muted"], height=34, corner_radius=8,
            command=_dismiss,
        ).pack(side="left")

        # Auto-dismiss after 8 seconds
        banner.after(8000, lambda: _dismiss() if self._premium_banner is banner else None)

    def _activate_license(self) -> None:
        """Activate a license key from the Settings UI."""
        key = self._license_key_entry.get().strip().upper()
        if not key:
            self._license_feedback.configure(text="Please enter a license key", text_color="#CF7B7B")
            return

        from wayfinder.license import validate_license_key, store_license, get_feature_gate
        result = validate_license_key(key)
        if result.is_valid:
            store_license(key)
            # Refresh the feature gate singleton
            self.feature_gate = get_feature_gate(force_refresh=True)
            self._license_status_label.configure(text="Premium", text_color=COLORS["accent"])
            self._license_feedback.configure(text="License activated! Restart for full effect.", text_color=COLORS["accent"])
            self.log("License activated — premium features unlocked")
        else:
            self._license_feedback.configure(text=result.error_message or "Invalid license key", text_color="#CF7B7B")

    def _deactivate_license(self) -> None:
        """Deactivate the current license."""
        from wayfinder.license import remove_license, get_feature_gate
        remove_license()
        self.feature_gate = get_feature_gate(force_refresh=True)
        self._license_status_label.configure(text="Free", text_color=COLORS["text_muted"])
        self._license_feedback.configure(text="License deactivated", text_color=COLORS["text_muted"])
        self._license_key_entry.delete(0, "end")
        self.log("License deactivated")

    def get_hotkey_display(self) -> str:
        hotkey_key = self.config.get("hotkey_key", 67)
        hotkey_modifiers = self.config.get("hotkey_modifiers", [])
        
        # Map codes to display names
        code_to_name = {
            59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
            65: "F7", 66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
            70: "ScrollLock", 119: "Pause", 57: "Space",
        }
        key_name = code_to_name.get(hotkey_key, f"Key{hotkey_key}")
        
        if hotkey_modifiers:
            mods = "+".join(m.capitalize() for m in hotkey_modifiers)
            return f"{mods}+{key_name}"
        return key_name
    
    def get_style_hotkey_display(self) -> str:
        """Get the display string for the style toggle hotkey."""
        style_key = self.config.get("style_toggle_key", 68)
        style_modifiers = self.config.get("style_toggle_modifiers", [])
        
        # Map codes to display names
        code_to_name = {
            59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
            65: "F7", 66: "F8", 67: "F9", 68: "F10", 87: "F11", 88: "F12",
            70: "ScrollLock", 119: "Pause", 57: "Space",
        }
        key_name = code_to_name.get(style_key, f"Key{style_key}")
        
        if style_modifiers:
            mods = "+".join(m.capitalize() for m in style_modifiers)
            return f"{mods}+{key_name}"
        return key_name

    def _apply_hotkey_change(self):
        """Apply hotkey config change and update the listener."""
        new_hotkey = self.get_hotkey_display()
        if hasattr(self, 'hotkey_label'):
            self.hotkey_label.configure(text=f"Press {new_hotkey} to toggle")
        self.log(f"⚙ Hotkey: {new_hotkey}")
        if sys.platform == "darwin":
            # macOS: pynput reads config live — nothing to restart.
            self.log("⚙ Hotkey updated (live)")
        else:
            self.restart_evdev_listener("config change")

    def _on_hotkey_key_changed(self, value):
        """Handle inline hotkey key dropdown change."""
        new_code = self._hotkey_key_codes.get(value, 67)
        self.config["hotkey_key"] = new_code
        save_config(self.config)
        self._apply_hotkey_change()

    def _on_hotkey_mod_changed(self):
        """Handle inline hotkey modifier checkbox change."""
        new_mods = [m for m, v in self._hotkey_mod_vars.items() if v.get()]
        self.config["hotkey_modifiers"] = new_mods
        save_config(self.config)
        self._apply_hotkey_change()

    def _apply_style_hotkey_change(self):
        """Apply style hotkey config change and update the listener."""
        new_hotkey = self.get_style_hotkey_display()
        self.log(f"⚙ Style toggle hotkey: {new_hotkey}")
        if sys.platform == "darwin":
            # macOS: pynput reads config live — nothing to restart.
            self.log("⚙ Hotkey updated (live)")
        else:
            self.restart_evdev_listener("config change")

    def _on_style_hotkey_key_changed(self, value):
        """Handle inline style hotkey key dropdown change."""
        new_code = self._hotkey_key_codes.get(value, 68)
        self.config["style_toggle_key"] = new_code
        save_config(self.config)
        self._apply_style_hotkey_change()

    def _on_style_hotkey_mod_changed(self):
        """Handle inline style hotkey modifier checkbox change."""
        new_mods = [m for m, v in self._style_hotkey_mod_vars.items() if v.get()]
        self.config["style_toggle_modifiers"] = new_mods
        save_config(self.config)
        self._apply_style_hotkey_change()

    def _on_device_selected(self, value):
        """Handle inline device dropdown change."""
        if value == "All Devices":
            self.config["enabled_input_devices"] = []
        else:
            self.config["enabled_input_devices"] = [value]
        save_config(self.config)
        self.log(f"⚙ Input device: {value}")
        self.restart_evdev_listener("config change")

    def get_microphone_display(self) -> str:
        """Get display name for current audio input device."""
        device_id = self.config.get("audio_device")
        
        # Auto-detect mode
        if device_id is None:
            # Show what was actually selected
            if hasattr(self, '_resolved_audio_device') and self._resolved_audio_device is not None:
                try:
                    import sounddevice as sd
                    dev_info = sd.query_devices(self._resolved_audio_device)
                    name = dev_info.get('name', '')
                    # Extract short name (e.g., "Shure MV7" from "Shure MV7 Mono")
                    short_name = name.split(':')[0].strip() if ':' in name else name.split('(')[0].strip()
                    if len(short_name) > 18:
                        short_name = short_name[:15] + "..."
                    return f"{short_name} (Auto)"
                except Exception:
                    pass
            return "Auto-detect  ▼"
        
        # Manual selection - find the device name
        try:
            devices = get_audio_input_devices()
            for device in devices:
                if device["index"] == device_id:
                    # Truncate long names
                    name = device["name"]
                    short_name = name.split(':')[0].strip() if ':' in name else name.split('(')[0].strip()
                    if len(short_name) > 20:
                        return short_name[:17] + "...  ▼"
                    return f"{short_name}  ▼"
        except Exception:
            pass
        
        return f"Device {device_id}  ▼"

    def _get_microphone_dropdown_options(self) -> tuple[list[str], str]:
        """
        Get list of microphone options for dropdown and current selection.
        Only shows actual microphone inputs, filtering out outputs and virtual devices.
        
        Returns:
            Tuple of (list of display names, current selection name)
        """
        # Build options list with auto-detect first
        options = ["🎤 Auto-detect (Recommended)"]
        device_map = {}  # Map display name -> device index
        
        try:
            all_devices = list_input_devices(exclude_outputs=True)
            
            for dev in all_devices:
                name = dev.get('name', f"Device {dev['index']}")
                
                # Create display name - truncate long names
                short_name = name.split(':')[0].strip() if ':' in name else name.split('(')[0].strip()
                if len(short_name) > 35:
                    short_name = short_name[:32] + "..."
                
                # Add star badge for recommended mics
                if dev.get('recommended'):
                    display_name = f"{short_name} (★)"
                else:
                    display_name = short_name
                
                # Ensure unique display names
                if display_name in device_map:
                    display_name = f"{display_name} [{dev['index']}]"
                
                options.append(display_name)
                device_map[display_name] = dev['index']
        except Exception as e:
            print(f"Error getting audio devices for dropdown: {e}")
        
        # Store the device map for selection handling
        self._mic_device_map = device_map
        
        # Also build a reverse map of device name -> display name for name-based lookup
        self._mic_name_to_display = {}
        try:
            for dev in list_input_devices(exclude_outputs=True):
                dev_name = dev.get('name', '')
                for display_name, idx in device_map.items():
                    if idx == dev['index']:
                        self._mic_name_to_display[dev_name.lower()] = display_name
                        break
        except Exception:
            pass
        
        # Find current selection
        # First check if we have a saved device name (preferred, since indices can change)
        device_name = self.config.get("audio_device_name")
        current_device = self.config.get("audio_device")
        
        if current_device is None and device_name is None:
            current = options[0]  # Auto-detect
        else:
            current = options[0]  # Default to auto-detect if not found
            
            # Try to find by saved device name first (handles index changes across restarts)
            if device_name:
                device_name_lower = device_name.lower()
                # Exact match first
                if device_name_lower in self._mic_name_to_display:
                    current = self._mic_name_to_display[device_name_lower]
                else:
                    # Partial match - find display name whose device name contains/is contained in saved name
                    for saved_name, display_name in self._mic_name_to_display.items():
                        if device_name_lower in saved_name or saved_name in device_name_lower:
                            current = display_name
                            break
            
            # Fall back to index-based lookup if name-based failed
            if current == options[0] and current_device is not None:
                for display_name, idx in device_map.items():
                    if idx == current_device:
                        current = display_name
                        break
        
        return options, current

    def _on_microphone_selected(self, selection: str):
        """Handle microphone dropdown selection."""
        if "Auto-detect" in selection:
            # Auto-detect mode
            self.config["audio_device"] = None
            self.config["audio_device_name"] = None
            device_display = "Auto-detect"
        else:
            # Find the device index from the map
            device_idx = self._mic_device_map.get(selection)
            if device_idx is not None:
                self.config["audio_device"] = device_idx
                # Also save the device name for reconnection
                try:
                    from wayfinder.core.recorder import list_input_devices
                    for dev in list_input_devices():
                        if dev['index'] == device_idx:
                            self.config["audio_device_name"] = dev['name']
                            break
                except Exception:
                    pass
                device_display = selection
            else:
                self.log(f"⚠ Could not find device: {selection}")
                return
        
        # Save config
        save_config(self.config)
        
        # Update recorder with new device
        self.update_audio_device()
        
        # Log the change
        self.log(f"🎤 Microphone: {device_display}")

    def _on_audio_processing_selected(self, selection: str):
        """Handle audio processing dropdown selection."""
        level = selection.lower()
        self.config["audio_preprocessing"] = level
        save_config(self.config)
        
        # Update recorder if it exists
        if hasattr(self, 'recorder') and self.recorder:
            self.recorder.preprocessing = level
        
        self.log(f"⚙ Audio processing: {level}")

    def _build_audio_calibration_section(self, parent):
        """Build simple mic test section - record and playback with level meter."""
        # Container frame
        self._mic_test_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._mic_test_frame.pack(fill="x", pady=(8, 0))
        
        # Header row
        header_row = ctk.CTkFrame(self._mic_test_frame, fg_color="transparent")
        header_row.pack(fill="x")
        
        ctk.CTkLabel(
            header_row,
            text="Mic Test",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_primary"],
        ).pack(side="left", padx=(8, 0))
        
        # Buttons on the right
        btn_frame = ctk.CTkFrame(header_row, fg_color="transparent")
        btn_frame.pack(side="right", padx=(0, 8))
        
        self._mic_play_btn = ctk.CTkButton(
            btn_frame,
            text="▶ Play",
            font=(self.font_body[0], 12),
            fg_color=COLORS["bg_elevated"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_bright"],
            corner_radius=RADIUS["sm"],
            height=32,
            width=70,
            command=self._toggle_playback,
            state="disabled",
        )
        self._mic_play_btn.pack(side="left", padx=(0, 4))
        
        self._mic_test_btn = ctk.CTkButton(
            btn_frame,
            text="● Record",
            font=(self.font_body[0], 12),
            fg_color=COLORS["accent_red"],
            hover_color="#c55",
            text_color=COLORS["text_bright"],
            corner_radius=RADIUS["sm"],
            height=32,
            width=80,
            command=self._toggle_mic_test,
        )
        self._mic_test_btn.pack(side="left")
        
        # Level meter (shown during recording)
        self._mic_meter_frame = ctk.CTkFrame(self._mic_test_frame, fg_color="transparent")
        # Don't pack yet - shown during recording
        
        ctk.CTkLabel(
            self._mic_meter_frame,
            text="Level:",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
            width=40,
        ).pack(side="left", padx=(8, 0))
        
        self._mic_meter_bg = ctk.CTkFrame(
            self._mic_meter_frame,
            fg_color=COLORS["bg_input"],
            corner_radius=4,
            height=14,
        )
        self._mic_meter_bg.pack(side="left", fill="x", expand=True, padx=(4, 8))
        
        self._mic_meter_bar = ctk.CTkFrame(
            self._mic_meter_bg,
            fg_color=COLORS["accent_green"],
            corner_radius=3,
            width=0,
            height=10,
        )
        self._mic_meter_bar.place(x=2, y=2)
        
        # Status label (shows recording time or "Ready")
        self._mic_test_status = ctk.CTkLabel(
            self._mic_test_frame,
            text="Click Record to test your mic",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        )
        self._mic_test_status.pack(pady=(4, 0), padx=8, anchor="w")
        
        # State
        self._mic_test_recording = False
        self._mic_test_playing = False
        self._mic_test_recorder = None
        self._mic_test_audio_path = None
        self._mic_test_update_job = None
    
    def _build_audio_ducking_section(self, parent):
        """Build audio ducking controls - toggle and percentage slider."""
        # Separator line
        separator = ctk.CTkFrame(parent, fg_color=COLORS["border_subtle"], height=1)
        separator.pack(fill="x", padx=16, pady=(16, 8))
        
        # Section label
        ctk.CTkLabel(
            parent,
            text="Audio Ducking",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", padx=(24, 0), pady=(0, 4))
        
        ctk.CTkLabel(
            parent,
            text="Automatically lower other audio while recording",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=(24, 0), pady=(0, 8))
        
        # Enable toggle
        ducking_enabled = self.config.get("audio_ducking_enabled", True)
        self.audio_ducking_var = ctk.BooleanVar(value=ducking_enabled)
        self.create_toggle_row(
            parent, "Enable Audio Ducking",
            self.audio_ducking_var, self._on_audio_ducking_toggled,
            tooltip="Automatically lower music and other audio while recording",
        )
        
        # Duck percentage slider
        self._create_audio_ducking_slider_row(parent)
    
    def _create_audio_ducking_slider_row(self, parent):
        """Create inline audio ducking percentage slider."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=8)
        
        row.grid_columnconfigure(0, weight=0)  # Label column
        row.grid_columnconfigure(1, weight=1)  # Slider column - grows
        row.grid_columnconfigure(2, weight=0)  # Value display column
        
        # Label
        label_widget = ctk.CTkLabel(
            row,
            text="Duck Amount",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["text_secondary"],
        )
        label_widget.grid(row=0, column=0, sticky="w")
        
        tooltip_text = "How much to lower other audio. Higher = quieter music while recording."
        ToolTip(label_widget, tooltip_text)
        
        # Get current duck percentage
        duck_percent = self.config.get("audio_ducking_percent", 30)
        
        # Value display (right side, shows percentage)
        self.duck_percent_value_label = ctk.CTkLabel(
            row,
            text=f"{duck_percent}%",
            font=(self.font_body[0], self.font_sizes["body"]),
            text_color=COLORS["accent"],
            width=60,
        )
        self.duck_percent_value_label.grid(row=0, column=2, sticky="e", padx=(10, 0))
        
        # Slider in the middle
        self.duck_percent_slider_var = ctk.DoubleVar(value=duck_percent)
        
        # Debounce timer for applying changes
        self._duck_percent_apply_timer = None
        
        def on_duck_slider_change(value):
            """Update display and schedule duck percent application."""
            value = int(float(value))
            # Snap to nearest 5%
            snapped = round(value / 5) * 5
            self.duck_percent_value_label.configure(text=f"{snapped}%")
            
            # Cancel any pending apply
            if self._duck_percent_apply_timer is not None:
                try:
                    self.after_cancel(self._duck_percent_apply_timer)
                except:
                    pass
            
            # Schedule apply after a short delay (debounce)
            self._duck_percent_apply_timer = self.after(150, lambda: self._apply_duck_percent(snapped))
        
        self.duck_percent_slider = ctk.CTkSlider(
            row,
            from_=0,
            to=50,
            variable=self.duck_percent_slider_var,
            command=on_duck_slider_change,
            height=18,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            fg_color=COLORS["bg_input"],
        )
        self.duck_percent_slider.grid(row=0, column=1, sticky="ew", padx=(20, 10))
        
        ToolTip(self.duck_percent_slider, tooltip_text)
        
        # Update slider enabled state based on toggle
        self._update_ducking_slider_state()
    
    def _on_audio_ducking_toggled(self):
        """Handle audio ducking toggle change."""
        enabled = self.audio_ducking_var.get()
        self.config["audio_ducking_enabled"] = enabled
        save_config(self.config)
        
        # Update slider enabled state
        self._update_ducking_slider_state()
        
        status = "enabled" if enabled else "disabled"
        self.log(f"🔉 Audio ducking {status}")
    
    def _update_ducking_slider_state(self):
        """Enable or disable the ducking slider based on toggle state."""
        if hasattr(self, 'duck_percent_slider') and hasattr(self, 'audio_ducking_var'):
            enabled = self.audio_ducking_var.get()
            if enabled:
                self.duck_percent_slider.configure(state="normal")
                self.duck_percent_value_label.configure(text_color=COLORS["accent"])
            else:
                self.duck_percent_slider.configure(state="disabled")
                self.duck_percent_value_label.configure(text_color=COLORS["text_muted"])
    
    def _apply_duck_percent(self, new_percent):
        """Apply duck percentage change."""
        current = self.config.get("audio_ducking_percent", 30)
        if new_percent == current:
            return  # No change
        
        self.config["audio_ducking_percent"] = new_percent
        save_config(self.config)
        
        # Update the audio ducker instance
        if hasattr(self, 'audio_ducker'):
            self.audio_ducker.set_duck_percent(new_percent)
        
        self.log(f"🔉 Duck amount: {new_percent}%")
    
    def _toggle_mic_test(self):
        """Start or stop mic test recording."""
        if self._mic_test_recording:
            self._stop_mic_test()
        else:
            self._start_mic_test()
    
    def _start_mic_test(self):
        """Start recording for mic test."""
        # Get device and preprocessing settings
        device_id = self.config.get("audio_device")
        if device_id is None and hasattr(self, '_resolved_audio_device'):
            device_id = self._resolved_audio_device
        preprocessing = self.config.get("audio_preprocessing", "light")
        
        try:
            # Use the same AudioRecorder as main recording
            self._mic_test_recorder = AudioRecorder(
                sample_rate=16000,
                channels=1,
                device=device_id,
                preprocessing=preprocessing,
            )
            self._mic_test_recorder.start()
            self._mic_test_recording = True
            self._mic_test_start_time = time.time()
            
            # Update UI - show meter
            self._mic_meter_frame.pack(fill="x", pady=(6, 0))
            self._mic_meter_bar.configure(width=0)
            self._mic_meter_bar.place(x=2, y=2)
            self._mic_test_btn.configure(text="■ Stop", fg_color=COLORS["accent"])
            self._mic_play_btn.configure(state="disabled")
            self._mic_test_status.configure(
                text="🎤 Recording...",
                text_color=COLORS["accent"],
            )
            
            # Start update loop
            self._update_mic_test()
            
        except Exception as e:
            self._mic_test_status.configure(
                text=f"❌ Error: {str(e)[:50]}",
                text_color=COLORS["accent_red"],
            )
    
    def _update_mic_test(self):
        """Update mic test UI during recording - including level meter."""
        if not self._mic_test_recording or not self._mic_test_recorder:
            return
        
        elapsed = time.time() - self._mic_test_start_time
        
        # Get audio level and update meter
        level = self._mic_test_recorder.get_audio_level()
        
        # Calculate meter width
        self._mic_meter_bg.update_idletasks()
        bg_width = self._mic_meter_bg.winfo_width()
        if bg_width < 10:
            bg_width = 200
        meter_width = max(2, int(level * (bg_width - 4)))
        
        # Color based on level
        if level > 0.9:
            color = COLORS["accent_red"]  # Clipping
        elif level > 0.6:
            color = COLORS["accent_yellow"]  # Hot
        else:
            color = COLORS["accent_green"]  # Good
        
        self._mic_meter_bar.configure(fg_color=color, width=meter_width)
        self._mic_meter_bar.place(x=2, y=2)
        
        # Update status with time
        self._mic_test_status.configure(
            text=f"🎤 Recording... {elapsed:.1f}s",
            text_color=COLORS["accent"],
        )
        
        # Auto-stop after 10 seconds
        if elapsed >= 10.0:
            self._stop_mic_test()
        else:
            self._mic_test_update_job = self.after(50, self._update_mic_test)
    
    def _stop_mic_test(self):
        """Stop mic test recording."""
        self._mic_test_recording = False
        
        if self._mic_test_update_job:
            self.after_cancel(self._mic_test_update_job)
            self._mic_test_update_job = None
        
        # Hide meter
        self._mic_meter_frame.pack_forget()
        
        if self._mic_test_recorder:
            try:
                # Save the recording
                self._mic_test_audio_path = self._mic_test_recorder.stop()
                duration = self._mic_test_recorder.get_duration()
                
                self._mic_test_status.configure(
                    text=f"✓ Recorded {duration:.1f}s - click Play to listen",
                    text_color=COLORS["accent_green"],
                )
                self._mic_play_btn.configure(state="normal", text="▶ Play")
                
            except Exception as e:
                self._mic_test_status.configure(
                    text=f"❌ Error: {str(e)[:50]}",
                    text_color=COLORS["accent_red"],
                )
            
            self._mic_test_recorder = None
        
        # Reset button
        self._mic_test_btn.configure(text="● Record", fg_color=COLORS["accent_red"])
    
    def _toggle_playback(self):
        """Toggle playback - play or stop."""
        if self._mic_test_playing:
            self._stop_playback()
        else:
            self._play_mic_test()
    
    def _play_mic_test(self):
        """Play back the recorded mic test."""
        if not self._mic_test_audio_path:
            return
        
        import threading
        
        self._mic_test_playing = True
        self._mic_play_btn.configure(text="■ Stop", fg_color=COLORS["accent"])
        self._mic_test_btn.configure(state="disabled")
        self._mic_test_status.configure(
            text="🔊 Playing...",
            text_color=COLORS["accent"],
        )
        
        def play_audio():
            try:
                import sounddevice as sd
                import wave
                import numpy as np
                
                with wave.open(self._mic_test_audio_path, 'rb') as wf:
                    sample_rate = wf.getframerate()
                    frames = wf.readframes(wf.getnframes())
                    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
                
                # Play audio (can be stopped with sd.stop())
                sd.play(audio, sample_rate)
                sd.wait()
                
                # Done (only update if we weren't stopped)
                if self._mic_test_playing:
                    self.after(0, self._on_playback_done)
                
            except Exception as e:
                self.after(0, lambda: self._on_playback_error(str(e)))
        
        thread = threading.Thread(target=play_audio, daemon=True)
        thread.start()
    
    def _stop_playback(self):
        """Stop audio playback."""
        import sounddevice as sd
        self._mic_test_playing = False
        sd.stop()
        self._on_playback_done()
    
    def _on_playback_done(self):
        """Called when playback finishes or is stopped."""
        self._mic_test_playing = False
        self._mic_play_btn.configure(text="▶ Play", fg_color=COLORS["bg_elevated"], state="normal")
        self._mic_test_btn.configure(state="normal")
        self._mic_test_status.configure(
            text="✓ Ready - Record again or adjust settings",
            text_color=COLORS["accent_green"],
        )
    
    def _on_playback_error(self, error_msg: str):
        """Called when playback fails."""
        self._mic_test_playing = False
        self._mic_play_btn.configure(text="▶ Play", fg_color=COLORS["bg_elevated"], state="normal")
        self._mic_test_btn.configure(state="normal")
        self._mic_test_status.configure(
            text=f"❌ Playback error: {error_msg[:40]}",
            text_color=COLORS["accent_red"],
        )

    # Legacy calibration methods removed - using simple mic test instead
    def _toggle_calibration(self):
        """Legacy method - redirects to mic test."""
        self._toggle_mic_test()
    
    # Old calibration methods removed - using simple mic test now
    def _update_calibration(self):
        """Legacy - no longer used."""
        pass
    
    def _stop_calibration_test(self):
        """Legacy - no longer used."""
        pass
    
    def _show_calibration_results(self):
        """Legacy - no longer used."""
        pass
    
    def _apply_calibration(self):
        """Legacy - no longer used."""
        pass

    def get_model_display(self) -> str:
        """Get display name for current model."""
        model_path = self.config.get("model_path", "")
        # Extract model name from path - check specific models first (most specific to least)
        # Turbo Q5 quantized
        if "large-v3-turbo-q5" in model_path:
            return "Turbo Q5 (Fast)"
        # Turbo full precision
        elif "large-v3-turbo" in model_path:
            return "Turbo (Fast)"
        # Large v3 (non-turbo)
        elif "large-v3" in model_path:
            return "Large v3 (Slow)"
        # Legacy large
        elif "large" in model_path:
            return "Large (Slow)"
        # English-only models
        elif "tiny.en" in model_path:
            return "Tiny (Fastest)"
        elif "base.en" in model_path:
            return "Base"
        elif "small.en" in model_path:
            return "Small"
        elif "medium.en" in model_path:
            return "Medium"
        # Multi-language models
        elif "tiny" in model_path:
            return "Tiny (Multi)"
        elif "base" in model_path:
            return "Base (Multi)"
        elif "small" in model_path:
            return "Small (Multi)"
        elif "medium" in model_path:
            return "Medium (Multi)"
        return "Unknown"

    def get_prompt_display(self) -> str:
        """Get display name for current prompt."""
        prompt = self.config.get("prompt", "")
        # Check for preset prompts
        preset_prompts = self.get_preset_prompts()
        for name, text in preset_prompts.items():
            if prompt == text:
                return name
        # Custom prompt - show truncated version
        if len(prompt) > 20:
            return f"Custom: {prompt[:17]}..."
        return f"Custom: {prompt}" if prompt else "None"

    def get_preset_prompts(self) -> dict:
        """Return preset prompt options.
        
        Note: Prompts work best as comma-separated vocabulary lists, not sentences.
        Whisper treats the prompt as "previous transcript context" so listing
        terms you'll say helps the model recognize them.
        """
        return {
            # General purpose
            "General Dictation": "Hello, this is a dictation with proper punctuation and grammar.",
            "Conversational": "Okay, so, you know, like, actually, basically, I mean, right, anyway.",
            
            # Casual/relaxed style
            "Casual": "yeah, nah, kinda, gonna, wanna, gotta, lemme, dunno, cool, awesome, honestly, literally, basically, like, right, haha, lol...",
            
            # Technical prompts - vocabulary style
            "Technical/Code": "Python, TypeScript, JavaScript, React, API, JSON, async, await, function, const, import, export, npm, git, commit, merge, PR.",
            "Technical/AI-ML": "whisper.cpp, ggml, LLM, GPT, transformer, model, inference, CUDA, tensor, embedding, tokenizer, fine-tuning, quantization.",
            
            # Work/Business prompts
            "Work Mode": "Revenue management, commission tracker, sales pipeline, quarterly review, stakeholders, deliverables, KPIs, ROI, metrics, forecast.",
            "Business Email": "Dear, regards, please find attached, following up, as discussed, let me know, happy to help, best regards, sincerely.",
            
            # Specialized
            "Automotive": "Subaru WRX STI, Tesla Model 3 Performance, FSD, autopilot, horsepower, torque, AWD, turbo, EV, regenerative braking, range.",
            "Medical": "Diagnosis, prognosis, mg, mL, BID, TID, PRN, CBC, MRI, CT scan, hypertension, diabetes, prescription, contraindicated.",
            "Legal": "Plaintiff, defendant, hereby, whereas, pursuant to, jurisdiction, affidavit, deposition, statute, liability, damages, counsel.",
        }

    def get_available_models(self) -> list[dict]:
        """Scan for available whisper models."""
        models_dir = Path.home() / "whisper.cpp" / "models"
        models = []
        
        # Default model info with estimated latencies
        # These are overridden by benchmark results if available
        model_info = {
            "ggml-tiny.en.bin": ("Tiny (English)", "75MB", "⚡ TBD", "tiny.en"),
            "ggml-base.en.bin": ("Base (English)", "142MB", "⚡ TBD", "base.en"),
            "ggml-small.en.bin": ("Small (English)", "466MB", "🟡 TBD", "small.en"),
            "ggml-medium.en.bin": ("Medium (English)", "1.5GB", "🔴 TBD", "medium.en"),
            "ggml-large-v3-turbo.bin": ("Large v3 Turbo ⭐", "1.6GB", "🚀 TBD", "large-v3-turbo"),
            "ggml-large-v3-turbo-q5_0.bin": ("Large v3 Turbo Q5", "547MB", "🚀 TBD", "large-v3-turbo-q5"),
            "ggml-tiny.bin": ("Tiny (Multi-lang)", "75MB", "⚡ TBD", "tiny"),
            "ggml-base.bin": ("Base (Multi-lang)", "142MB", "⚡ TBD", "base"),
            "ggml-small.bin": ("Small (Multi-lang)", "466MB", "🟡 TBD", "small"),
            "ggml-medium.bin": ("Medium (Multi-lang)", "1.5GB", "🔴 TBD", "medium"),
            "ggml-large-v3.bin": ("Large v3", "3GB", "🔴 TBD", "large-v3"),
            "ggml-large.bin": ("Large (Multi-lang)", "3GB", "🐌 TBD", "large"),
        }
        
        # Get benchmark results for dynamic speed display
        benchmark_results = self.config.get("benchmark_results", {})
        fastest = self.config.get("benchmark_fastest_processor", None)
        
        for filename, (name, size, default_speed, model_id) in model_info.items():
            path = models_dir / filename
            if path.exists():
                # Try to get benchmarked speed
                speed = default_speed
                if model_id in benchmark_results:
                    result = benchmark_results[model_id]
                    if fastest == "gpu" and "gpu_10s" in result:
                        speed = f"🚀 GPU: {result['gpu_10s']}s"
                    elif "cpu_10s" in result:
                        speed = f"⚙️ CPU: {result['cpu_10s']}s"
                
                models.append({
                    "name": name,
                    "path": str(path),
                    "size": size,
                    "speed": speed,
                    "filename": filename,
                    "model_id": model_id,
                })
        
        return models

    def open_model_settings(self):
        """Show inline panel to select or download whisper models."""
        container = self.mode_settings_container

        def build_panel(content, close_panel):
            # Initialize model downloader
            downloader = ModelDownloader()

            # Subtitle
            ctk.CTkLabel(
                content,
                text="Select an installed model or download new ones.",
                font=(self.font_body[0], 10),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w", padx=8, pady=(0, 8))

            # Tab button container
            tab_container = ctk.CTkFrame(content, fg_color="transparent")
            tab_container.pack(fill="x", padx=8, pady=(0, 8))

            installed_btn = ctk.CTkButton(
                tab_container, text="Installed",
                font=(self.font_body[0], 12), height=30,
                corner_radius=6, fg_color=COLORS["accent"], text_color="#000000",
                hover_color=COLORS["accent_glow"],
            )
            installed_btn.pack(side="left", fill="x", expand=True, padx=(0, 3))

            download_btn = ctk.CTkButton(
                tab_container, text="Download",
                font=(self.font_body[0], 12), height=30,
                corner_radius=6, fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"],
                hover_color=COLORS["bg_elevated"],
            )
            download_btn.pack(side="left", fill="x", expand=True, padx=(3, 0))

            # Content area
            content_area = ctk.CTkFrame(content, fg_color=COLORS["bg_card"], corner_radius=12)
            content_area.pack(fill="both", expand=True, padx=8)

            current_path = os.path.expanduser(self.config.get("model_path", ""))
            model_var = ctk.StringVar(value=current_path)

            def clear_content():
                for widget in content_area.winfo_children():
                    widget.destroy()

            def show_installed():
                clear_content()
                installed_btn.configure(fg_color=COLORS["accent"], text_color="#000000")
                download_btn.configure(fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"])

                models = self.get_available_models()

                if not models:
                    ctk.CTkLabel(
                        content_area,
                        text="No models installed yet",
                        font=(self.font_body[0], 14, "bold"),
                        text_color=COLORS["text_primary"],
                    ).pack(pady=(40, 8))
                    ctk.CTkButton(
                        content_area, text="Download Models",
                        font=(self.font_body[0], 12, "bold"), height=36, corner_radius=8,
                        fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"],
                        text_color="#000000", command=show_download,
                    ).pack(pady=(0, 30))
                    return

                scroll = ctk.CTkScrollableFrame(content_area, fg_color="transparent")
                scroll.pack(fill="both", expand=True, padx=5, pady=5)
                self._trap_scroll(scroll)

                for model in models:
                    is_current = os.path.expanduser(model["path"]) == current_path

                    row = ctk.CTkFrame(scroll, fg_color=COLORS["bg_hover"] if is_current else "transparent", corner_radius=6)
                    row.pack(fill="x", pady=1, padx=2)

                    radio = ctk.CTkRadioButton(
                        row, text="", variable=model_var, value=model["path"],
                        width=18, fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"],
                    )
                    radio.pack(side="left", padx=(8, 4), pady=6)

                    info_frame = ctk.CTkFrame(row, fg_color="transparent")
                    info_frame.pack(side="left", fill="x", expand=True, pady=5)

                    ctk.CTkLabel(
                        info_frame, text=model["name"],
                        font=(self.font_body[0], 12, "bold" if is_current else "normal"),
                        text_color=COLORS["accent"] if is_current else COLORS["text_primary"],
                    ).pack(anchor="w")

                    ctk.CTkLabel(
                        info_frame, text=f"{model['speed']} • {model['size']}",
                        font=(self.font_body[0], 9), text_color=COLORS["text_muted"],
                    ).pack(anchor="w")

                def save_selection():
                    selected = model_var.get()
                    large_keywords = ("medium", "large", "turbo")
                    if any(kw in selected.lower() for kw in large_keywords) and not self.feature_gate.has_feature("large_models"):
                        self._show_premium_prompt("large_models")
                        return
                    if selected.startswith(str(Path.home())):
                        selected = "~" + selected[len(str(Path.home())):]
                    self.config["model_path"] = selected
                    save_config(self.config)
                    if hasattr(self, 'model_btn'):
                        self.model_btn.configure(text=self.get_model_display())
                    self.log(f"⚙ Model: {self.get_model_display()}")
                    close_panel()

                ctk.CTkButton(
                    content_area, text="Save & Apply",
                    font=(self.font_body[0], 13, "bold"), height=38, corner_radius=8,
                    fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"], text_color="#000000",
                    command=save_selection,
                ).pack(fill="x", padx=8, pady=8)

            def show_download():
                clear_content()
                installed_btn.configure(fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"])
                download_btn.configure(fg_color=COLORS["accent"], text_color="#000000")

                scroll = ctk.CTkScrollableFrame(content_area, fg_color="transparent")
                scroll.pack(fill="both", expand=True, padx=5, pady=5)
                self._trap_scroll(scroll)

                categories = [
                    ("RECOMMENDED", ["large-v3-turbo", "large-v3-turbo-q5_0"]),
                    ("ENGLISH ONLY", ["tiny.en", "base.en", "small.en", "medium.en"]),
                    ("MULTI-LANGUAGE", ["tiny", "base", "small", "medium", "large-v3"]),
                ]

                for section_title, model_ids in categories:
                    ctk.CTkLabel(
                        scroll, text=section_title,
                        font=(self.font_body[0], 10, "bold"),
                        text_color=COLORS["text_muted"],
                    ).pack(anchor="w", padx=8, pady=(8, 3))

                    for model_id in model_ids:
                        if model_id not in WHISPER_CPP_MODELS:
                            continue

                        info = WHISPER_CPP_MODELS[model_id]
                        is_installed = downloader.is_installed(model_id)

                        row = ctk.CTkFrame(scroll, fg_color=COLORS["bg_hover"] if is_installed else "transparent", corner_radius=6)
                        row.pack(fill="x", pady=1, padx=2)

                        info_frame = ctk.CTkFrame(row, fg_color="transparent")
                        info_frame.pack(side="left", fill="x", expand=True, padx=8, pady=5)

                        name_text = info["name"]
                        if info.get("recommended"):
                            name_text += " *"

                        ctk.CTkLabel(
                            info_frame, text=name_text,
                            font=(self.font_body[0], 11, "bold"),
                            text_color=COLORS["accent"] if is_installed else COLORS["text_primary"],
                        ).pack(anchor="w")

                        ctk.CTkLabel(
                            info_frame, text=f"{info['size']} • {info['speed']}",
                            font=(self.font_body[0], 9), text_color=COLORS["text_muted"],
                        ).pack(anchor="w")

                        if is_installed:
                            ctk.CTkLabel(
                                row, text="Installed",
                                font=(self.font_body[0], 10),
                                text_color=COLORS["accent_green"],
                            ).pack(side="right", padx=12, pady=6)
                        else:
                            def make_handler(mid=model_id):
                                return lambda: do_download(mid)

                            ctk.CTkButton(
                                row, text="Get",
                                font=(self.font_body[0], 10), width=50, height=24,
                                corner_radius=5, fg_color=COLORS["bg_elevated"],
                                hover_color=COLORS["accent_dim"], text_color=COLORS["text_primary"],
                                command=make_handler(),
                            ).pack(side="right", padx=8, pady=5)

            def do_download(model_id: str):
                """Download a model with inline progress."""
                info = WHISPER_CPP_MODELS[model_id]
                clear_content()

                ctk.CTkLabel(
                    content_area, text=f"Downloading {info['name']}",
                    font=(self.font_body[0], 14, "bold"),
                    text_color=COLORS["text_bright"],
                ).pack(pady=(20, 4))

                ctk.CTkLabel(
                    content_area, text=f"Size: {info['size']}",
                    font=(self.font_body[0], 11),
                    text_color=COLORS["text_secondary"],
                ).pack(pady=(0, 12))

                progress_bar = ctk.CTkProgressBar(content_area, height=16, corner_radius=8)
                progress_bar.pack(fill="x", padx=16, pady=(0, 6))
                progress_bar.set(0)

                status_lbl = ctk.CTkLabel(
                    content_area, text="Starting...",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["text_muted"],
                )
                status_lbl.pack()

                def on_progress(pct, done, total):
                    def update():
                        try:
                            progress_bar.set(pct)
                            mb_done = done / (1024 * 1024)
                            mb_total = total / (1024 * 1024)
                            status_lbl.configure(text=f"{mb_done:.1f} / {mb_total:.1f} MB ({pct*100:.0f}%)")
                        except Exception:
                            pass
                    self.after(0, update)

                def on_complete(path):
                    def update():
                        self.log(f"Downloaded: {info['name']}")
                        show_download()
                    self.after(0, update)

                def on_error(error):
                    def update():
                        try:
                            status_lbl.configure(text=f"Error: {error}", text_color=COLORS["accent_red"])
                        except Exception:
                            pass
                    self.after(0, update)

                downloader.download_model(model_id, on_progress, on_complete, on_error)

            installed_btn.configure(command=show_installed)
            download_btn.configure(command=show_download)
            show_installed()

        self._show_inline_panel(container, "Whisper Models", build_panel)

    def open_prompt_settings(self):
        """Open dialog to configure transcription prompt and vocabulary."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Transcription Prompt & Vocabulary")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 640, 920)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Transcription Prompt",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="The prompt helps Whisper understand context and improves accuracy.\nUse domain-specific vocabulary for better results.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Preset prompts section
        ctk.CTkLabel(
            inner,
            text="Preset Prompts:",
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(0, 8))
        
        preset_prompts = self.get_preset_prompts()
        current_prompt = self.config.get("prompt", "")
        
        # Find if current is a preset
        current_preset = None
        for name, text in preset_prompts.items():
            if text == current_prompt:
                current_preset = name
                break
        
        prompt_var = ctk.StringVar(value=current_preset or "Custom")
        
        # Preset buttons frame (scrollable for many presets)
        presets_frame = SmoothScrollableFrame(
            inner, 
            fg_color=COLORS["bg_card"], 
            corner_radius=10,
            height=200,
        )
        presets_frame.pack(fill="x", pady=(0, 15))
        
        preset_buttons = {}
        
        def on_preset_select(preset_name):
            prompt_var.set(preset_name)
            if preset_name in preset_prompts:
                custom_text.delete("1.0", "end")
                custom_text.insert("1.0", preset_prompts[preset_name])
            # Update button styling to show selection
            for name, btn in preset_buttons.items():
                if name == preset_name:
                    btn.configure(
                        fg_color=COLORS["accent_dim"],
                        text_color=COLORS["text_bright"],
                    )
                else:
                    btn.configure(
                        fg_color=COLORS["bg_hover"],
                        text_color=COLORS["text_primary"],
                    )
        
        for name in preset_prompts.keys():
            is_selected = name == current_preset
            btn = ctk.CTkButton(
                presets_frame,
                text=name,
                font=(self.font_body[0], 11),
                height=32,
                fg_color=COLORS["accent_dim"] if is_selected else COLORS["bg_hover"],
                hover_color=COLORS["accent_glow"] if is_selected else COLORS["bg_elevated"],
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_primary"],
                corner_radius=6,
                command=lambda n=name: on_preset_select(n),
            )
            btn.pack(fill="x", padx=10, pady=5)
            preset_buttons[name] = btn
        
        # Custom prompt section
        ctk.CTkLabel(
            inner,
            text="Custom Prompt:",
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(10, 8))
        
        custom_text = ctk.CTkTextbox(
            inner,
            font=(self.font_body[0], 12),
            fg_color=COLORS["bg_card"],
            text_color=COLORS["text_primary"],
            corner_radius=10,
            height=100,
            wrap="word",
        )
        custom_text.pack(fill="x", pady=(0, 15))
        custom_text.insert("1.0", current_prompt)
        
        # My Vocabulary section (always appended to active prompt)
        ctk.CTkLabel(
            inner,
            text="My Vocabulary:",
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w", pady=(5, 5))
        
        ctk.CTkLabel(
            inner,
            text="Personal terms always added to your prompt (comma-separated)",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 8))
        
        vocab_locked = not self.feature_gate.has_feature("custom_vocabulary")
        vocab_text = ctk.CTkTextbox(
            inner,
            font=(self.font_body[0], 12),
            fg_color=COLORS["bg_card"],
            text_color=COLORS["text_muted"] if vocab_locked else COLORS["accent"],
            corner_radius=10,
            height=60,
            wrap="word",
        )
        vocab_text.pack(fill="x", pady=(0, 10))

        if vocab_locked:
            vocab_text.insert("1.0", "Custom vocabulary requires Premium")
            vocab_text.configure(state="disabled")
        else:
            # Load existing vocabulary
            current_vocab = self.config.get("custom_vocabulary", [])
            if current_vocab:
                vocab_text.insert("1.0", ", ".join(current_vocab))
        
        # Tip
        tip_frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_hover"], corner_radius=8)
        tip_frame.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(
            tip_frame,
            text="💡 Vocabulary is appended to your prompt. Add names, jargon,\n    and technical terms (e.g. Bazzite, WRX STI, FSD, whisper.cpp)",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(padx=12, pady=10)
        
        # Save button
        def save():
            new_prompt = custom_text.get("1.0", "end").strip()
            self.config["prompt"] = new_prompt
            
            # Parse vocabulary (comma-separated, strip whitespace)
            vocab_raw = vocab_text.get("1.0", "end").strip()
            if vocab_raw:
                vocab_list = [v.strip() for v in vocab_raw.split(",") if v.strip()]
            else:
                vocab_list = []
            self.config["custom_vocabulary"] = vocab_list
            
            save_config(self.config)
            
            if hasattr(self, 'prompt_btn'):
                self.prompt_btn.configure(text=self.get_prompt_display())
            vocab_count = len(self.config.get("custom_vocabulary", []))
            self.log(f"⚙ Prompt updated: {self.get_prompt_display()} (+{vocab_count} vocab terms)")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Save & Apply",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def open_microphone_settings(self):
        """Open dialog to select audio input device (microphone)."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Select Microphone")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 560, 540)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="🎤 Select Microphone",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Choose which audio input device to use for voice recording.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Get all audio input devices
        audio_devices = get_audio_input_devices()
        current_device = self.config.get("audio_device")
        
        # Variable to track selection
        selected_device = ctk.IntVar(value=-1 if current_device is None else current_device)
        
        # Scrollable frame for devices
        scroll_frame = SmoothScrollableFrame(
            inner,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            height=220,
        )
        scroll_frame.pack(fill="both", expand=True, pady=(0, 20))
        
        # Auto-detect option (intelligent selection)
        auto_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        auto_frame.pack(fill="x", pady=5, padx=5)
        
        ctk.CTkRadioButton(
            auto_frame,
            text="🎤 Auto-detect Microphone",
            variable=selected_device,
            value=-1,
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["accent"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
        ).pack(side="left")
        
        ctk.CTkLabel(
            auto_frame,
            text="(Recommended)",
            font=(self.font_body[0], 11),
            text_color=COLORS["accent_green"],
        ).pack(side="left", padx=(10, 0))
        
        # Separator
        ctk.CTkFrame(scroll_frame, fg_color=COLORS["border"], height=1).pack(fill="x", pady=10, padx=10)
        
        # List each audio device
        for device in audio_devices:
            row = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            row.pack(fill="x", pady=4, padx=5)
            
            # Radio button
            rb = ctk.CTkRadioButton(
                row,
                text="",
                variable=selected_device,
                value=device["index"],
                width=24,
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
            )
            rb.pack(side="left")
            
            # Device info frame
            info_frame = ctk.CTkFrame(row, fg_color="transparent")
            info_frame.pack(side="left", fill="x", expand=True, padx=(5, 0))
            
            # Device name (truncated if needed)
            name = device["name"]
            display_name = name[:40] + "..." if len(name) > 40 else name
            
            # Mark default and recommended devices
            badges = []
            if device.get("is_default"):
                badges.append("★ Default")
            if device.get("recommended"):
                badges.append("✓ Mic")
            
            name_label = ctk.CTkLabel(
                info_frame,
                text=display_name,
                font=(self.font_body[0], 12),
                text_color=COLORS["accent"] if device.get("recommended") else COLORS["text_primary"],
                anchor="w",
            )
            name_label.pack(anchor="w")
            
            # Device details (channels, badges)
            details_parts = [f"{device['channels']}ch"]
            if badges:
                details_parts.extend(badges)
            details = " • ".join(details_parts)
            ctk.CTkLabel(
                info_frame,
                text=details,
                font=(self.font_body[0], 10),
                text_color=COLORS["text_secondary"],
                anchor="w",
            ).pack(anchor="w")
        
        if not audio_devices:
            ctk.CTkLabel(
                scroll_frame,
                text="No audio input devices found!\n\nCheck your audio settings and make sure\na microphone is connected.",
                font=(self.font_body[0], 13),
                text_color=COLORS["accent_red"],
                justify="center",
            ).pack(pady=30)
        
        # Save button
        def save():
            device_idx = selected_device.get()
            if device_idx == -1:
                self.config["audio_device"] = None
                device_name = "System Default"
            else:
                self.config["audio_device"] = device_idx
                # Get device name for display
                device_name = f"Device {device_idx}"
                for d in audio_devices:
                    if d["index"] == device_idx:
                        device_name = d["name"]
                        break
            
            save_config(self.config)
            
            # Update dropdown selection
            if hasattr(self, 'mic_dropdown'):
                mic_options, mic_current = self._get_microphone_dropdown_options()
                self.mic_dropdown.configure(values=mic_options)
                self.mic_dropdown.set(mic_current)
            
            # Update recorder with new device
            self.update_audio_device()
            
            self.log(f"🎤 Microphone: {device_name[:30]}...")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Save & Apply",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(10, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def update_audio_device(self):
        """Update the recorder with the new audio device setting."""
        # Re-resolve the audio device (handles intelligent selection)
        self._resolved_audio_device = resolve_audio_device(self.config)
        
        # Store the device name for persistence (device IDs can change)
        if self._resolved_audio_device is not None:
            try:
                import sounddevice as sd
                dev_info = sd.query_devices(self._resolved_audio_device)
                self.config["audio_device_name"] = dev_info.get("name", "")
            except Exception:
                pass
        
        # Update standard recorder
        self.recorder = AudioRecorder(
            sample_rate=self.config["sample_rate"],
            device=self._resolved_audio_device,
            preprocessing=self.config.get("audio_preprocessing", "light"),
        )
        
        # Chunked recorder will be recreated when needed with new device

    def open_accuracy_mode_settings(self):
        """Open dialog to select accuracy mode preset."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Accuracy Mode")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 520, 580)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Accuracy Mode",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Choose a preset that balances speed and accuracy.\nHigher accuracy = slower transcription.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Mode presets with their settings
        # Note: beam_size > 5 and best_of > 5 cause exponential slowdown
        modes = {
            "fast": {
                "label": "⚡ Fast",
                "desc": "Quick results • ~40% faster • May miss some words",
                "settings": {
                    "beam_size": 1,
                    "best_of": 1,
                    "entropy_threshold": 2.4,
                    "no_speech_threshold": 0.6,
                    "temperature_fallback": 0.0,
                    "suppress_nst": False,
                }
            },
            "balanced": {
                "label": "⚖️ Balanced",
                "desc": "Good accuracy • Baseline speed • Recommended",
                "settings": {
                    "beam_size": 5,
                    "best_of": 3,
                    "entropy_threshold": 2.6,
                    "no_speech_threshold": 0.5,
                    "temperature_fallback": 0.0,
                    "suppress_nst": False,
                }
            },
            "high": {
                "label": "🎯 High Accuracy",
                "desc": "Better accuracy • ~60% slower • Best results",
                "settings": {
                    "beam_size": 5,
                    "best_of": 5,
                    "entropy_threshold": 2.8,
                    "no_speech_threshold": 0.4,
                    "temperature_fallback": 0.0,
                    "suppress_nst": False,
                }
            },
        }
        
        current_mode = self.config.get("accuracy_mode", "balanced")
        mode_var = ctk.StringVar(value=current_mode)
        mode_buttons = {}
        
        def on_mode_select(mode_key):
            mode_var.set(mode_key)
            for key, btn in mode_buttons.items():
                if key == mode_key:
                    btn.configure(fg_color=COLORS["accent_dim"], text_color=COLORS["text_bright"])
                else:
                    btn.configure(fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"])
        
        for mode_key, mode_info in modes.items():
            is_selected = mode_key == current_mode
            frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_card"], corner_radius=10)
            frame.pack(fill="x", pady=5)
            
            btn = ctk.CTkButton(
                frame,
                text=f"{mode_info['label']}\n{mode_info['desc']}",
                font=(self.font_body[0], 13),
                height=60,
                fg_color=COLORS["accent_dim"] if is_selected else COLORS["bg_hover"],
                hover_color=COLORS["accent_glow"] if is_selected else COLORS["bg_elevated"],
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_primary"],
                corner_radius=8,
                command=lambda m=mode_key: on_mode_select(m),
            )
            btn.pack(fill="x", padx=5, pady=5)
            mode_buttons[mode_key] = btn
        
        # Apply button
        def apply_mode():
            selected = mode_var.get()
            mode_settings = modes[selected]["settings"]
            
            # Apply all settings from the preset
            self.config["accuracy_mode"] = selected
            for key, value in mode_settings.items():
                self.config[key] = value
            
            save_config(self.config)
            
            # Update UI
            mode_display = modes[selected]["label"]
            self.accuracy_mode_btn.configure(text=mode_display)
            self.beam_btn.configure(text=str(self.config["beam_size"]))
            
            self.log(f"⚙ Accuracy mode: {mode_display}")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Apply",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=apply_mode,
        ).pack(fill="x", pady=(20, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def open_beam_settings(self):
        """Open dialog to configure beam search size."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Beam Search Size")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 460, 440)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Beam Search Size",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Higher values = more accurate but slower.\n\n⏱️ 1 = fastest (-50%) | 5 = baseline | 10 = slowest (+100%)",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Current value display
        current = self.config.get("beam_size", 5)
        value_label = ctk.CTkLabel(
            inner,
            text=str(current),
            font=(self.font_header[0], 36, "bold"),
            text_color=COLORS["accent"],
        )
        value_label.pack(pady=(0, 15))
        
        # Slider
        beam_var = ctk.IntVar(value=current)
        
        def on_change(value):
            beam_var.set(int(float(value)))
            value_label.configure(text=str(int(float(value))))
        
        slider = ctk.CTkSlider(
            inner,
            from_=1,
            to=10,
            variable=beam_var,
            command=on_change,
            width=300,
            height=20,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
        )
        slider.pack(pady=(0, 5))
        
        # Preset buttons
        presets_frame = ctk.CTkFrame(inner, fg_color="transparent")
        presets_frame.pack(fill="x", pady=(10, 20))
        
        presets = [
            ("⚡ Fast (1)", 1),
            ("⚖️ Balanced (5)", 5),
            ("🎯 Accurate (10)", 10),
        ]
        for label, value in presets:
            ctk.CTkButton(
                presets_frame,
                text=label,
                width=100,
                height=30,
                font=(self.font_body[0], 11),
                fg_color=COLORS["bg_hover"],
                hover_color=COLORS["bg_elevated"],
                text_color=COLORS["text_primary"],
                corner_radius=6,
                command=lambda v=value: (beam_var.set(v), on_change(v)),
            ).pack(side="left", padx=5)
        
        # Clamp slider max to 3 for free users
        max_beam = 10 if self.feature_gate.has_feature("high_beam_search") else 3
        slider.configure(to=max_beam)
        if current > max_beam:
            beam_var.set(max_beam)
            on_change(max_beam)

        def save():
            val = beam_var.get()
            if val > 3 and not self.feature_gate.has_feature("high_beam_search"):
                self._show_premium_prompt("high_beam_search")
                return
            self.config["beam_size"] = val
            save_config(self.config)
            self.beam_btn.configure(text=str(val))
            self.log(f"⚙ Beam size: {val}")
            dialog.destroy()

        ctk.CTkButton(
            inner,
            text="Save",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def open_language_settings(self):
        """Open dialog to configure transcription language."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Language")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 460, 460)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Language",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Force a specific language for better accuracy,\nor use auto-detect for multilingual support.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        languages = [
            ("English", "en"),
            ("Auto-detect", "auto"),
            ("Spanish", "es"),
            ("French", "fr"),
            ("German", "de"),
            ("Italian", "it"),
            ("Portuguese", "pt"),
            ("Japanese", "ja"),
            ("Chinese", "zh"),
        ]
        
        current_lang = self.config.get("language", "en")
        lang_var = ctk.StringVar(value=current_lang)
        
        for label, code in languages:
            ctk.CTkRadioButton(
                inner,
                text=label,
                variable=lang_var,
                value=code,
                font=(self.font_body[0], 14),
                text_color=COLORS["text_primary"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
            ).pack(anchor="w", pady=4)
        
        def save():
            self.config["language"] = lang_var.get()
            save_config(self.config)
            lang_display = dict(languages).get(lang_var.get(), lang_var.get().upper())
            # Reverse lookup for display
            for label, code in languages:
                if code == lang_var.get():
                    lang_display = label
                    break
            self.language_btn.configure(text=lang_display)
            self.log(f"⚙ Language: {lang_display}")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Save",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(20, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def open_chunk_settings(self):
        """Open dialog to configure chunk duration."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Chunk Duration")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 480, 460)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Chunk Duration",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="How long each audio segment is during chunked recording.\nShorter = faster feedback | Longer = better context\n\n⏱️ Processing: 10s≈1-2s delay | 30s≈2-4s delay | 60s≈4-8s delay",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Current value display
        current = self.config.get("chunk_duration", 30)
        value_label = ctk.CTkLabel(
            inner,
            text=f"{current}s",
            font=(self.font_header[0], 36, "bold"),
            text_color=COLORS["accent"],
        )
        value_label.pack(pady=(0, 15))
        
        # Slider
        chunk_var = ctk.IntVar(value=current)
        
        def on_change(value):
            chunk_var.set(int(float(value)))
            value_label.configure(text=f"{int(float(value))}s")
        
        slider = ctk.CTkSlider(
            inner,
            from_=10,
            to=60,
            variable=chunk_var,
            command=on_change,
            width=300,
            height=20,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
        )
        slider.pack(pady=(0, 5))
        
        # Labels
        labels_frame = ctk.CTkFrame(inner, fg_color="transparent")
        labels_frame.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(labels_frame, text="10s", font=(self.font_body[0], 10), text_color=COLORS["text_muted"]).pack(side="left")
        ctk.CTkLabel(labels_frame, text="60s", font=(self.font_body[0], 10), text_color=COLORS["text_muted"]).pack(side="right")
        
        # Preset buttons with latency hints
        presets_frame = ctk.CTkFrame(inner, fg_color="transparent")
        presets_frame.pack(fill="x", pady=(0, 20))
        
        preset_labels = {
            15: "15s ⚡",  # Fast feedback
            30: "30s ⚖️",  # Balanced
            45: "45s",
            60: "60s 🎯",  # Best context
        }
        for val, label in preset_labels.items():
            ctk.CTkButton(
                presets_frame,
                text=label,
                width=70,
                height=30,
                font=(self.font_body[0], 11),
                fg_color=COLORS["bg_hover"],
                hover_color=COLORS["bg_elevated"],
                text_color=COLORS["text_primary"],
                corner_radius=6,
                command=lambda v=val: (chunk_var.set(v), on_change(v)),
            ).pack(side="left", padx=5)
        
        def save():
            self.config["chunk_duration"] = chunk_var.get()
            save_config(self.config)
            self.chunk_btn.configure(text=f"{chunk_var.get()}s")
            self.log(f"⚙ Chunk duration: {chunk_var.get()}s")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Save",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def open_backend_settings(self):
        """Open dialog to select transcription backend."""
        from wayfinder.core.transcriber import WhisperCppBackend, FasterWhisperBackend, OpenAIWhisperBackend, GroqWhisperBackend
        
        dialog = ctk.CTkToplevel(self)
        dialog.title("Transcription Backend")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 560, 740)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Select Backend",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Choose which transcription engine to use.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 10))
        
        # Show detected GPU info
        gpu_info = get_gpu_info()
        gpu_frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_card"], corner_radius=10)
        gpu_frame.pack(fill="x", pady=(0, 15))
        
        gpu_label_frame = ctk.CTkFrame(gpu_frame, fg_color="transparent")
        gpu_label_frame.pack(fill="x", padx=15, pady=10)
        
        # GPU icon based on vendor with latency estimates
        if gpu_info.is_nvidia:
            gpu_icon = "🟢"
            gpu_vendor_text = "NVIDIA GPU detected"
            gpu_rec = "⚡ 3-10x faster with Faster-Whisper + CUDA"
        elif gpu_info.is_amd:
            gpu_icon = "🔴"
            gpu_vendor_text = "AMD GPU detected"
            gpu_rec = "⚡ 2-5x faster with whisper.cpp + Vulkan"
        elif gpu_info.is_intel:
            gpu_icon = "🔵"
            gpu_vendor_text = "Intel GPU detected"
            gpu_rec = "🟡 Limited GPU support. CPU mode recommended."
        else:
            gpu_icon = "⚪"
            gpu_vendor_text = "GPU not detected"
            gpu_rec = "🔴 Running in CPU mode (slower)"
        
        ctk.CTkLabel(
            gpu_label_frame,
            text=f"{gpu_icon} {gpu_vendor_text}",
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_primary"],
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            gpu_label_frame,
            text=gpu_rec,
            font=(self.font_body[0], 11),
            text_color=COLORS["accent"] if gpu_info.is_nvidia or gpu_info.is_amd else COLORS["text_muted"],
        ).pack(anchor="w", pady=(2, 0))
        
        current = self.config.get("transcription_backend", "whisper_cpp")
        backend_var = ctk.StringVar(value=current)
        
        # Check availability
        whisper_cpp = WhisperCppBackend(
            whisper_binary=self.config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli"),
        )
        faster_whisper = FasterWhisperBackend()
        openai_whisper = OpenAIWhisperBackend()
        groq_whisper = GroqWhisperBackend()
        
        # Customize descriptions based on GPU with latency info
        if gpu_info.is_nvidia:
            whisper_cpp_desc = "C++ implementation. Requires CUDA rebuild.\n🟡 ~2-4x slower than Faster-Whisper on NVIDIA"
            faster_whisper_desc = "Python library with CUDA support.\n🚀 ★ Best for NVIDIA • 3-10x faster than CPU"
        elif gpu_info.is_amd:
            whisper_cpp_desc = "Fast C++ implementation.\n🚀 ★ Best for AMD • Vulkan acceleration"
            faster_whisper_desc = "Python library. Supports ROCm.\n🟡 ROCm setup required for GPU"
        else:
            whisper_cpp_desc = "Fast C++ implementation. Lightweight.\n⚡ Good for CPU-only systems"
            faster_whisper_desc = "Python library with CTranslate2.\n🟡 Moderate CPU performance"
        
        openai_whisper_desc = "OpenAI's cloud Whisper API.\n☁️ Requires API key • Always fast • Per-minute billing"
        groq_whisper_desc = "Groq's ultra-fast Whisper Large-v3.\n⚡ ★ ~10x faster than OpenAI • Free tier available"
        
        backends = [
            {
                "id": "whisper_cpp",
                "name": "whisper.cpp",
                "desc": whisper_cpp_desc,
                "available": whisper_cpp.is_available(),
                "gpu": whisper_cpp.supports_gpu(),
                "recommended": gpu_info.is_amd,
            },
            {
                "id": "faster_whisper",
                "name": "Faster-Whisper",
                "desc": faster_whisper_desc,
                "available": faster_whisper.is_available(),
                "gpu": faster_whisper.supports_gpu(),
                "recommended": gpu_info.is_nvidia,
            },
            {
                "id": "groq_whisper",
                "name": "Groq Whisper (Ultra-Fast)",
                "desc": groq_whisper_desc,
                "available": groq_whisper.is_available(),
                "gpu": True,  # Cloud = always "GPU"
                "recommended": True,  # Recommend Groq for speed
                "cloud": True,
            },
            {
                "id": "openai_whisper",
                "name": "OpenAI Whisper (Cloud)",
                "desc": openai_whisper_desc,
                "available": openai_whisper.is_available(),
                "gpu": True,  # Cloud = always "GPU"
                "recommended": False,
                "cloud": True,
            },
        ]
        
        scroll_frame = SmoothScrollableFrame(
            inner,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            height=280,
        )
        scroll_frame.pack(fill="both", expand=True, pady=(0, 20))
        
        for backend in backends:
            frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            frame.pack(fill="x", padx=10, pady=8)
            
            # Radio button
            radio = ctk.CTkRadioButton(
                frame,
                text="",
                variable=backend_var,
                value=backend["id"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_dim"],
                state="normal" if backend["available"] else "disabled",
            )
            radio.pack(side="left", padx=(5, 10))
            
            # Info container
            info = ctk.CTkFrame(frame, fg_color="transparent")
            info.pack(side="left", fill="x", expand=True)
            
            # Name row
            name_row = ctk.CTkFrame(info, fg_color="transparent")
            name_row.pack(fill="x")
            
            name_color = COLORS["text_primary"] if backend["available"] else COLORS["text_muted"]
            ctk.CTkLabel(
                name_row,
                text=backend["name"],
                font=(self.font_body[0], 14, "bold"),
                text_color=name_color,
            ).pack(side="left")
            
            # Recommended badge (based on GPU detection)
            if backend.get("recommended"):
                rec_badge = ctk.CTkLabel(
                    name_row,
                    text="★ Recommended",
                    font=(self.font_body[0], 10, "bold"),
                    text_color=COLORS["accent_yellow"],
                )
                rec_badge.pack(side="left", padx=8)
            
            # Status badges
            if backend["available"]:
                ctk.CTkLabel(
                    name_row,
                    text="✓",
                    font=(self.font_body[0], 11),
                    text_color=COLORS["accent_green"],
                ).pack(side="left", padx=5)
            else:
                ctk.CTkLabel(
                    name_row,
                    text="Not installed",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["accent_red"],
                ).pack(side="left", padx=5)
            
            if backend["gpu"]:
                ctk.CTkLabel(
                    name_row,
                    text="GPU ✓",
                    font=(self.font_body[0], 10),
                    text_color=COLORS["accent"],
                ).pack(side="left", padx=5)
            
            # Description
            ctk.CTkLabel(
                info,
                text=backend["desc"],
                font=(self.font_body[0], 11),
                text_color=COLORS["text_secondary"],
            ).pack(anchor="w")
        
        # Faster-Whisper specific settings (shown when selected)
        fw_frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_card"], corner_radius=12)
        fw_frame.pack(fill="x", pady=(0, 15))
        
        ctk.CTkLabel(
            fw_frame,
            text="Faster-Whisper Model",
            font=(self.font_body[0], 12, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=15, pady=(10, 5))
        
        # Add latency hint for model selection
        ctk.CTkLabel(
            fw_frame,
            text="⏱️ tiny ~0.5s | base ~1s | small ~1.5s | turbo ⭐~2s | large ~6s",
            font=(self.font_body[0], 10),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=15, pady=(0, 5))
        
        all_fw_models = ["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"]
        if not self.feature_gate.has_feature("large_models"):
            fw_models = [m for m in all_fw_models if m in ("tiny", "base", "small")]
        else:
            fw_models = all_fw_models
        current_fw = self.config.get("faster_whisper_model", "small")
        if current_fw not in fw_models:
            current_fw = "small"
        fw_model_var = ctk.StringVar(value=current_fw)
        fw_model_menu = ctk.CTkOptionMenu(
            fw_frame,
            values=fw_models,
            variable=fw_model_var,
            fg_color=COLORS["bg_hover"],
            button_color=COLORS["bg_elevated"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
        )
        fw_model_menu.pack(padx=15, pady=(0, 10), anchor="w")
        
        ctk.CTkLabel(
            fw_frame,
            text="Compute Type",
            font=(self.font_body[0], 12, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=15, pady=(5, 5))
        
        # Add latency hint for compute types
        ctk.CTkLabel(
            fw_frame,
            text="⏱️ float16 = fastest (GPU) | int8 = -30% VRAM | int8_float16 = balanced",
            font=(self.font_body[0], 10),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=15, pady=(0, 5))
        
        fw_compute_var = ctk.StringVar(value=self.config.get("faster_whisper_compute_type", "float16"))
        fw_compute_menu = ctk.CTkOptionMenu(
            fw_frame,
            values=["float16", "int8", "int8_float16"],
            variable=fw_compute_var,
            fg_color=COLORS["bg_hover"],
            button_color=COLORS["bg_elevated"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
        )
        fw_compute_menu.pack(padx=15, pady=(0, 15), anchor="w")
        
        # NVIDIA-specific setup help
        if gpu_info.is_nvidia:
            nvidia_frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_hover"], corner_radius=10)
            nvidia_frame.pack(fill="x", pady=(0, 15))
            
            ctk.CTkLabel(
                nvidia_frame,
                text="🟢 NVIDIA CUDA Setup",
                font=(self.font_body[0], 12, "bold"),
                text_color=COLORS["accent_green"],
            ).pack(anchor="w", padx=15, pady=(10, 5))
            
            setup_text = (
                "To enable GPU acceleration with Faster-Whisper:\n"
                "  pip install torch --index-url https://download.pytorch.org/whl/cu121\n"
                "  pip install faster-whisper\n\n"
                "Then select Faster-Whisper above and enable GPU in Advanced settings."
            )
            ctk.CTkLabel(
                nvidia_frame,
                text=setup_text,
                font=("Consolas", 10) if os.name == "nt" else ("monospace", 10),
                text_color=COLORS["text_secondary"],
                justify="left",
            ).pack(anchor="w", padx=15, pady=(0, 10))
        
        def save():
            self.config["transcription_backend"] = backend_var.get()
            self.config["faster_whisper_model"] = fw_model_var.get()
            self.config["faster_whisper_compute_type"] = fw_compute_var.get()
            save_config(self.config)
            
            display_names = {
                "whisper_cpp": "whisper.cpp",
                "faster_whisper": "Faster-Whisper",
                "openai_whisper": "OpenAI (Cloud)",
                "groq_whisper": "Groq (Ultra-Fast)",
            }
            display = display_names.get(backend_var.get(), backend_var.get())
            self.backend_btn.configure(text=display)
            self.log(f"⚙ Backend: {display}")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Save",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    def open_gpu_layers_settings(self):
        """Open dialog to configure GPU layers for whisper.cpp."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("GPU Layers")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        self._setup_dialog(dialog, 480, 500)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="GPU Layers",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Number of model layers to offload to GPU.\n0 = Auto (all layers → maximum speed, more VRAM)\n\n⏱️ More layers = faster | Fewer = saves VRAM",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Current value display
        current = self.config.get("gpu_layers", 0)
        display_text = "Auto (all)" if current == 0 else str(current)
        value_label = ctk.CTkLabel(
            inner,
            text=display_text,
            font=(self.font_header[0], 36, "bold"),
            text_color=COLORS["accent"],
        )
        value_label.pack(pady=(0, 15))
        
        # Slider (0 = auto, then 1-99)
        layers_var = ctk.IntVar(value=current)
        
        def on_change(value):
            val = int(float(value))
            layers_var.set(val)
            if val == 0:
                value_label.configure(text="Auto (all)")
            else:
                value_label.configure(text=str(val))
        
        slider = ctk.CTkSlider(
            inner,
            from_=0,
            to=64,
            variable=layers_var,
            command=on_change,
            width=300,
            height=20,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
        )
        slider.pack(pady=(0, 5))
        
        # Labels
        labels_frame = ctk.CTkFrame(inner, fg_color="transparent")
        labels_frame.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(labels_frame, text="Auto", font=(self.font_body[0], 10), text_color=COLORS["text_muted"]).pack(side="left")
        ctk.CTkLabel(labels_frame, text="64", font=(self.font_body[0], 10), text_color=COLORS["text_muted"]).pack(side="right")
        
        # Preset buttons
        presets_frame = ctk.CTkFrame(inner, fg_color="transparent")
        presets_frame.pack(fill="x", pady=(0, 20))
        
        for val, label in [(0, "⚡ Auto"), (16, "16"), (32, "32"), (48, "48")]:
            ctk.CTkButton(
                presets_frame,
                text=label,
                width=60,
                height=30,
                font=(self.font_body[0], 11),
                fg_color=COLORS["bg_hover"],
                hover_color=COLORS["bg_elevated"],
                text_color=COLORS["text_primary"],
                corner_radius=6,
                command=lambda v=val: (layers_var.set(v), on_change(v)),
            ).pack(side="left", padx=5)
        
        # Note about whisper.cpp
        ctk.CTkLabel(
            inner,
            text="Note: Only applies when using whisper.cpp backend\nwith GPU acceleration enabled.",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
            justify="center",
        ).pack(pady=(10, 15))
        
        def save():
            self.config["gpu_layers"] = layers_var.get()
            save_config(self.config)
            display = "Auto (all)" if layers_var.get() == 0 else str(layers_var.get())
            self.gpu_layers_btn.configure(text=display)
            self.log(f"⚙ GPU layers: {display}")
            dialog.destroy()
        
        ctk.CTkButton(
            inner,
            text="Save",
            font=(self.font_body[0], 15, "bold"),
            height=50,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=save,
        ).pack(fill="x", pady=(0, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=dialog.destroy,
        ).pack(fill="x")

    # === Tray Functions ===
    
    def _create_audio_processing_tray_menu(self):
        """Dynamically create audio processing submenu."""
        current = self.config.get("audio_preprocessing", "light")
        
        levels = [
            ("off", "Off"),
            ("light", "Light"),
            ("medium", "Medium"),
            ("heavy", "Heavy"),
        ]
        
        items = []
        for level_key, level_name in levels:
            def make_checker(lk):
                return lambda item: self.config.get("audio_preprocessing", "light") == lk
            
            items.append(pystray.MenuItem(
                level_name,
                self.create_audio_processing_setter(level_key),
                checked=make_checker(level_key),
            ))
        
        return pystray.Menu(*items)
    
    def create_audio_processing_setter(self, level: str):
        """Create a callback function for setting audio processing level from tray menu."""
        def setter(icon=None, item=None):
            self.after(0, lambda: self._apply_audio_processing_from_tray(level))
        return setter
    
    def _apply_audio_processing_from_tray(self, level: str):
        """Apply audio processing selection from tray menu - runs on main Tk thread."""
        self.config["audio_preprocessing"] = level
        save_config(self.config)
        self.log(f"⚙ Audio processing: {level}")
        
        # Update recorder if it exists
        if hasattr(self, 'recorder') and self.recorder:
            self.recorder.preprocessing = level
        
        # Update UI dropdown if visible (dropdown uses capitalized values)
        if hasattr(self, 'preprocess_var'):
            try:
                self.preprocess_var.set(level.capitalize())
            except:
                pass
    
    def _create_microphone_tray_menu(self):
        """Dynamically create microphone submenu with audio processing options."""
        return pystray.Menu(
            pystray.MenuItem(
                "Audio Processing",
                self._create_audio_processing_tray_menu,
            ),
        )
    
    def _create_model_tray_menu(self):
        """Dynamically create model submenu showing only installed models."""
        models = self.get_available_models()
        
        if not models:
            # No models installed - show a message
            return pystray.Menu(
                pystray.MenuItem("No models installed", None, enabled=False),
                pystray.MenuItem("Open app to download", self.show_from_tray),
            )
        
        # Create menu items for each installed model
        items = []
        current_path = os.path.expanduser(self.config.get("model_path", ""))
        
        for model in models:
            model_path = model["path"]
            model_name = model["name"]
            filename = model.get("filename", "")
            
            # Extract the model key from filename (e.g., "ggml-small.en.bin" -> "small.en")
            model_key = filename.replace("ggml-", "").replace(".bin", "") if filename else ""
            
            # Create a checker function that captures the model path
            def make_checker(mp):
                return lambda item: os.path.expanduser(mp) == current_path
            
            items.append(pystray.MenuItem(
                model_name,
                self.create_model_setter(model_key),
                checked=make_checker(model_path),
            ))
        
        return pystray.Menu(*items)

    def create_model_setter(self, model_name: str):
        """Create a callback function for setting a specific model from tray menu."""
        def setter(icon=None, item=None):
            # Schedule on main thread - pystray callbacks run on a different thread
            # and Tk is NOT thread-safe
            self.after(0, lambda: self._apply_model_from_tray(model_name))
        return setter
    
    def _apply_model_from_tray(self, model_name: str):
        """Apply model selection from tray menu - runs on main Tk thread."""
        models_dir = Path.home() / "whisper.cpp" / "models"
        model_path = models_dir / f"ggml-{model_name}.bin"
        
        if model_path.exists():
            # Store with ~ for portability
            relative_path = f"~/whisper.cpp/models/ggml-{model_name}.bin"
            self.config["model_path"] = relative_path
            save_config(self.config)
            self.log(f"⚙ Model: {model_name}")
            # Update the button in main UI if visible
            if hasattr(self, 'model_btn'):
                try:
                    self.model_btn.configure(text=self.get_model_display())
                except:
                    pass
        else:
            self.log(f"⚠ Model not found: {model_name}")
    
    def setup_tray(self):
        # Skip entirely if pystray couldn't import its tray backend (sandbox / no AppIndicator).
        if not HAS_PYSTRAY:
            self.tray_icon = None
            return
        # Check if tray icon is disabled (useful for Wayland where pystray doesn't work well)
        if not self.config.get("enable_tray_icon", True):
            self.tray_icon = None
            print("[Tray] System tray icon disabled in settings")
            return
        
        self.custom_icon = None
        if ICON_PATH.exists():
            try:
                self.custom_icon = Image.open(ICON_PATH).resize((64, 64))
            except:
                pass
        
        # Create menu with dynamic model submenu (only installed models)
        # UI is only accessible via "Open Settings" - app always starts minimized to tray
        menu = pystray.Menu(
            pystray.MenuItem("Toggle Recording", self.tray_record, default=True),
            pystray.MenuItem("Reset (unstick overlay)", self.tray_reset),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Settings", self.show_from_tray),
            pystray.MenuItem(
                "Model",
                self._create_model_tray_menu,
            ),
            pystray.MenuItem(
                "Microphone",
                self._create_microphone_tray_menu,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit_app),
        )
        
        icon_image = self.custom_icon if self.custom_icon else self.get_tray_icon(AppState.IDLE)
        self.tray_icon = pystray.Icon("wayfinder-aura", icon_image, "Wayfinder Aura", menu)

        if sys.platform == "darwin":
            # On macOS, pystray's run() calls NSApplication.run() from a background
            # thread, which crashes with NSUpdateCycleInitialize. Use run_detached()
            # instead — tray icon events are handled by Tkinter's own NSApp run loop.
            self.tray_icon.run_detached()
        else:
            def _tray_thread_wrapper():
                try:
                    self.tray_icon.run()
                except Exception as e:
                    raise
            threading.Thread(target=_tray_thread_wrapper, daemon=True).start()

    def get_tray_icon(self, state: AppState, pulse_scale: float = 1.0) -> Image.Image:
        """Create a navigation arrow tray icon.
        
        Clean cursor/pointer arrow shape. Color indicates state.
        Recording state has a "drawing" animation that traces the arrow outline.
        """
        size = 64
        
        # Create transparent background
        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        
        # Arrow points (30° rotated symmetric arrow)
        arrow_points = [
            (42, 15),   # 0: Tip (upper-right)
            (34, 56),   # 1: Right corner
            (30, 44),   # 2: Inner right (notch start)
            (24, 46),   # 3: Notch bottom
            (23, 40),   # 4: Inner left (notch end)
            (10, 42),   # 5: Left corner
        ]
        
        if state == AppState.RECORDING:
            # Red color for recording
            glyph_color = (255, 77, 77, 255)
            
            # Drawing animation: trace the arrow outline from bottom-left to tip to right
            # pulse_scale goes from 0.0 to 1.0
            
            # Define drawing path segments
            segments = [
                (arrow_points[5], arrow_points[0]),  # Left corner → Tip
                (arrow_points[0], arrow_points[1]),  # Tip → Right corner
                (arrow_points[1], arrow_points[2]),  # Right corner → Inner right
                (arrow_points[2], arrow_points[3]),  # Inner right → Notch
                (arrow_points[3], arrow_points[4]),  # Notch → Inner left
                (arrow_points[4], arrow_points[5]),  # Inner left → Left corner
            ]
            
            if pulse_scale < 0.85:
                # Drawing phase: trace outline with thick stroke
                num_segments = len(segments)
                progress = pulse_scale / 0.85  # Normalize for drawing phase
                segments_to_draw = progress * num_segments
                
                for i, (start, end) in enumerate(segments):
                    if i < int(segments_to_draw):
                        # Draw complete segment
                        draw.line([start, end], fill=glyph_color, width=4)
                    elif i < segments_to_draw:
                        # Partial segment - interpolate endpoint
                        frac = segments_to_draw - int(segments_to_draw)
                        partial_end = (
                            int(start[0] + (end[0] - start[0]) * frac),
                            int(start[1] + (end[1] - start[1]) * frac)
                        )
                        draw.line([start, partial_end], fill=glyph_color, width=4)
            else:
                # Fill phase: show complete filled arrow
                draw.polygon(arrow_points, fill=glyph_color)
        
        elif state == AppState.PROCESSING:
            glyph_color = (229, 172, 42, 255)
            draw.polygon(arrow_points, fill=glyph_color)
        elif state == AppState.PASTING:
            glyph_color = (93, 212, 168, 255)
            draw.polygon(arrow_points, fill=glyph_color)
        else:  # IDLE
            glyph_color = (255, 255, 255, 255)
            draw.polygon(arrow_points, fill=glyph_color)
        
        return icon

    def update_tray(self, state: AppState):
        if self.tray_icon:
            # Create icon image first (this is thread-safe)
            new_icon = self.get_tray_icon(state)
            new_title = f"Wayfinder Aura - {STATE_LABELS[state]}"
            # Update tray - pystray handles thread safety for these operations
            try:
                self.tray_icon.icon = new_icon
                self.tray_icon.title = new_title
            except Exception as e:
                pass
        
        # Start/stop tray pulse animation based on state
        if state == AppState.RECORDING:
            self._start_tray_pulse()
        else:
            self._stop_tray_pulse()
    
    def _start_tray_pulse(self):
        """Start the tray icon pulsing animation."""
        # Cancel any existing pulse to prevent duplicate timer loops
        if hasattr(self, '_tray_pulse_job') and self._tray_pulse_job:
            try:
                self.after_cancel(self._tray_pulse_job)
            except Exception:
                pass
            self._tray_pulse_job = None
        if not hasattr(self, '_tray_pulse_frame'):
            self._tray_pulse_frame = 0
        self._tray_pulse_step()
    
    def _tray_pulse_step(self):
        """Animate the tray icon with a drawing effect.
        
        Smoothly traces the arrow outline, then fills it, then loops.
        """
        if self.app_state != AppState.RECORDING:
            return
        
        # Smooth animation: increment progress each step
        self._tray_pulse_progress = getattr(self, '_tray_pulse_progress', 0.0)
        self._tray_pulse_progress += 0.04  # 4% per step = 25 steps for full cycle
        
        if self._tray_pulse_progress > 1.0:
            self._tray_pulse_progress = 0.0  # Loop
        
        pulse_scale = self._tray_pulse_progress
        
        # Update tray icon with current progress
        if self.tray_icon:
            self.tray_icon.icon = self.get_tray_icon(AppState.RECORDING, pulse_scale)
        
        # Schedule next frame at 50ms (~20 fps for smooth animation)
        self._tray_pulse_job = self.after(50, self._tray_pulse_step)
    
    def _stop_tray_pulse(self):
        """Stop the tray icon pulsing animation."""
        if hasattr(self, '_tray_pulse_job') and self._tray_pulse_job:
            try:
                self.after_cancel(self._tray_pulse_job)
            except:
                pass
            self._tray_pulse_job = None
        self._tray_pulse_phase = 0.0

    def show_from_tray(self):
        self.after(0, self._show_window)

    def _show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def hide_to_tray(self):
        # Only withdraw (fully hide) if there's a tray surface to restore the window from —
        # otherwise the window vanishes with no way back (the Flatpak had no tray). Check
        # ACTUAL availability, not HAS_PYSTRAY: self.tray_icon is None when the tray is
        # disabled in settings even though pystray imported, and the overlay's
        # QSystemTrayIcon availability is reported live on the controller (so it stays correct
        # across an overlay restart). With no tray, minimize to the taskbar — always restorable.
        tray_present = (getattr(self, 'tray_icon', None) is not None) or (
            getattr(self, 'overlay_controller', None) is not None
            and getattr(self.overlay_controller, 'tray_available', False)
        )
        if tray_present:
            self.withdraw()
        else:
            self.iconify()

    def tray_record(self):
        self.after(0, self.on_record_button)

    def tray_reset(self, icon=None, item=None):
        """Tray 'Reset' action — abandon any stuck/in-flight dictation and return to idle."""
        self.force_reset()

    def quit_app(self, icon=None, item=None):
        """Clean shutdown of the app and all subprocesses."""
        # Signal all background threads to stop
        self.stop_event.set()
        if hasattr(self, '_evdev_stop_event'):
            self._evdev_stop_event.set()

        # Shutdown thread pool executors gracefully
        try:
            if hasattr(self, 'executor'):
                self.executor.shutdown(wait=False, cancel_futures=True)
            if hasattr(self, 'transcription_executor'):
                self.transcription_executor.shutdown(wait=False, cancel_futures=True)
        except:
            pass
        
        # Clean up recorder if active
        try:
            if hasattr(self, 'recorder') and self.recorder:
                self.recorder.cleanup()
            if hasattr(self, 'chunked_recorder') and self.chunked_recorder:
                self.chunked_recorder.cleanup()
        except:
            pass
        
        # Clean up whisper-server if running
        try:
            from wayfinder.core.transcriber import WhisperServerBackend
            WhisperServerBackend.shutdown()
        except:
            pass

        # Clean up overlay
        self._cleanup_overlay()
        
        # Give a moment for cleanup to complete
        time.sleep(0.2)
        
        # Force kill any remaining overlay processes as safety net
        try:
            subprocess.run(["pkill", "-9", "-f", "overlay.py"], 
                          capture_output=True, timeout=1)
        except:
            pass
        
        os._exit(0)  # Clean exit
    
    def _cleanup_overlay(self):
        """Clean up overlay subprocess - called on exit."""
        if hasattr(self, 'overlay_controller') and self.overlay_controller:
            try:
                self.overlay_controller.quit()
            except:
                pass
            self.overlay_controller = None
        
        # Also clean up CTk indicator
        if hasattr(self, 'indicator') and self.indicator:
            try:
                self.indicator.hide()
            except:
                pass

    # === State Management ===
    
    def update_state(self, new_state: AppState):
        old_state = self.app_state
        self.app_state = new_state
        color = STATE_COLORS[new_state]

        # Diagnostics: trace state transitions (goes to journal/stderr, not the UI log) so a
        # stuck-overlay report can be correlated against the overlay-debug.log timeline.
        if old_state != new_state:
            print(f"[STATE] {old_state.name} -> {new_state.name}", flush=True)

        # Audio ducking on state transitions
        if self.config.get("audio_ducking_enabled", True) and hasattr(self, 'audio_ducker'):
            if new_state == AppState.RECORDING and old_state != AppState.RECORDING:
                # Entering recording - duck other audio
                self.audio_ducker.duck()
            elif old_state == AppState.RECORDING and new_state != AppState.RECORDING:
                # Leaving recording - restore audio
                self.audio_ducker.restore()
        
        # Update tray FIRST - this is critical for user feedback
        self.update_tray(new_state)
        
        # Update UI elements with error handling
        try:
            # Update mic button with new state color
            if hasattr(self, 'mic_button_canvas'):
                self._draw_mic_button(color)
            
            # Update status label
            if hasattr(self, 'status_label'):
                self.status_label.configure(text=STATE_LABELS[new_state], text_color=color)
            
            # Update hero frame border for subtle glow effect
            if hasattr(self, 'hero_frame'):
                if new_state == AppState.IDLE:
                    self.hero_frame.configure(border_color=COLORS["border_subtle"])
                    self._stop_hero_animation()
                else:
                    self.hero_frame.configure(border_color=color)
                    self._start_hero_animation()
            
            # Redraw waveform with new state color
            if hasattr(self, 'hero_canvas'):
                self._draw_hero_waveform()
        except Exception as e:
            # Log but don't break recording functionality
            self.log(f"⚠ UI update error: {e}")
    
    # === Hero Animation System ===
    
    def _start_hero_animation(self):
        """Start the hero waveform animation loop."""
        # Stop idle breathing first
        self._stop_idle_breath()
        
        if hasattr(self, '_hero_animation_job') and self._hero_animation_job:
            return  # Already running
        self._animate_hero()
    
    def _stop_hero_animation(self):
        """Stop the hero waveform animation loop."""
        if hasattr(self, '_hero_animation_job') and self._hero_animation_job:
            self.after_cancel(self._hero_animation_job)
            self._hero_animation_job = None
        # Reset to idle state
        self._hero_audio_level = 0.0
        self._draw_hero_waveform()
        # Start gentle idle breathing animation
        self._start_idle_breath()
    
    def _start_idle_breath(self):
        """Start the gentle idle breathing animation for the waveform."""
        if hasattr(self, '_idle_breath_job') and self._idle_breath_job:
            return  # Already running
        self._animate_idle_breath()
    
    def _stop_idle_breath(self):
        """Stop the idle breathing animation."""
        if hasattr(self, '_idle_breath_job') and self._idle_breath_job:
            self.after_cancel(self._idle_breath_job)
            self._idle_breath_job = None
    
    def _animate_idle_breath(self):
        """Animation frame for idle breathing waveform - runs at 2fps to save CPU."""
        # Guard: Stop if not in IDLE state
        if self.app_state != AppState.IDLE:
            self._idle_breath_job = None
            return
        
        # Skip animation when window is not visible (minimized, hidden to tray, withdrawn)
        try:
            win_state = self.state()
            if win_state in ("iconic", "withdrawn"):
                # Check again in 2 seconds (no point animating an invisible window)
                self._idle_breath_job = self.after(2000, self._animate_idle_breath)
                return
        except Exception:
            pass
        
        # Also skip if the hero canvas is not visible (e.g. on a different tab)
        try:
            if not self.hero_canvas.winfo_viewable():
                self._idle_breath_job = self.after(2000, self._animate_idle_breath)
                return
        except Exception:
            pass
        
        # 30fps idle animation (PIL rendering is cheap — single image blit per frame)
        idle_fps_scale = 2.0  # 60/30

        # Slow, gentle time progression for calm wave motion
        self._hero_wave_time += 0.04 * idle_fps_scale
        self._hero_audio_level = 0.0

        # Redraw waveform (PIL render + single canvas image update)
        self._draw_hero_waveform()

        # Schedule next frame at 33ms (30fps) — smooth silk ribbon animation
        self._idle_breath_job = self.after(33, self._animate_idle_breath)
    
    def _animate_hero(self):
        """Animation frame for hero waveform - STABLE at 15fps."""
        # Guard: Stop if in IDLE state (idle breath handles that)
        if self.app_state == AppState.IDLE:
            self._hero_animation_job = None
            return
        
        import math
        hero_fps_scale = 4.0  # 60/15
        
        # Update animation time
        self._hero_wave_time += 0.15 * hero_fps_scale
        
        # Get current audio level from active recorder
        target_level = 0.0
        try:
            if hasattr(self, 'chunked_recorder') and self.chunked_recorder and self.chunked_recorder.is_recording():
                target_level = self.chunked_recorder.get_audio_level()
            elif hasattr(self, 'recorder') and self.recorder and self.recorder.is_recording():
                target_level = self.recorder.get_audio_level()
        except:
            pass
        
        # Smooth the audio level
        smooth_factor = 0.7 ** hero_fps_scale
        self._hero_audio_level = self._hero_audio_level * smooth_factor + target_level * (1 - smooth_factor)
        
        # Redraw waveform (uses stable pre-created items)
        self._draw_hero_waveform()
        
        # Pulse the mic button during recording
        if self.app_state == AppState.RECORDING:
            pulse = 0.9 + 0.1 * math.sin(self._hero_wave_time * 2)
            self._draw_mic_button_with_pulse(STATE_COLORS[self.app_state], pulse)
        
        # Schedule next frame at fixed 66ms (15fps)
        self._hero_animation_job = self.after(66, self._animate_hero)
    
    def _init_mic_pulse_items(self, color: str):
        """Create mic button pulse items ONCE."""
        if self._mic_items_created:
            return
        
        canvas = self.mic_button_canvas
        size = 80
        cx, cy = size // 2, size // 2
        
        # Create 3 glow layers
        self._mic_glow_ids = []
        for radius in [38, 34, 30]:
            item_id = canvas.create_oval(
                cx - radius, cy - radius, cx + radius, cy + radius,
                fill=color, outline=""
            )
            self._mic_glow_ids.append(item_id)
        
        # Main button circle
        self._mic_button_id = canvas.create_oval(
            cx - 24, cy - 24, cx + 24, cy + 24,
            fill=color, outline=""
        )
        
        # Stop icon (square)
        self._mic_stop_id = canvas.create_rectangle(
            cx - 8, cy - 8, cx + 8, cy + 8,
            fill=COLORS["bg_base"], outline=""
        )
        
        self._mic_items_created = True
    
    def _draw_mic_button_with_pulse(self, color: str, pulse: float = 1.0):
        """Update mic button with pulse - STABLE version using coords/itemconfig."""
        canvas = self.mic_button_canvas
        
        # Initialize items on first call
        if not self._mic_items_created:
            self._init_mic_pulse_items(color)
            return
        
        # Wrap all canvas operations in try/except - Tk 9.0 can crash on coords
        try:
            # Validate pulse to avoid NaN/Inf crashes
            if not isinstance(pulse, (int, float)) or pulse != pulse or abs(pulse) > 100:
                pulse = 1.0
            
            size = 80
            cx, cy = size // 2, size // 2
            
            # Parse color
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            
            bg_r = int(COLORS["bg_card"][1:3], 16)
            bg_g = int(COLORS["bg_card"][3:5], 16)
            bg_b = int(COLORS["bg_card"][5:7], 16)
            
            # Update glow layers
            glow_configs = [
                (int(38 * pulse), 0.06 * pulse),
                (int(34 * pulse), 0.12 * pulse),
                (int(30 * pulse), 0.22 * pulse),
            ]
            
            for i, (radius, intensity) in enumerate(glow_configs):
                if i < len(self._mic_glow_ids):
                    gr = int(bg_r + (r - bg_r) * intensity)
                    gg = int(bg_g + (g - bg_g) * intensity)
                    gb = int(bg_b + (b - bg_b) * intensity)
                    glow_color = f"#{gr:02x}{gg:02x}{gb:02x}"
                    
                    canvas.coords(self._mic_glow_ids[i],
                        cx - radius, cy - radius, cx + radius, cy + radius)
                    canvas.itemconfig(self._mic_glow_ids[i], fill=glow_color)
            
            # Update main button
            button_radius = int(24 * (0.95 + 0.05 * pulse))
            canvas.coords(self._mic_button_id,
                cx - button_radius, cy - button_radius, cx + button_radius, cy + button_radius)
            canvas.itemconfig(self._mic_button_id, fill=color)
        except Exception:
            pass  # Tk 9.0 canvas coord bug - ignore to prevent crash
    
    # Legacy method for compatibility
    def _draw_status_indicator(self, color: str):
        """Legacy method - redirects to mic button drawing."""
        self._draw_mic_button(color)

    def on_record_button(self):
        self.on_hotkey()

    # === Hotkey & Events ===
    
    def start_hotkey_listener(self):
        # Socket listener stays up for the app's lifetime (config changes restart only the
        # keyboard listener — see restart_evdev_listener). Liveness-based so it self-heals.
        self._ensure_socket_listener()

        if sys.platform == "darwin":
            # macOS: pynput global listener reads config live — no evdev, no restart.
            self._start_pynput_listener()
            return

        # Inside a Flatpak sandbox (X11 OR Wayland): evdev can't read /dev/input and pynput
        # can't do Wayland global keys, so the GlobalShortcuts portal is the only viable path.
        # KDE's portal serves X11 sessions too, so gate on IS_FLATPAK, not the session type
        # (the Deck Desktop session is X11). Non-sandboxed installs keep using evdev + the
        # 'input' group (the validated Steam Deck dev-path).
        if IS_FLATPAK:
            self.log("🖥️ Flatpak — using the GlobalShortcuts portal for hotkeys")
            self._start_portal_listener()
            return
        is_wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
        if is_wayland:
            self.log("🖥️ Wayland detected - using evdev (requires 'input' group)")
        else:
            self.log("🖥️ X11 detected - using evdev")
        self._start_evdev_listener()

    def _ensure_socket_listener(self):
        """Start the socket listener if it isn't already running. Idempotent.

        Liveness-based (not a one-shot flag): if the listener ever dies — e.g. a transient
        SOCKET_PATH bind failure — the supervisor calls this again to bring it back, instead of
        the socket trigger being dead until an app restart.
        """
        existing = self._socket_thread
        if existing is not None and existing.is_alive():
            return
        self._socket_thread = threading.Thread(
            target=socket_listener,
            args=(self.event_queue, self.stop_event, self.log),
            daemon=True,
        )
        self._socket_thread.start()

    def _start_pynput_listener(self):
        """macOS global hotkey listener (pynput). Reads config live; started once."""
        if getattr(self, '_pynput_listener_started', False):
            # Already running — config changes are picked up automatically.
            self.log("⚙ Hotkey updated (live)")
            return
        from wayfinder.hotkeys import pynput_hotkey_listener, is_pynput_available
        if not is_pynput_available():
            self.log("⚠️ pynput not installed — hotkeys unavailable")
            return
        hotkey_key = self.config.get("hotkey_key", 67)
        hotkey_modifiers = self.config.get("hotkey_modifiers", [])
        style_toggle_key = self.config.get("style_toggle_key", 68)
        style_toggle_modifiers = self.config.get("style_toggle_modifiers", [])
        self.log("🖥️ macOS — using pynput (global keyboard listener)")
        self._pynput_listener_started = True

        def _pynput_wrapper():
            try:
                pynput_hotkey_listener(
                    self.event_queue, hotkey_key, hotkey_modifiers,
                    self.stop_event, self.log,
                    style_toggle_key, style_toggle_modifiers,
                    config_ref=self.config,
                )
            except Exception as e:
                print(f"[Hotkey] pynput listener crashed: {e}", flush=True)
                import traceback
                traceback.print_exc()
                self._pynput_listener_started = False

        threading.Thread(target=_pynput_wrapper, daemon=True).start()

    def _start_portal_listener(self):
        """Start the XDG GlobalShortcuts portal listener (Wayland/Flatpak) in a daemon thread.

        The portal is the only sandbox-viable global-hotkey mechanism. The user binds/rebinds
        the trigger in System Settings → Shortcuts; wayfinder requests a sensible default.
        """
        if getattr(self, '_portal_listener_started', False):
            return
        self._portal_listener_started = True
        hotkey_display = self.get_hotkey_display()

        def _portal_wrapper():
            try:
                wayland_hotkey_listener(self.event_queue, hotkey_display, self.stop_event, self.log)
            except Exception as e:
                print(f"[Hotkey] portal listener crashed: {e}", flush=True)
            finally:
                # Clear the flag on ANY exit — a normal False return (D-Bus unavailable /
                # setup failed) as well as an exception — UNLESS we're shutting down, so the
                # hotkey supervisor can re-arm the portal listener. (Codex #4)
                if not self.stop_event.is_set():
                    self._portal_listener_started = False

        threading.Thread(target=_portal_wrapper, daemon=True).start()

    def _start_evdev_listener(self):
        """Start (or restart) the evdev hotkey listener thread, cleanly stopping any prior one."""
        if not HAS_EVDEV:
            self.log("⚠️ evdev not installed — hotkeys limited to socket/D-Bus methods")
            return

        hotkey_key = self.config.get("hotkey_key", 67)
        hotkey_modifiers = self.config.get("hotkey_modifiers", [])
        enabled_devices = self.config.get("enabled_input_devices", [])
        style_toggle_key = self.config.get("style_toggle_key", 68)  # F10 default
        style_toggle_modifiers = self.config.get("style_toggle_modifiers", [])

        # Cleanly stop a previous evdev thread (config change / supervisor restart) using its
        # dedicated stop event — the shared self.stop_event (and the socket listener) is untouched.
        old_thread = self._hotkey_thread
        if old_thread is not None and old_thread.is_alive():
            self._evdev_stop_event.set()
            # Join longer than the listener's rescan backoff (2s) so the old thread is gone
            # before the new one starts — otherwise two listeners briefly double-fire hotkeys.
            old_thread.join(timeout=2.5)
            if old_thread.is_alive():
                self.log("⚠️ Old hotkey listener still winding down; starting new one anyway")
        self._evdev_stop_event = threading.Event()

        hotkey_name = self.get_hotkey_display()
        style_key_name = {59: "F1", 60: "F2", 61: "F3", 62: "F4", 63: "F5", 64: "F6",
                         65: "F7", 66: "F8", 67: "F9", 68: "F10"}.get(style_toggle_key, f"Key{style_toggle_key}")
        self.log(f"⌨️ Record hotkey: {hotkey_name} | Style toggle: {style_key_name}")

        self._hotkey_thread = threading.Thread(
            target=hotkey_listener,
            args=(self.event_queue, hotkey_key, hotkey_modifiers, self._evdev_stop_event,
                  enabled_devices, self.log, style_toggle_key, style_toggle_modifiers),
            daemon=True,
        )
        self._hotkey_thread.start()

    def restart_evdev_listener(self, reason: str = ""):
        """Cleanly restart only the evdev listener (config changes + the health supervisor).

        The socket listener keeps running — it doesn't depend on the hotkey key/device.
        """
        with self._hotkey_restart_lock:
            if reason:
                self.log(f"🔁 Hotkey listener restart ({reason})")
            self._start_evdev_listener()

    def poll_events(self):
        try:
            while True:
                event_type, data = self.event_queue.get_nowait()
                self.handle_event(event_type, data)
        except queue.Empty:
            pass
        # Adaptive polling: slow when idle (saves CPU), fast when active (responsive)
        # 250ms idle = imperceptible hotkey delay, 60% less CPU than 100ms
        interval = 250 if self.app_state == AppState.IDLE else 100
        self.after(interval, self.poll_events)

    @staticmethod
    def _split_gen(data):
        """Split a terminal event payload into (payload, generation).

        Workers tag terminal events as (payload, gen). Untagged/legacy payloads return
        (data, None), which disables the staleness check for that event (fail-open).
        """
        if isinstance(data, tuple) and len(data) == 2 and isinstance(data[1], (int, type(None))):
            return data[0], data[1]
        return data, None

    def handle_event(self, event_type, data):
        if event_type == EventType.HOTKEY_PRESSED:
            self.on_hotkey()
        elif event_type == EventType.STYLE_TOGGLE:
            self.on_style_toggle(data)  # data may be None (cycle) or a specific style name
        elif event_type == EventType.SHOW_WINDOW:
            self.show_from_tray()
        elif event_type == EventType.FORCE_RESET:
            self.force_reset()
        elif event_type == EventType.QUIT_APP:
            self.quit_app()
        elif event_type == EventType.TRANSCRIPTION_DONE:
            text, gen = self._split_gen(data)
            self.on_transcription_done(text, gen)
        elif event_type == EventType.TRANSCRIPTION_ERROR:
            msg, gen = self._split_gen(data)
            self.on_error(f"Transcription: {msg}", gen)
        elif event_type == EventType.INJECTION_DONE:
            _payload, gen = self._split_gen(data)
            self.on_injection_done(gen)
        elif event_type == EventType.INJECTION_ERROR:
            msg, gen = self._split_gen(data)
            self.on_error(f"Injection: {msg}", gen)
        elif event_type == EventType.CHUNK_TRANSCRIBED:
            chunk_index, text, had_context = data if len(data) == 3 else (*data, False)
            preview = text[:30] + "..." if len(text) > 30 else text
            context_indicator = " →" if had_context else ""
            self.log(f"✓ Chunk {chunk_index + 1}{context_indicator}: \"{preview}\"")
        elif event_type == EventType.CHUNKED_TRANSCRIPTION_DONE:
            text, gen = self._split_gen(data)
            self.on_transcription_done(text, gen)
        elif event_type == EventType.LOG_MESSAGE:
            self._do_log(data)

    def on_hotkey(self):
        if self.app_state == AppState.IDLE:
            self.start_recording()
        elif self.app_state == AppState.RECORDING:
            self.stop_recording_and_process()
    
    def on_style_toggle(self, target_style=None):
        """
        Cycle through or set output styles (5 presets).
        
        Args:
            target_style: If None, cycle to next style. Otherwise set to specified style.
        """
        # Style cycle order: Minimal → Professional → Casual → AI Prompt → Personal
        STYLE_CYCLE = ["minimal", "professional", "casual", "dev", "personal"]
        STYLE_NAMES = {
            "minimal": "🎤 Minimal",
            "professional": "💼 Professional",
            "casual": "💬 Casual",
            "dev": "🖥️ Dev",
            "personal": "✨ Personal"
        }
        
        if target_style and target_style in STYLE_CYCLE:
            # Set specific style
            next_style = target_style
        else:
            # Cycle to next style
            current_style = self.config.get("output_tone", "professional")
            try:
                current_index = STYLE_CYCLE.index(current_style)
            except ValueError:
                current_index = 0
            
            next_index = (current_index + 1) % len(STYLE_CYCLE)
            next_style = STYLE_CYCLE[next_index]
        
        # Skip if already on this style
        if next_style == self.config.get("output_tone"):
            return
        
        # Update config
        self.config["output_tone"] = next_style
        save_config(self.config)
        
        # Log the change
        style_name = STYLE_NAMES.get(next_style, next_style.title())
        self.log(f"✎ Style: {style_name}")
        
        # Update overlay indicator
        if self._use_pyqt_overlay and self.overlay_controller:
            self.overlay_controller.send_command({"cmd": "style", "value": next_style})
        
        # Update main UI Style tab to match (smooth, no flicker)
        try:
            self._update_style_selection(next_style)
        except Exception:
            pass  # UI might not be ready

    def start_recording(self):
        try:
            self.log("🎤 Listening...")

            # New recording session: bump the generation so any still-in-flight work or
            # scheduled callbacks from a previous session are recognised as stale and ignored.
            self.session_generation += 1
            gen = self.session_generation
            if self._finish_injection_job is not None:
                try:
                    self.after_cancel(self._finish_injection_job)
                except Exception:
                    pass
                self._finish_injection_job = None

            # Update state FIRST for immediate feedback
            self.update_state(AppState.RECORDING)
            
            # Show floating indicator / overlay
            try:
                if self._use_pyqt_overlay and self.overlay_controller:
                    self.overlay_controller.show("listening")
                elif self.indicator:
                    self.indicator.show("Listening...", COLORS["state_recording"])
            except Exception as e:
                self.log(f"⚠ Indicator error: {e}")
            
            # Check if chunked mode is enabled
            # Note: Remote backends (Groq, OpenAI Whisper) handle long audio natively,
            # so we skip chunked mode for them to avoid prompt length issues
            backend = self.config.get("transcription_backend", "whisper_cpp")
            is_remote = backend in ("groq_whisper", "openai_whisper")
            use_chunked = self.config.get("chunked_mode", True) and not is_remote
            
            if use_chunked:
                self._start_chunked_recording(gen)
            else:
                if is_remote and self.config.get("chunked_mode", True):
                    self.log("ℹ️ Chunked mode skipped (cloud API handles long audio)")
                self.recorder.start()
            
            # Start duration update timer
            import time
            self._recording_start_time = time.time()
            self._update_recording_duration()
            
        except Exception as e:
            self.on_error(f"Microphone: {e}")
    
    def _start_chunked_recording(self, gen=None):
        """Start recording with chunked processing for indefinite duration."""
        self.chunk_transcriptions = []
        # Per-session chunk store: workers hold THIS list reference, so a stale worker can never
        # read/write a newer session's chunk state even if start_recording rebinds the attribute.
        store = self.chunk_transcriptions

        def on_chunk_ready(chunk_path: str, chunk_index: int):
            """Called when a chunk is ready for transcription."""
            self.log(f"📦 Chunk {chunk_index + 1} ready")
            # Submit chunk for transcription in background (tagged with this session's gen + store)
            self.transcription_executor.submit(
                self._transcribe_chunk, chunk_path, chunk_index, gen, store
            )
        
        self.chunked_recorder = ChunkedRecorder(
            sample_rate=self.config["sample_rate"],
            device=self._resolved_audio_device,
            preprocessing=self.config.get("audio_preprocessing", "light"),
            chunk_duration=self.config.get("chunk_duration", 30),
            chunk_overlap=self.config.get("chunk_overlap", 2),
            on_chunk_ready=on_chunk_ready,
        )
        self.chunked_recorder.start()
    
    def _transcribe_chunk(self, chunk_path: str, chunk_index: int, gen=None, store=None):
        """Transcribe a single chunk in the background."""
        if store is None:
            store = self.chunk_transcriptions
        try:
            # Skip if this chunk belongs to a superseded session (optimisation — the per-session
            # `store` already isolates writes, so this only avoids wasted transcription work).
            if gen is not None and gen != self.session_generation:
                return
            # Get context from previous chunk for continuity
            context = ""
            if chunk_index > 0:
                with self.chunk_transcription_lock:
                    if len(store) >= chunk_index:
                        prev_text = store[chunk_index - 1]
                        if prev_text and prev_text != "[error]":
                            context = prev_text

            # Skip post-processing per-chunk - will be applied to final combined text
            text = transcribe_with_config(
                chunk_path,
                self.config,
                context=context,
                skip_post_processing=True,
            )
            # Write into this session's private store — never touches a newer session's list.
            with self.chunk_transcription_lock:
                while len(store) <= chunk_index:
                    store.append("")
                store[chunk_index] = text.strip() if text.strip() else "[empty]"
            
            # Log with context indicator for chunks after the first
            if chunk_index > 0 and context:
                self.event_queue.put((EventType.CHUNK_TRANSCRIBED, (chunk_index, text, True)))  # True = had context
            else:
                self.event_queue.put((EventType.CHUNK_TRANSCRIBED, (chunk_index, text, False)))
        except Exception as e:
            self.log(f"⚠ Chunk {chunk_index + 1} error: {e}")
            # Mark chunk as failed so this session's finalizer doesn't wait forever. Writes go to
            # the per-session store, so this can't poison a newer session's chunk count.
            with self.chunk_transcription_lock:
                while len(store) <= chunk_index:
                    store.append("")
                store[chunk_index] = "[error]"
    
    def _update_recording_duration(self):
        """Update the status label with recording duration."""
        if self.app_state != AppState.RECORDING or self._recording_start_time is None:
            return
        
        elapsed = time.time() - self._recording_start_time
        minutes = int(elapsed // 60)
        seconds = int(elapsed % 60)
        
        if minutes > 0:
            duration_text = f"Listening... {minutes}m {seconds}s"
        else:
            duration_text = f"Listening... {seconds}s"
        
        self.status_label.configure(text=duration_text)
        
        # Schedule next update
        self._duration_update_job = self.after(1000, self._update_recording_duration)

    def stop_recording_and_process(self):
        # Cancel duration update timer
        if self._duration_update_job:
            self.after_cancel(self._duration_update_job)
            self._duration_update_job = None
        self._recording_start_time = None
        
        # Track when we entered processing state for minimum display time
        import time as time_module
        self._processing_start_time = time_module.time()
        
        # Update floating indicator / overlay to processing FIRST
        if self._use_pyqt_overlay and self.overlay_controller:
            self.overlay_controller.update("processing")
        elif self.indicator:
            self.indicator.update("Processing...", COLORS["accent_yellow"])
        
        self.update_state(AppState.PROCESSING)

        gen = self.session_generation  # this recording's session id (set in start_recording)
        try:
            # Check which recorder was used
            if self.chunked_recorder is not None and self.chunked_recorder.is_recording():
                self._stop_chunked_recording(gen)
            else:
                self._stop_simple_recording(gen)
        except Exception as e:
            self.on_error(f"Processing: {e}", gen)
    
    def _stop_simple_recording(self, gen=None):
        """Stop simple (non-chunked) recording and process."""
        duration = self.recorder.get_duration()
        self.log(f"⏱ Duration: {duration:.1f}s")

        if duration < self.config["min_recording_duration"]:
            self.recorder.stop()
            self.recorder.cleanup()
            self.on_error("Too short - speak longer", gen)
            return

        audio_path = self.recorder.stop()
        self.executor.submit(self.transcribe_and_inject, audio_path, gen)
    
    def _stop_chunked_recording(self, gen=None):
        """Stop chunked recording and process all chunks."""
        # Capture the recorder + chunk store HERE (Tk thread, at stop time) so they unambiguously
        # belong to this session — the finalizer runs later on a (possibly delayed) worker, by
        # which time self.chunked_recorder / self.chunk_transcriptions may belong to a new session.
        recorder = self.chunked_recorder
        store = self.chunk_transcriptions

        # Stop FIRST so the chunk-monitor thread is joined and the chunk count is stable. Reading
        # get_chunk_count() before stop() can race a final on_chunk_ready (duplicate chunk index
        # / undercounted expected_chunks).
        final_path, all_paths = recorder.stop()
        duration = recorder.get_duration()
        chunk_count = recorder.get_chunk_count()
        self.log(f"⏱ Duration: {duration:.1f}s ({chunk_count} chunks)")

        if duration < self.config["min_recording_duration"]:
            recorder.cleanup()
            if self.chunked_recorder is recorder:
                self.chunked_recorder = None
            self.on_error("Too short - speak longer", gen)
            return

        # Submit final chunk for transcription if exists
        if final_path:
            final_index = chunk_count
            self.log(f"📦 Final chunk ready")
            self.transcription_executor.submit(
                self._transcribe_chunk, final_path, final_index, gen, store
            )

        # Wait for all transcriptions to complete and combine
        self.executor.submit(
            self._finalize_chunked_transcription,
            chunk_count + (1 if final_path else 0), gen, store, recorder,
        )
    
    def _finalize_chunked_transcription(self, expected_chunks: int, gen=None, store=None, recorder_instance=None):
        """Wait for all chunks to be transcribed and combine them.

        `store` and `recorder_instance` are captured at stop time (Tk thread) and belong
        unambiguously to this session, so a delayed finalizer never touches a newer session's
        chunk list or recorder.
        """
        emitted = False
        if store is None:
            store = self.chunk_transcriptions
        try:
            # Wait for all chunks to be transcribed (with timeout)
            timeout = 120  # 2 minutes max wait
            start_time = time.time()

            while True:
                # Bail early if this session was superseded (new recording / force_reset) — saves
                # waiting/work; the per-session store/recorder make it safe regardless.
                if gen is not None and gen != self.session_generation:
                    return
                with self.chunk_transcription_lock:
                    completed = len([t for t in store if t])

                if completed >= expected_chunks:
                    break

                if time.time() - start_time > timeout:
                    self.log(f"⚠ Timeout: only {completed}/{expected_chunks} chunks transcribed")
                    break

                time.sleep(0.5)

            if gen is not None and gen != self.session_generation:
                return

            # Combine all transcriptions with overlap deduplication
            with self.chunk_transcription_lock:
                combined_text = self._deduplicate_overlap_text(store)

            # Apply post-processing to the final combined text (not per-chunk)
            # This gives the LLM full context and avoids per-chunk prompt leakage issues
            if combined_text.strip() and self.config.get("post_processing_enabled", True):
                try:
                    from wayfinder.core.postprocessor import process_with_config
                    self.log("🔧 Post-processing combined text...")
                    original_text = combined_text
                    combined_text = process_with_config(combined_text, self.config)
                    if combined_text != original_text:
                        self.log(f"✓ Text cleaned ({len(original_text)} → {len(combined_text)} chars)")
                except Exception as e:
                    self.log(f"⚠ Post-processing error: {e}")
                    # Continue with original combined text

            # Re-check AFTER the (potentially slow) post-processing: a new chunked recording may
            # have started during the LLM call. If so, bail — the finally cleans up our own
            # recorder without touching the newer session's recorder/state.
            if gen is not None and gen != self.session_generation:
                return

            if combined_text.strip():
                self.log(f"📝 \"{combined_text[:50]}{'...' if len(combined_text) > 50 else ''}\"")
                self.event_queue.put((EventType.CHUNKED_TRANSCRIPTION_DONE, (combined_text, gen)))
            else:
                self.event_queue.put((EventType.TRANSCRIPTION_ERROR, ("No speech detected", gen)))
            emitted = True
        except Exception as e:
            self.event_queue.put((EventType.TRANSCRIPTION_ERROR, (f"Finalize failed: {e}", gen)))
            emitted = True
        finally:
            # Clean up THIS session's recorder instance (idempotent), and only detach the shared
            # handle if it still points at our instance — a newer session may have replaced it.
            if recorder_instance is not None:
                try:
                    recorder_instance.cleanup()
                except Exception:
                    pass
            if self.chunked_recorder is recorder_instance:
                self.chunked_recorder = None
            # Guarantee the overlay/app is never left stuck in PROCESSING: if no terminal event
            # was emitted and this session is still current, emit one now.
            if not emitted and (gen is None or gen == self.session_generation):
                self.event_queue.put((EventType.TRANSCRIPTION_ERROR, ("Transcription did not complete", gen)))
    
    def _deduplicate_overlap_text(self, transcriptions: list[str]) -> str:
        """
        Combine chunk transcriptions while removing duplicated text at boundaries.
        
        Due to the overlap between chunks, some words/phrases may appear in both
        the end of one chunk and the start of the next. This method removes such
        duplicates to create a clean combined transcription.
        
        Args:
            transcriptions: List of transcribed text from each chunk
            
        Returns:
            Combined text with overlapping duplicates removed
        """
        if not transcriptions:
            return ""
        
        # Filter out empty transcriptions and error/empty markers
        valid_transcriptions = [
            t.strip() for t in transcriptions 
            if t and t.strip() and t.strip() not in ("[error]", "[empty]")
        ]
        
        if not valid_transcriptions:
            return ""
        
        if len(valid_transcriptions) == 1:
            return valid_transcriptions[0]
        
        # Start with the first chunk
        combined = valid_transcriptions[0]
        
        for i in range(1, len(valid_transcriptions)):
            next_chunk = valid_transcriptions[i]
            
            if not next_chunk:
                continue
            
            # Find overlap between end of combined and start of next_chunk
            overlap = self._find_text_overlap(combined, next_chunk)
            
            if overlap:
                # Skip the overlapping part from the next chunk
                combined += " " + next_chunk[len(overlap):].lstrip()
            else:
                # No overlap found, just append with space
                combined += " " + next_chunk
        
        # Clean up multiple spaces
        import re
        combined = re.sub(r'\s+', ' ', combined).strip()
        
        return combined
    
    def _find_text_overlap(self, text1: str, text2: str, min_words: int = 2, max_words: int = 15) -> str:
        """
        Find overlapping text between end of text1 and start of text2.
        
        Args:
            text1: First text (look at the end)
            text2: Second text (look at the start)
            min_words: Minimum words for a valid overlap
            max_words: Maximum words to check for overlap
            
        Returns:
            The overlapping text, or empty string if no overlap found
        """
        # Split into words
        words1 = text1.split()
        words2 = text2.split()
        
        if len(words1) < min_words or len(words2) < min_words:
            return ""
        
        # Check for overlapping sequences of words
        # Start with longer sequences (more confident matches)
        for overlap_len in range(min(max_words, len(words1), len(words2)), min_words - 1, -1):
            # Get last N words of text1
            end_of_text1 = " ".join(words1[-overlap_len:]).lower()
            # Get first N words of text2
            start_of_text2 = " ".join(words2[:overlap_len]).lower()
            
            if end_of_text1 == start_of_text2:
                # Found overlap - return the actual text from text2 (preserves casing)
                return " ".join(words2[:overlap_len])
        
        # Also check for partial word overlaps (fuzzy matching)
        # This helps when transcription slightly differs at boundaries
        for overlap_len in range(min(max_words, len(words1), len(words2)), min_words - 1, -1):
            end_words = [w.lower().strip('.,!?;:') for w in words1[-overlap_len:]]
            start_words = [w.lower().strip('.,!?;:') for w in words2[:overlap_len]]
            
            # Check if at least 80% of words match
            matches = sum(1 for a, b in zip(end_words, start_words) if a == b)
            if matches >= overlap_len * 0.8:
                return " ".join(words2[:overlap_len])
        
        return ""

    def transcribe_and_inject(self, audio_path, gen=None):
        import time as time_module
        try:
            self.log("🔄 Transcribing...")
            trans_start = time_module.perf_counter()
            text = transcribe_with_config(audio_path, self.config)
            trans_elapsed = time_module.perf_counter() - trans_start
            self.log(f"📝 Transcribed in {trans_elapsed:.2f}s: \"{text[:40]}{'...' if len(text) > 40 else ''}\"")
            self.event_queue.put((EventType.TRANSCRIPTION_DONE, (text, gen)))
        except Exception as e:
            self.event_queue.put((EventType.TRANSCRIPTION_ERROR, (str(e), gen)))
        finally:
            # Delete THIS session's audio file specifically — not self.recorder.cleanup(), which
            # wipes the recorder's *current* temp files and could delete a newer session's audio
            # (the recorder instance is reused across sessions).
            try:
                Path(audio_path).unlink(missing_ok=True)
            except Exception:
                pass

    def on_transcription_done(self, text, gen=None):
        # Ignore results from a superseded session (force_reset / a newer recording).
        if gen is not None and gen != self.session_generation:
            self.log("⏭ Ignoring stale transcription (session changed)")
            return
        if not text.strip():
            self.on_error("No speech detected", gen)
            return

        # Add to voice learning history when "Personal" style is active
        if self.config.get("output_tone") == "personal":
            self._add_to_voice_learning(text.strip())

        # Note: Post-processing is already applied in transcribe_with_config()
        # The text received here is already fully processed
        processed_text = text.strip()
        
        # Store and display in Dictate tab
        self.last_transcription = processed_text
        if hasattr(self, 'transcription_label'):
            self.transcription_label.configure(
                text=self.last_transcription,
                text_color=COLORS["text_primary"],
            )
        
        self.update_state(AppState.PASTING)
        g = gen if gen is not None else self.session_generation
        self.executor.submit(self.do_inject, processed_text, g)

    def do_inject(self, text, gen=None):
        # Don't inject for a superseded session (force_reset / a newer recording took over).
        if gen is not None and gen != self.session_generation:
            return
        try:
            # Replace newlines with spaces to avoid sending Enter keys via ydotool
            # This prevents unwanted line breaks and accidental form submissions
            # Collapse multiple spaces and strip whitespace
            import re
            text = text.replace("\n", " ").replace("\r", " ")
            text = re.sub(r'\s+', ' ', text).strip()

            if not text:
                self.event_queue.put((EventType.LOG_MESSAGE, "⚠ Empty text after cleanup — nothing to inject"))
                self.event_queue.put((EventType.INJECTION_DONE, (None, gen)))
                return

            # Brief delay to let focus settle back to the user's target window
            # On macOS the overlay uses WindowDoesNotAcceptFocus so focus rarely shifts,
            # but 30ms provides a safety margin for OS-level focus handling
            import time as _time
            _time.sleep(0.03)

            # Re-check after the delay: a reset / new recording during the sleep must NOT result
            # in stale text being typed into whatever window the user has now focused.
            if gen is not None and gen != self.session_generation:
                return

            inject_text(text, typing_speed="instant")
            self.event_queue.put((EventType.INJECTION_DONE, (None, gen)))
        except Exception as e:
            self.event_queue.put((EventType.INJECTION_ERROR, (str(e), gen)))

    def on_injection_done(self, gen=None):
        # Ignore completion of a superseded session (force_reset / newer recording).
        if gen is not None and gen != self.session_generation:
            return
        self.log("✓ Text inserted")

        # Ensure processing state was visible for at least 800ms
        # This prevents the overlay from flashing too quickly
        import time as time_module
        if hasattr(self, '_processing_start_time') and self._processing_start_time:
            elapsed_ms = (time_module.time() - self._processing_start_time) * 1000
            min_display_ms = 800
            if elapsed_ms < min_display_ms:
                remaining_ms = int(min_display_ms - elapsed_ms)
                # Schedule the ready state after remaining time. Capture gen so a stale delayed
                # reset can't clobber a newer recording; track the job id so it can be cancelled.
                self._finish_injection_job = self.after(remaining_ms, lambda: self._finish_injection(gen))
                return

        self._finish_injection(gen)

    def _finish_injection(self, gen=None, _retries=0):
        """Complete the injection and return overlay to ready state."""
        self._finish_injection_job = None
        # A newer session already took over — don't reset it back to idle.
        if gen is not None and gen != self.session_generation:
            return
        self._processing_start_time = None
        # Return overlay to ready state, and verify the command actually reached the overlay
        # (a dropped critical command would otherwise leave the overlay stuck visually).
        if self._use_pyqt_overlay and self.overlay_controller:
            ok = self.overlay_controller.show("ready")  # Return to grey ready state
            if ok is False and _retries < 3:
                self.log("⚠ Overlay reset command failed to send — retrying")
                self._finish_injection_job = self.after(150, lambda: self._finish_injection(gen, _retries + 1))
                return
            # After a few failed retries, fall through and reset app state anyway — the overlay's
            # own watchdog / the health-check supervisor will recover the overlay separately.
        elif self.indicator:
            self.indicator.hide()
        self.update_state(AppState.IDLE)

    def on_error(self, message, gen=None):
        # Ignore errors from a superseded session.
        if gen is not None and gen != self.session_generation:
            self.log(f"⏭ Ignoring stale error (session changed): {message}")
            return
        self.log(f"⚠ {message}")

        # Ensure processing state was visible for at least 800ms
        import time as time_module
        if hasattr(self, '_processing_start_time') and self._processing_start_time:
            elapsed_ms = (time_module.time() - self._processing_start_time) * 1000
            min_display_ms = 800
            if elapsed_ms < min_display_ms:
                remaining_ms = int(min_display_ms - elapsed_ms)
                self._finish_injection_job = self.after(remaining_ms, lambda: self._finish_injection(gen))
                return

        self._finish_injection(gen)

    def force_reset(self):
        """Explicit recovery: abandon any in-flight dictation and return to a clean IDLE state.

        Safe to call from any thread (tray callbacks run off the Tk thread); the work is
        marshalled onto the Tk thread. This is a deliberate user-initiated escape hatch — it is
        NOT wired into the mic button (which keeps its normal stop-and-process toggle) and is NOT
        auto-invoked by the supervisor.
        """
        self.after(0, self._do_force_reset)

    def _do_force_reset(self):
        # Bump the generation so every in-flight worker + scheduled callback becomes stale.
        self.session_generation += 1
        self.log("🧹 Reset — returning to idle")

        # Cancel any pending delayed overlay reset / recording duration timer.
        for attr in ("_finish_injection_job", "_duration_update_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._recording_start_time = None
        self._processing_start_time = None

        # Stop any active recorder.
        try:
            if getattr(self, "chunked_recorder", None) is not None:
                try:
                    self.chunked_recorder.stop()
                except Exception:
                    pass
                try:
                    self.chunked_recorder.cleanup()
                except Exception:
                    pass
                self.chunked_recorder = None
            elif getattr(self, "recorder", None) is not None:
                try:
                    self.recorder.stop()
                except Exception:
                    pass
                try:
                    self.recorder.cleanup()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"⚠ Reset recorder cleanup: {e}")

        # Stop the audio-level flood and return the overlay to ready. update() (unlike show())
        # stops audio polling when leaving the listening state — this is what unsticks a frozen
        # "Listening…" overlay.
        try:
            if self._use_pyqt_overlay and self.overlay_controller:
                self.overlay_controller.update("ready")
            elif self.indicator:
                self.indicator.hide()
        except Exception as e:
            self.log(f"⚠ Reset overlay: {e}")

        self.update_state(AppState.IDLE)

    def _add_to_voice_learning(self, text: str):
        """Add transcription to voice learning history."""
        try:
            from wayfinder.core.voice_profile import get_voice_profile
            
            voice_profile = get_voice_profile(
                history_limit=self.config.get("voice_learning_history_limit", 100),
                regen_interval=self.config.get("voice_learning_regen_interval", 20),
            )
            
            # Create LLM callback for profile regeneration (uses post-processing backend)
            llm_callback = None
            if self.config.get("post_processing_enabled", True):
                llm_callback = self._get_llm_callback_for_voice_learning()
            
            voice_profile.add_transcription(text, llm_callback=llm_callback)
            
        except Exception as e:
            print(f"[Voice Learning] ⚠ Error: {e}")
    
    def _get_llm_callback_for_voice_learning(self):
        """Create an LLM callback for voice profile regeneration."""
        def call_llm(prompt: str) -> str:
            try:
                from wayfinder.core.postprocessor import get_backend
                backend = get_backend(self.config)
                if backend.is_available():
                    # Use backend's process method with our custom prompt
                    # The prompt is already complete, just need to run it through
                    return backend.process("", prompt)
                return ""
            except Exception as e:
                print(f"[Voice Learning] LLM call failed: {e}")
                return ""
        return call_llm


def _check_single_instance() -> bool:
    """Check if another instance is already running using a lock file.
    
    Returns True if this is the only instance, False if another is running.
    """
    lock_file = CONFIG_DIR / "wayfinder.lock"
    pid_to_check = None
    
    # Ensure config dir exists
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        # Check if lock file exists and contains a valid PID
        if lock_file.exists():
            try:
                stored_pid = int(lock_file.read_text().strip())
                pid_to_check = stored_pid
                
                # Check if that process is still running
                if stored_pid > 0:
                    try:
                        os.kill(stored_pid, 0)  # Signal 0 = check if process exists
                        # Process exists - check if it's actually wayfinder
                        cmdline_path = Path(f"/proc/{stored_pid}/cmdline")
                        if cmdline_path.exists():
                            cmdline = cmdline_path.read_text()
                            if "wayfinder" in cmdline.lower():
                                # Another instance is running
                                print(f"⚠️ Wayfinder Aura is already running (PID {stored_pid})")
                                return False
                    except (OSError, ProcessLookupError):
                        # Process doesn't exist, we can take over
                        pass
            except (ValueError, FileNotFoundError):
                # Invalid lock file, we can take over
                pass
        
        # Write our PID to the lock file
        lock_file.write_text(str(os.getpid()))
        return True
        
    except Exception as e:
        print(f"⚠️ Lock file error: {e}")
        return True  # Proceed anyway on error


def main():
    
    # === SINGLE INSTANCE CHECK ===
    if not _check_single_instance():
        print("Use 'pkill -f wayfinder' to stop existing instances.")
        return
    
    # === STARTUP CLEANUP: Kill any ghost overlay processes from previous runs ===
    try:
        # Kill any leftover overlay processes
        subprocess.run(["pkill", "-9", "-f", "overlay.py"], 
                      capture_output=True, timeout=2)
    except:
        pass
    
    app = None
    
    def cleanup_all():
        """Comprehensive cleanup for all threads, executors, and subprocesses."""
        nonlocal app
        try:
            if app:
                # Signal all threads to stop
                app.stop_event.set()
                
                # Shutdown thread pool executors gracefully
                if hasattr(app, 'executor'):
                    app.executor.shutdown(wait=False, cancel_futures=True)
                if hasattr(app, 'transcription_executor'):
                    app.transcription_executor.shutdown(wait=False, cancel_futures=True)
                
                # Clean up overlay
                app._cleanup_overlay()
                
                # Clean up recorder if active
                if hasattr(app, 'recorder') and app.recorder:
                    try:
                        app.recorder.cleanup()
                    except:
                        pass
                if hasattr(app, 'chunked_recorder') and app.chunked_recorder:
                    try:
                        app.chunked_recorder.cleanup()
                    except:
                        pass
        except:
            pass
        
        # Force kill any overlay processes as safety net
        try:
            subprocess.run(["pkill", "-9", "-f", "overlay.py"], 
                          capture_output=True, timeout=1)
        except:
            pass
        
        # Remove the lock file
        try:
            lock_file = CONFIG_DIR / "wayfinder.lock"
            if lock_file.exists():
                lock_file.unlink()
        except:
            pass
    
    def signal_handler(signum, frame):
        """Handle SIGTERM/SIGINT for graceful shutdown."""
        cleanup_all()
        os._exit(0)
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Register atexit cleanup as additional safety net
    atexit.register(cleanup_all)
    
    try:
        app = WayfinderApp()
        
        app.mainloop()
    except KeyboardInterrupt:
        cleanup_all()
    except Exception as e:
        cleanup_all()
        raise


if __name__ == "__main__":
    main()

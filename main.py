#!/usr/bin/env python3
"""
Wayfinder Voice - Local voice dictation for Bazzite (Wayland)
Toggle-to-record with whisper.cpp transcription.
"""

import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

SOCKET_PATH = "/tmp/wayfinder-voice.sock"

import customtkinter as ctk
import evdev
from evdev import InputDevice, categorize, ecodes
from PIL import Image, ImageDraw, ImageFilter
import pystray

# D-Bus for Wayland GlobalShortcuts
try:
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
    DBUS_AVAILABLE = True
except ImportError:
    DBUS_AVAILABLE = False

from injector import inject_text, InjectionError
from recorder import AudioRecorder, ChunkedRecorder
from transcriber import transcribe_with_config, TranscriptionError


# === Configuration ===

# Detect Flatpak environment
IS_FLATPAK = os.environ.get("FLATPAK_ID") is not None or os.environ.get("WAYFINDER_FLATPAK") is not None

CONFIG_DIR = Path.home() / ".config" / "wayfinder-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
SCRIPT_DIR = Path(__file__).parent.resolve()

# Handle icon path for Flatpak vs regular install
if IS_FLATPAK:
    ICON_PATH = Path("/app/share/icons/hicolor/256x256/apps") / f"{os.environ.get('FLATPAK_ID', 'io.github.user.WayfinderVoice')}.png"
    if not ICON_PATH.exists():
        ICON_PATH = SCRIPT_DIR / "assets" / "icon.png"
else:
    ICON_PATH = SCRIPT_DIR / "assets" / "icon.png"

# Default whisper paths - Flatpak uses bundled binary and models
if IS_FLATPAK:
    _default_whisper_binary = "/app/bin/whisper-cli"
    _default_model_dir = os.environ.get("WHISPER_MODELS_DIR", "/app/share/whisper-models")
    _default_model_path = f"{_default_model_dir}/ggml-small.en.bin"
else:
    _default_whisper_binary = "~/whisper.cpp/build/bin/whisper-cli"
    _default_model_path = "~/whisper.cpp/models/ggml-large-v3-turbo.bin"

DEFAULT_CONFIG = {
    "whisper_binary": _default_whisper_binary,
    "model_path": _default_model_path,
    "hotkey_key": 67,  # F9 - works reliably on Bazzite/KDE
    "hotkey_modifiers": [],
    "audio_device": None,
    "sample_rate": 16000,
    "prompt": "Hello, this is a dictation with proper punctuation and grammar.",
    "threads": 6,
    "timeout": 120,
    "min_recording_duration": 0.5,
    "start_minimized": False,
    "enabled_input_devices": [],  # Empty = all devices; otherwise list of device names
    "typing_speed": "instant",  # instant, fast, normal, slow, very_slow
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
    # Chunked recording settings
    "chunked_mode": True,  # Enable chunked processing for long recordings
    "chunk_duration": 15,  # Seconds per chunk (shorter = faster feedback)
    "chunk_overlap": 2,  # Overlap seconds to avoid word cuts
    "max_recording_duration": 0,  # 0 = unlimited
    # GPU acceleration settings
    "transcription_backend": "whisper_cpp",  # whisper_cpp | faster_whisper
    "use_gpu": True,  # Enable GPU acceleration
    "gpu_layers": 0,  # 0 = auto (all layers), or specific layer count for whisper.cpp
    # Faster-Whisper specific settings
    "faster_whisper_model": "large-v3-turbo",  # tiny, base, small, medium, large-v3, large-v3-turbo
    "faster_whisper_compute_type": "float16",  # float16, int8, int8_float16
    # Floating indicator settings
    "indicator_fps": 0,  # 0 = auto-detect monitor refresh rate, or set manually (60, 120, 144, etc.)
}

KEY_CODES = {
    "f1": 59, "f2": 60, "f3": 61, "f4": 62, "f5": 63, "f6": 64,
    "f7": 65, "f8": 66, "f9": 67, "f10": 68, "f11": 87, "f12": 88,
    "space": 57, "enter": 28, "tab": 15, "backspace": 14,
    "scrolllock": 70, "pause": 119,
}

MODIFIER_CODES = {
    "ctrl": [29, 97], "alt": [56, 100], "shift": [42, 54], "super": [125, 126],
}


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
        self.vendor = vendor  # "nvidia", "amd", "intel", "unknown"
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


def detect_gpu() -> GPUInfo:
    """
    Detect the primary GPU on the system.
    
    Returns:
        GPUInfo with vendor, name, and driver information.
    """
    try:
        # Try lspci first (most reliable on Linux)
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        
        if result.returncode == 0:
            lines = result.stdout.lower()
            
            # Check for discrete GPUs first (VGA/3D controllers)
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
                        if vendor_id == "0x10de":  # NVIDIA
                            return GPUInfo("nvidia", "NVIDIA GPU", "nvidia")
                        elif vendor_id == "0x1002":  # AMD
                            return GPUInfo("amd", "AMD GPU", "amdgpu")
                        elif vendor_id == "0x8086":  # Intel
                            return GPUInfo("intel", "Intel GPU", "i915")
        
    except Exception:
        pass
    
    return GPUInfo("unknown", "Unknown GPU", "")


def get_optimal_thread_count() -> int:
    """Get optimal thread count based on CPU cores."""
    try:
        cpu_count = os.cpu_count() or 4
        # Use ~75% of cores, minimum 2, maximum 16
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


# Wayfinder OS Color Palette - Premium dark theme with cyan accents
COLORS = {
    # Backgrounds - deep space dark with subtle warmth
    "bg_base": "#0D0D12",           # Near-black base
    "bg_dark": "#0D0D12",           # Alias for compatibility
    "bg_card": "#141419",           # Card surfaces
    "bg_surface": "#141419",        # Alias
    "bg_hover": "#1E1E26",          # Hover states
    "bg_elevated": "#252530",       # Elevated elements
    
    # Accent - Wayfinder cyan
    "accent": "#00D4FF",            # Primary cyan
    "accent_cyan": "#00D4FF",       # Alias
    "accent_glow": "#0099BB",       # Glow/shadow color
    "accent_dim": "#007799",        # Dimmed accent
    
    # State colors - refined palette
    "accent_green": "#00E5A0",      # Success/ready - softer green
    "accent_red": "#FF4466",        # Recording - coral red
    "accent_yellow": "#FFB830",     # Processing - warm amber
    "accent_blue": "#00D4FF",       # Typing - matches accent
    
    # Text hierarchy
    "text_bright": "#FFFFFF",       # Maximum contrast
    "text_primary": "#E8E8F0",      # Primary text
    "text_secondary": "#8888A0",    # Secondary text
    "text_muted": "#505068",        # Muted/disabled
    
    # Borders and dividers
    "border": "#2A2A3A",            # Subtle borders
    "border_glow": "#00D4FF33",     # Glowing border (with alpha)
}

STATE_COLORS = {
    AppState.IDLE: COLORS["accent"],           # Cyan when ready
    AppState.RECORDING: COLORS["accent_red"],   # Red when recording
    AppState.PROCESSING: COLORS["accent_yellow"], # Amber when processing
    AppState.PASTING: COLORS["accent_green"],   # Green when typing
}

STATE_LABELS = {
    AppState.IDLE: "Ready",
    AppState.RECORDING: "Listening",
    AppState.PROCESSING: "Processing",
    AppState.PASTING: "Typing",
}


# === Tooltip Helper ===

class ToolTip:
    """Hover tooltip for CustomTkinter widgets."""
    
    def __init__(self, widget, text, delay=400):
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
        
        # Create tooltip window first to get its dimensions
        self.tooltip_window = tw = ctk.CTkToplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)
        
        # Tooltip frame with styling
        frame = ctk.CTkFrame(
            tw,
            fg_color=COLORS["bg_elevated"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border"],
        )
        frame.pack(fill="both", expand=True)
        
        label = ctk.CTkLabel(
            frame,
            text=self.text,
            font=("Inter", 11),
            text_color=COLORS["text_secondary"],
            wraplength=280,
            justify="left",
            padx=12,
            pady=8,
        )
        label.pack()
        
        # Position the tooltip ABOVE the widget with clearance
        tw.update_idletasks()
        tw_width = tw.winfo_width()
        tw_height = tw.winfo_height()
        screen_width = tw.winfo_screenwidth()
        
        x = self.widget.winfo_rootx()
        # Position above the widget with 8px gap
        y = self.widget.winfo_rooty() - tw_height - 8
        
        # Adjust if tooltip would go off-screen horizontally
        if x + tw_width > screen_width:
            x = screen_width - tw_width - 10
        
        # If tooltip would go above screen top, show below instead
        if y < 0:
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        
        tw.wm_geometry(f"+{x}+{y}")
    
    def hide_tooltip(self):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None


# Setting tooltip descriptions with latency indicators
# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY GUIDE: ⚡ = none, 🟢 = <10ms, 🟡 = 10-100ms, 🔴 = 100ms+, 🚀 = speedup
# ═══════════════════════════════════════════════════════════════════════════════
SETTING_TOOLTIPS = {
    # ⚡ No latency impact - UI/configuration only
    "hotkey": "The keyboard shortcut to start/stop voice recording.\n⚡ Latency: None",
    "microphone": "Select which microphone/audio input device to use.\n⚡ Latency: None",
    "hotkey_devices": "Which keyboards, mice, or keypads can trigger the hotkey.\n⚡ Latency: None",
    "start_minimized": "Start the app minimized to the system tray.\n⚡ Latency: None",
    "ui_scale": "Adjust the size of the user interface.\n⚡ Latency: None",
    "prompt": "Initial text that guides transcription style.\n⚡ Latency: None (processed at model load)",
    "language": "The language for transcription. English is most optimized.\n⚡ Latency: None",
    
    # 🟢 Minimal latency impact (<10ms per invocation)
    "typing_speed": "How fast text is typed out.\n🟢 Instant: 0ms | Fast: ~50ms | Normal: ~200ms | Slow: ~500ms per sentence",
    "ensure_punctuation": "Extra punctuation fixes if model output lacks periods/caps.\n🟢 Latency: +1-3ms (optional, most models handle this well)",
    "audio_preprocessing": "Audio signal processing before transcription.\n🟢 Off: 0ms | Light: +2ms | Medium: +5ms | Heavy: +10ms",
    
    # 🟡 Moderate latency impact (10-100ms)
    "chunked_mode": "Process in segments for unlimited recording length.\n🟡 Latency: +50-100ms overhead per chunk boundary",
    "chunk_duration": "Seconds per audio chunk (when chunked mode is on).\n🟡 Shorter = faster feedback, more overhead | 15s = balanced",
    
    # 🔴 MAJOR latency impact - These are the biggest factors
    "whisper_model": "AI model size - the #1 factor in transcription speed.\n🔴 GPU: Tiny ~0.5s | Base ~1s | Small ~1.5s | Medium ~3s | Large ~6s | Turbo ~2s\n🔴 CPU: Tiny ~2s | Base ~4s | Small ~6s | Medium ~12s | Large ~25s | Turbo ~8s",
    "accuracy_mode": "Speed vs accuracy preset - affects beam search depth.\n🔴 Fast: -40% time (beam=1) | Balanced: baseline (beam=5) | High: +60% time (beam=8)",
    "beam_size": "Search width for finding best transcription.\n🔴 1 = fastest (-50%) | 5 = balanced | 10 = slowest (+100%)",
    
    # GPU/Backend - Can dramatically change all timings
    "backend": "Transcription engine selection.\n⚙️ whisper.cpp: CPU-optimized, lower memory\n⚙️ Faster-Whisper: Better GPU utilization (up to 10x faster)",
    "gpu_acceleration": "Use GPU for transcription.\n🚀 Enabled: 3-10x faster than CPU (requires CUDA/ROCm/Vulkan)",
    "gpu_layers": "Model layers to offload to GPU.\n⚙️ Auto: Maximum speed | Fewer: Saves VRAM, slower",
}


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
    """Get all available audio input devices."""
    import sounddevice as sd
    devices = []
    try:
        all_devices = sd.query_devices()
        for idx, device in enumerate(all_devices):
            # Only include input devices (max_input_channels > 0)
            if device.get("max_input_channels", 0) > 0:
                devices.append({
                    "index": idx,
                    "name": device["name"],
                    "channels": device["max_input_channels"],
                    "sample_rate": int(device.get("default_samplerate", 16000)),
                    "is_default": idx == sd.default.device[0],  # default input device
                })
    except Exception as e:
        print(f"Error querying audio devices: {e}")
    return devices


class EventType(Enum):
    HOTKEY_PRESSED = auto()
    TRANSCRIPTION_DONE = auto()
    TRANSCRIPTION_ERROR = auto()
    INJECTION_DONE = auto()
    INJECTION_ERROR = auto()
    CHUNK_TRANSCRIBED = auto()  # A chunk was transcribed during recording
    CHUNKED_TRANSCRIPTION_DONE = auto()  # All chunks transcribed


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


def find_keyboard_devices(enabled_devices: list[str] = None) -> list[InputDevice]:
    """Find input devices to listen to based on config."""
    all_devices = get_all_input_devices()
    
    if enabled_devices:
        # Only use explicitly enabled devices
        return [d["device"] for d in all_devices if d["name"] in enabled_devices]
    else:
        # Default: use all available devices
        return [d["device"] for d in all_devices]


def hotkey_listener(event_queue, hotkey_key, hotkey_modifiers, stop_event, enabled_devices=None, log_callback=None):
    import select
    
    def log(msg):
        if log_callback:
            try:
                log_callback(msg)
            except:
                pass
    
    devices = find_keyboard_devices(enabled_devices)
    if not devices:
        log("⚠️ No input devices found!")
        log("   Check: sudo usermod -aG input $USER")
        return

    log(f"🎮 Monitoring {len(devices)} input device(s):")
    for dev in devices:
        short_name = dev.name[:40] + "..." if len(dev.name) > 40 else dev.name
        log(f"   • {short_name}")
    
    required_modifiers = set()
    for name in hotkey_modifiers:
        if name.lower() in MODIFIER_CODES:
            required_modifiers.update(MODIFIER_CODES[name.lower()])
    
    pressed_modifiers = set()
    all_modifier_codes = set()
    for codes in MODIFIER_CODES.values():
        all_modifier_codes.update(codes)

    # Map file descriptors to devices
    fd_to_device = {dev.fd: dev for dev in devices}

    try:
        while not stop_event.is_set():
            # Wait for input from any device
            r, _, _ = select.select(list(fd_to_device.keys()), [], [], 0.5)
            
            for fd in r:
                device = fd_to_device[fd]
                for event in device.read():
                    if event.type == ecodes.EV_KEY:
                        key_event = categorize(event)
                        keycode = key_event.scancode
                        
                        if keycode in all_modifier_codes:
                            if key_event.keystate == 1:
                                pressed_modifiers.add(keycode)
                            elif key_event.keystate == 0:
                                pressed_modifiers.discard(keycode)
                        
                        if keycode == hotkey_key and key_event.keystate == 1:
                            if required_modifiers:
                                all_mods_pressed = all(
                                    pressed_modifiers & set(MODIFIER_CODES.get(mod.lower(), []))
                                    for mod in hotkey_modifiers
                                )
                                if all_mods_pressed:
                                    event_queue.put((EventType.HOTKEY_PRESSED, None))
                            else:
                                event_queue.put((EventType.HOTKEY_PRESSED, None))
    except Exception as e:
        log(f"⚠️ Hotkey error: {e}")


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
                if data == b"toggle":
                    log("🎯 Toggle received via socket")
                    event_queue.put((EventType.HOTKEY_PRESSED, None))
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
    """
    Wayland-compatible hotkey listener using XDG GlobalShortcuts portal.
    This is the proper way to do global hotkeys on Wayland/KDE.
    """
    def log(msg):
        if log_callback:
            try:
                log_callback(msg)
            except:
                pass
    
    if not DBUS_AVAILABLE:
        log("⚠️ D-Bus not available for Wayland shortcuts")
        return False
    
    try:
        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        
        portal = bus.get_object(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop"
        )
        
        shortcuts_iface = dbus.Interface(
            portal,
            "org.freedesktop.portal.GlobalShortcuts"
        )
        
        # Create session
        log("🔗 Connecting to GlobalShortcuts portal...")
        
        session_options = dbus.Dictionary({
            "handle_token": dbus.String("wayfinder_voice_session"),
            "session_handle_token": dbus.String("wayfinder_voice"),
        }, signature="sv")
        
        request_path = shortcuts_iface.CreateSession(session_options)
        log(f"✓ Session created")
        
        # Wait for session to be ready
        import time
        time.sleep(0.5)
        
        # Try to get the session
        session_path = f"/org/freedesktop/portal/desktop/session/{os.environ.get('USER', 'user')}/wayfinder_voice"
        
        # Bind shortcuts
        shortcuts = dbus.Array([
            dbus.Struct([
                dbus.String("record-toggle"),
                dbus.Dictionary({
                    "description": dbus.String("Toggle voice recording"),
                    "preferred_trigger": dbus.String(hotkey_display),
                }, signature="sv")
            ], signature="(sa{sv})")
        ], signature="(sa{sv})")
        
        bind_options = dbus.Dictionary({
            "handle_token": dbus.String("wayfinder_bind"),
        }, signature="sv")
        
        try:
            shortcuts_iface.BindShortcuts(
                dbus.ObjectPath(session_path),
                shortcuts,
                "",  # parent_window
                bind_options
            )
            log(f"✓ Shortcut registered: {hotkey_display}")
        except dbus.exceptions.DBusException as e:
            log(f"⚠️ Could not bind shortcut: {e}")
            log("   You may need to set it manually in System Settings")
        
        # Listen for activation signals
        def on_activated(session, shortcut_id, timestamp, options):
            if shortcut_id == "record-toggle":
                log(f"🎯 Hotkey activated!")
                event_queue.put((EventType.HOTKEY_PRESSED, None))
        
        def on_deactivated(session, shortcut_id, timestamp, options):
            pass
        
        bus.add_signal_receiver(
            on_activated,
            signal_name="Activated",
            dbus_interface="org.freedesktop.portal.GlobalShortcuts",
            path="/org/freedesktop/portal/desktop"
        )
        
        log("🎧 Listening for Wayland global shortcuts...")
        
        # Run GLib main loop
        loop = GLib.MainLoop()
        
        def check_stop():
            if stop_event.is_set():
                loop.quit()
                return False
            return True
        
        GLib.timeout_add(500, check_stop)
        loop.run()
        
        return True
        
    except Exception as e:
        log(f"⚠️ Wayland hotkey setup failed: {e}")
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
    """
    
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
        
        # Determine target FPS
        if target_fps <= 0:
            # Auto-detect from monitor
            self._target_fps = get_monitor_refresh_rate()
            self._fps_auto_detected = True
        else:
            self._target_fps = target_fps
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
        
        # Premium compact design colors
        pill_bg = "#18181f"  # Slightly darker for premium feel
        shadow_color = "#08080a"  # Deep shadow for depth
        
        # Parse accent color for dynamic glow
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        # Create a dimmer version for outer glow
        glow_r = int(r * 0.25 + 8)
        glow_g = int(g * 0.25 + 8)
        glow_b = int(b * 0.25 + 8)
        outer_glow_color = f"#{glow_r:02x}{glow_g:02x}{glow_b:02x}"
        
        self.window.configure(fg_color=shadow_color)
        
        # Outer glow/shadow frame - tight rounded pill
        self.glow_frame = ctk.CTkFrame(
            self.window,
            fg_color=shadow_color,
            corner_radius=20,
            border_width=2,
            border_color=outer_glow_color,
        )
        self.glow_frame.pack(padx=0, pady=0)
        
        # Main pill-shaped frame - ultra compact
        self.main_frame = ctk.CTkFrame(
            self.glow_frame,
            fg_color=pill_bg,
            corner_radius=16,
            border_width=2,
            border_color=color,
        )
        self.main_frame.pack(padx=2, pady=2)
        
        # Content container with waveform as background
        content = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        content.pack(padx=10, pady=6)
        
        # Single row: dot + label + waveform all together
        row = ctk.CTkFrame(content, fg_color="transparent")
        row.pack()
        
        # Glowing status indicator dot (smaller)
        self.dot_canvas = ctk.CTkCanvas(
            row,
            width=18,
            height=18,
            bg=pill_bg,
            highlightthickness=0,
        )
        self.dot_canvas.pack(side="left", padx=(0, 6))
        self._draw_dot(color)
        
        # Label with status text
        self.label = ctk.CTkLabel(
            row,
            text=text,
            font=("SF Pro Display", 12, "bold") if self._font_exists("SF Pro Display") else ("Inter", 12, "bold"),
            text_color=COLORS["text_bright"],
        )
        self.label.pack(side="left", padx=(0, 4))
        
        # Voice-reactive waveform - right next to text, fills remaining space
        self.wave_canvas = ctk.CTkCanvas(
            row,
            width=90,
            height=18,
            bg=pill_bg,
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
            
    def _draw_dot(self, color: str, scale: float = 1.0) -> None:
        """Draw an enhanced glowing status indicator with pulsing ring."""
        if not self.dot_canvas:
            return
            
        import math
        
        self.dot_canvas.delete("all")
        
        # Parse color
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        
        # Canvas is 18x18 (ultra compact)
        canvas_size = 18
        cx, cy = canvas_size // 2, canvas_size // 2
        
        # Background color for blending (pill_bg = #18181f)
        bg_r, bg_g, bg_b = 24, 24, 31
        
        # Expanding ring animation - ring grows outward and fades
        ring_progress = self._ring_phase % 1.0  # 0 to 1
        if ring_progress > 0:
            ring_radius = 4 + ring_progress * 6  # Expands from 4 to 10 (fits 18px canvas)
            ring_alpha = 0.6 * (1 - ring_progress)  # Fades out as it expands
            
            if ring_alpha > 0.05:
                ring_r = int(r * ring_alpha + bg_r * (1 - ring_alpha))
                ring_g = int(g * ring_alpha + bg_g * (1 - ring_alpha))
                ring_b = int(b * ring_alpha + bg_b * (1 - ring_alpha))
                ring_color = f"#{ring_r:02x}{ring_g:02x}{ring_b:02x}"
                
                self.dot_canvas.create_oval(
                    cx - ring_radius, cy - ring_radius,
                    cx + ring_radius, cy + ring_radius,
                    outline=ring_color,
                    width=1.5,
                    fill="",
                )
        
        # Draw intensified outer glow layers (sized for 18px canvas)
        glow_configs = [
            (8, 0.15),    # Outermost - subtle halo
            (6.5, 0.25),  # Outer glow
            (5, 0.4),     # Middle glow  
            (3.5, 0.55),  # Inner glow
            (2.5, 0.7),   # Core glow
        ]
        
        for base_radius, alpha in glow_configs:
            # Scale the glow with the pulse
            glow_radius = base_radius * scale
            
            # Mix color with background for alpha effect
            gr = int(r * alpha + bg_r * (1 - alpha))
            gg = int(g * alpha + bg_g * (1 - alpha))
            gb = int(b * alpha + bg_b * (1 - alpha))
            glow_color = f"#{gr:02x}{gg:02x}{gb:02x}"
            
            self.dot_canvas.create_oval(
                cx - glow_radius, cy - glow_radius,
                cx + glow_radius, cy + glow_radius,
                fill=glow_color,
                outline="",
            )
        
        # Draw bright core (scales more dramatically)
        core_radius = 2.5 * scale
        self.dot_canvas.create_oval(
            cx - core_radius, cy - core_radius,
            cx + core_radius, cy + core_radius,
            fill=color,
            outline="",
        )
        
        # Add bright highlight spot for premium look
        highlight_radius = 1 * scale
        highlight_offset = -0.6 * scale
        # Lighter version of color for highlight
        hr = min(255, r + 60)
        hg = min(255, g + 60)
        hb = min(255, b + 60)
        highlight_color = f"#{hr:02x}{hg:02x}{hb:02x}"
        
        self.dot_canvas.create_oval(
            cx + highlight_offset - highlight_radius,
            cy + highlight_offset - highlight_radius,
            cx + highlight_offset + highlight_radius,
            cy + highlight_offset + highlight_radius,
            fill=highlight_color,
            outline="",
        )
    
    def _draw_waveform(self, color: str) -> None:
        """Draw a HYPER voice-reactive waveform - exaggerated and grandiose!"""
        if not self.wave_canvas:
            return
            
        self.wave_canvas.delete("all")
        
        import math
        
        # Canvas dimensions (compact, inline with text)
        width = 90
        height = 18
        center_y = height // 2
        max_amp = height // 2  # Full amplitude - waves can touch the very edges!
        
        # Parse color for wave layers
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        
        # Background color for alpha blending (pill_bg = #18181f)
        bg_r, bg_g, bg_b = 24, 24, 31
        
        # HYPER voice-reactive - exaggerated response!
        audio_level = self._current_audio_level
        
        # Slightly larger base motion
        base_breath = 0.15 + 0.12 * (0.5 + 0.5 * math.sin(self._wave_breath))
        
        # MASSIVE voice response - exponential curve for dramatic effect
        # Even more sensitive - fills space faster
        voice_boost = (audio_level ** 0.6) * 15.0  # More exponential + bigger multiplier!
        amplitude_factor = base_breath + voice_boost
        
        # Cap at 1.0 which = max_amp (fills the entire space edge to edge)
        amplitude_factor = min(1.0, amplitude_factor)
        
        # Draw layered sine waves - dramatic and flowing
        wave_configs = [
            # (frequency, phase_offset, alpha)
            (0.07, 0.0, 0.2),       # Slow background wave
            (0.11, 1.0, 0.35),      # Medium wave
            (0.16, 2.2, 0.5),       # Faster wave
            (0.22, 0.7, 0.65),      # Quick wave
        ]
        
        for freq, phase_offset, alpha in wave_configs:
            # All waves use the same amplitude factor - fills the space!
            amp = max_amp * amplitude_factor
            
            # Build wave points
            points = []
            for x in range(0, width + 1, 2):
                # Combine sine waves for organic motion
                y = center_y + amp * math.sin(freq * x + self._wave_time + phase_offset)
                # Add harmonics for richness
                y += (amp * 0.4) * math.sin(freq * 2.3 * x + self._wave_time * 1.6 + phase_offset)
                y += (amp * 0.2) * math.sin(freq * 3.7 * x + self._wave_time * 2.1 + phase_offset * 0.5)
                # Clamp to canvas bounds (allow touching edges)
                y = max(0, min(height, y))
                points.append((x, y))
            
            # Blend color with background for alpha effect
            wr = int(r * alpha + bg_r * (1 - alpha))
            wg = int(g * alpha + bg_g * (1 - alpha))
            wb = int(b * alpha + bg_b * (1 - alpha))
            wave_color = f"#{wr:02x}{wg:02x}{wb:02x}"
            
            # Draw as smooth line
            if len(points) >= 2:
                flat_points = [coord for point in points for coord in point]
                self.wave_canvas.create_line(
                    *flat_points,
                    fill=wave_color,
                    width=2,
                    smooth=True,
                    splinesteps=10,
                )
        
        # Draw center line highlight (brightest, fills space when loud)
        highlight_amp = max_amp * amplitude_factor
        highlight_points = []
        for x in range(0, width + 1, 2):
            y = center_y + highlight_amp * math.sin(0.13 * x + self._wave_time * 1.4)
            y += (highlight_amp * 0.5) * math.sin(0.26 * x + self._wave_time * 2.0 + 0.8)
            # Clamp to canvas bounds (allow touching edges)
            y = max(0, min(height, y))
            highlight_points.append((x, y))
        
        if len(highlight_points) >= 2:
            flat_highlight = [coord for point in highlight_points for coord in point]
            self.wave_canvas.create_line(
                *flat_highlight,
                fill=color,
                width=2,
                smooth=True,
                splinesteps=10,
            )
        
    def _start_pulse(self) -> None:
        """Start the pulsing animation."""
        self._pulse_step()
        
    def _pulse_step(self) -> None:
        """Animate one pulse step with smooth sinusoidal easing and voice reactivity."""
        if not self._visible:
            return
        
        import math
            
        self.pulse_state = (self.pulse_state + 1) % 60  # 60 steps for ~2 second cycle at 30fps
        
        # Get audio level from callback (voice reactivity)
        if self._audio_level_callback:
            try:
                raw_level = self._audio_level_callback()
                # INSTANT response - no smoothing on attack for maximum impact!
                if raw_level > self._current_audio_level:
                    # Instant attack - respond IMMEDIATELY!
                    self._current_audio_level = raw_level
                else:
                    # Moderate decay - keeps energy visible
                    self._current_audio_level = self._current_audio_level * 0.75 + raw_level * 0.25
            except:
                self._current_audio_level *= 0.85  # Decay if error
        else:
            self._current_audio_level *= 0.85  # Decay if no callback
        
        # Sinusoidal easing for smooth breathing effect
        # Maps pulse_state (0-59) to a smooth sine wave
        t = self.pulse_state / 60.0 * 2 * math.pi
        ease = 0.5 + 0.5 * math.sin(t)  # 0 to 1 smooth
        
        # Dramatic scale range: 0.7 to 1.3
        # Add strong voice reactivity to dot scale
        voice_scale_boost = self._current_audio_level * 0.5
        scale = 0.7 + ease * 0.6 + voice_scale_boost
        
        # Update ring animation phase (completes a ring every ~1.5 seconds)
        self._ring_phase = (self._ring_phase + 0.022) % 1.0
        
        # Update waveform animation time (continuous scrolling)
        self._wave_time += 0.08
        
        # Update breathing amplitude for waveform (slower breath cycle)
        self._wave_breath += 0.04
        
        # Redraw both the dot and waveform
        self._draw_dot(self.current_color, scale)
        self._draw_waveform(self.current_color)
        
        # Schedule next step at ~30fps for buttery smooth animation
        if self.window:
            self.pulse_job = self.window.after(33, self._pulse_step)
            
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


# === Main Application ===

class WayfinderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        # Set WM_CLASS so Linux desktop environments show the correct icon
        # This must match the .desktop file's StartupWMClass
        self.tk.call('tk', 'appname', 'wayfinder-voice')
        
        self.config = load_config()
        self.app_state = AppState.IDLE
        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()
        
        # Standard recorder for short recordings
        self.recorder = AudioRecorder(
            sample_rate=self.config["sample_rate"],
            device=self.config["audio_device"],
            preprocessing=self.config.get("audio_preprocessing", "light"),
        )
        
        # Chunked recorder for indefinite recording
        self.chunked_recorder: ChunkedRecorder | None = None
        self.chunk_transcriptions: list[str] = []
        self.transcription_executor = ThreadPoolExecutor(max_workers=2)
        self.chunk_transcription_lock = threading.Lock()
        
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.logs = []
        
        # UI scaling for high-DPI screens (saved in config)
        self.ui_scale = self.config.get("ui_scale", 1.0)
        
        # Recording duration tracking
        self._recording_start_time: float | None = None
        self._duration_update_job: str | None = None
        
        # Floating status indicator
        self.indicator: FloatingIndicator | None = None
        
        # Dialog tracking to prevent multiple instances
        self._device_settings_dialog: ctk.CTkToplevel | None = None
        
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
        
        self.indicator = FloatingIndicator(
            self, 
            target_fps=self.config.get("indicator_fps", 0),
            audio_level_callback=get_audio_level
        )
        self.setup_tray()
        self.setup_ui()
        self.setup_scaling_shortcuts()
        self.start_hotkey_listener()
        self.poll_events()
        
        # Log indicator FPS info
        if self.indicator:
            self.log(f"🎯 Indicator refresh: {self.indicator.get_fps_info()}")
        
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        
        # Don't start minimized for now so user can see UI
        # if self.config.get("start_minimized", True):
        #     self.after(100, self.hide_to_tray)

    def setup_window(self) -> None:
        self.title("Wayfinder Voice")
        
        # Apply saved UI scale
        base_w, base_h = 480, 720
        scaled_w = int(base_w * self.ui_scale)
        scaled_h = int(base_h * self.ui_scale)
        self.geometry(f"{scaled_w}x{scaled_h}")
        self.minsize(int(400 * self.ui_scale), int(600 * self.ui_scale))
        self.configure(fg_color=COLORS["bg_dark"])
        
        # Apply CustomTkinter scaling
        ctk.set_widget_scaling(self.ui_scale)
        ctk.set_window_scaling(self.ui_scale)
        
        if ICON_PATH.exists():
            try:
                from PIL import ImageTk
                icon_img = Image.open(ICON_PATH)
                self.icon_photo = ImageTk.PhotoImage(icon_img.resize((64, 64)))
                self.iconphoto(True, self.icon_photo)
            except:
                pass
        
        ctk.set_appearance_mode("dark")
    
    def setup_scaling_shortcuts(self):
        """Bind Ctrl+Plus/Minus for UI scaling."""
        self.bind("<Control-plus>", lambda e: self.scale_ui(1.1))
        self.bind("<Control-equal>", lambda e: self.scale_ui(1.1))  # Ctrl+= (plus without shift)
        self.bind("<Control-minus>", lambda e: self.scale_ui(0.9))
        self.bind("<Control-0>", lambda e: self.reset_scale())
    
    def scale_ui(self, factor: float):
        """Scale the UI by the given factor."""
        new_scale = self.ui_scale * factor
        # Clamp between 0.7 and 2.5
        new_scale = max(0.7, min(2.5, new_scale))
        
        if new_scale != self.ui_scale:
            self.ui_scale = new_scale
            self.config["ui_scale"] = new_scale
            save_config(self.config)
            self._apply_scale()
            self.log(f"⚙ UI Scale: {int(new_scale * 100)}%")
    
    def reset_scale(self):
        """Reset UI scale to 100%."""
        if self.ui_scale != 1.0:
            self.ui_scale = 1.0
            self.config["ui_scale"] = 1.0
            save_config(self.config)
            self._apply_scale()
            self.log("⚙ UI Scale: 100% (reset)")
    
    def _apply_scale(self):
        """Apply the current scale to all UI elements."""
        # Update window size - more flexible
        base_w, base_h = 440, 680
        new_w = int(base_w * self.ui_scale)
        new_h = int(base_h * self.ui_scale)
        self.geometry(f"{new_w}x{new_h}")
        self.minsize(int(320 * self.ui_scale), int(400 * self.ui_scale))
        
        # Update CustomTkinter widget scaling
        ctk.set_widget_scaling(self.ui_scale)
        ctk.set_window_scaling(self.ui_scale)
    
    def _scaled_font(self, base_size: int) -> int:
        """Return a scaled font size."""
        return int(base_size * self.ui_scale)

    def setup_ui(self) -> None:
        # Font families with fallbacks (Exo 2 for headers, Inter for body)
        self.font_header = ("Exo 2", "Rajdhani", "Segoe UI", "Ubuntu")
        self.font_body = ("Inter", "DM Sans", "Segoe UI", "Ubuntu")
        self.font_mono = ("JetBrains Mono", "Fira Code", "Consolas", "monospace")
        
        # Main container - will hold gradient background
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
        
        # Scrollable content frame - allows fluid resizing
        self.scroll_container = ctk.CTkScrollableFrame(
            self.main_container,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
        )
        self.scroll_container.pack(fill="both", expand=True, padx=20, pady=16)
        
        # Main content inside scrollable frame
        main = ctk.CTkFrame(self.scroll_container, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=4)
        
        # === Header - Horizontal "WAYFINDER VOICE" ===
        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", pady=(0, 16))
        
        # Title container for horizontal layout
        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left")
        
        # "WAYFINDER" in silver/gray
        ctk.CTkLabel(
            title_frame,
            text="WAYFINDER",
            font=(self.font_header[0], 20, "bold"),
            text_color="#B8B8C8",  # Silver gray
        ).pack(side="left")
        
        # "VOICE" in accent cyan
        ctk.CTkLabel(
            title_frame,
            text=" VOICE",
            font=(self.font_header[0], 20, "bold"),
            text_color=COLORS["accent"],
        ).pack(side="left")
        
        # Close button - minimal
        ctk.CTkButton(
            header,
            text="×",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            font=(self.font_body[0], 20),
            corner_radius=8,
            command=self.hide_to_tray,
        ).pack(side="right")
        
        # === Compact Status Card with Subtle Glow ===
        self.status_card = ctk.CTkFrame(
            main,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        self.status_card.pack(fill="x", pady=(0, 12))
        
        status_inner = ctk.CTkFrame(self.status_card, fg_color="transparent")
        status_inner.pack(fill="x", padx=14, pady=12)
        
        # Compact glowing dot indicator
        self.status_canvas = ctk.CTkCanvas(
            status_inner,
            width=44,
            height=44,
            bg=COLORS["bg_card"],
            highlightthickness=0,
        )
        self.status_canvas.pack(side="left")
        
        # Draw compact glowing indicator
        self._draw_status_indicator(STATE_COLORS[AppState.IDLE])
        
        # Status text - more compact
        status_text = ctk.CTkFrame(status_inner, fg_color="transparent")
        status_text.pack(side="left", padx=14, fill="both", expand=True)
        
        self.status_label = ctk.CTkLabel(
            status_text,
            text="Ready",
            font=(self.font_header[0], 20, "bold"),
            text_color=STATE_COLORS[AppState.IDLE],
            anchor="w",
        )
        self.status_label.pack(anchor="w")
        
        self.hotkey_label = ctk.CTkLabel(
            status_text,
            text=f"Press {self.get_hotkey_display()} to record",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
            anchor="w",
        )
        self.hotkey_label.pack(anchor="w", pady=(2, 0))
        
        # === Record Button - Premium styling with tooltip ===
        self.record_btn = ctk.CTkButton(
            main,
            text="Start Listening",
            font=(self.font_body[0], 17, "bold"),
            height=58,
            corner_radius=14,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
            text_color="#000000",
            command=self.on_record_button,
        )
        self.record_btn.pack(fill="x", pady=(0, 24))
        
        # Add tooltip explaining the record functionality
        ToolTip(
            self.record_btn,
            f"Toggle {self.get_hotkey_display()}\n\nWhen recording stops, text is automatically\ninjected into any focused text field in Linux."
        )
        
        # === Settings Section ===
        settings_header = ctk.CTkFrame(main, fg_color="transparent")
        settings_header.pack(fill="x", pady=(0, 6))
        
        ctk.CTkLabel(
            settings_header,
            text="SETTINGS",
            font=(self.font_body[0], 10, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(side="left")
        
        # Settings card with subtle styling
        settings_card = ctk.CTkFrame(
            main,
            fg_color=COLORS["bg_card"],
            corner_radius=14,
            border_width=1,
            border_color=COLORS["border"],
        )
        settings_card.pack(fill="x", pady=(0, 14))
        
        # Hotkey setting
        self.hotkey_btn = self.create_setting_row(
            settings_card,
            "Hotkey",
            self.get_hotkey_display(),
            self.open_hotkey_settings,
            tooltip=SETTING_TOOLTIPS["hotkey"],
        )
        
        # Microphone/Audio input device setting
        mic_display = self.get_microphone_display()
        self.mic_btn = self.create_setting_row(
            settings_card,
            "Microphone",
            mic_display,
            self.open_microphone_settings,
            tooltip=SETTING_TOOLTIPS["microphone"],
        )
        
        # Typing speed setting
        speed = self.config.get("typing_speed", "instant")
        speed_display = speed.replace("_", " ").title()
        self.speed_btn = self.create_setting_row(
            settings_card,
            "Typing Speed",
            speed_display,
            self.open_speed_settings,
            tooltip=SETTING_TOOLTIPS["typing_speed"],
        )
        
        # Model setting
        model_display = self.get_model_display()
        self.model_btn = self.create_setting_row(
            settings_card,
            "Whisper Model",
            model_display,
            self.open_model_settings,
            tooltip=SETTING_TOOLTIPS["whisper_model"],
        )
        
        # Prompt setting
        prompt_display = self.get_prompt_display()
        self.prompt_btn = self.create_setting_row(
            settings_card,
            "Prompt",
            prompt_display,
            self.open_prompt_settings,
            tooltip=SETTING_TOOLTIPS["prompt"],
        )
        
        # Start minimized setting
        self.start_min_var = ctk.BooleanVar(value=self.config.get("start_minimized", True))
        self.create_toggle_row(
            settings_card,
            "Start minimized to tray",
            self.start_min_var,
            self.toggle_start_minimized,
            tooltip=SETTING_TOOLTIPS["start_minimized"],
        )
        
        # UI Scale setting
        scale_display = f"{int(self.ui_scale * 100)}%"
        self.scale_btn = self.create_setting_row(
            settings_card,
            "UI Scale",
            scale_display,
            self.open_scale_settings,
            tooltip=SETTING_TOOLTIPS["ui_scale"],
        )
        
        # === Collapsible Advanced Settings ===
        self.advanced_expanded = False
        
        advanced_header = ctk.CTkFrame(main, fg_color="transparent")
        advanced_header.pack(fill="x", pady=(0, 8))
        
        advanced_toggle_btn = ctk.CTkButton(
            advanced_header,
            text="▶  ADVANCED",
            font=(self.font_body[0], 10, "bold"),
            text_color=COLORS["text_muted"],
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            anchor="w",
            height=24,
            command=self.toggle_advanced_settings,
        )
        advanced_toggle_btn.pack(side="left")
        self.advanced_toggle_btn = advanced_toggle_btn
        
        # Advanced container (collapsible) - flows naturally in main scroll
        self.advanced_container = ctk.CTkFrame(main, fg_color="transparent")
        # Initially collapsed - don't pack
        
        advanced_card = ctk.CTkFrame(
            self.advanced_container,
            fg_color=COLORS["bg_card"],
            corner_radius=14,
            border_width=1,
            border_color=COLORS["border"],
        )
        advanced_card.pack(fill="x", pady=(0, 8))
        
        # Accuracy settings label
        ctk.CTkLabel(
            advanced_card,
            text="Accuracy",
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=16, pady=(12, 4))
        
        # Accuracy Mode dropdown
        accuracy_mode = self.config.get("accuracy_mode", "balanced")
        self.accuracy_mode_var = ctk.StringVar(value=accuracy_mode)
        self.accuracy_mode_dropdown = self.create_dropdown_row(
            advanced_card,
            "Accuracy Mode",
            ["fast", "balanced", "high"],
            self.accuracy_mode_var,
            self.on_accuracy_mode_changed,
            tooltip=SETTING_TOOLTIPS["accuracy_mode"],
            width=140,
        )
        
        # Beam size dropdown
        beam_size = self.config.get("beam_size", 5)
        self.beam_size_var = ctk.StringVar(value=str(beam_size))
        self.beam_size_dropdown = self.create_dropdown_row(
            advanced_card,
            "Beam Size",
            ["1", "2", "3", "4", "5", "6", "7", "8", "10", "12"],
            self.beam_size_var,
            self.on_beam_size_changed,
            tooltip=SETTING_TOOLTIPS["beam_size"],
            width=100,
        )
        
        # Language dropdown
        language = self.config.get("language", "en")
        self.language_var = ctk.StringVar(value=language)
        self.language_dropdown = self.create_dropdown_row(
            advanced_card,
            "Language",
            ["en", "auto", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "zh", "ja", "ko"],
            self.language_var,
            self.on_language_changed,
            tooltip=SETTING_TOOLTIPS["language"],
            width=100,
        )
        
        # Audio preprocessing level dropdown
        preprocess_level = self.config.get("audio_preprocessing", "light")
        # Handle legacy boolean values
        if preprocess_level is True:
            preprocess_level = "light"
        elif preprocess_level is False:
            preprocess_level = "off"
        
        preprocess_row = ctk.CTkFrame(advanced_card, fg_color="transparent")
        preprocess_row.pack(fill="x", padx=16, pady=10)
        
        # Configure grid for proportional scaling
        preprocess_row.grid_columnconfigure(0, weight=0)  # Label column - fixed
        preprocess_row.grid_columnconfigure(1, weight=1, minsize=160)  # Dropdown column - grows
        
        # Left side: label + info icon (matches create_setting_row style)
        left_frame = ctk.CTkFrame(preprocess_row, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w")
        
        ctk.CTkLabel(
            left_frame,
            text="Audio Processing",
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
        info_label = ctk.CTkLabel(
            left_frame,
            text="ⓘ",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
            cursor="hand2",
        )
        info_label.pack(side="left", padx=(6, 0))
        info_label.bind("<Button-1>", lambda e: self.show_preprocessing_help())
        
        # Right side: description + dropdown (styled like other buttons)
        right_frame = ctk.CTkFrame(preprocess_row, fg_color="transparent")
        right_frame.grid(row=0, column=1, sticky="e", padx=(20, 0))
        
        # Brief description label
        self.preprocess_desc_label = ctk.CTkLabel(
            right_frame,
            text=self._get_preprocess_desc(preprocess_level),
            font=(self.font_body[0], 12),
            text_color=COLORS["text_muted"],
            anchor="e",
        )
        self.preprocess_desc_label.pack(side="left", padx=(0, 10))
        
        self.preprocess_var = ctk.StringVar(value=preprocess_level)
        self.preprocess_dropdown = ctk.CTkOptionMenu(
            right_frame,
            values=["off", "light", "medium", "heavy"],
            variable=self.preprocess_var,
            command=self.on_preprocessing_changed,
            fg_color=COLORS["bg_hover"],
            button_color=COLORS["bg_elevated"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["bg_hover"],
            dropdown_text_color=COLORS["text_primary"],
            text_color=COLORS["accent"],
            font=(self.font_body[0], 14),
            width=120,
            height=46,
            corner_radius=10,
        )
        self.preprocess_dropdown.pack(side="right")
        
        # Punctuation toggle (optional - most models already punctuate well)
        self.punctuation_var = ctk.BooleanVar(value=self.config.get("ensure_punctuation", False))
        self.create_toggle_row(
            advanced_card,
            "Additional Punctuation",
            self.punctuation_var,
            self.toggle_punctuation,
            tooltip=SETTING_TOOLTIPS["ensure_punctuation"],
        )
        
        # Chunked recording settings label
        ctk.CTkLabel(
            advanced_card,
            text="Recording",
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=16, pady=(14, 6))
        
        # Chunked mode toggle
        self.chunked_var = ctk.BooleanVar(value=self.config.get("chunked_mode", True))
        self.create_toggle_row(
            advanced_card,
            "Chunked Recording",
            self.chunked_var,
            self.toggle_chunked_mode,
            tooltip=SETTING_TOOLTIPS["chunked_mode"],
        )
        
        # Chunk duration dropdown
        chunk_duration = self.config.get("chunk_duration", 30)
        self.chunk_duration_var = ctk.StringVar(value=str(chunk_duration))
        self.chunk_duration_dropdown = self.create_dropdown_row(
            advanced_card,
            "Chunk Duration",
            ["15", "20", "30", "45", "60", "90", "120"],
            self.chunk_duration_var,
            self.on_chunk_duration_changed,
            tooltip=SETTING_TOOLTIPS["chunk_duration"],
            width=100,
        )
        
        # GPU Acceleration settings label
        ctk.CTkLabel(
            advanced_card,
            text="GPU Acceleration",
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=16, pady=(14, 6))
        
        # Transcription backend dropdown
        backend = self.config.get("transcription_backend", "whisper_cpp")
        self.backend_var = ctk.StringVar(value=backend)
        self.backend_dropdown = self.create_dropdown_row(
            advanced_card,
            "Backend",
            ["whisper_cpp", "faster_whisper"],
            self.backend_var,
            self.on_backend_changed,
            tooltip=SETTING_TOOLTIPS["backend"],
            width=160,
        )
        
        # GPU toggle
        self.gpu_var = ctk.BooleanVar(value=self.config.get("use_gpu", False))
        self.create_toggle_row(
            advanced_card,
            "GPU Acceleration",
            self.gpu_var,
            self.toggle_gpu,
            tooltip=SETTING_TOOLTIPS["gpu_acceleration"],
        )
        
        # GPU layers dropdown (only relevant for whisper.cpp)
        gpu_layers = self.config.get("gpu_layers", 0)
        self.gpu_layers_var = ctk.StringVar(value="auto" if gpu_layers == 0 else str(gpu_layers))
        self.gpu_layers_dropdown = self.create_dropdown_row(
            advanced_card,
            "GPU Layers",
            ["auto", "1", "4", "8", "16", "24", "32", "48", "64", "99"],
            self.gpu_layers_var,
            self.on_gpu_layers_changed,
            tooltip=SETTING_TOOLTIPS["gpu_layers"],
            width=100,
        )
        
        # Devices settings label
        ctk.CTkLabel(
            advanced_card,
            text="Devices",
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=16, pady=(14, 6))
        
        # Hotkey devices setting (keyboards/mice that can trigger the hotkey)
        device_count = len(get_all_input_devices())
        enabled = self.config.get("enabled_input_devices", [])
        device_text = f"All ({device_count})  ▼" if not enabled else f"{len(enabled)} selected  ▼"
        self.devices_btn = self.create_setting_row(
            advanced_card,
            "Hotkey Devices",
            device_text,
            self.open_device_settings,
            tooltip=SETTING_TOOLTIPS["hotkey_devices"],
        )
        
        # === Collapsible Activity Log ===
        self.log_expanded = False
        
        # Log header (always visible)
        self.log_header_frame = ctk.CTkFrame(main, fg_color="transparent")
        self.log_header_frame.pack(fill="x", pady=(0, 6))
        
        # Clickable header to expand/collapse
        log_toggle_btn = ctk.CTkButton(
            self.log_header_frame,
            text="▶  ACTIVITY",
            font=(self.font_body[0], 10, "bold"),
            text_color=COLORS["text_muted"],
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            anchor="w",
            height=24,
            command=self.toggle_activity_log,
        )
        log_toggle_btn.pack(side="left")
        self.log_toggle_btn = log_toggle_btn
        
        ctk.CTkButton(
            self.log_header_frame,
            text="Clear",
            width=44,
            height=22,
            font=(self.font_body[0], 9),
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            corner_radius=5,
            command=self.clear_log,
        ).pack(side="right")
        
        # Log container (collapsible)
        self.log_container = ctk.CTkFrame(main, fg_color="transparent")
        # Initially collapsed - don't pack
        
        self.log_frame = ctk.CTkFrame(
            self.log_container,
            fg_color=COLORS["bg_card"],
            corner_radius=10,
            border_width=1,
            border_color=COLORS["border"],
        )
        self.log_frame.pack(fill="both", expand=True)
        
        self.log_textbox = ctk.CTkTextbox(
            self.log_frame,
            font=(self.font_mono[0], 9),
            fg_color="transparent",
            text_color=COLORS["text_secondary"],
            wrap="word",
            activate_scrollbars=True,
            height=100,
            scrollbar_button_color=COLORS["bg_hover"],
            scrollbar_button_hover_color=COLORS["accent_dim"],
        )
        self.log_textbox.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_textbox.configure(state="disabled")
        
        # Initial log
        self.log("✓ Wayfinder Voice started")
        self.log(f"⌨ Hotkey: {self.get_hotkey_display()}")
        
        # Log detected hardware
        gpu_info = get_gpu_info()
        cpu_count = os.cpu_count() or 4
        if gpu_info.is_nvidia:
            self.log(f"🟢 GPU: NVIDIA detected (use Faster-Whisper + CUDA)")
        elif gpu_info.is_amd:
            self.log(f"🔴 GPU: AMD detected (Vulkan ready)")
        elif gpu_info.is_intel:
            self.log(f"🔵 GPU: Intel detected (CPU mode recommended)")
        else:
            self.log(f"⚪ GPU: Not detected (using CPU)")
        
        # Log thread recommendation if system has fewer cores
        threads_config = self.config.get("threads", 6)
        if cpu_count < threads_config:
            optimal = get_optimal_thread_count()
            self.log(f"💡 Tip: {cpu_count} CPU cores detected, consider setting threads to {optimal}")
    
    def _draw_gradient_bg(self, event=None):
        """Draw radial gradient background with cyan glow."""
        canvas = self.bg_canvas
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        
        if w <= 1 or h <= 1:
            return
        
        canvas.delete("gradient")
        
        # Base color
        base_r, base_g, base_b = 13, 13, 18  # #0D0D12
        
        # Glow color (cyan)
        glow_r, glow_g, glow_b = 0, 60, 80  # Subtle cyan tint
        
        # Create radial gradient effect using concentric ovals
        cx, cy = w // 2, h // 3  # Center the glow higher up
        max_radius = int(max(w, h) * 0.8)
        
        steps = 30
        for i in range(steps, 0, -1):
            ratio = i / steps
            radius = int(max_radius * ratio)
            
            # Blend colors based on distance from center
            blend = (1 - ratio) ** 2  # Quadratic falloff
            r = int(base_r + (glow_r - base_r) * blend * 0.3)
            g = int(base_g + (glow_g - base_g) * blend * 0.5)
            b = int(base_b + (glow_b - base_b) * blend * 0.5)
            
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
    
    def _draw_status_indicator(self, color: str):
        """Draw compact status indicator with pleasant subtle glow."""
        canvas = self.status_canvas
        canvas.delete("all")
        
        size = 44  # Compact size
        cx, cy = size // 2, size // 2
        
        # Parse color for glow layers
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        
        # Blend with background for soft glow effect
        bg_r, bg_g, bg_b = 30, 32, 38  # COLORS["bg_card"] approximate
        
        # Soft outer glow layers (pleasant, not harsh)
        glow_layers = [
            (18, 0.12),  # Outermost - very subtle
            (14, 0.20),  # Middle - soft
            (10, 0.35),  # Inner - more visible
        ]
        
        for radius, intensity in glow_layers:
            # Blend color with background for soft glow
            gr = int(bg_r + (r - bg_r) * intensity)
            gg = int(bg_g + (g - bg_g) * intensity)
            gb = int(bg_b + (b - bg_b) * intensity)
            glow_color = f"#{gr:02x}{gg:02x}{gb:02x}"
            
            canvas.create_oval(
                cx - radius, cy - radius,
                cx + radius, cy + radius,
                fill=glow_color,
                outline="",
                tags="glow",
            )
        
        # Main filled circle
        canvas.create_oval(
            cx - 8, cy - 8,
            cx + 8, cy + 8,
            fill=color,
            outline="",
            tags="center",
        )
        
        # Subtle highlight for depth
        canvas.create_oval(
            cx - 4, cy - 5,
            cx + 1, cy - 1,
            fill="#ffffff",
            outline="",
            tags="highlight",
        )
    
    def toggle_activity_log(self):
        """Toggle the activity log expanded/collapsed state."""
        self.log_expanded = not self.log_expanded
        
        if self.log_expanded:
            self.log_toggle_btn.configure(text="▼  ACTIVITY")
            self.log_container.pack(fill="both", expand=True)
        else:
            self.log_toggle_btn.configure(text="▶  ACTIVITY")
            self.log_container.pack_forget()
    
    def toggle_advanced_settings(self):
        """Toggle the advanced settings expanded/collapsed state."""
        self.advanced_expanded = not self.advanced_expanded
        
        if self.advanced_expanded:
            self.advanced_toggle_btn.configure(text="▼  ADVANCED")
            self.advanced_container.pack(fill="x", pady=(0, 10))
        else:
            self.advanced_toggle_btn.configure(text="▶  ADVANCED")
            self.advanced_container.pack_forget()
    
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
        """Handle chunk duration change from dropdown."""
        self.config["chunk_duration"] = int(value)
        save_config(self.config)
        self.log(f"⚙ Chunk duration: {value}s")
    
    def on_backend_changed(self, value: str):
        """Handle transcription backend change from dropdown."""
        self.config["transcription_backend"] = value
        save_config(self.config)
        display = "whisper.cpp" if value == "whisper_cpp" else "Faster-Whisper"
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
    
    def show_preprocessing_help(self):
        """Show help dialog explaining audio processing levels."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Audio Processing Help")
        dialog.geometry("420x300")
        dialog.configure(fg_color=COLORS["bg_dark"])
        dialog.transient(self)
        dialog.grab_set()
        
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
        self.config["use_gpu"] = self.gpu_var.get()
        save_config(self.config)
        status = "enabled" if self.gpu_var.get() else "disabled"
        self.log(f"⚙ GPU acceleration: {status}")

    def create_setting_row(self, parent, label, value, command, tooltip=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=10)
        
        # Configure grid for proportional scaling
        row.grid_columnconfigure(0, weight=0)  # Label column - fixed
        row.grid_columnconfigure(1, weight=1, minsize=160)  # Button column - grows
        
        # Left side: label + optional info icon
        left_frame = ctk.CTkFrame(row, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w")
        
        ctk.CTkLabel(
            left_frame,
            text=label,
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
        if tooltip:
            info_label = ctk.CTkLabel(
                left_frame,
                text="ⓘ",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
                cursor="question_arrow",
            )
            info_label.pack(side="left", padx=(6, 0))
            ToolTip(info_label, tooltip)
        
        btn = ctk.CTkButton(
            row,
            text=value,
            font=(self.font_body[0], 14),
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["accent"],
            height=46,
            corner_radius=10,
            command=command,
        )
        btn.grid(row=0, column=1, sticky="e", padx=(20, 0))
        return btn

    def create_toggle_row(self, parent, label, variable, command, tooltip=None):
        row = ctk.CTkFrame(parent, fg_color="transparent", height=46)
        row.pack(fill="x", padx=16, pady=10)
        
        # Configure grid for alignment with setting rows
        row.grid_columnconfigure(0, weight=1)  # Label takes available space
        row.grid_columnconfigure(1, weight=0)  # Switch column - fixed
        
        # Left side: label + optional info icon
        left_frame = ctk.CTkFrame(row, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w")
        
        ctk.CTkLabel(
            left_frame,
            text=label,
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
        if tooltip:
            info_label = ctk.CTkLabel(
                left_frame,
                text="ⓘ",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
                cursor="question_arrow",
            )
            info_label.pack(side="left", padx=(6, 0))
            ToolTip(info_label, tooltip)
        
        switch = ctk.CTkSwitch(
            row,
            text="",
            variable=variable,
            command=command,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            switch_width=50,
            switch_height=26,
        )
        switch.grid(row=0, column=1, sticky="e", padx=(20, 0))

    def create_dropdown_row(self, parent, label, values, variable, command, tooltip=None, width=140):
        """Create a setting row with a dropdown menu instead of a button."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=10)
        
        # Configure grid for proportional scaling
        row.grid_columnconfigure(0, weight=0)  # Label column - fixed
        row.grid_columnconfigure(1, weight=1, minsize=160)  # Dropdown column - grows
        
        # Left side: label + optional info icon
        left_frame = ctk.CTkFrame(row, fg_color="transparent")
        left_frame.grid(row=0, column=0, sticky="w")
        
        ctk.CTkLabel(
            left_frame,
            text=label,
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
        if tooltip:
            info_label = ctk.CTkLabel(
                left_frame,
                text="ⓘ",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
                cursor="question_arrow",
            )
            info_label.pack(side="left", padx=(6, 0))
            ToolTip(info_label, tooltip)
        
        dropdown = ctk.CTkOptionMenu(
            row,
            values=values,
            variable=variable,
            command=command,
            fg_color=COLORS["bg_hover"],
            button_color=COLORS["bg_elevated"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_card"],
            dropdown_hover_color=COLORS["bg_hover"],
            dropdown_text_color=COLORS["text_primary"],
            text_color=COLORS["accent"],
            font=(self.font_body[0], 14),
            width=width,
            height=46,
            corner_radius=10,
        )
        dropdown.grid(row=0, column=1, sticky="e", padx=(20, 0))
        return dropdown

    def toggle_start_minimized(self):
        self.config["start_minimized"] = self.start_min_var.get()
        save_config(self.config)

    def clear_log(self):
        self.log_textbox.configure(state="normal")
        self.log_textbox.delete("1.0", "end")
        self.log_textbox.configure(state="disabled")

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {message}\n"
        
        def update():
            self.log_textbox.configure(state="normal")
            self.log_textbox.insert("end", log_line)
            self.log_textbox.see("end")
            self.log_textbox.configure(state="disabled")
        
        try:
            self.after(0, update)
        except:
            pass

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

    def get_microphone_display(self) -> str:
        """Get display name for current audio input device."""
        device_id = self.config.get("audio_device")
        if device_id is None:
            return "System Default"
        
        # Find the device name
        try:
            devices = get_audio_input_devices()
            for device in devices:
                if device["index"] == device_id:
                    # Truncate long names
                    name = device["name"]
                    if len(name) > 25:
                        return name[:22] + "..."
                    return name
        except Exception:
            pass
        
        return f"Device {device_id}"

    def get_model_display(self) -> str:
        """Get display name for current model."""
        model_path = self.config.get("model_path", "")
        # Extract model name from path
        if "tiny.en" in model_path:
            return "Tiny (Fast)"
        elif "base.en" in model_path:
            return "Base"
        elif "small.en" in model_path:
            return "Small"
        elif "medium.en" in model_path:
            return "Medium (Slow)"
        elif "large" in model_path:
            return "Large (Slowest)"
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
            
            # Technical prompts - vocabulary style
            "Technical/Linux": "Linux, Fedora, Bazzite, Wayfinder, sudo, grep, bash, terminal, CLI, SSH, systemd, dnf, flatpak, GNOME, KDE, Wayland.",
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
        
        # Define model info with latency estimates (GPU / CPU for 10s audio)
        model_info = {
            "ggml-tiny.en.bin": ("Tiny (English)", "75MB", "⚡ GPU: ~0.5s | CPU: ~2s"),
            "ggml-base.en.bin": ("Base (English)", "142MB", "⚡ GPU: ~1s | CPU: ~4s"),
            "ggml-small.en.bin": ("Small (English)", "466MB", "🟡 GPU: ~1.5s | CPU: ~6s"),
            "ggml-medium.en.bin": ("Medium (English)", "1.5GB", "🔴 GPU: ~3s | CPU: ~12s"),
            "ggml-large-v3-turbo.bin": ("Large v3 Turbo ⭐", "1.6GB", "🚀 GPU: ~2s | CPU: ~8s"),
            "ggml-large-v3-turbo-q5_0.bin": ("Large v3 Turbo Q5", "547MB", "🚀 GPU: ~2s | CPU: ~6s"),
            "ggml-tiny.bin": ("Tiny (Multi-lang)", "75MB", "⚡ GPU: ~0.5s | CPU: ~2s"),
            "ggml-base.bin": ("Base (Multi-lang)", "142MB", "⚡ GPU: ~1s | CPU: ~4s"),
            "ggml-small.bin": ("Small (Multi-lang)", "466MB", "🟡 GPU: ~1.5s | CPU: ~6s"),
            "ggml-medium.bin": ("Medium (Multi-lang)", "1.5GB", "🔴 GPU: ~3s | CPU: ~12s"),
            "ggml-large-v3.bin": ("Large v3", "3GB", "🔴 GPU: ~6s | CPU: ~25s"),
            "ggml-large.bin": ("Large (Multi-lang)", "3GB", "🐌 GPU: ~6s | CPU: ~25s"),
        }
        
        for filename, (name, size, speed) in model_info.items():
            path = models_dir / filename
            if path.exists():
                models.append({
                    "name": name,
                    "path": str(path),
                    "size": size,
                    "speed": speed,
                    "filename": filename,
                })
        
        return models

    def open_model_settings(self):
        """Open dialog to select or download whisper models."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Whisper Models")
        dialog.geometry("580x680")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        # Initialize model downloader
        downloader = ModelDownloader()
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        # Header with tabs
        header = ctk.CTkFrame(inner, fg_color="transparent")
        header.pack(fill="x", pady=(0, 15))
        
        ctk.CTkLabel(
            header,
            text="Whisper Models",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            header,
            text="Select an installed model or download new ones.\nTime estimates are per 10 seconds of audio.",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(5, 0))
        
        # Tab buttons
        tab_frame = ctk.CTkFrame(inner, fg_color="transparent")
        tab_frame.pack(fill="x", pady=(0, 10))
        
        current_tab = ctk.StringVar(value="installed")
        
        # Content frame that will be refreshed
        content_frame = ctk.CTkFrame(inner, fg_color="transparent")
        content_frame.pack(fill="both", expand=True)
        
        current_path = os.path.expanduser(self.config.get("model_path", ""))
        model_var = ctk.StringVar(value=current_path)
        
        def show_installed_tab():
            current_tab.set("installed")
            for widget in content_frame.winfo_children():
                widget.destroy()
            
            installed_btn.configure(fg_color=COLORS["accent"], text_color="#000000")
            download_btn.configure(fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"])
            
            models = self.get_available_models()
            
            if not models:
                empty_frame = ctk.CTkFrame(content_frame, fg_color=COLORS["bg_card"], corner_radius=12)
                empty_frame.pack(fill="both", expand=True, pady=10)
                
                ctk.CTkLabel(
                    empty_frame,
                    text="📦 No models installed yet",
                    font=(self.font_body[0], 16, "bold"),
                    text_color=COLORS["text_primary"],
                ).pack(pady=(40, 10))
                
                ctk.CTkLabel(
                    empty_frame,
                    text="Click 'Download Models' to get started!",
                    font=(self.font_body[0], 12),
                    text_color=COLORS["text_muted"],
                ).pack(pady=(0, 10))
                
                ctk.CTkButton(
                    empty_frame,
                    text="⬇️ Download Models",
                    font=(self.font_body[0], 14, "bold"),
                    height=40,
                    corner_radius=10,
                    fg_color=COLORS["accent"],
                    hover_color=COLORS["accent_glow"],
                    text_color="#000000",
                    command=show_download_tab,
                ).pack(pady=(10, 40))
                return
            
            scroll = ctk.CTkScrollableFrame(content_frame, fg_color=COLORS["bg_card"], corner_radius=12, height=320)
            scroll.pack(fill="both", expand=True, pady=(0, 15))
            
            for model in models:
                is_current = os.path.expanduser(model["path"]) == current_path
                
                frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_hover"] if is_current else "transparent", corner_radius=8)
                frame.pack(fill="x", pady=3, padx=5)
                
                radio = ctk.CTkRadioButton(
                    frame, text="", variable=model_var, value=model["path"],
                    width=20, fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"],
                )
                radio.pack(side="left", padx=(10, 5), pady=10)
                
                info = ctk.CTkFrame(frame, fg_color="transparent")
                info.pack(side="left", fill="x", expand=True, pady=8)
                
                ctk.CTkLabel(
                    info, text=model["name"],
                    font=(self.font_body[0], 14, "bold" if is_current else "normal"),
                    text_color=COLORS["accent"] if is_current else COLORS["text_primary"],
                    anchor="w",
                ).pack(anchor="w")
                
                ctk.CTkLabel(
                    info, text=f"{model['speed']} • {model['size']}",
                    font=(self.font_body[0], 11), text_color=COLORS["text_muted"], anchor="w",
                ).pack(anchor="w")
            
            # Save button
            def save():
                selected = model_var.get()
                if selected.startswith(str(Path.home())):
                    selected = "~" + selected[len(str(Path.home())):]
                self.config["model_path"] = selected
                save_config(self.config)
                self.model_btn.configure(text=self.get_model_display())
                self.log(f"⚙ Model: {self.get_model_display()}")
                dialog.destroy()
            
            ctk.CTkButton(
                content_frame, text="Save & Apply",
                font=(self.font_body[0], 15, "bold"), height=50, corner_radius=12,
                fg_color=COLORS["accent"], hover_color=COLORS["accent_glow"], text_color="#000000",
                command=save,
            ).pack(fill="x", pady=(0, 5))
        
        def show_download_tab():
            current_tab.set("download")
            for widget in content_frame.winfo_children():
                widget.destroy()
            
            installed_btn.configure(fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"])
            download_btn.configure(fg_color=COLORS["accent"], text_color="#000000")
            
            scroll = ctk.CTkScrollableFrame(content_frame, fg_color=COLORS["bg_card"], corner_radius=12, height=380)
            scroll.pack(fill="both", expand=True, pady=(0, 10))
            
            # Group models by category
            english_models = ["tiny.en", "base.en", "small.en", "medium.en"]
            turbo_models = ["large-v3-turbo", "large-v3-turbo-q5_0"]
            multi_models = ["tiny", "base", "small", "medium", "large-v3"]
            
            def add_section(title, model_ids):
                ctk.CTkLabel(
                    scroll, text=title,
                    font=(self.font_body[0], 11, "bold"),
                    text_color=COLORS["text_muted"],
                ).pack(anchor="w", padx=15, pady=(12, 5))
                
                for model_id in model_ids:
                    if model_id not in WHISPER_CPP_MODELS:
                        continue
                    info = WHISPER_CPP_MODELS[model_id]
                    is_installed = downloader.is_installed(model_id)
                    
                    frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_hover"] if is_installed else "transparent", corner_radius=8)
                    frame.pack(fill="x", pady=2, padx=5)
                    
                    # Info section
                    info_frame = ctk.CTkFrame(frame, fg_color="transparent")
                    info_frame.pack(side="left", fill="x", expand=True, padx=12, pady=8)
                    
                    name_row = ctk.CTkFrame(info_frame, fg_color="transparent")
                    name_row.pack(anchor="w")
                    
                    ctk.CTkLabel(
                        name_row, text=info["name"],
                        font=(self.font_body[0], 13, "bold"),
                        text_color=COLORS["accent"] if is_installed else COLORS["text_primary"],
                    ).pack(side="left")
                    
                    if info.get("recommended"):
                        ctk.CTkLabel(
                            name_row, text=" RECOMMENDED",
                            font=(self.font_body[0], 9, "bold"),
                            text_color=COLORS["accent_green"],
                        ).pack(side="left", padx=(8, 0))
                    
                    ctk.CTkLabel(
                        info_frame, text=f"{info['speed']} • {info['size']}",
                        font=(self.font_body[0], 10), text_color=COLORS["text_muted"],
                    ).pack(anchor="w")
                    
                    # Action button
                    btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
                    btn_frame.pack(side="right", padx=10)
                    
                    if is_installed:
                        ctk.CTkLabel(
                            btn_frame, text="✓ Installed",
                            font=(self.font_body[0], 11),
                            text_color=COLORS["accent_green"],
                        ).pack(pady=10)
                    else:
                        def make_download_handler(mid=model_id, frm=frame):
                            return lambda: start_download(mid, frm)
                        
                        ctk.CTkButton(
                            btn_frame, text="Download",
                            font=(self.font_body[0], 11), width=80, height=28,
                            corner_radius=6, fg_color=COLORS["bg_elevated"],
                            hover_color=COLORS["accent_dim"], text_color=COLORS["text_primary"],
                            command=make_download_handler(),
                        ).pack(pady=8)
            
            add_section("⭐ RECOMMENDED", turbo_models)
            add_section("🇺🇸 ENGLISH ONLY (Optimized)", english_models)
            add_section("🌍 MULTI-LANGUAGE", multi_models)
        
        def start_download(model_id: str, parent_frame):
            """Start downloading a model with progress UI."""
            info = WHISPER_CPP_MODELS[model_id]
            
            # Create download progress dialog
            progress_dialog = ctk.CTkToplevel(dialog)
            progress_dialog.title(f"Downloading {info['name']}")
            progress_dialog.geometry("400x200")
            progress_dialog.configure(fg_color=COLORS["bg_base"])
            progress_dialog.transient(dialog)
            progress_dialog.grab_set()
            
            pd_inner = ctk.CTkFrame(progress_dialog, fg_color="transparent")
            pd_inner.pack(fill="both", expand=True, padx=30, pady=30)
            
            ctk.CTkLabel(
                pd_inner, text=f"Downloading {info['name']}",
                font=(self.font_body[0], 16, "bold"),
                text_color=COLORS["text_bright"],
            ).pack(pady=(0, 5))
            
            ctk.CTkLabel(
                pd_inner, text=f"Size: {info['size']}",
                font=(self.font_body[0], 12),
                text_color=COLORS["text_secondary"],
            ).pack(pady=(0, 15))
            
            progress_bar = ctk.CTkProgressBar(pd_inner, width=340, height=20, corner_radius=10)
            progress_bar.pack(pady=(0, 10))
            progress_bar.set(0)
            
            status_label = ctk.CTkLabel(
                pd_inner, text="Starting download...",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            )
            status_label.pack()
            
            cancel_btn = ctk.CTkButton(
                pd_inner, text="Cancel",
                font=(self.font_body[0], 12), width=100, height=32,
                fg_color=COLORS["bg_hover"], hover_color=COLORS["bg_elevated"],
                text_color=COLORS["text_secondary"],
                command=lambda: [downloader.cancel_download(), progress_dialog.destroy()],
            )
            cancel_btn.pack(pady=(15, 0))
            
            def on_progress(progress, downloaded, total):
                def update():
                    progress_bar.set(progress)
                    mb_done = downloaded / (1024 * 1024)
                    mb_total = total / (1024 * 1024)
                    status_label.configure(text=f"{mb_done:.1f} MB / {mb_total:.1f} MB ({progress*100:.0f}%)")
                progress_dialog.after(0, update)
            
            def on_complete(path):
                def update():
                    progress_dialog.destroy()
                    self.log(f"✓ Downloaded: {info['name']}")
                    # Refresh the download tab
                    show_download_tab()
                progress_dialog.after(0, update)
            
            def on_error(error):
                def update():
                    status_label.configure(text=f"Error: {error}", text_color=COLORS["accent_red"])
                    cancel_btn.configure(text="Close")
                progress_dialog.after(0, update)
            
            downloader.download_model(model_id, on_progress, on_complete, on_error)
        
        # Tab buttons
        installed_btn = ctk.CTkButton(
            tab_frame, text="📁 Installed",
            font=(self.font_body[0], 13), width=120, height=36,
            corner_radius=8, fg_color=COLORS["accent"], text_color="#000000",
            hover_color=COLORS["accent_glow"],
            command=show_installed_tab,
        )
        installed_btn.pack(side="left", padx=(0, 8))
        
        download_btn = ctk.CTkButton(
            tab_frame, text="⬇️ Download Models",
            font=(self.font_body[0], 13), width=140, height=36,
            corner_radius=8, fg_color=COLORS["bg_hover"], text_color=COLORS["text_primary"],
            hover_color=COLORS["bg_elevated"],
            command=show_download_tab,
        )
        download_btn.pack(side="left")
        
        # Show installed tab initially
        show_installed_tab()

    def open_prompt_settings(self):
        """Open dialog to configure transcription prompt and vocabulary."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Transcription Prompt & Vocabulary")
        dialog.geometry("580x880")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        presets_frame = ctk.CTkScrollableFrame(
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
        
        vocab_text = ctk.CTkTextbox(
            inner,
            font=(self.font_body[0], 12),
            fg_color=COLORS["bg_card"],
            text_color=COLORS["accent"],
            corner_radius=10,
            height=60,
            wrap="word",
        )
        vocab_text.pack(fill="x", pady=(0, 10))
        
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

    def open_hotkey_settings(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Hotkey Settings")
        dialog.geometry("450x550")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Configure Hotkey",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 10))
        
        # Warning about KDE shortcuts
        warning = ctk.CTkFrame(inner, fg_color="#2A1F00", corner_radius=10)
        warning.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(
            warning,
            text="⚠ On Bazzite/KDE, Super and some Ctrl combos are\ncaptured by the system. Use F9 or Scroll Lock for reliability.",
            font=(self.font_body[0], 11),
            text_color=COLORS["accent_yellow"],
            justify="left",
        ).pack(padx=15, pady=12)
        
        # Current hotkey display
        ctk.CTkLabel(
            inner,
            text=f"Current: {self.get_hotkey_display()}",
            font=(self.font_body[0], 14),
            text_color=COLORS["accent"],
        ).pack(anchor="w", pady=(0, 15))
        
        # Key selection (put this first and simplified)
        ctk.CTkLabel(
            inner,
            text="Trigger Key (recommended: F9):",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 8))
        
        key_options = ["F9", "F10", "F8", "F7", "F6", "F5", "F4", "F3", "F2", "F1", "F11", "F12", "ScrollLock", "Pause"]
        key_codes_map = {
            "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64,
            "F7": 65, "F8": 66, "F9": 67, "F10": 68, "F11": 87, "F12": 88,
            "ScrollLock": 70, "Pause": 119,
        }
        
        current_key = self.config.get("hotkey_key", 67)
        current_name = "F9"
        for name, code in key_codes_map.items():
            if code == current_key:
                current_name = name
                break
        
        key_var = ctk.StringVar(value=current_name)
        ctk.CTkOptionMenu(
            inner,
            values=key_options,
            variable=key_var,
            font=(self.font_body[0], 14),
            width=200,
            height=40,
            fg_color=COLORS["bg_hover"],
            button_color=COLORS["bg_elevated"],
            button_hover_color=COLORS["border"],
            dropdown_fg_color=COLORS["bg_hover"],
            dropdown_hover_color=COLORS["bg_elevated"],
        ).pack(anchor="w", pady=(0, 20))
        
        # Modifiers section
        ctk.CTkLabel(
            inner,
            text="Modifiers (optional - may conflict with system):",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 8))
        
        mod_frame = ctk.CTkFrame(inner, fg_color="transparent")
        mod_frame.pack(fill="x", pady=(0, 25))
        
        current_mods = self.config.get("hotkey_modifiers", [])
        mod_vars = {}
        for mod in ["ctrl", "alt", "shift"]:  # Removed super - too problematic on KDE
            var = ctk.BooleanVar(value=mod in current_mods)
            mod_vars[mod] = var
            ctk.CTkCheckBox(
                mod_frame,
                text=mod.capitalize(),
                variable=var,
                font=(self.font_body[0], 13),
                text_color=COLORS["text_primary"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
                checkmark_color="#000000",
            ).pack(side="left", padx=(0, 20))
        
        # Save button
        def save():
            new_mods = [mod for mod, var in mod_vars.items() if var.get()]
            key_name = key_var.get()
            new_key = key_codes_map.get(key_name, 67)
            
            self.config["hotkey_modifiers"] = new_mods
            self.config["hotkey_key"] = new_key
            save_config(self.config)
            
            new_hotkey = self.get_hotkey_display()
            
            # Update all UI elements showing the hotkey
            self.hotkey_label.configure(text=f"Press {new_hotkey} to record")
            self.hotkey_btn.configure(text=new_hotkey)
            
            self.log(f"⚙ Hotkey saved: {new_hotkey} (code: {new_key})")
            self.log("↻ Restarting hotkey listener...")
            
            # Restart hotkey listener with new settings
            self.stop_event.set()
            self.stop_event = threading.Event()
            self.start_hotkey_listener()
            self.log("✓ Hotkey listener restarted")
            
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
        ).pack(fill="x", pady=(0, 12))
        
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
        
        # Test section
        test_frame = ctk.CTkFrame(inner, fg_color=COLORS["bg_card"], corner_radius=10)
        test_frame.pack(fill="x", pady=(20, 0))
        
        ctk.CTkLabel(
            test_frame,
            text="💡 Test: Press keys to see if they're detected",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_muted"],
        ).pack(padx=15, pady=(10, 5))
        
        test_label = ctk.CTkLabel(
            test_frame,
            text="Waiting for key press...",
            font=(self.font_mono[0], 12),
            text_color=COLORS["text_secondary"],
        )
        test_label.pack(padx=15, pady=(0, 10))
        
        # Key detection in background (listens to all devices)
        def detect_keys():
            import select
            devices = find_keyboard_devices()
            if not devices:
                return
            fd_to_device = {dev.fd: dev for dev in devices}
            
            while True:
                try:
                    if not dialog.winfo_exists():
                        break
                except:
                    break
                    
                r, _, _ = select.select(list(fd_to_device.keys()), [], [], 0.3)
                for fd in r:
                    device = fd_to_device[fd]
                    for event in device.read():
                        if event.type == ecodes.EV_KEY:
                            key_event = categorize(event)
                            if key_event.keystate == 1:
                                code = key_event.scancode
                                name = ecodes.KEY.get(code, f"code_{code}")
                                if isinstance(name, list):
                                    name = name[0]
                                name = str(name).replace("KEY_", "")
                                try:
                                    dialog.after(0, lambda n=name, c=code: test_label.configure(
                                        text=f"Detected: {n} (code: {c})",
                                        text_color=COLORS["accent"]
                                    ))
                                except:
                                    return
        
        threading.Thread(target=detect_keys, daemon=True).start()

    def open_microphone_settings(self):
        """Open dialog to select audio input device (microphone)."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Select Microphone")
        dialog.geometry("500x480")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        scroll_frame = ctk.CTkScrollableFrame(
            inner,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            height=220,
        )
        scroll_frame.pack(fill="both", expand=True, pady=(0, 20))
        
        # System default option
        default_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        default_frame.pack(fill="x", pady=5, padx=5)
        
        ctk.CTkRadioButton(
            default_frame,
            text="System Default",
            variable=selected_device,
            value=-1,
            font=(self.font_body[0], 13, "bold"),
            text_color=COLORS["text_bright"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_glow"],
        ).pack(side="left")
        
        ctk.CTkLabel(
            default_frame,
            text="(Use system audio settings)",
            font=(self.font_body[0], 11),
            text_color=COLORS["text_secondary"],
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
            display_name = name[:45] + "..." if len(name) > 45 else name
            
            # Mark default device
            if device.get("is_default"):
                display_name += " ★"
            
            name_label = ctk.CTkLabel(
                info_frame,
                text=display_name,
                font=(self.font_body[0], 12),
                text_color=COLORS["text_primary"],
                anchor="w",
            )
            name_label.pack(anchor="w")
            
            # Device details (channels, sample rate)
            details = f"{device['channels']}ch • {device['sample_rate']}Hz"
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
            
            # Update button text
            self.mic_btn.configure(text=self.get_microphone_display())
            
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
        device_id = self.config.get("audio_device")
        
        # Update standard recorder
        self.recorder = AudioRecorder(
            sample_rate=self.config["sample_rate"],
            device=device_id,
            preprocessing=self.config.get("audio_preprocessing", "light"),
        )
        
        # Chunked recorder will be recreated when needed with new device

    def open_device_settings(self):
        """Open dialog to manage which devices can trigger the hotkey."""
        # Check if dialog already exists and is still open
        if self._device_settings_dialog is not None:
            try:
                if self._device_settings_dialog.winfo_exists():
                    # Dialog exists, just focus it
                    self._device_settings_dialog.lift()
                    self._device_settings_dialog.focus_force()
                    return
            except:
                pass
            # Dialog was destroyed, clear reference
            self._device_settings_dialog = None
        
        dialog = ctk.CTkToplevel(self)
        self._device_settings_dialog = dialog
        dialog.title("Hotkey Devices")
        dialog.geometry("500x500")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        # Clear reference when dialog is closed
        def on_dialog_close():
            self._device_settings_dialog = None
            dialog.destroy()
        
        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="⌨️ Hotkey Devices",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Select which keyboards, mice, or keypads can trigger the hotkey.\nUseful for gaming peripherals with extra keys.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Get all available devices
        all_devices = get_all_input_devices()
        enabled = self.config.get("enabled_input_devices", [])
        
        # If nothing enabled, everything is enabled by default
        if not enabled:
            enabled = [d["name"] for d in all_devices]
        
        # Scrollable frame for devices
        scroll_frame = ctk.CTkScrollableFrame(
            inner,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            height=250,
        )
        scroll_frame.pack(fill="both", expand=True, pady=(0, 20))
        
        device_vars = {}
        type_icons = {"keyboard": "⌨️", "mouse": "🖱️", "gamepad": "🎮"}
        
        for device in all_devices:
            var = ctk.BooleanVar(value=device["name"] in enabled)
            device_vars[device["name"]] = var
            
            row = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            row.pack(fill="x", pady=5)
            
            ctk.CTkCheckBox(
                row,
                text="",
                variable=var,
                width=24,
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
                checkmark_color="#000000",
            ).pack(side="left", padx=(5, 10))
            
            icon = type_icons.get(device["type"], "📟")
            short_name = device["name"][:45] + "..." if len(device["name"]) > 45 else device["name"]
            
            ctk.CTkLabel(
                row,
                text=f"{icon} {short_name}",
                font=(self.font_body[0], 12),
                text_color=COLORS["text_primary"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
        
        if not all_devices:
            ctk.CTkLabel(
                scroll_frame,
                text="No input devices found!\n\nMake sure you're in the 'input' group:\nsudo usermod -aG input $USER",
                font=(self.font_body[0], 13),
                text_color=COLORS["accent_red"],
                justify="center",
            ).pack(pady=30)
        
        # Buttons
        btn_frame = ctk.CTkFrame(inner, fg_color="transparent")
        btn_frame.pack(fill="x")
        
        def select_all():
            for var in device_vars.values():
                var.set(True)
        
        def select_none():
            for var in device_vars.values():
                var.set(False)
        
        ctk.CTkButton(
            btn_frame,
            text="Select All",
            font=(self.font_body[0], 12),
            width=100,
            height=32,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=select_all,
        ).pack(side="left", padx=(0, 10))
        
        ctk.CTkButton(
            btn_frame,
            text="Select None",
            font=(self.font_body[0], 12),
            width=100,
            height=32,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=select_none,
        ).pack(side="left")
        
        # Save/Cancel
        def save():
            selected = [name for name, var in device_vars.items() if var.get()]
            
            # If all selected, store empty list (means "all")
            if len(selected) == len(all_devices):
                self.config["enabled_input_devices"] = []
            else:
                self.config["enabled_input_devices"] = selected
            
            save_config(self.config)
            
            # Update button text
            if not self.config["enabled_input_devices"]:
                self.devices_btn.configure(text=f"All ({len(all_devices)})  ▼")
            else:
                self.devices_btn.configure(text=f"{len(selected)} selected  ▼")
            
            self.log(f"⚙ Input devices updated: {len(selected)} enabled")
            self.log("↻ Restarting hotkey listener...")
            
            # Restart listener
            self.stop_event.set()
            self.stop_event = threading.Event()
            self.start_hotkey_listener()
            
            self._device_settings_dialog = None
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
        ).pack(fill="x", pady=(15, 10))
        
        ctk.CTkButton(
            inner,
            text="Cancel",
            font=(self.font_body[0], 13),
            height=40,
            corner_radius=10,
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"],
            command=on_dialog_close,
        ).pack(fill="x")

    def open_speed_settings(self):
        """Open dialog to configure typing speed."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Typing Speed")
        dialog.geometry("420x420")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Typing Speed",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Control how fast text is typed after transcription.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 20))
        
        # Speed options
        speeds = [
            ("Instant", "instant", "No delay - appears immediately"),
            ("Fast", "fast", "1ms delay between characters"),
            ("Normal", "normal", "Default typing speed"),
            ("Slow", "slow", "Slower, more natural feel"),
            ("Very Slow", "very_slow", "Watch it type out"),
        ]
        
        current_speed = self.config.get("typing_speed", "instant")
        speed_var = ctk.StringVar(value=current_speed)
        
        for label, value, desc in speeds:
            frame = ctk.CTkFrame(inner, fg_color="transparent")
            frame.pack(fill="x", pady=6)
            
            radio = ctk.CTkRadioButton(
                frame,
                text=label,
                variable=speed_var,
                value=value,
                font=(self.font_body[0], 14),
                text_color=COLORS["text_primary"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
            )
            radio.pack(side="left")
            
            ctk.CTkLabel(
                frame,
                text=f"  - {desc}",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
            ).pack(side="left", padx=(10, 0))
        
        # Save button
        def save():
            self.config["typing_speed"] = speed_var.get()
            save_config(self.config)
            
            speed_display = speed_var.get().replace("_", " ").title()
            self.speed_btn.configure(text=speed_display)
            self.log(f"⚙ Typing speed: {speed_display}")
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
        ).pack(fill="x", pady=(25, 10))
        
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

    def open_scale_settings(self):
        """Open dialog to adjust UI scale."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("UI Scale")
        dialog.geometry("400x320")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="UI Scale",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Adjust the size of text and controls.\nUse Ctrl+Plus/Minus for quick adjustments.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
            justify="left",
        ).pack(anchor="w", pady=(0, 20))
        
        # Current scale display
        scale_label = ctk.CTkLabel(
            inner,
            text=f"{int(self.ui_scale * 100)}%",
            font=(self.font_header[0], 36, "bold"),
            text_color=COLORS["accent"],
        )
        scale_label.pack(pady=(0, 15))
        
        # Scale slider
        scale_var = ctk.DoubleVar(value=self.ui_scale)
        
        def on_scale_change(value):
            scale_label.configure(text=f"{int(float(value) * 100)}%")
        
        slider = ctk.CTkSlider(
            inner,
            from_=0.7,
            to=2.5,
            variable=scale_var,
            command=on_scale_change,
            width=300,
            height=20,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
        )
        slider.pack(pady=(0, 5))
        
        # Scale labels
        labels_frame = ctk.CTkFrame(inner, fg_color="transparent")
        labels_frame.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(labels_frame, text="70%", font=(self.font_body[0], 10), text_color=COLORS["text_muted"]).pack(side="left")
        ctk.CTkLabel(labels_frame, text="250%", font=(self.font_body[0], 10), text_color=COLORS["text_muted"]).pack(side="right")
        
        # Preset buttons
        presets_frame = ctk.CTkFrame(inner, fg_color="transparent")
        presets_frame.pack(fill="x", pady=(0, 20))
        
        for preset in [100, 125, 150, 175, 200]:
            ctk.CTkButton(
                presets_frame,
                text=f"{preset}%",
                width=50,
                height=30,
                font=(self.font_body[0], 11),
                fg_color=COLORS["bg_hover"] if int(self.ui_scale * 100) != preset else COLORS["accent_dim"],
                hover_color=COLORS["bg_elevated"],
                text_color=COLORS["text_primary"],
                corner_radius=6,
                command=lambda p=preset: (scale_var.set(p/100), on_scale_change(p/100)),
            ).pack(side="left", padx=3)
        
        # Apply button
        def apply_scale():
            new_scale = scale_var.get()
            self.ui_scale = new_scale
            self.config["ui_scale"] = new_scale
            save_config(self.config)
            self._apply_scale()
            self.scale_btn.configure(text=f"{int(new_scale * 100)}%")
            self.log(f"⚙ UI Scale: {int(new_scale * 100)}%")
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
            command=apply_scale,
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

    def open_accuracy_mode_settings(self):
        """Open dialog to select accuracy mode preset."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Accuracy Mode")
        dialog.geometry("450x520")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        dialog.geometry("400x380")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        
        def save():
            self.config["beam_size"] = beam_var.get()
            save_config(self.config)
            self.beam_btn.configure(text=str(beam_var.get()))
            self.log(f"⚙ Beam size: {beam_var.get()}")
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
        dialog.geometry("400x400")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        dialog.geometry("420x400")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        from transcriber import WhisperCppBackend, FasterWhisperBackend
        
        dialog = ctk.CTkToplevel(self)
        dialog.title("Transcription Backend")
        dialog.geometry("500x680")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
        ]
        
        scroll_frame = ctk.CTkScrollableFrame(
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
        
        fw_model_var = ctk.StringVar(value=self.config.get("faster_whisper_model", "small"))
        fw_model_menu = ctk.CTkOptionMenu(
            fw_frame,
            values=["tiny", "base", "small", "medium", "large-v3-turbo", "large-v3"],
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
            
            display = "whisper.cpp" if backend_var.get() == "whisper_cpp" else "Faster-Whisper"
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
        dialog.geometry("420x450")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
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
            models_dir = Path.home() / "whisper.cpp" / "models"
            model_path = models_dir / f"ggml-{model_name}.bin"
            
            if model_path.exists():
                # Store with ~ for portability
                relative_path = f"~/whisper.cpp/models/ggml-{model_name}.bin"
                self.config["model_path"] = relative_path
                save_config(self.config)
                self.log(f"⚙ Model: {model_name}")
                # Update the button in main UI if visible
                try:
                    self.model_btn.configure(text=self.get_model_display())
                except:
                    pass
            else:
                self.log(f"⚠ Model not found: {model_name}")
        return setter
    
    def setup_tray(self):
        self.custom_icon = None
        if ICON_PATH.exists():
            try:
                self.custom_icon = Image.open(ICON_PATH).resize((64, 64))
            except:
                pass
        
        # Create menu with dynamic model submenu (only installed models)
        menu = pystray.Menu(
            pystray.MenuItem("Show", self.show_from_tray, default=True),
            pystray.MenuItem("Record", self.tray_record),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Model",
                self._create_model_tray_menu,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit_app),
        )
        
        icon_image = self.get_tray_icon(AppState.IDLE)
        self.tray_icon = pystray.Icon("wayfinder-voice", icon_image, "Wayfinder Voice", menu)
        
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def get_tray_icon(self, state: AppState, pulse_scale: float = 1.0) -> Image.Image:
        """Create a bold, obvious tray icon with full color fill."""
        color = STATE_COLORS[state]
        size = 64
        
        # Create a solid colored icon for maximum visibility
        icon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(icon)
        
        # Parse the hex color
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        
        # Fill the entire icon with the state color (rounded square)
        draw.rounded_rectangle(
            [4, 4, size - 4, size - 4],
            radius=12,
            fill=(r, g, b, 255),
        )
        
        # Add a subtle inner icon or symbol
        if state == AppState.IDLE:
            # Microphone symbol for ready (simple circle)
            draw.ellipse([22, 18, 42, 46], fill=(255, 255, 255, 200))
            draw.rectangle([28, 42, 36, 52], fill=(255, 255, 255, 200))
        elif state == AppState.RECORDING:
            # Animated pulsing dot for listening!
            # pulse_scale goes from 0.0 to 1.0
            # At 0.0 (min): 15% of square = ~8px diameter, offset = 28
            # At 1.0 (max): 80% of square = ~45px diameter, offset = 10
            min_offset = 28  # Small dot
            max_offset = 10  # Large dot
            offset = int(min_offset - (min_offset - max_offset) * pulse_scale)
            draw.ellipse(
                [offset, offset, size - offset, size - offset], 
                fill=(255, 255, 255, 230)
            )
        elif state == AppState.PROCESSING:
            # Gear/spinner symbol
            draw.ellipse([20, 20, 44, 44], outline=(255, 255, 255, 230), width=4)
            draw.ellipse([26, 26, 38, 38], fill=(r, g, b, 255))
        else:  # PASTING
            # Text/typing symbol
            draw.rectangle([16, 22, 48, 28], fill=(255, 255, 255, 200))
            draw.rectangle([16, 32, 40, 38], fill=(255, 255, 255, 200))
            draw.rectangle([16, 42, 44, 48], fill=(255, 255, 255, 200))
        
        return icon

    def update_tray(self, state: AppState):
        if self.tray_icon:
            self.tray_icon.icon = self.get_tray_icon(state)
            self.tray_icon.title = f"Wayfinder Voice - {STATE_LABELS[state]}"
        
        # Start/stop tray pulse animation based on state
        if state == AppState.RECORDING:
            self._start_tray_pulse()
        else:
            self._stop_tray_pulse()
    
    def _start_tray_pulse(self):
        """Start the tray icon pulsing animation."""
        if not hasattr(self, '_tray_pulse_job'):
            self._tray_pulse_job = None
        if not hasattr(self, '_tray_pulse_phase'):
            self._tray_pulse_phase = 0.0
        self._tray_pulse_step()
    
    def _tray_pulse_step(self):
        """Animate one step of the tray icon pulse."""
        if self.app_state != AppState.RECORDING:
            return
        
        import math
        
        # Update phase for smooth sine wave pulse
        self._tray_pulse_phase += 0.15  # Speed of pulse
        
        # Sine wave from 0 to 1 (smooth in-out)
        pulse_scale = 0.5 + 0.5 * math.sin(self._tray_pulse_phase)
        
        # Update tray icon with new pulse scale
        if self.tray_icon:
            self.tray_icon.icon = self.get_tray_icon(AppState.RECORDING, pulse_scale)
        
        # Schedule next step (~20fps for tray icon)
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
        self.withdraw()

    def tray_record(self):
        self.after(0, self.on_record_button)

    def quit_app(self, icon=None, item=None):
        os.kill(os.getpid(), signal.SIGKILL)

    # === State Management ===
    
    def update_state(self, new_state: AppState):
        self.app_state = new_state
        color = STATE_COLORS[new_state]
        
        # Redraw the enhanced status indicator with new color
        self._draw_status_indicator(color)
        self.status_label.configure(text=STATE_LABELS[new_state], text_color=color)
        
        # Update status card border for subtle glow effect
        if new_state == AppState.IDLE:
            self.status_card.configure(border_color=COLORS["border"])
        else:
            # Subtle glow border matching state color
            self.status_card.configure(border_color=color)
        
        self.update_tray(new_state)
        self.update_record_button(new_state)

    def update_record_button(self, state: AppState):
        if state == AppState.IDLE:
            self.record_btn.configure(
                text="Start Listening",
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
                state="normal",
            )
        elif state == AppState.RECORDING:
            self.record_btn.configure(
                text="Stop Listening",
                fg_color=COLORS["accent_red"],
                hover_color="#CC3355",
                state="normal",
            )
        else:
            self.record_btn.configure(
                text="Processing...",
                fg_color=COLORS["accent_yellow"],
                hover_color="#CC9520",
                state="disabled",
            )

    def on_record_button(self):
        self.on_hotkey()

    # === Hotkey & Events ===
    
    def start_hotkey_listener(self):
        hotkey_key = self.config.get("hotkey_key", 67)
        hotkey_modifiers = self.config.get("hotkey_modifiers", [])
        enabled_devices = self.config.get("enabled_input_devices", [])
        hotkey_display = self.get_hotkey_display()
        
        # Always start socket listener (most reliable for Wayland)
        threading.Thread(
            target=socket_listener,
            args=(self.event_queue, self.stop_event, self.log),
            daemon=True,
        ).start()
        
        # Check if we're on Wayland
        is_wayland = os.environ.get("XDG_SESSION_TYPE") == "wayland"
        
        if is_wayland:
            self.log("🖥️ Wayland detected")
            self.log("💡 Configure shortcut in System Settings → Shortcuts → Applications → Wayfinder Voice")
        else:
            self.log("🖥️ X11 detected - using evdev")
            threading.Thread(
                target=hotkey_listener,
                args=(self.event_queue, hotkey_key, hotkey_modifiers, self.stop_event, enabled_devices, self.log),
                daemon=True,
            ).start()

    def poll_events(self):
        try:
            while True:
                event_type, data = self.event_queue.get_nowait()
                self.handle_event(event_type, data)
        except queue.Empty:
            pass
        self.after(100, self.poll_events)

    def handle_event(self, event_type, data):
        if event_type == EventType.HOTKEY_PRESSED:
            self.on_hotkey()
        elif event_type == EventType.TRANSCRIPTION_DONE:
            self.on_transcription_done(data)
        elif event_type == EventType.TRANSCRIPTION_ERROR:
            self.on_error(f"Transcription: {data}")
        elif event_type == EventType.INJECTION_DONE:
            self.on_injection_done()
        elif event_type == EventType.INJECTION_ERROR:
            self.on_error(f"Injection: {data}")
        elif event_type == EventType.CHUNK_TRANSCRIBED:
            chunk_index, text, had_context = data if len(data) == 3 else (*data, False)
            preview = text[:30] + "..." if len(text) > 30 else text
            context_indicator = " →" if had_context else ""
            self.log(f"✓ Chunk {chunk_index + 1}{context_indicator}: \"{preview}\"")
        elif event_type == EventType.CHUNKED_TRANSCRIPTION_DONE:
            self.on_transcription_done(data)

    def on_hotkey(self):
        if self.app_state == AppState.IDLE:
            self.start_recording()
        elif self.app_state == AppState.RECORDING:
            self.stop_recording_and_process()

    def start_recording(self):
        try:
            self.log("🎤 Listening...")
            
            # Show floating indicator
            if self.indicator:
                self.indicator.show("Listening...", COLORS["accent_red"])
            
            # Check if chunked mode is enabled
            if self.config.get("chunked_mode", True):
                self._start_chunked_recording()
            else:
                self.recorder.start()
            
            self.update_state(AppState.RECORDING)
            
            # Start duration update timer
            import time
            self._recording_start_time = time.time()
            self._update_recording_duration()
            
        except Exception as e:
            self.on_error(f"Microphone: {e}")
    
    def _start_chunked_recording(self):
        """Start recording with chunked processing for indefinite duration."""
        self.chunk_transcriptions = []
        
        def on_chunk_ready(chunk_path: str, chunk_index: int):
            """Called when a chunk is ready for transcription."""
            self.log(f"📦 Chunk {chunk_index + 1} ready")
            # Submit chunk for transcription in background
            self.transcription_executor.submit(
                self._transcribe_chunk, chunk_path, chunk_index
            )
        
        self.chunked_recorder = ChunkedRecorder(
            sample_rate=self.config["sample_rate"],
            device=self.config["audio_device"],
            preprocessing=self.config.get("audio_preprocessing", "light"),
            chunk_duration=self.config.get("chunk_duration", 30),
            chunk_overlap=self.config.get("chunk_overlap", 2),
            on_chunk_ready=on_chunk_ready,
        )
        self.chunked_recorder.start()
    
    def _transcribe_chunk(self, chunk_path: str, chunk_index: int):
        """Transcribe a single chunk in the background."""
        try:
            # Get context from previous chunk for continuity
            context = ""
            if chunk_index > 0:
                with self.chunk_transcription_lock:
                    if len(self.chunk_transcriptions) >= chunk_index:
                        prev_text = self.chunk_transcriptions[chunk_index - 1]
                        if prev_text:
                            context = prev_text
            
            text = transcribe_with_config(chunk_path, self.config, context=context)
            with self.chunk_transcription_lock:
                # Ensure list is large enough
                while len(self.chunk_transcriptions) <= chunk_index:
                    self.chunk_transcriptions.append("")
                self.chunk_transcriptions[chunk_index] = text.strip()
            
            # Log with context indicator for chunks after the first
            if chunk_index > 0 and context:
                self.event_queue.put((EventType.CHUNK_TRANSCRIBED, (chunk_index, text, True)))  # True = had context
            else:
                self.event_queue.put((EventType.CHUNK_TRANSCRIBED, (chunk_index, text, False)))
        except Exception as e:
            self.log(f"⚠ Chunk {chunk_index + 1} error: {e}")
    
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
        
        # Update floating indicator to processing
        if self.indicator:
            self.indicator.update("Processing...", COLORS["accent_yellow"])
        
        self.update_state(AppState.PROCESSING)
        
        try:
            # Check which recorder was used
            if self.chunked_recorder is not None and self.chunked_recorder.is_recording():
                self._stop_chunked_recording()
            else:
                self._stop_simple_recording()
        except Exception as e:
            self.on_error(f"Processing: {e}")
    
    def _stop_simple_recording(self):
        """Stop simple (non-chunked) recording and process."""
        duration = self.recorder.get_duration()
        self.log(f"⏱ Duration: {duration:.1f}s")
        
        if duration < self.config["min_recording_duration"]:
            self.recorder.stop()
            self.recorder.cleanup()
            self.on_error("Too short - speak longer")
            return
        
        audio_path = self.recorder.stop()
        self.executor.submit(self.transcribe_and_inject, audio_path)
    
    def _stop_chunked_recording(self):
        """Stop chunked recording and process all chunks."""
        duration = self.chunked_recorder.get_duration()
        chunk_count = self.chunked_recorder.get_chunk_count()
        self.log(f"⏱ Duration: {duration:.1f}s ({chunk_count} chunks)")
        
        if duration < self.config["min_recording_duration"]:
            self.chunked_recorder.stop()
            self.chunked_recorder.cleanup()
            self.chunked_recorder = None
            self.on_error("Too short - speak longer")
            return
        
        # Stop and get final chunk
        final_path, all_paths = self.chunked_recorder.stop()
        
        # Submit final chunk for transcription if exists
        if final_path:
            final_index = chunk_count
            self.log(f"📦 Final chunk ready")
            self.transcription_executor.submit(
                self._transcribe_chunk, final_path, final_index
            )
        
        # Wait for all transcriptions to complete and combine
        self.executor.submit(self._finalize_chunked_transcription, chunk_count + (1 if final_path else 0))
    
    def _finalize_chunked_transcription(self, expected_chunks: int):
        """Wait for all chunks to be transcribed and combine them."""
        # Wait for all chunks to be transcribed (with timeout)
        timeout = 120  # 2 minutes max wait
        start_time = time.time()
        
        while True:
            with self.chunk_transcription_lock:
                completed = len([t for t in self.chunk_transcriptions if t])
            
            if completed >= expected_chunks:
                break
            
            if time.time() - start_time > timeout:
                self.log(f"⚠ Timeout: only {completed}/{expected_chunks} chunks transcribed")
                break
            
            time.sleep(0.5)
        
        # Combine all transcriptions with overlap deduplication
        with self.chunk_transcription_lock:
            combined_text = self._deduplicate_overlap_text(self.chunk_transcriptions)
        
        # Cleanup
        if self.chunked_recorder:
            self.chunked_recorder.cleanup()
            self.chunked_recorder = None
        
        if combined_text.strip():
            self.log(f"📝 \"{combined_text[:50]}{'...' if len(combined_text) > 50 else ''}\"")
            self.event_queue.put((EventType.CHUNKED_TRANSCRIPTION_DONE, combined_text))
        else:
            self.event_queue.put((EventType.TRANSCRIPTION_ERROR, "No speech detected"))
    
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
        
        # Filter out empty transcriptions
        valid_transcriptions = [t.strip() for t in transcriptions if t and t.strip()]
        
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

    def transcribe_and_inject(self, audio_path):
        try:
            self.log("🔄 Transcribing...")
            text = transcribe_with_config(audio_path, self.config)
            self.log(f"📝 \"{text[:50]}{'...' if len(text) > 50 else ''}\"")
            self.event_queue.put((EventType.TRANSCRIPTION_DONE, text))
        except Exception as e:
            self.event_queue.put((EventType.TRANSCRIPTION_ERROR, str(e)))
        finally:
            self.recorder.cleanup()

    def on_transcription_done(self, text):
        if not text.strip():
            self.on_error("No speech detected")
            return
        self.update_state(AppState.PASTING)
        self.executor.submit(self.do_inject, text)

    def do_inject(self, text):
        try:
            typing_speed = self.config.get("typing_speed", "instant")
            inject_text(text, typing_speed=typing_speed)
            self.event_queue.put((EventType.INJECTION_DONE, None))
        except Exception as e:
            self.event_queue.put((EventType.INJECTION_ERROR, str(e)))

    def on_injection_done(self):
        self.log("✓ Text inserted")
        # Hide floating indicator
        if self.indicator:
            self.indicator.hide()
        self.update_state(AppState.IDLE)

    def on_error(self, message):
        self.log(f"⚠ {message}")
        # Hide floating indicator
        if self.indicator:
            self.indicator.hide()
        self.update_state(AppState.IDLE)


def main():
    app = WayfinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

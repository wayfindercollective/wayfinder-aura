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

CONFIG_DIR = Path.home() / ".config" / "wayfinder-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
SCRIPT_DIR = Path(__file__).parent.resolve()
ICON_PATH = SCRIPT_DIR / "assets" / "icon.png"

DEFAULT_CONFIG = {
    "whisper_binary": "~/whisper.cpp/build/bin/whisper-cli",
    "model_path": "~/whisper.cpp/models/ggml-large-v3-turbo.bin",
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


# Setting tooltip descriptions
SETTING_TOOLTIPS = {
    "hotkey": "The keyboard shortcut to start/stop voice recording. Press and release to toggle.",
    "input_devices": "Which input devices to listen for the hotkey. Select specific devices or use all available.",
    "typing_speed": "How fast the transcribed text is typed out. Instant pastes immediately, others simulate typing.",
    "whisper_model": "The Whisper AI model for transcription. Larger models are more accurate but slower.",
    "prompt": "Initial text that guides the transcription style. Helps with punctuation and formatting.",
    "start_minimized": "Start the app minimized to the system tray instead of showing the main window.",
    "ui_scale": "Adjust the size of the user interface for different screen sizes or preferences.",
    "accuracy_mode": "Preset balancing speed vs accuracy. Fast is quicker, High Accuracy uses more processing.",
    "beam_size": "Search width for transcription. Higher values explore more possibilities but take longer.",
    "language": "The language for transcription. English is optimized, Auto-detect works for multiple languages.",
    "audio_preprocessing": "Audio processing level. Light = gain only (recommended). Medium adds filtering. Heavy adds noise gate (may cut words).",
    "chunked_mode": "Process long recordings in segments. Prevents memory issues and allows progressive output.",
    "chunk_duration": "Length of each audio segment when using chunked recording. Shorter chunks use less memory.",
    "backend": "The transcription engine. whisper.cpp is lightweight, Faster-Whisper has better GPU support.",
    "gpu_acceleration": "Use your GPU for faster transcription. Requires compatible hardware and drivers.",
    "gpu_layers": "How many model layers to run on GPU. Auto uses all layers, or set a specific number.",
}


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
    
    def __init__(self, parent: ctk.CTk, target_fps: int = 0):
        self.parent = parent
        self.window: ctk.CTkToplevel | None = None
        self.label: ctk.CTkLabel | None = None
        self.pulse_job: str | None = None
        self.follow_job: str | None = None
        self.pulse_state = 0
        self._visible = False
        self._window_width = 0
        self._window_height = 0
        
        # Track cursor position for stuck detection
        self._last_cursor_x = 0
        self._last_cursor_y = 0
        self._stuck_count = 0
        self._is_centered = False
        
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
    
    def get_fps_info(self) -> str:
        """Return a string describing the current FPS setting."""
        if self._fps_auto_detected:
            return f"{self._target_fps} Hz (auto-detected)"
        else:
            return f"{self._target_fps} Hz (manual)"
        
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
        
        # Configure the window
        self.window.configure(fg_color=COLORS["bg_card"])
        
        # Create a frame with rounded appearance
        frame = ctk.CTkFrame(
            self.window,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=2,
            border_color=color,
        )
        frame.pack(padx=2, pady=2)
        
        # Status indicator dot
        self.dot_canvas = ctk.CTkCanvas(
            frame,
            width=12,
            height=12,
            bg=COLORS["bg_card"],
            highlightthickness=0,
        )
        self.dot_canvas.pack(side="left", padx=(12, 6), pady=12)
        self._draw_dot(color)
        
        # Label with status text
        self.label = ctk.CTkLabel(
            frame,
            text=text,
            font=("Inter", 14, "bold"),
            text_color=COLORS["text_bright"],
        )
        self.label.pack(side="left", padx=(0, 16), pady=12)
        
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
            # Update border color
            for widget in self.window.winfo_children():
                if isinstance(widget, ctk.CTkFrame):
                    widget.configure(border_color=color)
                    
    def hide(self) -> None:
        """Hide and destroy the floating indicator."""
        self._stop_pulse()
        self._stop_follow()
        self._visible = False
        
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            self.window = None
            self.label = None
            self.dot_canvas = None
    
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
        
        # Center horizontally, position above taskbar (typically ~60px from bottom)
        pos_x = (screen_w - self._window_width) // 2
        pos_y = screen_h - self._window_height - 70  # 70px above bottom for taskbar
        
        self.window.geometry(f"+{pos_x}+{pos_y}")
        self._is_centered = True
            
    def _draw_dot(self, color: str, scale: float = 1.0) -> None:
        """Draw the status indicator dot."""
        if not self.dot_canvas:
            return
            
        self.dot_canvas.delete("all")
        
        # Parse color
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        
        # Draw glow (larger, faded circle)
        size = int(10 * scale)
        offset = (12 - size) // 2
        glow_color = f"#{r//3:02x}{g//3:02x}{b//3:02x}"
        self.dot_canvas.create_oval(
            offset - 1, offset - 1,
            offset + size + 1, offset + size + 1,
            fill=glow_color,
            outline="",
        )
        
        # Draw main dot
        inner_size = int(8 * scale)
        inner_offset = (12 - inner_size) // 2
        self.dot_canvas.create_oval(
            inner_offset, inner_offset,
            inner_offset + inner_size, inner_offset + inner_size,
            fill=color,
            outline="",
        )
        
    def _start_pulse(self) -> None:
        """Start the pulsing animation."""
        self._pulse_step()
        
    def _pulse_step(self) -> None:
        """Animate one pulse step."""
        if not self._visible:
            return
            
        self.pulse_state = (self.pulse_state + 1) % 20
        
        # Create a subtle pulse effect (scale between 0.8 and 1.2)
        if self.pulse_state < 10:
            scale = 0.8 + (self.pulse_state * 0.04)
        else:
            scale = 1.2 - ((self.pulse_state - 10) * 0.04)
            
        self._draw_dot(self.current_color, scale)
        
        # Schedule next step
        if self.window:
            self.pulse_job = self.window.after(50, self._pulse_step)
            
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
        """Update position to follow cursor."""
        if not self._visible or not self.window:
            return
            
        try:
            x = self.parent.winfo_pointerx()
            y = self.parent.winfo_pointery()
            
            # Check if cursor position is "stuck" (not changing = likely over native Wayland app)
            if x == self._last_cursor_x and y == self._last_cursor_y:
                self._stuck_count += 1
            else:
                self._stuck_count = 0
                self._last_cursor_x = x
                self._last_cursor_y = y
            
            # If stuck for ~0.5 seconds (15 frames at 30fps), center above taskbar
            if self._stuck_count > 15:
                if not self._is_centered:
                    self._center_above_taskbar()
            else:
                self._update_position(x, y)
            
            # Periodically re-assert topmost to fight Wayland stacking
            self.window.attributes("-topmost", True)
            self.window.lift()
        except:
            # On any error, fall back to centered
            if not self._is_centered:
                self._center_above_taskbar()
        
        # Schedule next update at target FPS (synced to monitor refresh rate)
        if self.window:
            self.follow_job = self.window.after(self._frame_interval_ms, self._follow_step)
            
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
        
        self.setup_window()
        self.indicator = FloatingIndicator(self, target_fps=self.config.get("indicator_fps", 0))
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
        # Update window size
        base_w, base_h = 480, 720
        new_w = int(base_w * self.ui_scale)
        new_h = int(base_h * self.ui_scale)
        self.geometry(f"{new_w}x{new_h}")
        self.minsize(int(400 * self.ui_scale), int(600 * self.ui_scale))
        
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
        
        # Content frame on top of gradient
        main = ctk.CTkFrame(self.main_container, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=24, pady=20)
        
        # === Header - Horizontal "WAYFINDER VOICE" ===
        header = ctk.CTkFrame(main, fg_color="transparent")
        header.pack(fill="x", pady=(0, 24))
        
        # Title container for horizontal layout
        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left")
        
        # "WAYFINDER" in silver/gray
        ctk.CTkLabel(
            title_frame,
            text="WAYFINDER",
            font=(self.font_header[0], 26, "bold"),
            text_color="#B8B8C8",  # Silver gray
        ).pack(side="left")
        
        # "VOICE" in accent cyan
        ctk.CTkLabel(
            title_frame,
            text=" VOICE",
            font=(self.font_header[0], 26, "bold"),
            text_color=COLORS["accent"],
        ).pack(side="left")
        
        # Close button - minimal
        ctk.CTkButton(
            header,
            text="×",
            width=36,
            height=36,
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            font=(self.font_body[0], 20),
            corner_radius=8,
            command=self.hide_to_tray,
        ).pack(side="right")
        
        # === Status Card with Glow ===
        status_card = ctk.CTkFrame(
            main,
            fg_color=COLORS["bg_card"],
            corner_radius=16,
            border_width=1,
            border_color=COLORS["border"],
        )
        status_card.pack(fill="x", pady=(0, 20))
        
        status_inner = ctk.CTkFrame(status_card, fg_color="transparent")
        status_inner.pack(fill="x", padx=24, pady=28)
        
        # Enhanced status indicator with ring design
        self.status_canvas = ctk.CTkCanvas(
            status_inner,
            width=90,
            height=90,
            bg=COLORS["bg_card"],
            highlightthickness=0,
        )
        self.status_canvas.pack(side="left")
        
        # Draw multi-layer glowing ring indicator
        self._draw_status_indicator(STATE_COLORS[AppState.IDLE])
        
        # Status text
        status_text = ctk.CTkFrame(status_inner, fg_color="transparent")
        status_text.pack(side="left", padx=24, fill="both", expand=True)
        
        self.status_label = ctk.CTkLabel(
            status_text,
            text="Ready",
            font=(self.font_header[0], 28, "bold"),
            text_color=STATE_COLORS[AppState.IDLE],
            anchor="w",
        )
        self.status_label.pack(anchor="w")
        
        self.hotkey_label = ctk.CTkLabel(
            status_text,
            text=f"Press {self.get_hotkey_display()} to record",
            font=(self.font_body[0], 13),
            text_color=COLORS["text_secondary"],
            anchor="w",
        )
        self.hotkey_label.pack(anchor="w", pady=(6, 0))
        
        # === Record Button - Premium styling ===
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
        
        # === Settings Section ===
        settings_header = ctk.CTkFrame(main, fg_color="transparent")
        settings_header.pack(fill="x", pady=(0, 8))
        
        ctk.CTkLabel(
            settings_header,
            text="SETTINGS",
            font=(self.font_body[0], 11, "bold"),
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
        settings_card.pack(fill="x", pady=(0, 20))
        
        # Hotkey setting
        self.hotkey_btn = self.create_setting_row(
            settings_card,
            "Hotkey",
            self.get_hotkey_display(),
            self.open_hotkey_settings,
            tooltip=SETTING_TOOLTIPS["hotkey"],
        )
        
        # Input devices setting
        device_count = len(get_all_input_devices())
        enabled = self.config.get("enabled_input_devices", [])
        device_text = f"All ({device_count})" if not enabled else f"{len(enabled)} selected"
        self.devices_btn = self.create_setting_row(
            settings_card,
            "Input Devices",
            device_text,
            self.open_device_settings,
            tooltip=SETTING_TOOLTIPS["input_devices"],
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
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            anchor="w",
            height=28,
            command=self.toggle_advanced_settings,
        )
        advanced_toggle_btn.pack(side="left")
        self.advanced_toggle_btn = advanced_toggle_btn
        
        # Advanced container (collapsible) - use scrollable frame for all options
        self.advanced_container = ctk.CTkFrame(main, fg_color="transparent")
        # Initially collapsed - don't pack
        
        advanced_card = ctk.CTkScrollableFrame(
            self.advanced_container,
            fg_color=COLORS["bg_card"],
            corner_radius=14,
            border_width=1,
            border_color=COLORS["border"],
            height=300,  # Fixed height with scroll
        )
        advanced_card.pack(fill="x", pady=(0, 10))
        
        # Accuracy settings label
        ctk.CTkLabel(
            advanced_card,
            text="Accuracy",
            font=(self.font_body[0], 10, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=18, pady=(12, 4))
        
        # Accuracy Mode preset selector
        accuracy_mode = self.config.get("accuracy_mode", "balanced")
        mode_display = {"fast": "Fast", "balanced": "Balanced", "high": "High Accuracy"}.get(accuracy_mode, "Balanced")
        self.accuracy_mode_btn = self.create_setting_row(
            advanced_card,
            "Accuracy Mode",
            mode_display,
            self.open_accuracy_mode_settings,
            tooltip=SETTING_TOOLTIPS["accuracy_mode"],
        )
        
        # Beam size setting
        beam_size = self.config.get("beam_size", 8)
        self.beam_btn = self.create_setting_row(
            advanced_card,
            "Beam Size",
            str(beam_size),
            self.open_beam_settings,
            tooltip=SETTING_TOOLTIPS["beam_size"],
        )
        
        # Language setting
        language = self.config.get("language", "en")
        lang_display = "English" if language == "en" else "Auto-detect" if language == "auto" else language.upper()
        self.language_btn = self.create_setting_row(
            advanced_card,
            "Language",
            lang_display,
            self.open_language_settings,
            tooltip=SETTING_TOOLTIPS["language"],
        )
        
        # Audio preprocessing level dropdown
        preprocess_level = self.config.get("audio_preprocessing", "light")
        # Handle legacy boolean values
        if preprocess_level is True:
            preprocess_level = "light"
        elif preprocess_level is False:
            preprocess_level = "off"
        
        preprocess_row = ctk.CTkFrame(advanced_card, fg_color="transparent")
        preprocess_row.pack(fill="x", padx=18, pady=11)
        
        # Left side: label + info icon (matches create_setting_row style)
        left_frame = ctk.CTkFrame(preprocess_row, fg_color="transparent")
        left_frame.pack(side="left")
        
        ctk.CTkLabel(
            left_frame,
            text="Audio Processing",
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
        info_label = ctk.CTkLabel(
            left_frame,
            text="ⓘ",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_muted"],
            cursor="hand2",
        )
        info_label.pack(side="left", padx=(6, 0))
        info_label.bind("<Button-1>", lambda e: self.show_preprocessing_help())
        
        # Right side: description + dropdown (styled like other buttons)
        right_frame = ctk.CTkFrame(preprocess_row, fg_color="transparent")
        right_frame.pack(side="right")
        
        # Brief description label
        self.preprocess_desc_label = ctk.CTkLabel(
            right_frame,
            text=self._get_preprocess_desc(preprocess_level),
            font=(self.font_body[0], 11),
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
            font=(self.font_body[0], 12),
            width=100,
            height=32,
            corner_radius=8,
        )
        self.preprocess_dropdown.pack(side="right")
        
        # Chunked recording settings label
        ctk.CTkLabel(
            advanced_card,
            text="Recording",
            font=(self.font_body[0], 10, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=18, pady=(12, 4))
        
        # Chunked mode toggle
        self.chunked_var = ctk.BooleanVar(value=self.config.get("chunked_mode", True))
        self.create_toggle_row(
            advanced_card,
            "Chunked Recording",
            self.chunked_var,
            self.toggle_chunked_mode,
            tooltip=SETTING_TOOLTIPS["chunked_mode"],
        )
        
        # Chunk duration setting
        chunk_duration = self.config.get("chunk_duration", 30)
        self.chunk_btn = self.create_setting_row(
            advanced_card,
            "Chunk Duration",
            f"{chunk_duration}s",
            self.open_chunk_settings,
            tooltip=SETTING_TOOLTIPS["chunk_duration"],
        )
        
        # GPU Acceleration settings label
        ctk.CTkLabel(
            advanced_card,
            text="GPU Acceleration",
            font=(self.font_body[0], 10, "bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=18, pady=(12, 4))
        
        # Transcription backend setting
        backend = self.config.get("transcription_backend", "whisper_cpp")
        backend_display = "whisper.cpp" if backend == "whisper_cpp" else "Faster-Whisper"
        self.backend_btn = self.create_setting_row(
            advanced_card,
            "Backend",
            backend_display,
            self.open_backend_settings,
            tooltip=SETTING_TOOLTIPS["backend"],
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
        
        # GPU layers setting (only relevant for whisper.cpp)
        gpu_layers = self.config.get("gpu_layers", 0)
        layers_display = "Auto (all)" if gpu_layers == 0 else str(gpu_layers)
        self.gpu_layers_btn = self.create_setting_row(
            advanced_card,
            "GPU Layers",
            layers_display,
            self.open_gpu_layers_settings,
            tooltip=SETTING_TOOLTIPS["gpu_layers"],
        )
        
        # === Collapsible Activity Log ===
        self.log_expanded = False
        
        # Log header (always visible)
        self.log_header_frame = ctk.CTkFrame(main, fg_color="transparent")
        self.log_header_frame.pack(fill="x", pady=(0, 8))
        
        # Clickable header to expand/collapse
        log_toggle_btn = ctk.CTkButton(
            self.log_header_frame,
            text="▶  ACTIVITY",
            font=(self.font_body[0], 11, "bold"),
            text_color=COLORS["text_muted"],
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            anchor="w",
            height=28,
            command=self.toggle_activity_log,
        )
        log_toggle_btn.pack(side="left")
        self.log_toggle_btn = log_toggle_btn
        
        ctk.CTkButton(
            self.log_header_frame,
            text="Clear",
            width=50,
            height=24,
            font=(self.font_body[0], 10),
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_muted"],
            corner_radius=6,
            command=self.clear_log,
        ).pack(side="right")
        
        # Log container (collapsible)
        self.log_container = ctk.CTkFrame(main, fg_color="transparent")
        # Initially collapsed - don't pack
        
        self.log_frame = ctk.CTkFrame(
            self.log_container,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )
        self.log_frame.pack(fill="both", expand=True)
        
        self.log_textbox = ctk.CTkTextbox(
            self.log_frame,
            font=(self.font_mono[0], 10),
            fg_color="transparent",
            text_color=COLORS["text_secondary"],
            wrap="word",
            activate_scrollbars=True,
            height=120,
        )
        self.log_textbox.pack(fill="both", expand=True, padx=12, pady=12)
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
        """Draw enhanced ring-style status indicator with glow."""
        canvas = self.status_canvas
        canvas.delete("all")
        
        size = 90
        cx, cy = size // 2, size // 2
        
        # Parse color for glow
        r = int(color[1:3], 16)
        g = int(color[3:5], 16)
        b = int(color[5:7], 16)
        
        # Outer glow rings (fading out)
        for i in range(4, 0, -1):
            alpha = 0.15 / i
            glow_color = f"#{int(r*alpha):02x}{int(g*alpha):02x}{int(b*alpha):02x}"
            offset = i * 4
            canvas.create_oval(
                cx - 38 - offset, cy - 38 - offset,
                cx + 38 + offset, cy + 38 + offset,
                outline=glow_color,
                width=3,
                tags="glow",
            )
        
        # Main ring (thicker)
        canvas.create_oval(
            cx - 32, cy - 32,
            cx + 32, cy + 32,
            outline=color,
            width=5,
            tags="ring",
        )
        
        # Inner filled circle
        canvas.create_oval(
            cx - 18, cy - 18,
            cx + 18, cy + 18,
            fill=color,
            outline="",
            tags="center",
        )
        
        # Highlight dot
        canvas.create_oval(
            cx - 8, cy - 12,
            cx + 2, cy - 4,
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
        """Get brief description for audio processing level."""
        descs = {
            "off": "Raw audio",
            "light": "Volume only",
            "medium": "Filter noise",
            "heavy": "Full cleanup",
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
            ("off", "No processing", "Raw audio passed directly to Whisper. Use if words are being cut off."),
            ("light", "Light (Recommended)", "Only adjusts volume levels. Preserves all speech including soft sounds."),
            ("medium", "Medium", "Adds a filter to remove low rumble/hum. Good for noisy environments."),
            ("heavy", "Heavy", "Adds noise gate that reduces quiet sounds. May cut off soft consonants."),
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

    def toggle_gpu(self):
        """Toggle GPU acceleration."""
        self.config["use_gpu"] = self.gpu_var.get()
        save_config(self.config)
        status = "enabled" if self.gpu_var.get() else "disabled"
        self.log(f"⚙ GPU acceleration: {status}")

    def create_setting_row(self, parent, label, value, command, tooltip=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=11)
        
        # Left side: label + optional info icon
        left_frame = ctk.CTkFrame(row, fg_color="transparent")
        left_frame.pack(side="left")
        
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
                font=(self.font_body[0], 12),
                text_color=COLORS["text_muted"],
                cursor="question_arrow",
            )
            info_label.pack(side="left", padx=(6, 0))
            ToolTip(info_label, tooltip)
        
        btn = ctk.CTkButton(
            row,
            text=value,
            font=(self.font_body[0], 12),
            fg_color=COLORS["bg_hover"],
            hover_color=COLORS["bg_elevated"],
            text_color=COLORS["accent"],
            height=32,
            corner_radius=8,
            command=command,
        )
        btn.pack(side="right")
        return btn

    def create_toggle_row(self, parent, label, variable, command, tooltip=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=11)
        
        # Left side: label + optional info icon
        left_frame = ctk.CTkFrame(row, fg_color="transparent")
        left_frame.pack(side="left")
        
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
                font=(self.font_body[0], 12),
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
        )
        switch.pack(side="right")

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
        
        # Define model info
        model_info = {
            "ggml-tiny.en.bin": ("Tiny (English)", "75MB", "⚡⚡⚡⚡ Fastest"),
            "ggml-base.en.bin": ("Base (English)", "142MB", "⚡⚡⚡ Fast"),
            "ggml-small.en.bin": ("Small (English)", "466MB", "⚡⚡ Good"),
            "ggml-medium.en.bin": ("Medium (English)", "1.5GB", "⚡ Slow"),
            "ggml-large-v3-turbo.bin": ("Large v3 Turbo", "1.6GB", "🚀 Fast+Accurate"),
            "ggml-large-v3-turbo-q5_0.bin": ("Large v3 Turbo Q5", "547MB", "🚀 Fast+Accurate"),
            "ggml-tiny.bin": ("Tiny (Multi-lang)", "75MB", "⚡⚡⚡⚡ Fastest"),
            "ggml-base.bin": ("Base (Multi-lang)", "142MB", "⚡⚡⚡ Fast"),
            "ggml-small.bin": ("Small (Multi-lang)", "466MB", "⚡⚡ Good"),
            "ggml-medium.bin": ("Medium (Multi-lang)", "1.5GB", "⚡ Slow"),
            "ggml-large-v3.bin": ("Large v3", "3GB", "🎯 Most Accurate"),
            "ggml-large.bin": ("Large (Multi-lang)", "3GB", "🐌 Slowest"),
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
        """Open dialog to select whisper model."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Whisper Model")
        dialog.geometry("500x520")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Select Model",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Larger models are more accurate but slower.",
            font=(self.font_body[0], 12),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(0, 20))
        
        # Get available models
        models = self.get_available_models()
        current_path = os.path.expanduser(self.config.get("model_path", ""))
        
        if not models:
            ctk.CTkLabel(
                inner,
                text="No models found!\n\nDownload models with:\ncd ~/whisper.cpp/models\n./download-ggml-model.sh small.en",
                font=(self.font_body[0], 13),
                text_color=COLORS["accent_red"],
                justify="left",
            ).pack(pady=30)
            return
        
        # Model selection
        model_var = ctk.StringVar(value=current_path)
        
        scroll_frame = ctk.CTkScrollableFrame(
            inner,
            fg_color=COLORS["bg_card"],
            corner_radius=12,
            height=280,
        )
        scroll_frame.pack(fill="both", expand=True, pady=(0, 20))
        
        for model in models:
            is_current = os.path.expanduser(model["path"]) == current_path
            
            frame = ctk.CTkFrame(
                scroll_frame,
                fg_color=COLORS["bg_hover"] if is_current else "transparent",
                corner_radius=8,
            )
            frame.pack(fill="x", pady=3, padx=5)
            
            radio = ctk.CTkRadioButton(
                frame,
                text="",
                variable=model_var,
                value=model["path"],
                width=20,
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent_glow"],
            )
            radio.pack(side="left", padx=(10, 5), pady=10)
            
            info_frame = ctk.CTkFrame(frame, fg_color="transparent")
            info_frame.pack(side="left", fill="x", expand=True, pady=8)
            
            ctk.CTkLabel(
                info_frame,
                text=model["name"],
                font=(self.font_body[0], 14, "bold" if is_current else "normal"),
                text_color=COLORS["accent"] if is_current else COLORS["text_primary"],
                anchor="w",
            ).pack(anchor="w")
            
            ctk.CTkLabel(
                info_frame,
                text=f"{model['speed']} • {model['size']}",
                font=(self.font_body[0], 11),
                text_color=COLORS["text_muted"],
                anchor="w",
            ).pack(anchor="w")
        
        # Save button
        def save():
            selected_path = model_var.get()
            # Store with ~ for portability
            if selected_path.startswith(str(Path.home())):
                selected_path = "~" + selected_path[len(str(Path.home())):]
            
            self.config["model_path"] = selected_path
            save_config(self.config)
            
            self.model_btn.configure(text=self.get_model_display())
            self.log(f"⚙ Model: {self.get_model_display()}")
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

    def open_device_settings(self):
        """Open dialog to manage which input devices to monitor."""
        dialog = ctk.CTkToplevel(self)
        dialog.title("Input Devices")
        dialog.geometry("500x500")
        dialog.configure(fg_color=COLORS["bg_base"])
        dialog.transient(self)
        dialog.after(100, dialog.lift)
        
        inner = ctk.CTkFrame(dialog, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=30, pady=30)
        
        ctk.CTkLabel(
            inner,
            text="Input Devices",
            font=(self.font_header[0], 22, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w", pady=(0, 5))
        
        ctk.CTkLabel(
            inner,
            text="Select which devices can trigger the hotkey.\nUseful for gaming mice, keypads, and multiple keyboards.",
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
                self.devices_btn.configure(text=f"All ({len(all_devices)})")
            else:
                self.devices_btn.configure(text=f"{len(selected)} selected")
            
            self.log(f"⚙ Input devices updated: {len(selected)} enabled")
            self.log("↻ Restarting hotkey listener...")
            
            # Restart listener
            self.stop_event.set()
            self.stop_event = threading.Event()
            self.start_hotkey_listener()
            
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
            command=dialog.destroy,
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
                "label": "Fast",
                "desc": "Quick results, may miss some words",
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
                "label": "Balanced",
                "desc": "Good accuracy with reasonable speed (default)",
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
                "label": "High Accuracy",
                "desc": "Better accuracy, ~2x slower",
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
            text="Higher values = more accurate but slower.\nDefault: 5, Max recommended: 10",
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
            ("Fast (1)", 1),
            ("Balanced (5)", 5),
            ("Accurate (10)", 10),
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
            text="How long each audio segment is during chunked recording.\nShorter chunks = faster feedback, longer = better context.",
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
        
        # Preset buttons
        presets_frame = ctk.CTkFrame(inner, fg_color="transparent")
        presets_frame.pack(fill="x", pady=(0, 20))
        
        for val in [15, 30, 45, 60]:
            ctk.CTkButton(
                presets_frame,
                text=f"{val}s",
                width=60,
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
        
        # GPU icon based on vendor
        if gpu_info.is_nvidia:
            gpu_icon = "🟢"
            gpu_vendor_text = "NVIDIA GPU detected"
            gpu_rec = "Recommended: Faster-Whisper with CUDA for best GPU performance"
        elif gpu_info.is_amd:
            gpu_icon = "🔴"
            gpu_vendor_text = "AMD GPU detected"
            gpu_rec = "Recommended: whisper.cpp with Vulkan (current setup)"
        elif gpu_info.is_intel:
            gpu_icon = "🔵"
            gpu_vendor_text = "Intel GPU detected"
            gpu_rec = "Note: Limited GPU support. CPU mode recommended."
        else:
            gpu_icon = "⚪"
            gpu_vendor_text = "GPU not detected"
            gpu_rec = "Running in CPU mode"
        
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
        
        # Customize descriptions based on GPU
        if gpu_info.is_nvidia:
            whisper_cpp_desc = "C++ implementation. Requires CUDA rebuild for NVIDIA GPU."
            faster_whisper_desc = "Python library with CUDA support. ★ Best for NVIDIA"
        elif gpu_info.is_amd:
            whisper_cpp_desc = "Fast C++ implementation. ★ Best for AMD (Vulkan)"
            faster_whisper_desc = "Python library. Supports ROCm for AMD GPU."
        else:
            whisper_cpp_desc = "Fast C++ implementation. Lightweight."
            faster_whisper_desc = "Python library with CTranslate2."
        
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
        
        fw_model_var = ctk.StringVar(value=self.config.get("faster_whisper_model", "small"))
        fw_model_menu = ctk.CTkOptionMenu(
            fw_frame,
            values=["tiny", "base", "small", "medium", "large-v3"],
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
            text="Number of model layers to offload to GPU.\n0 = Auto (offload all layers for maximum speed).",
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
        
        for val, label in [(0, "Auto"), (16, "16"), (32, "32"), (48, "48")]:
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
        
        # Create menu with model submenu
        menu = pystray.Menu(
            pystray.MenuItem("Show", self.show_from_tray, default=True),
            pystray.MenuItem("Record", self.tray_record),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Model",
                pystray.Menu(
                    pystray.MenuItem(
                        "Tiny (Fastest)",
                        self.create_model_setter("tiny.en"),
                        checked=lambda item: "tiny.en" in self.config.get("model_path", ""),
                    ),
                    pystray.MenuItem(
                        "Base (Fast)", 
                        self.create_model_setter("base.en"),
                        checked=lambda item: "base.en" in self.config.get("model_path", ""),
                    ),
                    pystray.MenuItem(
                        "Small (Balanced)",
                        self.create_model_setter("small.en"),
                        checked=lambda item: "small.en" in self.config.get("model_path", ""),
                    ),
                    pystray.MenuItem(
                        "Medium (Accurate)",
                        self.create_model_setter("medium.en"),
                        checked=lambda item: "medium.en" in self.config.get("model_path", ""),
                    ),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit_app),
        )
        
        icon_image = self.get_tray_icon(AppState.IDLE)
        self.tray_icon = pystray.Icon("wayfinder-voice", icon_image, "Wayfinder Voice", menu)
        
        threading.Thread(target=self.tray_icon.run, daemon=True).start()

    def get_tray_icon(self, state: AppState) -> Image.Image:
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
            # Pulsing dot for listening
            draw.ellipse([18, 18, 46, 46], fill=(255, 255, 255, 230))
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

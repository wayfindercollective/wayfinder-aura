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
from recorder import AudioRecorder
from transcriber import transcribe_with_config, TranscriptionError


# === Configuration ===

CONFIG_DIR = Path.home() / ".config" / "wayfinder-voice"
CONFIG_FILE = CONFIG_DIR / "config.json"
SCRIPT_DIR = Path(__file__).parent.resolve()
ICON_PATH = SCRIPT_DIR / "assets" / "icon.png"

DEFAULT_CONFIG = {
    "whisper_binary": "~/whisper.cpp/build/bin/whisper-cli",
    "model_path": "~/whisper.cpp/models/ggml-small.bin",
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


class EventType(Enum):
    HOTKEY_PRESSED = auto()
    TRANSCRIPTION_DONE = auto()
    TRANSCRIPTION_ERROR = auto()
    INJECTION_DONE = auto()
    INJECTION_ERROR = auto()


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


# === Main Application ===

class WayfinderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        
        self.config = load_config()
        self.app_state = AppState.IDLE
        self.event_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.recorder = AudioRecorder(
            sample_rate=self.config["sample_rate"],
            device=self.config["audio_device"],
        )
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.logs = []
        
        # UI scaling for high-DPI screens (saved in config)
        self.ui_scale = self.config.get("ui_scale", 1.0)
        
        self.setup_window()
        self.setup_tray()
        self.setup_ui()
        self.setup_scaling_shortcuts()
        self.start_hotkey_listener()
        self.poll_events()
        
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
        )
        
        # Typing speed setting
        speed = self.config.get("typing_speed", "instant")
        speed_display = speed.replace("_", " ").title()
        self.speed_btn = self.create_setting_row(
            settings_card,
            "Typing Speed",
            speed_display,
            self.open_speed_settings,
        )
        
        # Model setting
        model_display = self.get_model_display()
        self.model_btn = self.create_setting_row(
            settings_card,
            "Whisper Model",
            model_display,
            self.open_model_settings,
        )
        
        # Start minimized setting
        self.start_min_var = ctk.BooleanVar(value=self.config.get("start_minimized", True))
        self.create_toggle_row(
            settings_card,
            "Start minimized to tray",
            self.start_min_var,
            self.toggle_start_minimized,
        )
        
        # UI Scale setting
        scale_display = f"{int(self.ui_scale * 100)}%"
        self.scale_btn = self.create_setting_row(
            settings_card,
            "UI Scale",
            scale_display,
            self.open_scale_settings,
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

    def create_setting_row(self, parent, label, value, command):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=11)
        
        ctk.CTkLabel(
            row,
            text=label,
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
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

    def create_toggle_row(self, parent, label, variable, command):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=11)
        
        ctk.CTkLabel(
            row,
            text=label,
            font=(self.font_body[0], 14),
            text_color=COLORS["text_primary"],
        ).pack(side="left")
        
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
            "ggml-tiny.bin": ("Tiny (Multi-lang)", "75MB", "⚡⚡⚡⚡ Fastest"),
            "ggml-base.bin": ("Base (Multi-lang)", "142MB", "⚡⚡⚡ Fast"),
            "ggml-small.bin": ("Small (Multi-lang)", "466MB", "⚡⚡ Good"),
            "ggml-medium.bin": ("Medium (Multi-lang)", "1.5GB", "⚡ Slow"),
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

    def on_hotkey(self):
        if self.app_state == AppState.IDLE:
            self.start_recording()
        elif self.app_state == AppState.RECORDING:
            self.stop_recording_and_process()

    def start_recording(self):
        try:
            self.log("🎤 Listening...")
            self.recorder.start()
            self.update_state(AppState.RECORDING)
        except Exception as e:
            self.on_error(f"Microphone: {e}")

    def stop_recording_and_process(self):
        self.update_state(AppState.PROCESSING)
        try:
            duration = self.recorder.get_duration()
            self.log(f"⏱ Duration: {duration:.1f}s")
            
            if duration < self.config["min_recording_duration"]:
                self.recorder.stop()
                self.recorder.cleanup()
                self.on_error("Too short - speak longer")
                return
            
            audio_path = self.recorder.stop()
            self.executor.submit(self.transcribe_and_inject, audio_path)
        except Exception as e:
            self.on_error(f"Processing: {e}")

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
        self.update_state(AppState.IDLE)

    def on_error(self, message):
        self.log(f"⚠ {message}")
        self.update_state(AppState.IDLE)


def main():
    app = WayfinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()

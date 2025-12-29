"""
Wayfinder Voice - Main Application Module

This module will eventually contain the core WayfinderApp class.
Currently serves as a bridge to the legacy main.py during migration.

The full extraction of the 12,000+ line main.py is done incrementally:
1. Config, state, theme, components, hotkeys, utils - DONE (extracted to modules)
2. Core functionality (recorder, transcriber, etc.) - DONE (moved to core/)
3. Dialogs, main window, tray - TODO (future extraction)
4. Main app class - TODO (future extraction)
"""

# Re-export commonly used items from the new modular structure
from .config import (
    load_config,
    save_config,
    DEFAULT_CONFIG,
    CONFIG_DIR,
    CONFIG_FILE,
    ICON_PATH,
    KEY_CODES,
    MODIFIER_CODES,
    IS_FLATPAK,
)

from .state import (
    AppState,
    STATE_LABELS,
    get_state_label,
    get_next_state,
)

from .ui.theme import (
    COLORS,
    RADIUS,
    SPACING,
    STATE_COLORS,
    FONTS,
    FONT_SIZES,
    get_state_color,
)

from .ui.components import (
    ToolTip,
    ModeSelector,
    SmoothScrollableFrame,
    SETTING_TOOLTIPS,
    get_dynamic_tooltip,
)

from .hotkeys import (
    EventType,
    hotkey_listener,
    socket_listener,
    wayland_hotkey_listener,
    get_all_input_devices,
)

from .utils.gpu import (
    GPUInfo,
    detect_gpu,
    get_gpu_info,
    get_optimal_thread_count,
    get_system_info,
)

from .utils.platform import (
    is_flatpak,
    is_wayland,
    is_kde,
    get_desktop_environment,
)

# Re-export core functionality
from .core import (
    AudioRecorder,
    ChunkedRecorder,
    find_best_input_device,
    list_input_devices,
    transcribe_with_config,
    TranscriptionError,
    inject_text,
    InjectionError,
    process_with_config,
    get_ollama_manager,
)

__all__ = [
    # Config
    "load_config",
    "save_config",
    "DEFAULT_CONFIG",
    "CONFIG_DIR",
    "CONFIG_FILE",
    "ICON_PATH",
    "KEY_CODES",
    "MODIFIER_CODES",
    "IS_FLATPAK",
    # State
    "AppState",
    "STATE_LABELS",
    "get_state_label",
    "get_next_state",
    # Theme
    "COLORS",
    "RADIUS",
    "SPACING",
    "STATE_COLORS",
    "FONTS",
    "FONT_SIZES",
    "get_state_color",
    # Components
    "ToolTip",
    "ModeSelector",
    "SmoothScrollableFrame",
    "SETTING_TOOLTIPS",
    "get_dynamic_tooltip",
    # Hotkeys
    "EventType",
    "hotkey_listener",
    "socket_listener",
    "wayland_hotkey_listener",
    "get_all_input_devices",
    # GPU/Utils
    "GPUInfo",
    "detect_gpu",
    "get_gpu_info",
    "get_optimal_thread_count",
    "get_system_info",
    "is_flatpak",
    "is_wayland",
    "is_kde",
    "get_desktop_environment",
    # Core
    "AudioRecorder",
    "ChunkedRecorder",
    "find_best_input_device",
    "list_input_devices",
    "transcribe_with_config",
    "TranscriptionError",
    "inject_text",
    "InjectionError",
    "process_with_config",
    "get_ollama_manager",
]





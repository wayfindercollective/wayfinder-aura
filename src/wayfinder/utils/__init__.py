"""
Utility modules for Wayfinder Voice.

- gpu: GPU detection and capability checking
- platform: Platform-specific helpers
"""

from .gpu import (
    GPUInfo,
    detect_gpu,
    get_gpu_info,
    get_optimal_thread_count,
    get_system_info,
)

from .platform import (
    is_flatpak,
    is_wayland,
    is_x11,
    is_kde,
    is_gnome,
    get_desktop_environment,
    get_session_type,
    get_config_dir,
    get_data_dir,
    get_cache_dir,
    ensure_directories,
    is_ydotool_available,
    is_xdotool_available,
    get_text_injector,
)

__all__ = [
    # GPU
    "GPUInfo",
    "detect_gpu",
    "get_gpu_info",
    "get_optimal_thread_count",
    "get_system_info",
    # Platform
    "is_flatpak",
    "is_wayland",
    "is_x11",
    "is_kde",
    "is_gnome",
    "get_desktop_environment",
    "get_session_type",
    "get_config_dir",
    "get_data_dir",
    "get_cache_dir",
    "ensure_directories",
    "is_ydotool_available",
    "is_xdotool_available",
    "get_text_injector",
]

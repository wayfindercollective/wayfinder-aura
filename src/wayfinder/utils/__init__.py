"""
Utility modules for Wayfinder Aura.

- gpu: GPU detection and capability checking
- platform: Platform-specific helpers
- lazy_imports: Lazy loading for optional heavy dependencies
"""

from .gpu import (
    GPUInfo,
    detect_gpu,
    get_gpu_info,
    get_optimal_thread_count,
    get_system_info,
    get_optimal_vulkan_device,
    get_vulkan_env_vars,
    get_gpu_choices,
    benchmark_gpu_devices,
    run_gpu_benchmark_and_cache,
    detect_vulkan_devices,
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

from .lazy_imports import (
    LazyModule,
    get_pyqt6,
    get_faster_whisper,
    get_scipy,
    get_anthropic,
    get_llama_cpp,
    is_pyqt6_available,
    is_faster_whisper_available,
    is_scipy_available,
    is_anthropic_available,
    is_llama_cpp_available,
    get_optional_dependencies_status,
)

from .logging import (
    configure_logging,
    get_logger,
    set_level,
    get_log_file_path,
)

from .audio_ducker import (
    AudioDucker,
    is_pactl_available,
    get_sink_inputs,
    set_sink_input_volume,
)

__all__ = [
    # GPU
    "GPUInfo",
    "detect_gpu",
    "get_gpu_info",
    "get_optimal_thread_count",
    "get_system_info",
    "get_optimal_vulkan_device",
    "get_vulkan_env_vars",
    "get_gpu_choices",
    "benchmark_gpu_devices",
    "run_gpu_benchmark_and_cache",
    "detect_vulkan_devices",
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
    # Lazy Imports
    "LazyModule",
    "get_pyqt6",
    "get_faster_whisper",
    "get_scipy",
    "get_anthropic",
    "get_llama_cpp",
    "is_pyqt6_available",
    "is_faster_whisper_available",
    "is_scipy_available",
    "is_anthropic_available",
    "is_llama_cpp_available",
    "get_optional_dependencies_status",
    # Logging
    "configure_logging",
    "get_logger",
    "set_level",
    "get_log_file_path",
    # Audio Ducking
    "AudioDucker",
    "is_pactl_available",
    "get_sink_inputs",
    "set_sink_input_volume",
]

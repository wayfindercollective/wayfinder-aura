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
    ctranslate2_cuda_available,
    faster_whisper_package_available,
    recommend_local_transcription_backend,
    apply_auto_transcription_backend,
)

from .platform import (
    # Cross-platform detection
    get_platform,
    is_linux,
    is_macos,
    is_windows,
    # Linux-specific detection
    is_flatpak,
    is_wayland,
    is_x11,
    is_kde,
    is_gnome,
    get_desktop_environment,
    get_session_type,
    # Steam platform identity
    is_steamos,
    get_steam_platform,
    get_steam_platform_label,
    # Cross-platform directories
    get_config_dir,
    get_data_dir,
    get_cache_dir,
    ensure_directories,
    # Text injection detection
    is_ydotool_available,
    is_xdotool_available,
    is_pyautogui_available,
    get_text_injector,
    # Binary detection
    find_executable,
    get_default_whisper_binary,
    get_default_llama_binary,
    get_default_model_dir,
    get_default_llm_model_dir,
    # Platform info
    get_platform_info,
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
    "ctranslate2_cuda_available",
    "faster_whisper_package_available",
    "recommend_local_transcription_backend",
    "apply_auto_transcription_backend",
    # Platform - Cross-platform detection
    "get_platform",
    "is_linux",
    "is_macos",
    "is_windows",
    # Platform - Linux-specific
    "is_flatpak",
    "is_wayland",
    "is_x11",
    "is_kde",
    "is_gnome",
    "get_desktop_environment",
    "get_session_type",
    # Platform - Steam identity
    "is_steamos",
    "get_steam_platform",
    "get_steam_platform_label",
    # Platform - Directories
    "get_config_dir",
    "get_data_dir",
    "get_cache_dir",
    "ensure_directories",
    # Platform - Text injection
    "is_ydotool_available",
    "is_xdotool_available",
    "is_pyautogui_available",
    "get_text_injector",
    # Platform - Binary detection
    "find_executable",
    "get_default_whisper_binary",
    "get_default_llama_binary",
    "get_default_model_dir",
    "get_default_llm_model_dir",
    # Platform - Info
    "get_platform_info",
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

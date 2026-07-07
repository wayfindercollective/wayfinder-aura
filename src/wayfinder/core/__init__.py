"""
Core functionality modules for Wayfinder Aura.

The package keeps convenient re-exports such as
``from wayfinder.core import AudioRecorder`` without importing every heavy
runtime dependency at package import time. That matters for injector/setup tests
and source runs on hosts without PortAudio, GPU libraries, or optional LLM deps.
"""

from importlib import import_module
from typing import Any

_MODULES = {
    "recorder",
    "transcriber",
    "injector",
    "postprocessor",
    "voice_profile",
    "setup",
}

_EXPORT_MODULES = {
    # Recorder
    "AudioRecorder": "recorder",
    "ChunkedRecorder": "recorder",
    "find_best_input_device": "recorder",
    "list_input_devices": "recorder",
    "get_input_device_by_name": "recorder",
    "is_output_device": "recorder",
    "AudioCalibrator": "recorder",
    "AudioCalibrationResult": "recorder",
    "analyze_audio_calibration": "recorder",
    # Transcriber
    "transcribe_with_config": "transcriber",
    "TranscriptionError": "transcriber",
    "get_backend": "transcriber",
    # Injector
    "inject_text": "injector",
    "InjectionError": "injector",
    # Postprocessor
    "process_with_config": "postprocessor",
    "get_available_backends": "postprocessor",
    "get_template_names": "postprocessor",
    "check_settings_compatibility": "postprocessor",
    "get_upgrade_suggestion_for_intensity": "postprocessor",
    # Voice Profile
    "VoiceProfile": "voice_profile",
    "get_voice_profile": "voice_profile",
    "reset_voice_profile": "voice_profile",
    # Setup
    "get_dependencies": "setup",
    "get_recommended_model": "setup",
    "check_whisper_cpp": "setup",
    "check_whisper_model": "setup",
}

__all__ = [*_MODULES, *_EXPORT_MODULES]


def __getattr__(name: str) -> Any:
    if name in _MODULES:
        module = import_module(f".{name}", __name__)
        globals()[name] = module
        return module

    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value

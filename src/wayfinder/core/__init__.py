"""
Core functionality modules for Wayfinder Aura.

- recorder: Audio recording with sounddevice
- transcriber: Multi-backend speech-to-text
- injector: Text injection via ydotool
- postprocessor: LLM-based text cleanup
- ollama_manager: Ollama service management
"""

from .recorder import AudioRecorder, ChunkedRecorder, find_best_input_device, list_input_devices, get_input_device_by_name
from .transcriber import transcribe_with_config, TranscriptionError, get_backend
from .injector import inject_text, InjectionError
from .postprocessor import (
    process_with_config,
    get_available_backends,
    get_template_names,
    check_settings_compatibility,
    get_upgrade_suggestion_for_intensity,
)
from .ollama_manager import get_ollama_manager, OllamaManager
from .voice_profile import VoiceProfile, get_voice_profile, reset_voice_profile

__all__ = [
    # Recorder
    "AudioRecorder",
    "ChunkedRecorder", 
    "find_best_input_device",
    "list_input_devices",
    "get_input_device_by_name",
    # Transcriber
    "transcribe_with_config",
    "TranscriptionError",
    "get_backend",
    # Injector
    "inject_text",
    "InjectionError",
    # Postprocessor
    "process_with_config",
    "get_available_backends",
    "get_template_names",
    "check_settings_compatibility",
    "get_upgrade_suggestion_for_intensity",
    # Ollama
    "get_ollama_manager",
    "OllamaManager",
    # Voice Profile
    "VoiceProfile",
    "get_voice_profile",
    "reset_voice_profile",
]





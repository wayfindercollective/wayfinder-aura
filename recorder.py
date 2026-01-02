"""
Backward compatibility shim - imports from wayfinder.core.recorder

This module is maintained for backward compatibility with wayfinder_main.py.
New code should import directly from wayfinder.core.recorder.
"""

from wayfinder.core.recorder import (
    AudioRecorder,
    ChunkedRecorder,
    find_best_input_device,
    list_input_devices,
    get_input_device_by_name,
    get_supported_sample_rate,
    resample_audio,
    preprocess_audio,
    WHISPER_SAMPLE_RATE,
    PREFERRED_MIC_KEYWORDS,
    EXCLUDED_DEVICE_KEYWORDS,
)

__all__ = [
    "AudioRecorder",
    "ChunkedRecorder",
    "find_best_input_device",
    "list_input_devices",
    "get_input_device_by_name",
    "get_supported_sample_rate",
    "resample_audio",
    "preprocess_audio",
    "WHISPER_SAMPLE_RATE",
    "PREFERRED_MIC_KEYWORDS",
    "EXCLUDED_DEVICE_KEYWORDS",
]

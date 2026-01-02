"""
Backward compatibility shim - imports from wayfinder.core.transcriber

This module is maintained for backward compatibility with wayfinder_main.py.
New code should import directly from wayfinder.core.transcriber.
"""

from wayfinder.core.transcriber import (
    TranscriptionError,
    TranscriptionBackend,
    WhisperCppBackend,
    FasterWhisperBackend,
    GroqWhisperBackend,
    OpenAIWhisperBackend,
    get_backend,
    ensure_punctuation_postprocess,
    transcribe_with_config,
)

__all__ = [
    "TranscriptionError",
    "TranscriptionBackend",
    "WhisperCppBackend",
    "FasterWhisperBackend",
    "GroqWhisperBackend",
    "OpenAIWhisperBackend",
    "get_backend",
    "ensure_punctuation_postprocess",
    "transcribe_with_config",
]

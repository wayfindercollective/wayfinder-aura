"""
Backward compatibility shim - imports from wayfinder.core.postprocessor

This module is maintained for backward compatibility with wayfinder_main.py.
New code should import directly from wayfinder.core.postprocessor.
"""

from wayfinder.core.postprocessor import (
    PostProcessingError,
    PostProcessorBackend,
    LlamaCppBackend,
    OllamaBackend,
    AnthropicBackend,
    OpenAIBackend,
    get_backend,
    process_with_config,
    get_available_backends,
    get_tone_options,
    check_settings_compatibility,
)

# Re-export get_template_names as an alias for get_tone_options for compatibility
get_template_names = get_tone_options

__all__ = [
    "PostProcessingError",
    "PostProcessorBackend",
    "LlamaCppBackend",
    "OllamaBackend",
    "AnthropicBackend",
    "OpenAIBackend",
    "get_backend",
    "process_with_config",
    "get_available_backends",
    "get_template_names",
    "get_tone_options",
    "check_settings_compatibility",
]

"""
Backward compatibility shim - imports from wayfinder.core.ollama_manager

This module is maintained for backward compatibility with wayfinder_main.py.
New code should import directly from wayfinder.core.ollama_manager.
"""

from wayfinder.core.ollama_manager import (
    OllamaManager,
    get_ollama_manager,
)

# Re-export class attribute for backward compatibility
RECOMMENDED_MODELS = OllamaManager.RECOMMENDED_MODELS

__all__ = [
    "OllamaManager",
    "get_ollama_manager",
    "RECOMMENDED_MODELS",
]

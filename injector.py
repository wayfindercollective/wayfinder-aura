"""
Backward compatibility shim - imports from wayfinder.core.injector

This module is maintained for backward compatibility with wayfinder_main.py.
New code should import directly from wayfinder.core.injector.
"""

from wayfinder.core.injector import (
    InjectionError,
    inject_text,
    TYPING_SPEEDS,
)

__all__ = ["InjectionError", "inject_text", "TYPING_SPEEDS"]

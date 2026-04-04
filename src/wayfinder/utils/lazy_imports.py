"""
Lazy import utilities for heavy dependencies.

This module provides lazy loading for optional and heavy dependencies
to improve startup time and reduce memory usage when features aren't used.

Usage:
    from wayfinder.utils.lazy_imports import get_pyqt6, get_faster_whisper
    
    # These imports happen lazily on first access
    if (QtWidgets := get_pyqt6()) is not None:
        # PyQt6 is available
        app = QtWidgets.QApplication([])
"""

import importlib.util
import sys
from functools import lru_cache
from typing import Any, Optional


class LazyModule:
    """
    A lazy module loader that defers import until first attribute access.
    
    Usage:
        scipy = LazyModule("scipy")
        # scipy is not imported yet
        
        scipy.signal.resample(...)  # Now scipy is imported
    """
    
    def __init__(self, module_name: str, submodule: Optional[str] = None):
        self._module_name = module_name
        self._submodule = submodule
        self._module = None
    
    def _load(self):
        if self._module is None:
            try:
                self._module = importlib.import_module(self._module_name)
                if self._submodule:
                    self._module = getattr(self._module, self._submodule)
            except ImportError:
                self._module = None
        return self._module
    
    def __getattr__(self, name: str) -> Any:
        module = self._load()
        if module is None:
            raise ImportError(f"Module {self._module_name} is not available")
        return getattr(module, name)
    
    def __bool__(self) -> bool:
        return self._load() is not None
    
    @property
    def is_available(self) -> bool:
        return self._load() is not None


# === PyQt6 Lazy Loading ===

@lru_cache(maxsize=1)
def get_pyqt6():
    """
    Lazy load PyQt6 modules.
    
    Returns:
        A namespace object with QtCore, QtGui, QtWidgets, or None if not available.
    """
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        
        class PyQt6Namespace:
            QtCore = QtCore
            QtGui = QtGui
            QtWidgets = QtWidgets
        
        return PyQt6Namespace
    except ImportError:
        return None


def is_pyqt6_available() -> bool:
    """Check if PyQt6 is available without fully importing it."""
    return importlib.util.find_spec("PyQt6") is not None


# === Faster-Whisper Lazy Loading ===

@lru_cache(maxsize=1)
def get_faster_whisper():
    """
    Lazy load faster-whisper.
    
    Returns:
        The faster_whisper module, or None if not available.
    """
    try:
        import faster_whisper
        return faster_whisper
    except ImportError:
        return None


def is_faster_whisper_available() -> bool:
    """Check if faster-whisper is available without fully importing it."""
    return importlib.util.find_spec("faster_whisper") is not None


# === SciPy Lazy Loading ===

@lru_cache(maxsize=1)
def get_scipy():
    """
    Lazy load scipy modules.
    
    Returns:
        A namespace object with signal, io submodules, or None if not available.
    """
    try:
        from scipy import signal, io
        
        class SciPyNamespace:
            signal = signal
            io = io
        
        return SciPyNamespace
    except ImportError:
        return None


def is_scipy_available() -> bool:
    """Check if scipy is available without fully importing it."""
    return importlib.util.find_spec("scipy") is not None


# === Anthropic Lazy Loading ===

@lru_cache(maxsize=1)
def get_anthropic():
    """
    Lazy load anthropic client.
    
    Returns:
        The anthropic module, or None if not available.
    """
    try:
        import anthropic
        return anthropic
    except ImportError:
        return None


def is_anthropic_available() -> bool:
    """Check if anthropic is available without fully importing it."""
    return importlib.util.find_spec("anthropic") is not None


# === llama-cpp-python Lazy Loading ===

@lru_cache(maxsize=1)
def get_llama_cpp():
    """
    Lazy load llama-cpp-python.
    
    Returns:
        The llama_cpp module, or None if not available.
    """
    try:
        import llama_cpp
        return llama_cpp
    except ImportError:
        return None


def is_llama_cpp_available() -> bool:
    """Check if llama-cpp-python is available without fully importing it."""
    return importlib.util.find_spec("llama_cpp") is not None


# === Dependency Check Summary ===

def get_optional_dependencies_status() -> dict:
    """
    Get status of all optional dependencies.
    
    Returns:
        Dict mapping dependency name to availability status.
    """
    return {
        "PyQt6": is_pyqt6_available(),
        "faster-whisper": is_faster_whisper_available(),
        "scipy": is_scipy_available(),
        "anthropic": is_anthropic_available(),
        "llama-cpp-python": is_llama_cpp_available(),
    }

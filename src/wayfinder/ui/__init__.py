"""
UI components for Wayfinder Aura.

- theme: Colors, fonts, and styling constants
- components: Reusable CustomTkinter widgets
- overlay: Glassmorphic status overlay (PyQt6)
- dialogs: Settings, hotkey, model download, etc.
"""

from .theme import (
    COLORS,
    RADIUS,
    SPACING,
    STATE_COLORS,
    FONTS,
    FONT_SIZES,
    get_state_color,
    get_font,
    get_font_size,
    scaled_font_size,
)

_COMPONENT_EXPORTS = {
    "ToolTip",
    "ModeSelector",
    "SmoothScrollableFrame",
    "SETTING_TOOLTIPS",
    "get_dynamic_tooltip",
    "create_section_header",
    "create_body_label",
    "CompatibilityBanner",
}


def __getattr__(name: str):
    """Lazy-load CustomTkinter-backed widgets so pure UI helpers stay headless."""
    if name in _COMPONENT_EXPORTS:
        from . import components

        value = getattr(components, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    # Theme
    "COLORS",
    "RADIUS",
    "SPACING",
    "STATE_COLORS",
    "FONTS",
    "FONT_SIZES",
    "get_state_color",
    "get_font",
    "get_font_size",
    "scaled_font_size",
    # Components
    "ToolTip",
    "ModeSelector",
    "SmoothScrollableFrame",
    "SETTING_TOOLTIPS",
    "get_dynamic_tooltip",
    "create_section_header",
    "create_body_label",
    "CompatibilityBanner",
]

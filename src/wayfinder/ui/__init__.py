"""
UI components for Wayfinder Voice.

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

from .components import (
    ToolTip,
    ModeSelector,
    SmoothScrollableFrame,
    SETTING_TOOLTIPS,
    get_dynamic_tooltip,
    create_section_header,
    create_body_label,
)

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
]

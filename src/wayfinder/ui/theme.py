"""
Premium Design System for Wayfinder Aura (2025).

Contains all colors, typography, spacing, and styling constants.
Pre-blended colors for depth simulation (Tkinter doesn't support alpha).
"""

from ..state import AppState


# === Color Palette ===
# Base: #0D1117 (Deep Ink) - all blends calculated against this

COLORS: dict[str, str] = {
    # Background layers - calculated elevation via pre-blending
    "bg_base": "#0D1117",           # Deep Ink - main background
    "bg_dark": "#0D1117",           # Alias for compatibility
    "bg_surface": "#161B22",        # Sidebar/panels - slightly elevated
    "bg_card": "#1E1E1F",           # Bento tiles - 7% white on bg
    "bg_hover": "#131C2B",          # Hover = 10% accent glow (blue on deep ink)
    "bg_hover_strong": "#1B2D48",   # Hover = 25% accent glow — obvious selection (dropdown rows)
    "bg_elevated": "#2D333B",       # Pressed/elevated elements
    "bg_input": "#13171D",          # Input fields - slightly recessed
    
    # Accent - brand blue (matches tray / app icon arrow)
    "accent": "#4682DC",            # Primary accent — same family as assets/icon.png
    "accent_cyan": "#4682DC",       # Alias for compatibility
    "accent_glow": "#131C2B",       # Pre-blended: bg + (4682DC - bg) * 0.10
    "accent_hover": "#6B9FE8",      # Lighter on hover
    "accent_dim": "#3A6FC0",        # Pressed/active state
    "accent_bright": "#7FB0F0",     # Bright version for active toggles
    
    # Rim light border - 10% blue pre-blended
    "border_rim": "#131C2B",        # bg + (4682DC - bg) * 0.10
    
    # State colors - muted, sophisticated palette
    "state_ready": "#5B8FD4",       # Soft brand blue — idle/ready (same family as accent)
    "state_recording": "#E8707F",   # Muted rose - warm but not aggressive
    "state_processing": "#E5AC2A",  # Muted gold - warm processing
    "state_typing": "#5DD4A8",      # Muted mint - success/typing
    
    # Legacy state color aliases for compatibility
    "accent_green": "#5DD4A8",      # Muted mint - success
    "accent_red": "#E8707F",        # Muted rose - recording
    "accent_yellow": "#E5AC2A",     # Muted gold - processing
    "accent_blue": "#4682DC",       # Brand blue (same as primary accent)
    
    # Text hierarchy - calculated for dark bg readability
    "text_bright": "#F0F0F0",       # Maximum contrast (app title only)
    "text_primary": "#E8E8E8",      # Off-white - 90% brightness
    "text_secondary": "#8B8B8F",    # Section labels - muted grey
    "text_muted": "#5C5C60",        # Disabled/hints
    
    # Borders (pre-blended, no alpha)
    "border": "#2D333B",            # Standard borders
    "border_subtle": "#21262D",     # Subtle borders
    "border_light": "#1A1D24",      # Pre-blended 5% white
    "border_glow": "#131C2B",       # Pre-blended 10% blue
}


# === Corner Radius Design Tokens ===

# Mirrored in wayfinder_main.py (RADIUS) — keep both in sync.
RADIUS: dict[str, int] = {
    "xs": 6,    # Chips, progress bars, tight inline elements
    "sm": 8,    # Small elements (toggles, chips, badges)
    "md": 12,   # Buttons, inputs, dropdowns
    "lg": 24,   # Bento tiles - 24px for squircle feel
    "xl": 28,   # Hero sections, modals
}


# === Spacing Tokens ===
# For Bento grid layout (designer spec)

SPACING: dict[str, int] = {
    "gutter": 24,       # Between bento tiles
    "tile_pad": 20,     # Inside tile padding (20px = premium)
    "tile_pad_y": 16,   # Vertical padding inside tiles
    "xs": 4,
    "sm": 8,
    "md": 12,
    "lg": 16,
    "xl": 24,           # Bento gutter
    "2xl": 32,
}


# === State-Specific Colors ===

STATE_COLORS: dict[AppState, str] = {
    AppState.IDLE: COLORS["state_ready"],        # Soft brand blue when ready
    AppState.RECORDING: COLORS["state_recording"], # Rose when recording
    AppState.PROCESSING: COLORS["state_processing"], # Amber when processing
    AppState.PASTING: COLORS["state_typing"],    # Emerald when typing
}


# === Typography System ===
# Inter & Segoe UI Variable: designed for screen legibility at all weights
# Light text on dark BG looks thinner - use Semi-Bold/Bold for headers

FONTS: dict[str, tuple[str, ...]] = {
    "display": ("Inter", "Segoe UI Variable", "SF Pro Display", "Ubuntu"),
    "header": ("Inter", "Segoe UI Variable", "SF Pro Display", "Ubuntu"),
    "body": ("Inter", "Segoe UI Variable", "SF Pro Text", "system-ui"),
    "mono": ("JetBrains Mono", "Cascadia Code", "SF Mono", "monospace"),
}

# Font size tokens - optimized for dark mode readability
FONT_SIZES: dict[str, int] = {
    "display": 18,      # Main title
    "title": 15,        # Section titles - Semi-Bold
    "heading": 13,      # Card headings - Semi-Bold
    "body": 13,         # Body text, labels
    "small": 11,        # Secondary text
    "caption": 10,      # Captions, hints
}


def get_state_color(state: AppState) -> str:
    """Get the color for a given application state."""
    return STATE_COLORS.get(state, COLORS["accent"])


def get_font(font_type: str = "body") -> tuple[str, ...]:
    """Get font family tuple for a font type."""
    return FONTS.get(font_type, FONTS["body"])


def get_font_size(size_name: str = "body") -> int:
    """Get font size for a size token."""
    return FONT_SIZES.get(size_name, FONT_SIZES["body"])


def scaled_font_size(base_size: int, scale: float = 1.0) -> int:
    """Return a scaled font size."""
    return int(base_size * scale)





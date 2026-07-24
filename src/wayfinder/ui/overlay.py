#!/usr/bin/env python3
"""
Wayfinder Aura - Glassmorphic Status Overlay

A premium PyQt6-based status indicator with:
- Squircle (superellipse) shape for weighted, intentional feel
- Glassmorphism with backdrop blur and gradient borders
- Liquid light sine wave with glow effects
- Smooth morphing transitions between states
- Chasing light animation for processing state

Designed to feel like high-end hardware embedded in the screen.
"""

import ctypes
import json
import math
import os
import subprocess
import sys

# When launched as a bare-script subprocess on the desktop source run (the parent does
# `python .../src/wayfinder/ui/overlay.py`), only ui/ is on sys.path — so `import wayfinder`
# fails and the QSystemTrayIcon tray block (which needs wayfinder.config / wayfinder.ui.
# tray_icon) dies with "No module named 'wayfinder'", killing the tray on the first state
# change. Put src/ on the path so every `from wayfinder…` import resolves. The Flatpak
# installs wayfinder as a real package, so `import wayfinder` already works there → no-op.
try:
    import wayfinder  # noqa: F401
except ImportError:
    _SRC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _SRC_ROOT not in sys.path:
        sys.path.insert(0, _SRC_ROOT)

# Pure positioning math (Qt-free; unit-tested in tests/test_overlay_geometry.py). Works
# whether this file is imported as wayfinder.ui.overlay or run as a bare-script subprocess.
try:
    from wayfinder.ui.overlay_geometry import clamp_overlay_y, anchor_x, parse_anchor
except ImportError:  # standalone script — its own directory is on sys.path
    from overlay_geometry import clamp_overlay_y, anchor_x, parse_anchor

# Blocking stdin reader thread (Qt-free; unit-tested in tests/test_stdin_reader.py).
try:
    from wayfinder.ui.stdin_reader import StdinCommandReader
except ImportError:  # standalone script — its own directory is on sys.path
    from stdin_reader import StdinCommandReader

# Force native Wayland path for PyQt6 if running on Wayland
# This ensures proper transparency and always-on-top behavior
if os.environ.get("XDG_SESSION_TYPE") == "wayland":
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland")


def _force_kde_window_position(window_title: str, x: int, y: int, width: int, height: int) -> bool:
    """Force window position using KWin scripting - the ONLY way that works on Wayland."""
    try:
        import tempfile
        
        # Create a KWin script that FORCES position via frameGeometry
        script_content = f'''
        // KWin script to force overlay position (required for Wayland)
        var windows = workspace.windowList();
        for (var i = 0; i < windows.length; i++) {{
            var w = windows[i];
            if (w.caption && w.caption.indexOf("{window_title}") !== -1) {{
                // Force position - this is the only way on Wayland
                w.frameGeometry = {{
                    x: {x},
                    y: {y},
                    width: {width},
                    height: {height}
                }};
                w.keepAbove = true;
                w.skipTaskbar = true;
                w.skipPager = true;
                w.skipSwitcher = true;
            }}
        }}
        '''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
            f.write(script_content)
            script_path = f.name
        
        try:
            result = subprocess.run([
                "qdbus", "org.kde.KWin", "/Scripting",
                "org.kde.kwin.Scripting.loadScript", script_path
            ], capture_output=True, text=True, timeout=2)
            
            if result.returncode == 0:
                subprocess.run([
                    "qdbus", "org.kde.KWin", "/Scripting",
                    "org.kde.kwin.Scripting.start"
                ], capture_output=True, timeout=1)
                return True
        finally:
            try:
                os.unlink(script_path)
            except:
                pass
                
    except Exception as e:
        print(f"KWin position force failed: {e}", file=sys.stderr)
    
    return False


def _try_kde_window_setup(window_title: str, x: int, y: int, width: int, height: int) -> bool:
    """Try to set window properties using KWin scripting (no position - app handles that)."""
    try:
        import tempfile
        
        # Create a KWin script that sets properties only (not position)
        # Position is handled by the app to allow off-screen hiding
        script_content = f'''
        // KWin script to configure overlay window (properties only)
        var windows = workspace.windowList();
        for (var i = 0; i < windows.length; i++) {{
            var w = windows[i];
            if (w.caption && w.caption.indexOf("{window_title}") !== -1) {{
                w.keepAbove = true;
                w.skipTaskbar = true;
                w.skipPager = true;
                w.skipSwitcher = true;
                w.demandsAttention = false;
            }}
        }}
        '''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
            f.write(script_content)
            script_path = f.name
        
        try:
            result = subprocess.run([
                "qdbus", "org.kde.KWin", "/Scripting",
                "org.kde.kwin.Scripting.loadScript", script_path
            ], capture_output=True, text=True, timeout=2)
            
            if result.returncode == 0:
                subprocess.run([
                    "qdbus", "org.kde.KWin", "/Scripting",
                    "org.kde.kwin.Scripting.start"
                ], capture_output=True, timeout=1)
                return True
        finally:
            try:
                os.unlink(script_path)
            except:
                pass
                
    except Exception as e:
        print(f"KDE window setup failed: {e}", file=sys.stderr)
    
    return False


def _setup_kwin_window_rule(x: int, y: int, width: int, height: int) -> bool:
    """Create a persistent KWin window rule to position the overlay from first frame."""
    try:
        import tempfile
        
        # Create a KWin script that sets window properties but NOT position
        # Position is handled by the app itself to allow off-screen hiding
        script_content = f'''
        // KWin script - set overlay properties (not position - app handles that)
        workspace.windowAdded.connect(function(client) {{
            if (client.caption && client.caption.indexOf("Wayfinder Aura Overlay") !== -1) {{
                // Only set properties, not position
                client.keepAbove = true;
                client.skipTaskbar = true;
                client.skipPager = true;
                client.skipSwitcher = true;
                client.demandsAttention = false;
            }}
        }});
        
        // Also handle existing windows (properties only, not position)
        var windows = workspace.windowList();
        for (var i = 0; i < windows.length; i++) {{
            var w = windows[i];
            if (w.caption && w.caption.indexOf("Wayfinder Aura Overlay") !== -1) {{
                w.keepAbove = true;
                w.skipTaskbar = true;
                w.skipPager = true;
                w.skipSwitcher = true;
                w.demandsAttention = false;
            }}
        }}
        '''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
            f.write(script_content)
            script_path = f.name
        
        try:
            result = subprocess.run([
                "qdbus", "org.kde.KWin", "/Scripting",
                "org.kde.kwin.Scripting.loadScript", script_path
            ], capture_output=True, text=True, timeout=2)
            
            if result.returncode == 0:
                subprocess.run([
                    "qdbus", "org.kde.KWin", "/Scripting",
                    "org.kde.kwin.Scripting.start"
                ], capture_output=True, timeout=1)
                return True
        finally:
            try:
                os.unlink(script_path)
            except:
                pass
                
    except Exception as e:
        print(f"KWin rule setup failed: {e}", file=sys.stderr)
    
    return False


def _try_kde_always_on_top(window_title: str) -> bool:
    """Legacy wrapper - now uses full window setup."""
    return True
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtProperty,
    pyqtSignal,
    QObject,
    QSize,
)
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsBlurEffect,
    QGraphicsDropShadowEffect,
    QWidget,
)


# === State Definitions ===

class OverlayState(Enum):
    HIDDEN = auto()
    READY = auto()
    LISTENING = auto()
    PROCESSING = auto()
    ERROR = auto()


# === Color Palettes ===

@dataclass
class StateColors:
    """Color palette for a given state."""
    border_top: str      # Bright highlight
    border_bottom: str   # Dark shadow
    glow: str            # Outer glow color (read at set_state, :~1322)
    wave: str            # Wave color


# === Palette mirror-sync (parity enforced by tests/test_palette_parity.py) ===
# STATE_PALETTES and STYLE_PALETTES below derive their hues from the app's
# design tokens (wayfinder_main.py COLORS / src/wayfinder/ui/theme.py COLORS).
# The overlay runs as a bare-script subprocess and cannot cleanly import
# theme.py, so these values are DUPLICATED by design. When theme.py tokens
# change, update these to match; tests/test_palette_parity.py source-parses
# both files and fails if they drift.
STATE_PALETTES = {
    OverlayState.READY: StateColors(
        # Idle overlay reads as the same brand as the main window: hues are
        # exact blends of the app's state_ready soft blue (#5B8FD4) toward the
        # app bg (#0D1117), luminance-matched so the overlay stays quiet.
        # blend(f) = round(bg + (state_ready-bg)*f).
        border_top="#24364E",      # 29% state_ready→bg
        border_bottom="#161B22",   # GitHub Dark surface (deep shadow, unchanged)
        glow="#1D2A3D",            # 20% state_ready→bg
        wave="#38567F",            # 55% state_ready→bg
    ),
    OverlayState.LISTENING: StateColors(
        border_top="#E8A0A8",      # Muted rose highlight
        border_bottom="#2D1520",   # Deep rose shadow
        glow="#E8707F",            # Muted rose glow (app state_recording)
        wave="#E8707F",            # Muted rose wave (app state_recording)
    ),
    OverlayState.PROCESSING: StateColors(
        border_top="#E8C86A",      # Muted gold highlight
        border_bottom="#2D2208",   # Deep bronze shadow
        glow="#E5AC2A",            # Muted gold glow (app state_processing)
        wave="#E5AC2A",            # Muted gold wave (app state_processing)
    ),
    OverlayState.ERROR: StateColors(
        border_top="#5A2A2E",      # Muted danger highlight (app error red family)
        border_bottom="#3A1E22",   # Deep danger shadow
        glow="#E5484D",            # Danger red glow (app error/danger)
        wave="#F16A6F",            # Danger red wave (app error/danger)
    ),
}

STATE_LABELS = {
    OverlayState.READY: "",
    OverlayState.LISTENING: "Listening...",
    OverlayState.PROCESSING: "Processing...",
    OverlayState.ERROR: "",
}


# === Style Badge Colors ===
# Colors for the output tone indicator (P/T/C badge)

@dataclass
class StyleColors:
    """Color palette for output style indicator."""
    letter: str      # Single letter/word to display
    color: str       # Main badge color


# === Palette mirror-sync (parity enforced by tests/test_palette_parity.py) ===
# Each style hue is an exact app design token (theme.py / wayfinder_main.py
# COLORS). The overlay is a bare-script subprocess and cannot import theme.py,
# so these are DUPLICATED by design; the parity test fails if they drift.
STYLE_PALETTES = {
    "minimal": StyleColors(
        letter="Raw",       # Raw/unprocessed — reads quiet
        color="#8B8B8F",    # app text_secondary (muted gray)
    ),
    "professional": StyleColors(
        letter="Pro",
        color="#5B8FD4",    # app state_ready (soft brand blue)
    ),
    "casual": StyleColors(
        letter="Chat",      # Conversational, friendly
        color="#5DD4A8",    # app state_typing (mint)
    ),
    "dev": StyleColors(
        letter="Dev",
        color="#E5AC2A",    # app accent_yellow (gold)
    ),
    "personal": StyleColors(
        letter="You",       # Your personal voice — wears the brand color
        color="#4682DC",    # app accent (brand blue — matches tray/icon)
    ),
}

# Style cycle order for toggle (matches hotkey cycle)
STYLE_CYCLE = ["minimal", "professional", "casual", "dev", "personal"]


# === Squircle Path Generator (Kappa-based Bezier) ===

def create_squircle_path(rect: QRectF, n: float = 4.5) -> QPainterPath:
    """
    Create a premium squircle using Cubic Bezier curves.
    
    Standard drawRoundedRect uses circular arcs which create a sharp "break"
    where the straight line meets the curve. This implementation uses kappa
    (≈0.552) to create smooth, continuous curvature like iOS/macOS.
    
    Args:
        rect: Bounding rectangle for the squircle
        n: Controls corner aggressiveness (4.5 = balanced squircle)
        
    Returns:
        QPainterPath representing a true squircle
    """
    path = QPainterPath()
    
    # Kappa constant for smooth Bezier approximation of circular arcs
    # Standard kappa ≈ 0.552, but we use higher for more aggressive squircle
    kappa = 0.552284749831
    squircle_factor = 1.2  # Multiplier for more aggressive squircle feel
    
    # Calculate corner radius (proportional to smaller dimension)
    radius = min(rect.width(), rect.height()) * 0.3  # 30% of size
    offset = radius * (1 - kappa * squircle_factor)
    
    left = rect.left()
    right = rect.right()
    top = rect.top()
    bottom = rect.bottom()
    
    # Start from top-left, going clockwise
    path.moveTo(left + radius, top)
    
    # Top edge
    path.lineTo(right - radius, top)
    
    # Top-right corner (Bezier curve)
    path.cubicTo(
        right - offset, top,           # Control point 1
        right, top + offset,           # Control point 2
        right, top + radius            # End point
    )
    
    # Right edge
    path.lineTo(right, bottom - radius)
    
    # Bottom-right corner
    path.cubicTo(
        right, bottom - offset,
        right - offset, bottom,
        right - radius, bottom
    )
    
    # Bottom edge
    path.lineTo(left + radius, bottom)
    
    # Bottom-left corner
    path.cubicTo(
        left + offset, bottom,
        left, bottom - offset,
        left, bottom - radius
    )
    
    # Left edge
    path.lineTo(left, top + radius)
    
    # Top-left corner
    path.cubicTo(
        left, top + offset,
        left + offset, top,
        left + radius, top
    )
    
    path.closeSubpath()
    return path


# === Liquid Wave Renderer ===

class LiquidWaveRenderer:
    """
    Renders a thick, gradient-filled sine wave with glow effect.
    Creates a "liquid light" appearance that reacts to audio levels.
    """
    
    def __init__(self):
        self.time = 0.0
        self.breath = 0.0
        self.audio_level = 0.0
        self._smoothed_level = 0.0
        
    def update_audio_level(self, level: float):
        """Update audio level with hyperbolic reactivity (instant attack, slow decay)."""
        if level > self._smoothed_level:
            # Instant attack
            self._smoothed_level = level
        else:
            # Moderate decay
            self._smoothed_level = self._smoothed_level * 0.75 + level * 0.25
        self.audio_level = self._smoothed_level
    
    def advance_time(self, dt: float = 0.016):
        """Advance animation time."""
        # Guard against NaN/inf: a single bad dt would poison self.time forever and make every
        # subsequent frame compute NaN geometry (silently blank/frozen wave).
        if dt != dt or dt in (float('inf'), float('-inf')):
            dt = 0.016
        self.time += dt * 3.0  # Wave scrolling speed
        self.breath += dt * 0.5  # Breathing cycle speed
    
    def render(self, painter: QPainter, rect: QRectF, color: QColor):
        """
        Render the liquid wave within the given rectangle.
        
        Args:
            painter: QPainter to draw with
            rect: Bounding rectangle for the wave
            color: Base color for the wave
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        width = rect.width()
        height = rect.height()
        center_y = rect.center().y()
        max_amp = height * 0.4  # Maximum amplitude
        
        # Edge fade zone (pixels from edge where fade applies)
        fade_zone = min(12, width * 0.25)  # 25% of width or 12px max
        
        def get_edge_fade(x_pos: float) -> float:
            """Calculate fade factor (0-1) based on distance from edges."""
            dist_from_left = x_pos - rect.left()
            dist_from_right = rect.right() - x_pos
            min_dist = min(dist_from_left, dist_from_right)
            if min_dist >= fade_zone:
                return 1.0
            return min_dist / fade_zone if fade_zone > 0 else 1.0
        
        # Calculate amplitude based on audio level and breathing
        base_breath = 0.15 + 0.12 * (0.5 + 0.5 * math.sin(self.breath))
        voice_boost = (self.audio_level ** 0.6) * 12.0
        amplitude_factor = min(1.0, base_breath + voice_boost)
        
        # Wave configurations: (frequency, phase_offset, alpha, thickness)
        wave_configs = [
            (0.07, 0.0, 0.15, 6),      # Slow background wave
            (0.11, 1.0, 0.25, 5),      # Medium wave
            (0.16, 2.2, 0.40, 4),      # Faster wave
            (0.22, 0.7, 0.55, 3),      # Quick wave
        ]
        
        for freq, phase, base_alpha, thickness in wave_configs:
            amp = max_amp * amplitude_factor
            
            # Draw wave in segments with fading alpha at edges
            prev_point = None
            for x_pixel in range(int(rect.left()), int(rect.right()) + 1, 2):
                x = x_pixel - rect.left()
                
                # Combine sine waves for organic motion
                y = center_y + amp * math.sin(freq * x + self.time + phase)
                y += (amp * 0.4) * math.sin(freq * 2.3 * x + self.time * 1.6 + phase)
                y += (amp * 0.2) * math.sin(freq * 3.7 * x + self.time * 2.1 + phase * 0.5)
                
                # Clamp to bounds
                y = max(rect.top(), min(rect.bottom(), y))
                
                if prev_point is not None:
                    # Calculate edge fade for this segment
                    fade = get_edge_fade(x_pixel)
                    segment_alpha = base_alpha * fade
                    
                    # Draw glow segment
                    glow_color = QColor(color)
                    glow_color.setAlphaF(segment_alpha * 0.3)
                    glow_pen = QPen(glow_color, thickness + 4)
                    glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    painter.setPen(glow_pen)
                    painter.drawLine(QPointF(prev_point[0], prev_point[1]), QPointF(x_pixel, y))
                    
                    # Draw main wave segment
                    wave_color = QColor(color)
                    wave_color.setAlphaF(segment_alpha)
                    pen = QPen(wave_color, thickness)
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                    painter.setPen(pen)
                    painter.drawLine(QPointF(prev_point[0], prev_point[1]), QPointF(x_pixel, y))
                
                prev_point = (x_pixel, y)
        
        # Draw bright center highlight wave with edge fade
        highlight_amp = max_amp * amplitude_factor
        prev_point = None
        
        for x_pixel in range(int(rect.left()), int(rect.right()) + 1, 2):
            x = x_pixel - rect.left()
            y = center_y + highlight_amp * math.sin(0.13 * x + self.time * 1.4)
            y += (highlight_amp * 0.5) * math.sin(0.26 * x + self.time * 2.0 + 0.8)
            y = max(rect.top(), min(rect.bottom(), y))
            
            if prev_point is not None:
                fade = get_edge_fade(x_pixel)
                
                # Highlight glow with fade
                highlight_glow = QColor(color)
                highlight_glow.setAlphaF(0.4 * fade)
                painter.setPen(QPen(highlight_glow, 6))
                painter.drawLine(QPointF(prev_point[0], prev_point[1]), QPointF(x_pixel, y))
                
                # Bright highlight core with fade
                highlight_core = QColor(color)
                highlight_core.setAlphaF(fade)
                painter.setPen(QPen(highlight_core, 2))
                painter.drawLine(QPointF(prev_point[0], prev_point[1]), QPointF(x_pixel, y))
            
            prev_point = (x_pixel, y)
        
        painter.restore()


# === Border Chaser Animation ===

class BorderChaser:
    """
    Animates a bright light traveling around the border perimeter.
    Used for the Processing state.
    """
    
    def __init__(self):
        self.position = 0.0  # 0.0 to 1.0 around perimeter
        # Slow drift, not a sprint — a calm loop reads as ambient, not "dancing".
        self.speed = 1.0 / 2.6  # Complete loop in 2.6 seconds
        
    def advance(self, dt: float):
        """Advance the chaser position."""
        self.position = (self.position + self.speed * dt) % 1.0
    
    def render(self, painter: QPainter, path: QPainterPath, color: QColor):
        """
        Render the chasing light effect on the given path.
        
        Args:
            painter: QPainter to draw with
            path: Path representing the border
            color: Base color for the chaser
        """
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Point on the border at the current position.
        point = path.pointAtPercent(self.position)

        # A soft, low-alpha glow that drifts the border — deliberately subtle.
        # No bright white hot-spot (that read as an attention-grabbing "dancing"
        # light); this is a gentle shimmer in the state colour instead.
        glow_radius = 22
        gradient = QRadialGradient(point, glow_radius)

        bright_color = QColor(color)
        bright_color.setAlphaF(0.30)
        gradient.setColorAt(0, bright_color)

        mid_color = QColor(color)
        mid_color.setAlphaF(0.12)
        gradient.setColorAt(0.45, mid_color)

        transparent = QColor(color)
        transparent.setAlphaF(0)
        gradient.setColorAt(1.0, transparent)

        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(point, glow_radius, glow_radius)

        # Faint core in the state colour (NOT white) for a touch of definition.
        core_gradient = QRadialGradient(point, 5)
        core = QColor(color)
        core.setAlphaF(0.45)
        core_gradient.setColorAt(0, core)
        core_gradient.setColorAt(1.0, transparent)
        painter.setBrush(QBrush(core_gradient))
        painter.drawEllipse(point, 5, 5)

        painter.restore()


# === State Animator ===

class AnimatedValue(QObject):
    """Helper class for animating float values with signals."""
    
    valueChanged = pyqtSignal(float)
    
    def __init__(self, initial: float = 0.0, parent=None):
        super().__init__(parent)
        self._value = initial
        self._animation: Optional[QVariantAnimation] = None
    
    @pyqtProperty(float)
    def value(self) -> float:
        return self._value
    
    @value.setter
    def value(self, val: float):
        if self._value != val:
            self._value = val
            self.valueChanged.emit(val)
    
    def animate_to(self, target: float, duration: int = 250, on_finished=None):
        """Animate value to target over duration ms (250ms ease-out = engineered feel).

        on_finished (optional) fires when the animation reaches its end. It is NOT
        called if the animation is superseded (stopped early by a newer animate_to) —
        only on natural completion, or immediately in the duration<=0 jump case. The
        overlay opacity fade uses it to defer hide-teardown until fade-out finishes
        (no extra QTimer — Rule 1).
        """
        if self._animation:
            self._animation.stop()

        # duration<=0 means "jump now" — a zero-duration QVariantAnimation never
        # emits valueChanged, so the jump would silently not happen.
        if duration <= 0:
            self.value = target
            if on_finished is not None:
                on_finished()
            return

        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(self._value)
        self._animation.setEndValue(target)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)  # 250ms OutCubic = premium
        self._animation.valueChanged.connect(lambda v: setattr(self, 'value', v))
        if on_finished is not None:
            self._animation.finished.connect(on_finished)
        self._animation.start()


class ColorAnimator(QObject):
    """Animates between colors smoothly."""
    
    colorChanged = pyqtSignal(QColor)
    
    def __init__(self, initial: QColor = QColor("#000000"), parent=None):
        super().__init__(parent)
        self._color = initial
        self._animation: Optional[QVariantAnimation] = None
    
    @property
    def color(self) -> QColor:
        return self._color
    
    def set_color_immediate(self, color: QColor):
        """Set color without animation."""
        self._color = color
        self.colorChanged.emit(color)
    
    def animate_to(self, target: QColor, duration: int = 250):
        """Animate color to target over duration ms (250ms ease-out = engineered feel)."""
        if self._animation:
            self._animation.stop()

        # duration<=0 means "jump now" — a zero-duration QVariantAnimation never
        # emits valueChanged, so animate=False transitions silently kept the old
        # color (grey waves at boot until the first dictation, 2026-07-02).
        if duration <= 0:
            self.set_color_immediate(target)
            return

        start_color = self._color
        
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)  # 250ms OutCubic = premium
        
        def interpolate(t):
            r = int(start_color.red() + (target.red() - start_color.red()) * t)
            g = int(start_color.green() + (target.green() - start_color.green()) * t)
            b = int(start_color.blue() + (target.blue() - start_color.blue()) * t)
            a = int(start_color.alpha() + (target.alpha() - start_color.alpha()) * t)
            self._color = QColor(r, g, b, a)
            self.colorChanged.emit(self._color)
        
        self._animation.valueChanged.connect(interpolate)
        self._animation.start()


# === Main Overlay Widget ===

class GlassmorphicOverlay(QWidget):
    """
    Premium glassmorphic status overlay widget.
    
    Features:
    - Squircle shape with gradient border
    - Backdrop blur (KDE Plasma) or simulated glass
    - Liquid light sine wave visualization
    - Chasing light animation for processing
    - Smooth state transitions
    """
    
    # Base dimensions - compact but readable
    BASE_HEIGHT = 24  # Thinner profile (was 32)
    BASE_PADDING_H = 10
    BASE_WAVE_WIDTH = 50
    # Space reserved around the squircle for the glow. Must cover the FULL glow
    # falloff: at 6 the outer glow layers (up to 12px) were clipped by the widget
    # bounds, leaving a harsh flat cutoff at the edge (visible as a hard red rim
    # in the LISTENING state). 14 lets the glow fade to zero inside the widget.
    BASE_GLOW_MARGIN = 14
    
    # Layout constants
    TASKBAR_GAP = 12  # Gap above taskbar to prevent overlap
    STARTUP_POSITION_RETRIES = 5  # Number of position checks after startup
    FADE_MS = 200  # window-opacity fade in/out on show/hide (QVariantAnimation, NOT a QTimer — Rule 1 safe)
    
    def __init__(self, scale: float = 0.7, vertical_offset: int = 0, anchor: str = "bottom-center",
                 quality: str = "high"):
        # Scale factor must be set first (before any property access)
        self._scale = max(0.5, min(2.0, scale))
        self._vertical_offset = vertical_offset  # pixels: negative = higher, positive = lower
        self._anchor = anchor  # corner/edge placement: {top,bottom}-{left,center,right}

        # Render quality: "high" = ambient wave animates continuously (smoothest look);
        # "performance" = freeze the render loop once idle in READY to save CPU/battery on
        # handhelds. Visual output is IDENTICAL in both — only whether the idle wave moves.
        self._quality = quality if quality in ("high", "performance") else "high"
        self._idle_frozen = False  # True once we've stopped repainting an idle READY frame

        # Static-chrome cache. The outer glow (8 antialiased superellipse layers), glass
        # background and gradient border are recomputed identically every frame even though
        # they only change on state/colour/size transitions. Rendering them once into a
        # devicePixelRatio-matched pixmap and blitting each frame drops the idle repaint cost
        # (~15-20% of a Deck core) without touching a single pixel of the output. Keyed on the
        # colours/intensity/geometry that actually affect the chrome; rebuilt only when the key
        # changes (i.e. during the brief 250ms state transitions), reused otherwise.
        self._chrome_pixmap: Optional[QPixmap] = None
        self._chrome_key = None
        
        # Setup KWin positioning rule BEFORE creating window
        # This ensures the window is positioned correctly from the first frame
        self._setup_kwin_positioning_rule()
        
        super().__init__()
        
        # State
        self._state = OverlayState.HIDDEN
        self._target_width = 200
        self._current_width = 200.0
        
        # Style indicator state (professional/dev/casual)
        self._current_style = "professional"
        
        # Animation components
        self.wave_renderer = LiquidWaveRenderer()
        self.border_chaser = BorderChaser()
        
        # Animated properties
        self._width_animator = AnimatedValue(200.0, self)
        self._width_animator.valueChanged.connect(self._on_width_changed)
        
        self._glow_intensity = AnimatedValue(0.0, self)
        self._glow_intensity.valueChanged.connect(lambda _: self.update())
        
        self._opacity = AnimatedValue(0.0, self)
        self._opacity.valueChanged.connect(self._on_opacity_changed)
        
        # Seed from the READY palette so the very first painted frame is already
        # on-brand. These previously duplicated pre-2026-07 idle greys and only
        # healed on the first ANIMATED state change (boot showed grey waves).
        _boot = STATE_PALETTES[OverlayState.READY]
        self._border_color_top = ColorAnimator(QColor(_boot.border_top), self)
        self._border_color_bottom = ColorAnimator(QColor(_boot.border_bottom), self)
        self._glow_color = ColorAnimator(QColor(_boot.glow), self)
        self._wave_color = ColorAnimator(QColor(_boot.wave), self)
        
        # Style badge color animator
        initial_style = STYLE_PALETTES.get("professional", STYLE_PALETTES["professional"])
        self._style_badge_color = ColorAnimator(QColor(initial_style.color), self)
        
        for animator in [self._border_color_top, self._border_color_bottom, 
                         self._glow_color, self._wave_color, self._style_badge_color]:
            animator.colorChanged.connect(lambda _: self.update())
        
        # Setup window
        self._setup_window()
        self._setup_fonts()
        self._setup_timers()
        
        # Position at bottom center
        self._position_at_bottom()
    
    @property
    def PADDING_H(self):
        """Scaled horizontal padding."""
        return int(self.BASE_PADDING_H * self._scale)
    
    @property
    def WAVE_WIDTH(self):
        """Scaled wave width."""
        return int(self.BASE_WAVE_WIDTH * self._scale)
    
    @property
    def scaled_height(self):
        """Scaled height for content area."""
        return int(self.BASE_HEIGHT * self._scale)
    
    @property
    def glow_margin(self):
        """Scaled glow margin - keeps proportions consistent at all scales."""
        return max(8, int(self.BASE_GLOW_MARGIN * self._scale))  # Min 8px so the falloff has room
    
    @property
    def widget_height(self):
        """Total widget height including glow margins."""
        return self.scaled_height + (self.glow_margin * 2)
    
    def _calculate_position(self, widget_width: int, widget_height: int) -> tuple[int, int]:
        """
        Calculate overlay position: centered horizontally, at bottom of available screen area.
        
        Uses availableGeometry() which automatically excludes taskbars/panels,
        making this work correctly regardless of taskbar size or position.
        
        Visual layout:
            +------------------+  <- widget top (y)
            |   glow_margin    |
            |  +------------+  |  <- squircle top
            |  | squircle   |  |
            |  +------------+  |  <- squircle bottom
            |   glow_margin    |  <- glow fills this space
            +------------------+  <- widget bottom (at available area bottom - gap)
                 TASKBAR_GAP      <- gap to prevent overlap
            [     TASKBAR     ]   <- excluded from availableGeometry
        
        Returns:
            (x, y) position tuple
        """
        screen = QApplication.primaryScreen()
        if not screen:
            return (0, 0)
        
        # Get both full geometry and available geometry
        full = screen.geometry()
        avail = screen.availableGeometry()
        
        # Position from the configured anchor (corner/edge) within the usable area; pure helpers
        # keep the math testable. The vertical edge also takes the user's fine-tune offset.
        vertical, horizontal = parse_anchor(self._anchor)
        x = anchor_x(avail.x(), avail.width(), widget_width, horizontal, self.TASKBAR_GAP)
        # visual_inset = bottom glow: transparent falloff may hang into the panel strip
        # so the *pill* can sit closer without inventing a permanent empty gap. Positive
        # offset can travel past the old ~12px ceiling (was clamped at usable bottom).
        y = clamp_overlay_y(
            avail.y(), avail.height(), full.y(), full.height(),
            widget_height, self._vertical_offset, self.TASKBAR_GAP,
            vertical=vertical, visual_inset=self.glow_margin,
        )

        return (x, y)
    
    def set_vertical_offset(self, offset: int):
        """Update the vertical position offset and reposition."""
        if offset == self._vertical_offset:
            return
        self._vertical_offset = offset
        self._position_at_bottom()

    def set_anchor(self, anchor: str):
        """Update the corner/edge anchor and reposition."""
        if anchor == self._anchor:
            return
        self._anchor = anchor
        self._position_at_bottom()

    def set_scale(self, scale: float):
        """Update the overlay scale and resize."""
        if abs(scale - self._scale) < 0.01:
            return
        self._scale = max(0.5, min(2.0, scale))
        self._update_font()
        
        # Recalculate target width for current state's text (width depends on scale)
        label = STATE_LABELS.get(self._state, "")
        new_target_width = self._calculate_target_width(label)
        self._current_width = float(new_target_width)
        self._target_width = new_target_width
        
        # Update both width AND height (height changes with scale)
        self._update_size_full()
        self._position_at_bottom()
        self.update()  # Trigger repaint
    
    def _setup_window(self):
        """Configure window flags for overlay behavior (Wayland-proof)."""
        # CRITICAL: All flags must be combined in a SINGLE call
        # 
        # Window flags for overlay:
        # - Tool: Helper window (better Wayland support)
        # - FramelessWindowHint: No decorations
        # - WindowStaysOnTopHint: Stay on top
        # 
        # Note: Actual positioning on Wayland is done via KWin scripting
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowDoesNotAcceptFocus  # Never steal focus from text fields
        )
        
        # Critical for ARGB transparency on all platforms
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAutoFillBackground(False)
        
        # Absolutely refuse to accept focus
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        # X11-specific: don't accept focus
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)
        except:
            pass
        
        # Try to enable blur on KDE Plasma
        self._request_blur()
        
        # Set initial size (with glow margin)
        self.setFixedHeight(self.widget_height)
        self._update_size()
        
        # Set window title for KWin script identification
        self.setWindowTitle("Wayfinder Aura Overlay")
        
        # Periodic raise timer: kept as stopped no-op so existing start()/stop() calls don't crash.
        # On Linux/KDE Wayland: disabled (focus stealing). On macOS: not needed (native level set below).
        self._raise_timer = QTimer(self)
        self._raise_timer.setInterval(60000)  # Effectively never fires

        # macOS: set native window level to "floating panel" — stays above normal windows
        # without stealing focus or needing periodic raise()
        import sys as _sys
        if _sys.platform == "darwin":
            self._set_macos_floating_level()
    
    def _ensure_on_top(self):
        """Ensure window stays on top of other windows.

        Note: On KDE Wayland, periodic raise_() can steal focus from the
        active window (even with WindowDoesNotAcceptFocus). We rely on
        WindowStaysOnTopHint + KWin keepAbove instead.
        """
        # Disabled on Linux: periodic raise() causes focus stealing on KDE Wayland.
        pass

    def _set_macos_floating_level(self):
        """macOS: set native NSWindow level to floating panel.

        Uses PyObjC to set the window to NSFloatingWindowLevel (3), which keeps
        it above all normal windows without stealing focus. Deferred until the
        native window handle is ready.
        """
        QTimer.singleShot(200, self._apply_macos_level)

    def _apply_macos_level(self):
        """Apply NSFloatingWindowLevel after the native window is created."""
        try:
            from AppKit import NSApp
            for nswin in NSApp.windows():
                title = str(nswin.title()) if nswin.title() else ""
                if "Wayfinder" in title or "overlay" in title.lower():
                    # NSFloatingWindowLevel = 3 — above normal, below modal/alerts
                    nswin.setLevel_(3)
                    # canJoinAllSpaces (1<<0) + stationary (1<<4) = visible on all desktops
                    nswin.setCollectionBehavior_((1 << 0) | (1 << 4))
                    nswin.setHidesOnDeactivate_(False)  # Don't hide when app loses focus
                    break
            else:
                # Window not found yet, retry
                QTimer.singleShot(500, self._apply_macos_level)
        except ImportError:
            pass
        except Exception:
            pass
    
    def _request_blur(self):
        """Request backdrop blur from compositor."""
        try:
            # For KDE Plasma, we need to set the blur region via X11
            # This requires the window to be shown first
            # We'll do this in showEvent
            pass
        except Exception as e:
            print(f"Could not enable blur: {e}")
    
    def showEvent(self, event):
        """Handle show event to setup blur, mask, position, and KDE always-on-top."""
        super().showEvent(event)
        self._setup_kde_blur()
        self._apply_squircle_mask()
        
        # Position after window is shown (Wayland often ignores pre-show positioning)
        self._position_at_bottom()
        
        # One delayed retry to handle WM not being ready yet
        QTimer.singleShot(500, self._position_at_bottom)
        
        # Try KDE-specific always-on-top via KWin script (once, with delay)
        if os.environ.get("XDG_CURRENT_DESKTOP", "").upper() == "KDE":
            try:
                if self.windowHandle():
                    QTimer.singleShot(1000, self._try_kde_keep_above)
            except Exception:
                pass
    
    def _try_kde_keep_above(self):
        """Attempt to set window position via KDE KWin scripting (only way that works on Wayland)."""
        try:
            # Use unified position calculation
            x, y = self._calculate_position(self.width(), self.height())
            
            # Use KWin script to force position (only way on Wayland)
            _force_kde_window_position(
                "Wayfinder Aura Overlay",
                x, y,
                self.width(), self.height()
            )
        except Exception as e:
            print(f"KWin positioning failed: {e}", file=sys.stderr)
    
    def _apply_squircle_mask(self):
        """Clear any window mask - rely on transparent background instead.
        
        Previously this applied a polygon mask to clip corners, but the polygon
        approximation of the squircle curves created visible rectangular artifacts.
        Modern Wayland compositors handle transparency correctly without a mask.
        """
        # Clear any existing mask - let transparency handle it
        self.clearMask()
    
    def _setup_kde_blur(self):
        """Setup KDE Plasma blur behind window."""
        try:
            from PyQt6.QtGui import QGuiApplication
            
            # Get the native window handle
            if self.windowHandle():
                # For X11/XWayland, try to set blur property
                # This is KDE-specific
                # Desktop probe — env only (no shell=True subprocess).
                _ = os.environ.get("XDG_CURRENT_DESKTOP", "")
                # Blur will be handled by compositor if available
        except Exception:
            pass  # Blur not available, fallback to semi-transparent
    
    def _setup_fonts(self):
        """Setup premium typography - refined mono for pro-tool aesthetic."""
        # Prefer Geist Mono for modern look, fallback chain
        font_families = ["Geist Mono", "JetBrains Mono", "SF Mono", "Cascadia Code", "monospace"]
        
        # Find first available font
        available_families = QFontDatabase.families()
        self._font_family = "monospace"
        for family in font_families:
            if family in available_families:
                self._font_family = family
                break
        
        self._update_font()
    
    def _update_font(self):
        """Update font with current scale."""
        base_size = 10  # Readable base size
        scaled_size = max(8, int(base_size * self._scale))
        self._font = QFont(self._font_family, scaled_size)
        self._font.setWeight(QFont.Weight.Medium)
        self._font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4 * self._scale)
    
    def _setup_timers(self):
        """Setup animation timers."""
        # Main render timer (15 FPS - optimized for CPU usage while keeping smooth feel)
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._on_frame)
        self._render_timer.setInterval(66)  # ~15 FPS
    
    def _setup_kwin_positioning_rule(self):
        """Setup KWin rule to position overlay before window is created."""
        if os.environ.get("XDG_CURRENT_DESKTOP", "").upper() != "KDE":
            return
        
        try:
            # Calculate target position based on primary screen
            app = QApplication.instance()
            if app:
                screen = app.primaryScreen()
                if screen:
                    # Get both full geometry and available geometry
                    full = screen.geometry()
                    avail = screen.availableGeometry()
                    
                    # Calculate exact widget size
                    # Initial width for "Listening..." text (approximate)
                    estimated_content_width = 200  # Will be recalculated when shown
                    estimated_width = estimated_content_width + (self.glow_margin * 2)
                    estimated_height = self.widget_height
                    
                    # Same single-source positioning as _calculate_position — respects the
                    # configured anchor + the user's vertical offset at startup.
                    vertical, horizontal = parse_anchor(self._anchor)
                    x = anchor_x(avail.x(), avail.width(), estimated_width, horizontal, self.TASKBAR_GAP)
                    y = clamp_overlay_y(
                        avail.y(), avail.height(), full.y(), full.height(),
                        estimated_height, self._vertical_offset, self.TASKBAR_GAP,
                        vertical=vertical, visual_inset=self.glow_margin,
                    )
                    _setup_kwin_window_rule(x, y, estimated_width, estimated_height)
        except Exception as e:
            print(f"KWin positioning rule setup failed: {e}", file=sys.stderr)
    
    @staticmethod
    def _is_wayland_session() -> bool:
        return (
            os.environ.get("XDG_SESSION_TYPE") == "wayland"
            or bool(os.environ.get("WAYLAND_DISPLAY"))
        )

    def _position_at_bottom(self):
        """Position overlay on the configured anchor (default bottom-center).

        On **Wayland/KDE**, Qt ``setGeometry`` / ``move`` do not reliably set absolute
        position — they often flash the window to the compositor default (center /
        top-left) for a frame, then KWin corrects it. That is the listen-button
        "overlay jumps" bug. So on Wayland we **never** call setGeometry for on-screen
        placement; only KWin ``frameGeometry`` (the path that actually sticks).

        On X11, native setGeometry/move work and we skip KWin scripting (loadScript
        spam can freeze the overlay on SteamOS X11).
        """
        w, h = self.width(), self.height()
        x, y = self._calculate_position(w, h)
        
        # Validate position - if y is negative or unreasonable, use fallback
        screen = QApplication.primaryScreen()
        if screen:
            full = screen.geometry()
            # Sanity check: y should be positive and widget should be on screen
            if y < 0 or y > full.y() + full.height() - h:
                # Position seems wrong, use a safe fallback (60px from bottom)
                y = full.y() + full.height() - 60 - h

        if self._is_wayland_session():
            # Size-only via Qt is OK (setFixedWidth in _update_size); position = KWin only.
            if self.isVisible():
                _force_kde_window_position("Wayfinder Aura Overlay", x, y, w, h)
            return

        # X11 (and non-Wayland): Qt owns geometry.
        self.setGeometry(x, y, w, h)
        self.move(x, y)
        try:
            if self.windowHandle():
                from PyQt6.QtCore import QPoint
                self.windowHandle().setPosition(QPoint(x, y))
        except Exception:
            pass
    
    def _update_size(self):
        """Update widget width based on current width (called during animations)."""
        width = int(self._current_width) + (self.glow_margin * 2)
        self.setFixedWidth(width)
        self._position_at_bottom()
        # Re-apply mask when size changes
        if self.isVisible():
            self._apply_squircle_mask()
    
    def _update_size_full(self):
        """Update both widget width AND height (called when scale changes)."""
        width = int(self._current_width) + (self.glow_margin * 2)
        height = self.widget_height
        self.setFixedSize(width, height)
        # Re-apply mask when size changes
        if self.isVisible():
            self._apply_squircle_mask()
            # Force reposition after size change (critical for Wayland)
            self._position_at_bottom()
    
    def _on_width_changed(self, width: float):
        """Handle width animation updates."""
        self._current_width = width
        self._update_size()
        self.update()
    
    def _on_opacity_changed(self, opacity: float):
        """Handle opacity animation updates — drives the window-level fade in/out.

        Teardown after a fade-OUT (move off-screen / hide) is owned by _finish_hide
        (mode-aware), NOT here: calling self.hide() when opacity hits 0 would break
        persistent mode, which must move OFF-SCREEN (never hide()) to avoid stealing
        focus on the next show.
        """
        self.setWindowOpacity(opacity)
    
    def _calculate_target_width(self, text: str) -> int:
        """Calculate required width for text and wave."""
        from PyQt6.QtGui import QFontMetrics
        
        # Space for integrated style label + divider (dynamic based on label text)
        style_label_width = self._get_style_label_width() + int(4 * self._scale)
        
        if not text:
            # READY state: compact pill with style label + centered wave.
            if self._quality == "performance":
                # Performance: no idle waveform — the pill collapses to just the style indicator.
                return style_label_width + int(self.PADDING_H * 0.8)
            return style_label_width + self.WAVE_WIDTH + int(self.PADDING_H * 0.8)
        
        # LISTENING/PROCESSING: full width with label + text + wave
        fm = QFontMetrics(self._font)
        text_width = fm.horizontalAdvance(text)
        return self.PADDING_H * 2 + style_label_width + text_width + 6 + self.WAVE_WIDTH
    
    def set_state(self, state: OverlayState, animate: bool = True):
        """
        Transition to a new state.
        
        Args:
            state: Target state
            animate: Whether to animate the transition
        """
        # Import the debug logger from the run_overlay scope
        import time
        def _log(msg):
            try:
                _xdg_cache = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
                _log_dir = os.path.join(_xdg_cache, "wayfinder-aura")
                os.makedirs(_log_dir, mode=0o700, exist_ok=True)
                _log_path = os.path.join(_log_dir, "overlay-debug.log")
                # Owner-only create/open (no create-then-chmod race for new files).
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
                fd = os.open(_log_path, flags, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(f"{time.time():.3f}: {msg}\n")
                try:
                    os.chmod(_log_path, 0o600)
                except OSError:
                    pass
            except Exception:
                pass
        
        _log(f"set_state: current={self._state} -> new={state}")
        
        # Cancel any pending delayed (min-display) transition — a newer state change supersedes
        # it. Done BEFORE the same-state early-return so even a repeated PROCESSING command
        # clears a queued PROCESSING->READY that would otherwise show "ready" while recording.
        # (The delayed callback below also re-checks the state.)
        existing_delayed = getattr(self, '_delayed_state_timer', None)
        if existing_delayed is not None:
            try:
                existing_delayed.stop()
            except Exception:
                pass
            self._delayed_state_timer = None

        if state == self._state:
            _log(f"set_state: EARLY RETURN (state unchanged)")
            return

        # Enforce minimum display time for PROCESSING state
        # Otherwise it flashes by too fast to see (< 20ms sometimes)
        if self._state == OverlayState.PROCESSING and state == OverlayState.READY:
            elapsed = getattr(self, '_processing_start_time', 0)
            if elapsed:
                elapsed_ms = (time.time() - elapsed) * 1000
                min_display_ms = 800  # Show "Processing..." for at least 800ms
                if elapsed_ms < min_display_ms:
                    remaining = int(min_display_ms - elapsed_ms)
                    # QTimer(0) busy-spins the Qt loop (seen as "DELAYING by 0ms" storms).
                    # Sub-millisecond remainder: apply immediately; otherwise use ≥1ms.
                    if remaining <= 0:
                        pass  # fall through to apply READY now
                    else:
                        remaining = max(1, remaining)
                        _log(f"set_state: DELAYING transition by {remaining}ms (min display time)")
                        # Parent the timer to the widget so it can't be garbage-collected before it
                        # fires (which would drop the Processing->Ready transition). The callback is
                        # guarded and only applies if we're STILL in PROCESSING — a newer transition
                        # (which also cancels this timer at the top of set_state) must win.
                        self._delayed_state_timer = QTimer(self)
                        self._delayed_state_timer.setSingleShot(True)

                        def _do_delayed_transition():
                            try:
                                if self._state == OverlayState.PROCESSING:
                                    self.set_state(state, animate)
                            except Exception as exc:
                                _log(f"set_state: delayed transition error: {exc}")

                        self._delayed_state_timer.timeout.connect(_do_delayed_transition)
                        self._delayed_state_timer.start(remaining)
                        return
        
        old_state = self._state
        self._state = state
        
        # Track when we entered PROCESSING state
        if state == OverlayState.PROCESSING:
            import time
            self._processing_start_time = time.time()

        # ERROR is a transient banner state: show the red pill, then auto-return to READY
        # after ~2s via a single-shot QTimer (NOT a self-rearming sub-100ms loop — Rule 1
        # safe). Parented to the widget so it survives until it fires; guarded so it only
        # applies if we're STILL in ERROR. Any newer state change cancels our prior error
        # timer here, so a real transition (e.g. next dictation) always wins.
        existing_error = getattr(self, '_error_return_timer', None)
        if existing_error is not None:
            try:
                existing_error.stop()
            except Exception:
                pass
            self._error_return_timer = None
        if state == OverlayState.ERROR:
            self._error_return_timer = QTimer(self)
            self._error_return_timer.setSingleShot(True)

            def _do_error_return():
                try:
                    if self._state == OverlayState.ERROR:
                        self.set_state(OverlayState.READY, animate)
                except Exception as exc:
                    _log(f"set_state: error auto-return failed: {exc}")

            self._error_return_timer.timeout.connect(_do_error_return)
            self._error_return_timer.start(2000)  # ~2s error display

        _log(f"set_state: CHANGED to {self._state}")
        
        duration = 250 if animate else 0  # 250ms ease-out = engineered, not vibe-coded
        
        if state == OverlayState.HIDDEN:
            self._render_timer.stop()
            self._raise_timer.stop()

            # Fade the whole window out, THEN tear down (off-screen move / hide) so it
            # doesn't pop out. Reuses the _opacity AnimatedValue (QVariantAnimation) and
            # defers the mode-aware teardown to that animation's finished callback —
            # adds NO timer (Rule 1). animate=False -> duration 0 -> jump to 0 and tear
            # down immediately (the finished hook fires synchronously in the jump path).
            self._opacity.animate_to(
                0.0, self.FADE_MS if animate else 0, on_finished=self._finish_hide
            )
            return
        
        # Get colors for new state
        palette = STATE_PALETTES.get(state, STATE_PALETTES[OverlayState.READY])
        label = STATE_LABELS.get(state, "")
        
        # Calculate new width
        target_width = self._calculate_target_width(label)

        # On Wayland, animating width fires setFixedWidth + KWin every tick. Even without
        # setGeometry, multi-step size changes can still reflow the surface. Snap width
        # instantly so READY→Listening is one size + one KWin place (no jump train).
        # Color/glow still animate for polish.
        width_duration = 0 if self._is_wayland_session() else duration
        
        # Animate properties (guarded: a bad color/NaN must not abort the transition or stop
        # future repaints — the new state has already been committed above).
        try:
            self._width_animator.animate_to(float(target_width), width_duration)
            self._border_color_top.animate_to(QColor(palette.border_top), duration)
            self._border_color_bottom.animate_to(QColor(palette.border_bottom), duration)
            self._glow_color.animate_to(QColor(palette.glow), duration)
            self._wave_color.animate_to(QColor(palette.wave), duration)

            # Set glow intensity based on state
            if state == OverlayState.LISTENING:
                self._glow_intensity.animate_to(1.0, duration)
            elif state == OverlayState.PROCESSING:
                self._glow_intensity.animate_to(0.8, duration)
            elif state == OverlayState.ERROR:
                self._glow_intensity.animate_to(0.8, duration)
            else:
                self._glow_intensity.animate_to(0.3, duration)
        except Exception as exc:
            _log(f"set_state: animator error: {exc}")

        # Force immediate repaint to show new text (don't wait for animators)
        self.update()

        # Wake the render loop for the transition + the new state's animation. In performance
        # mode it may have parked itself while idle in READY; any change to a visible state must
        # un-park it. Harmless no-op in high mode (the loop is always running). HIDDEN returned
        # above, so this only runs for visible states.
        self._idle_frozen = False
        if not self._render_timer.isActive():
            self._render_timer.start()

        # Show and fade in if hidden
        if old_state == OverlayState.HIDDEN:
            self._render_timer.start()
            self._raise_timer.start()
            
            # Delay showing entirely - let KWin script prepare first
            # This completely eliminates the center flash
            QTimer.singleShot(50, lambda: self._delayed_show(animate))

    def _finish_hide(self):
        """Complete the hide after the fade-out finishes: move off-screen (persistent)
        or hide (standard). Guarded so a newer show() that superseded the hide (state is
        no longer HIDDEN, e.g. rapid record-toggle) does NOT yank a now-visible overlay
        off-screen. Wrapped defensively — the callback can fire during app teardown.
        """
        if self._state != OverlayState.HIDDEN:
            return
        try:
            mode = getattr(self, '_overlay_mode', 'persistent')
            if mode == "persistent":
                # Move off-screen instead of hiding (prevents focus stealing on show)
                self.setGeometry(-9999, -9999, self.width(), self.height())
            else:
                # Standard mode: actually hide
                self.hide()
        except Exception:
            pass

    def _delayed_show(self, animate: bool = True):
        """Show or move the overlay on-screen based on mode."""
        # Ensure we're still supposed to be visible
        if self._state == OverlayState.HIDDEN:
            return
        
        # Calculate final size based on current state's text
        label = STATE_LABELS.get(self._state, "")
        content_width = self._calculate_target_width(label)
        final_width = content_width + (self.glow_margin * 2)  # content + glow margins
        final_height = self.widget_height
        
        # Stop any running width animation and set final value immediately
        if self._width_animator._animation:
            self._width_animator._animation.stop()
        self._current_width = float(content_width)
        self._target_width = content_width
        self._width_animator._value = float(content_width)
        
        # Lock size to prevent "growing" animation
        self.setFixedSize(final_width, final_height)
        
        # Calculate position using unified method
        x, y = self._calculate_position(final_width, final_height)

        # Wayland: do NOT setGeometry first — that is what flashes the pill off-anchor
        # (often to center) before KWin can place it. X11 uses Qt geometry.
        if self._is_wayland_session():
            _force_kde_window_position(
                "Wayfinder Aura Overlay",
                x, y,
                final_width, final_height,
            )
        else:
            self.setGeometry(x, y, final_width, final_height)
        
        # Start transparent BEFORE mapping so the first mapped frame is invisible, then
        # fade up. Force the window to 0 directly (the value-setter guard skips the emit
        # when _value is already 0, so setWindowOpacity wouldn't be called) and sync
        # _value so the fade starts from 0. animate=False (boot/instant) skips this and
        # jumps straight to full opacity — no flash.
        if animate:
            self.setWindowOpacity(0.0)
            self._opacity._value = 0.0

        # Check mode for how to show
        mode = getattr(self, '_overlay_mode', 'persistent')
        if mode == "persistent":
            # Window is already shown, just raise it
            self.raise_()
        else:
            # Standard mode: show the window
            self.show()
            self.raise_()

        self.update()

        # Fade in (reuses _opacity AnimatedValue / QVariantAnimation — no new timer,
        # Rule 1). duration 0 when animate=False jumps straight to full opacity.
        self._opacity.animate_to(1.0, self.FADE_MS if animate else 0)
    
    def set_audio_level(self, level: float):
        """Update audio level for wave visualization."""
        # Guard against NaN/inf/garbage: a corrupted level would poison every subsequent frame.
        try:
            level = float(level)
        except (TypeError, ValueError):
            return
        if level != level or level in (float('inf'), float('-inf')):  # NaN or inf
            return
        level = max(0.0, min(1.0, level))  # clamp to the expected range
        self.wave_renderer.update_audio_level(level)
    
    def set_style_indicator(self, style: str, animate: bool = True):
        """
        Update the style badge indicator.
        
        Args:
            style: One of "minimal", "professional", "casual", "dev", "personal"
            animate: Whether to animate the color transition
        """
        if style not in STYLE_PALETTES:
            style = "professional"
        
        if style == self._current_style:
            return
        
        self._current_style = style
        palette = STYLE_PALETTES[style]
        
        duration = 200 if animate else 0
        self._style_badge_color.animate_to(QColor(palette.color), duration)
        self.update()
    
    def get_style_indicator(self) -> str:
        """Get the current style indicator value."""
        return self._current_style
    
    def _on_frame(self):
        """Called each frame to update animations."""
        try:
            # Performance mode: once idle in READY and every state-transition animation has
            # settled, nothing is left to move (audio is silent in READY), so park the render
            # loop — idle CPU falls to ~0. A state change or a quality flip restarts it (see
            # set_state / set_quality). In "high" mode this branch is never taken, so the
            # ambient wave keeps animating exactly as before.
            if (self._quality == "performance"
                    and self._state == OverlayState.READY
                    and not self._transitions_active()):
                if not self._idle_frozen:
                    self._idle_frozen = True
                    self.update()  # paint one final resting frame before parking
                self._render_timer.stop()
                return
            self._idle_frozen = False

            dt = 0.066  # 15 FPS (optimized for CPU usage)

            # Update wave animation
            self.wave_renderer.advance_time(dt)

            # Update border chaser if processing
            if self._state == OverlayState.PROCESSING:
                self.border_chaser.advance(dt)

            self.update()
        except Exception:
            # Never let a frame update raise out of the render timer and stop it. (Rule #10)
            pass

    def _transitions_active(self) -> bool:
        """True while any state-transition animation (width / colour / glow) is still running.

        Performance mode uses this to keep painting through the ~250ms transition into READY
        and only freeze once everything has settled.
        """
        for animated in (self._width_animator, self._glow_intensity,
                         self._border_color_top, self._border_color_bottom,
                         self._glow_color, self._wave_color, self._style_badge_color):
            anim = getattr(animated, "_animation", None)
            if anim is not None and anim.state() == QAbstractAnimation.State.Running:
                return True
        return False

    def _get_chrome_pixmap(self, bar_rect: QRectF, squircle: QPainterPath):
        """Return a cached pixmap of the static chrome (outer glow + glass + gradient border).

        Rebuilt only when a visual input actually changes — glow intensity/colour, the two
        border colours, or the geometry (width / scale / widget size / device pixel ratio).
        During the brief 250ms transitions the key changes each frame so it rebuilds every
        frame (same cost as before); at rest the key is constant so the pixmap is reused and the
        per-frame paint becomes a single blit. The glow is state-driven, NOT audio-reactive, so
        caching it while LISTENING is safe. Returns None if allocation fails (caller draws
        the chrome directly, exactly as before).
        """
        dpr = self.devicePixelRatioF()
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return None
        key = (
            w, h, round(dpr, 4),
            round(self._current_width, 2), round(self._scale, 4),
            round(self._glow_intensity.value, 4),
            self._glow_color.color.rgba(),
            self._border_color_top.color.rgba(),
            self._border_color_bottom.color.rgba(),
        )
        if key == self._chrome_key and self._chrome_pixmap is not None:
            return self._chrome_pixmap
        try:
            pm = QPixmap(int(round(w * dpr)), int(round(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                self._draw_outer_glow(p, squircle, bar_rect)
                self._draw_glass_background(p, squircle)
                self._draw_gradient_border(p, squircle, bar_rect)
            finally:
                p.end()
        except Exception:
            return None
        self._chrome_pixmap = pm
        self._chrome_key = key
        return pm

    def set_quality(self, quality: str):
        """Switch render quality live (from the settings toggle, over the stdin command channel).

        "high" shows the animated ambient wave in every state. "performance" hides the idle
        waveform — in READY the pill collapses to just the style indicator and the render loop
        parks itself (idle CPU ~0); the wave still animates while dictating. Re-lays-out the
        current pill so the waveform appears/disappears immediately on toggle.
        """
        quality = quality if quality in ("high", "performance") else "high"
        if quality == self._quality:
            return
        self._quality = quality
        # Any switch wakes the render loop: "high" must animate immediately; "performance"
        # still needs a running loop so _on_frame can decide to park once idle.
        self._idle_frozen = False
        if self._state != OverlayState.HIDDEN and not self._render_timer.isActive():
            self._render_timer.start()
        # The idle pill width differs between modes (Performance drops the waveform), so animate
        # the current state to its new width — the waveform appears/disappears with it.
        if self._state != OverlayState.HIDDEN:
            try:
                label = STATE_LABELS.get(self._state, "")
                self._width_animator.animate_to(float(self._calculate_target_width(label)), 250)
            except Exception:
                pass
        self.update()
    
    def paintEvent(self, event):
        """Custom paint for glassmorphic overlay."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

            # Explicitly clear to fully transparent (required on macOS)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            # Calculate centered rect for the squircle
            # Margin provides room for subtle glow
            bar_rect = QRectF(
                self.glow_margin,
                self.glow_margin,
                self._current_width,
                self.scaled_height
            )

            # Create squircle path for main shape
            squircle = create_squircle_path(bar_rect, n=4.5)

            # Static chrome (outer glow + glass background + gradient border) — rendered once
            # into a cached pixmap and blitted, instead of recomputed every frame. The result is
            # pixel-identical to drawing the three directly (see _get_chrome_pixmap); the dynamic
            # wave / text / border-chaser / style badge are still drawn live below, in the same
            # order, so nothing about the look changes.
            chrome = self._get_chrome_pixmap(bar_rect, squircle)
            if chrome is not None:
                painter.drawPixmap(0, 0, chrome)
            else:
                # Fallback (pixmap allocation failed) — draw the chrome directly, exactly as before.
                self._draw_outer_glow(painter, squircle, bar_rect)
                self._draw_glass_background(painter, squircle)
                self._draw_gradient_border(painter, squircle, bar_rect)

            # Draw border chaser if processing
            if self._state == OverlayState.PROCESSING:
                self.border_chaser.render(painter, squircle, self._glow_color.color)

            # Clip to squircle for content (text, wave)
            painter.setClipPath(squircle)

            # Draw wave visualization. Performance mode hides the idle waveform: in READY the
            # pill collapses to just the style indicator (the wave still animates while dictating).
            label = STATE_LABELS.get(self._state, "")
            style_label_width = self._get_style_label_width() + int(4 * self._scale)

            suppress_idle_wave = (self._quality == "performance" and not label)
            if not suppress_idle_wave:
                if label:
                    # LISTENING/PROCESSING: wave on right side, text on left
                    wave_rect = QRectF(
                        bar_rect.right() - self.WAVE_WIDTH - self.PADDING_H + 10,
                        bar_rect.top() + 4,
                        self.WAVE_WIDTH - 10,
                        bar_rect.height() - 8
                    )
                else:
                    # READY state: wave to the right of the style label
                    wave_width = self.WAVE_WIDTH - 10
                    # Position wave after the style label area
                    wave_x = bar_rect.left() + style_label_width + self.PADDING_H * 0.3
                    wave_rect = QRectF(
                        wave_x,
                        bar_rect.top() + 4,
                        wave_width,
                        bar_rect.height() - 8
                    )
                self.wave_renderer.render(painter, wave_rect, self._wave_color.color)

            # Draw text (only if there's text to draw)
            if label:
                self._draw_text(painter, bar_rect)

            # Draw style badge (always visible in READY state, left side when text is shown)
            self._draw_style_badge(painter, bar_rect)
        except Exception:
            # Qt can throw during rapid redraws / window destruction; never let one bad frame
            # stop all future repaints (which would freeze the overlay visually). (Rule #10)
            pass
        finally:
            try:
                painter.end()
            except Exception:
                pass
    
    def _draw_outer_glow(self, painter: QPainter, path: QPainterPath, rect: QRectF):
        """Draw the outer glow effect.

        Many thin stacked layers whose alphas shrink toward the outer edge, so
        the composited falloff is smooth and reaches ~zero BEFORE the widget
        boundary. The old version used 4 chunky layers (visible banding) that
        extended past glow_margin and got clipped flat by the widget bounds —
        the harsh red rim users saw in the LISTENING state. Layer extent scales
        with the overlay scale so proportions hold on any display.
        """
        intensity = self._glow_intensity.value
        if intensity <= 0:
            return

        painter.save()

        glow_color = self._glow_color.color

        layers = 8
        # Fade fully inside the reserved margin (1px of safety for antialiasing)
        max_extend = self.glow_margin - max(1.0, self._scale)
        for k in range(1, layers + 1):
            expand = max_extend * k / layers
            # Quadratic-ish tail: inner layers stack to ~0.47 total (matches the
            # old peak brightness); the outermost layer is nearly transparent.
            alpha = 0.19 * (1 - k / (layers + 1)) ** 1.5
            color = QColor(glow_color)
            color.setAlphaF(alpha * intensity)

            expanded_rect = rect.adjusted(-expand, -expand, expand, expand)
            glow_path = create_squircle_path(expanded_rect, n=4.5)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawPath(glow_path)

        painter.restore()
    
    def _draw_glass_background(self, painter: QPainter, path: QPainterPath):
        """Draw the frosted glass background - GitHub Dark base."""
        painter.save()
        
        # GitHub Dark glass (#0D1117 base) - reduces eye strain
        glass_color = QColor(13, 17, 23, int(255 * 0.94))
        
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glass_color))
        painter.drawPath(path)
        
        painter.restore()
    
    def _draw_gradient_border(self, painter: QPainter, path: QPainterPath, rect: QRectF):
        """Draw the metallic gradient border."""
        painter.save()
        
        # Create vertical gradient for metallic effect
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, self._border_color_top.color)
        gradient.setColorAt(1.0, self._border_color_bottom.color)
        
        pen = QPen(QBrush(gradient), 1.5)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        
        painter.restore()
    
    def _draw_text(self, painter: QPainter, rect: QRectF):
        """Draw the status text."""
        label = STATE_LABELS.get(self._state, "")
        if not label:
            return
        
        painter.save()
        
        # Setup font and color
        painter.setFont(self._font)
        
        # Status label is intentionally hardcoded #E8E8E8 (app text_primary,
        # "off-white, no pure white") for every state — the per-state text
        # color was dead, so StateColors has no `text` field by design.
        text_color = QColor("#E8E8E8")
        text_color.setAlphaF(0.92)
        painter.setPen(QPen(text_color))
        
        # Position text with padding - offset for integrated style label
        style_label_width = self._get_style_label_width() + int(4 * self._scale)
        text_rect = QRectF(
            rect.left() + self.PADDING_H + style_label_width,
            rect.top(),
            rect.width() - self.WAVE_WIDTH - self.PADDING_H * 2 - style_label_width,
            rect.height()
        )
        
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        
        painter.restore()
    
    def _get_style_label_width(self) -> int:
        """Calculate width needed for the current style label."""
        from PyQt6.QtGui import QFontMetrics
        palette = STYLE_PALETTES.get(self._current_style, STYLE_PALETTES["professional"])
        label_font = QFont(self._font_family, int(9 * self._scale))
        label_font.setWeight(QFont.Weight.Medium)
        fm = QFontMetrics(label_font)
        return fm.horizontalAdvance(palette.letter) + int(8 * self._scale)
    
    def _draw_style_badge(self, painter: QPainter, rect: QRectF):
        """Draw the integrated style label on the left side of the pill."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Get current style palette
        palette = STYLE_PALETTES.get(self._current_style, STYLE_PALETTES["professional"])
        badge_color = self._style_badge_color.color
        
        # Calculate label width dynamically
        label_width = self._get_style_label_width()
        label_x = rect.left() + self.PADDING_H * 0.4
        
        # Label rect (vertically centered)
        label_rect = QRectF(
            label_x,
            rect.top(),
            label_width,
            rect.height()
        )
        
        # Draw the style label - more blended (lower opacity, lighter weight)
        label_font = QFont(self._font_family, int(9 * self._scale))
        label_font.setWeight(QFont.Weight.Medium)
        painter.setFont(label_font)
        
        # Blend with main text color but tinted with style color
        label_color = QColor(badge_color)
        label_color.setAlphaF(0.7)  # More subtle
        painter.setPen(QPen(label_color))
        
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, palette.letter)

        # Draw very subtle vertical divider after the label — skipped in Performance mode's idle
        # pill, where there's no waveform to divide from (it would be a trailing stub).
        if not (self._quality == "performance" and self._state == OverlayState.READY):
            divider_x = label_x + label_width
            divider_top = rect.top() + rect.height() * 0.3
            divider_bottom = rect.bottom() - rect.height() * 0.3

            divider_color = QColor("#FFFFFF")
            divider_color.setAlphaF(0.08)  # Very subtle
            pen = QPen(divider_color, 1.0 * self._scale)
            painter.setPen(pen)
            painter.drawLine(
                int(divider_x), int(divider_top),
                int(divider_x), int(divider_bottom)
            )

        painter.restore()


# === IPC Command Handler ===

def run_overlay():
    """Run the overlay as a standalone application with stdin command handling."""
    import signal
    import io
    
    # Make stdin unbuffered for immediate command processing
    # This is critical for responsive state changes
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, line_buffering=True)

    # All command input flows through a blocking reader thread. readline() on a
    # buffered stream can pull several pipe writes into Python's buffer in one
    # go, where a select() gate on the raw fd can no longer see them — a state
    # change coalesced with the final audio-level burst then sat undelivered
    # until the next write, seconds later (the "stuck on Listening..." bug).
    stdin_reader = StdinCommandReader(sys.stdin).start()
    
    # Handle termination signals for clean shutdown
    def signal_handler(signum, frame):
        # Do not raise SystemExit from a Python signal handler while Qt may be
        # dispatching a callback, or write to parent-owned pipes that may
        # already be closed during cgroup shutdown.  PyQt treats exceptions
        # escaping into C++ as fatal and aborts the process.  Quitting the
        # event loop lets the existing sys.exit(app.exec()) below return
        # normally instead.
        QApplication.quit()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Parse command line arguments
    mode = "persistent"  # default
    initial_style = "professional"  # default
    initial_scale = 0.7
    initial_offset = 0
    initial_anchor = "bottom-center"
    initial_quality = "high"  # high = ambient wave always animates; performance = freeze when idle
    enable_tray = False  # --tray: host a QSystemTrayIcon in this subprocess (Flatpak/KDE)
    tray_only = False  # --tray-only: no on-screen pill; tray icon + menu only
    for arg in sys.argv:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
        elif arg.startswith("--style="):
            initial_style = arg.split("=", 1)[1]
        elif arg.startswith("--scale="):
            try:
                initial_scale = float(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg.startswith("--offset="):
            try:
                initial_offset = int(arg.split("=", 1)[1])
            except ValueError:
                pass
        elif arg.startswith("--anchor="):
            initial_anchor = arg.split("=", 1)[1]
        elif arg.startswith("--quality="):
            initial_quality = arg.split("=", 1)[1]
        elif arg == "--tray":
            enable_tray = True
        elif arg == "--tray-only":
            tray_only = True
            enable_tray = True  # tray-only implies StatusNotifier tray

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Give KDE's StatusNotifier host the identity it needs to render our tray item.
    # Without an app name / desktop-file name / window icon, Plasma's systemtray reads
    # the item's Id/Title/Icon as null and spams "Cannot read property 'name' of null"
    # from BackgroundAppItem.qml — the icon renders broken ("notification icon crashed").
    try:
        from PyQt6.QtGui import QIcon as _QIcon
        app.setApplicationName("Wayfinder Aura")
        app.setApplicationDisplayName("Wayfinder Aura")
        from wayfinder.utils.platform import get_portal_app_id
        app.setDesktopFileName(get_portal_app_id())
        try:
            from wayfinder.config import ICON_PATH as _APP_ICON
            if _APP_ICON and os.path.exists(str(_APP_ICON)):
                app.setWindowIcon(_QIcon(str(_APP_ICON)))
        except Exception:
            pass
    except Exception:
        pass

    overlay = GlassmorphicOverlay(scale=initial_scale, vertical_offset=initial_offset,
                                  anchor=initial_anchor, quality=initial_quality)
    overlay._overlay_mode = mode  # Store mode for later use
    overlay._tray_only = tray_only
    overlay.set_style_indicator(initial_style, animate=False)  # Set initial style
    
    if tray_only:
        # No on-screen pill: window stays HIDDEN; StatusNotifier tray still runs below.
        overlay._overlay_mode = mode
        overlay.hide()
    elif mode == "persistent":
        # Start in READY state (visible, soft brand-blue READY palette)
        overlay._overlay_mode = mode
        
        # Calculate initial size for "Ready" text
        label = STATE_LABELS.get(OverlayState.READY, "Ready")
        content_width = overlay._calculate_target_width(label)
        final_width = content_width + (overlay.glow_margin * 2)
        final_height = overlay.widget_height
        
        # Use overlay's unified position calculation
        x, y = overlay._calculate_position(final_width, final_height)

        overlay.setFixedSize(final_width, final_height)
        # Wayland: avoid setGeometry (center flash); KWin places after map.
        if not (
            os.environ.get("XDG_SESSION_TYPE") == "wayland"
            or os.environ.get("WAYLAND_DISPLAY")
        ):
            overlay.setGeometry(x, y, final_width, final_height)
        overlay.show()
        overlay.set_state(OverlayState.READY, animate=False)

        def _boot_place():
            _force_kde_window_position(
                "Wayfinder Aura Overlay", x, y, final_width, final_height
            )

        if (
            os.environ.get("XDG_SESSION_TYPE") == "wayland"
            or os.environ.get("WAYLAND_DISPLAY")
        ):
            QTimer.singleShot(50, _boot_place)
            QTimer.singleShot(150, _boot_place)
        else:
            QTimer.singleShot(100, _boot_place)
    # In "standard" mode, window starts hidden
    
    # Command processing timer
    import time as _time_mod
    _last_command_time = [_time_mod.time()]  # mutable ref for closure
    _stdin_eof = [False]
    # Auto-return to READY if no commands received for this long (seconds)
    # Prevents overlay staying stuck in LISTENING/PROCESSING if stdin pipe breaks
    _COMMAND_TIMEOUT = 30.0

    def process_commands():
        """Check for and process stdin commands."""
        try:
            if _stdin_eof[0]:
                if enable_tray:
                    # Parent process is gone — don't leave an orphan tray whose menu actions
                    # would hit a dead socket. Quit so the tray dies with the app. (Codex #3)
                    _debug_log("STDIN EOF in tray mode — quitting overlay")
                    app.quit()
                    return
                # Stdin pipe broke — auto-hide to READY after timeout
                if overlay._state in (OverlayState.LISTENING, OverlayState.PROCESSING):
                    elapsed = _time_mod.time() - _last_command_time[0]
                    if elapsed > _COMMAND_TIMEOUT:
                        _debug_log(f"TIMEOUT: no commands for {elapsed:.0f}s, returning to READY")
                        overlay.set_state(OverlayState.READY)
                return

            # Read ALL commands received since the last tick — the reader thread
            # hands lines over the moment they arrive, so a state change can no
            # longer hide in the stream buffer behind level updates.
            commands = []
            for line in stdin_reader.drain():
                try:
                    commands.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            if stdin_reader.at_eof and not _stdin_eof[0]:
                # EOF — stdin pipe broke (main process died or pipe closed)
                _stdin_eof[0] = True
                _debug_log("STDIN EOF detected — pipe broken")

            if commands:
                _last_command_time[0] = _time_mod.time()

            # Process commands, prioritizing state changes over level updates
            # Sort: state-changing commands first, level updates last
            state_commands = [c for c in commands if c.get("cmd") != "level"]
            level_commands = [c for c in commands if c.get("cmd") == "level"]

            # Process state commands immediately
            for cmd in state_commands:
                handle_command(overlay, cmd)

            # Only process the LAST level command (skip intermediate ones)
            if level_commands:
                handle_command(overlay, level_commands[-1])

            # Watchdog: if stuck in active state with no commands, auto-return to READY
            if not _stdin_eof[0] and overlay._state in (OverlayState.LISTENING, OverlayState.PROCESSING):
                elapsed = _time_mod.time() - _last_command_time[0]
                if elapsed > _COMMAND_TIMEOUT:
                    _debug_log(f"WATCHDOG: no commands for {elapsed:.0f}s, returning to READY")
                    overlay.set_state(OverlayState.READY)

        except Exception as e:
            _debug_log(f"process_commands error: {e}")
    
    # Debug log file for tracing overlay commands (XDG-compliant, not world-readable /tmp)
    _cache_dir = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    _debug_log_dir = os.path.join(_cache_dir, "wayfinder-aura")
    os.makedirs(_debug_log_dir, exist_ok=True)
    _debug_log_file = os.path.join(_debug_log_dir, "overlay-debug.log")
    
    def _debug_log(msg):
        """Write debug message to file for tracing."""
        try:
            import time
            with open(_debug_log_file, "a") as f:
                f.write(f"{time.time():.3f}: {msg}\n")
        except:
            pass
    
    def _update_tray(state):
        """Drive the tray icon to match the overlay state, like the from-source arrow tray:
        LISTENING -> animated red "drawing" arrow; PROCESSING -> gold; otherwise white idle.

        Reads ``tray_icon`` from the enclosing scope at call time (created below, before any
        command is processed). All Qt calls are guarded -- the tray may be absent or tearing down.
        """
        try:
            if tray_icon is None:
                return
            timer = getattr(tray_icon, '_wfa_anim_timer', None)
            if state == OverlayState.LISTENING:
                tray_icon.setToolTip("Wayfinder Aura - Recording")
                if timer is not None and not timer.isActive():
                    tray_icon._wfa_progress[0] = 0.0
                    timer.start(50)
                return
            if timer is not None and timer.isActive():
                timer.stop()
            if state == OverlayState.PROCESSING:
                tray_icon.setIcon(tray_icon._wfa_make("processing"))
                tray_icon.setToolTip("Wayfinder Aura - Processing")
            else:
                tray_icon.setIcon(tray_icon._wfa_idle_icon)
                tray_icon.setToolTip("Wayfinder Aura")
        except Exception as _e:
            _debug_log(f"tray: state update failed: {_e}")

    def _emit_cmd_ack(cmd: dict, *, ok: bool = True, state_name: str | None = None):
        """Emit a Qt-thread ack on stdout when the parent sent a nonce.

        Protocol (Phase 1): parent critical show/ping attach ``nonce``; we only
        ack after the command is handled on the Qt thread (this function runs
        from the QTimer command poller). Write-success on stdin alone is NOT
        proof the overlay is alive — the parent waits for this line.
        Shape: ``{"ack": true, "nonce": "...", "state": "...", "ok": true}``
        """
        nonce = cmd.get("nonce")
        if nonce is None:
            return
        if state_name is None:
            try:
                state_name = (
                    overlay._state.name.lower()
                    if hasattr(overlay._state, "name")
                    else str(overlay._state)
                )
            except Exception:
                state_name = "unknown"
        try:
            print(
                json.dumps(
                    {
                        "ack": True,
                        "nonce": nonce,
                        "state": state_name,
                        "ok": bool(ok),
                    }
                ),
                flush=True,
            )
        except Exception as e:
            _debug_log(f"ack emit failed: {e}")

    def handle_command(overlay: GlassmorphicOverlay, cmd: dict):
        """Handle a command from the main process."""
        command = cmd.get("cmd", "")
        
        # Only log state-changing commands (not level updates)
        if command != "level":
            _debug_log(f"RECV cmd={command} full={cmd}")
        
        if command == "show":
            state_name = cmd.get("state", "listening")
            state_map = {
                "ready": OverlayState.READY,
                "listening": OverlayState.LISTENING,
                "processing": OverlayState.PROCESSING,
                "error": OverlayState.ERROR,
            }
            state = state_map.get(state_name, OverlayState.LISTENING)
            _debug_log(f"SHOW state_name={state_name} -> enum={state} current={overlay._state}")
            try:
                if tray_only:
                    # Drive tray icon/tooltip only — never map the floating pill.
                    _update_tray(state)
                else:
                    overlay.set_state(state)
                    _update_tray(state)
                _emit_cmd_ack(cmd, ok=True, state_name=state_name)
            except Exception as e:
                _debug_log(f"SHOW failed: {e}")
                _emit_cmd_ack(cmd, ok=False, state_name=state_name)

        elif command == "ping":
            # Liveness probe: Qt thread handled the command (no visual change).
            try:
                st = (
                    overlay._state.name.lower()
                    if hasattr(overlay._state, "name")
                    else "ready"
                )
            except Exception:
                st = "ready"
            _emit_cmd_ack(cmd, ok=True, state_name=st)

        elif command == "hide":
            if tray_only:
                _update_tray(OverlayState.READY)
            else:
                overlay.set_state(OverlayState.HIDDEN)
                _update_tray(OverlayState.READY)
        
        elif command == "level":
            if tray_only:
                return  # no visual waveform to drive
            level = cmd.get("value", 0.0)
            overlay.set_audio_level(level)
        
        elif command == "scale":
            scale = cmd.get("value", 1.0)
            overlay.set_scale(scale)

        elif command == "offset":
            offset = int(cmd.get("value", 0))
            overlay.set_vertical_offset(offset)

        elif command == "anchor":
            overlay.set_anchor(str(cmd.get("value", "bottom-center")))

        elif command == "style":
            style = cmd.get("value", "professional")
            overlay.set_style_indicator(style)

        elif command == "quality":
            overlay.set_quality(str(cmd.get("value", "high")))

        elif command == "quit":
            app.quit()
    
    # Optional QSystemTrayIcon, created only when the main process has no in-process pystray
    # tray (the Flatpak case — passed via --tray). It lives in this already-running subprocess
    # and QApplication loop, so it costs no extra process. Menu actions are sent back to the
    # app over its Unix socket — the same channel the R4 shortcut uses. KDE surfaces it via
    # StatusNotifierItem (org.kde.StatusNotifierWatcher).
    tray_icon = None
    tray_available = False
    if enable_tray:
        try:
            from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
            from PyQt6.QtGui import QIcon, QPixmap, QPainter, QColor
            if QSystemTrayIcon.isSystemTrayAvailable():
                import socket as _tray_socket
                try:
                    from wayfinder.config import SOCKET_PATH as _tray_sock_path
                    from wayfinder.config import ICON_PATH as _tray_icon_cfg
                    _tray_icon_path = str(_tray_icon_cfg) if _tray_icon_cfg else None
                except Exception:
                    _tray_sock_path = os.path.join(
                        os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                        "wayfinder-aura", "wayfinder-aura.sock")
                    _tray_icon_path = None

                def _tray_send(verb):
                    try:
                        _s = _tray_socket.socket(_tray_socket.AF_UNIX, _tray_socket.SOCK_STREAM)
                        _s.connect(_tray_sock_path)
                        _s.send(verb.encode("utf-8"))
                        _s.close()
                    except Exception as _e:
                        _debug_log(f"tray: send '{verb}' failed: {_e}")

                # Wayfinder navigation-arrow tray icon (matches the from-source pystray tray):
                # white idle, gold processing, and the outline traced then filled red while
                # recording. The logo PNG (_tray_icon_path) is deliberately NOT used here so the
                # indicator is identical to the desktop tray.
                from wayfinder.ui.tray_icon import make_arrow_icon
                _idle_icon = make_arrow_icon("idle")
                tray_icon = QSystemTrayIcon(_idle_icon)
                tray_icon._wfa_idle_icon = _idle_icon
                tray_icon._wfa_make = make_arrow_icon
                tray_icon._wfa_progress = [0.0]
                # Recording "drawing" animation: a QTimer traces the arrow then fills it (~20fps,
                # 50ms), matching the from-source tray. _update_tray() runs it only while
                # recording and stops it otherwise, so idle CPU is unaffected.
                def _wfa_tray_anim():
                    try:
                        pr = tray_icon._wfa_progress
                        pr[0] = 0.0 if pr[0] > 1.0 else pr[0] + 0.04
                        tray_icon.setIcon(make_arrow_icon("recording", pr[0]))
                    except Exception:
                        pass
                _wfa_anim_timer = QTimer()
                _wfa_anim_timer.timeout.connect(_wfa_tray_anim)
                tray_icon._wfa_anim_timer = _wfa_anim_timer
                tray_icon.setToolTip("Wayfinder Aura")
                # Essential menu, mirroring the (retired) pystray layout the user knows.
                # Every action routes over the Unix socket — the same channel the F3/R4
                # shortcut uses — so it dispatches reliably (unlike pystray's dead clicks).
                _tray_menu = QMenu()
                _tray_menu.addAction("Toggle Recording").triggered.connect(lambda: _tray_send("toggle"))
                _tray_menu.addAction("Reset (unstick overlay)").triggered.connect(lambda: _tray_send("reset"))
                _tray_menu.addSeparator()
                _tray_menu.addAction("Open Settings").triggered.connect(lambda: _tray_send("show"))
                _tray_menu.addAction("Hide to tray").triggered.connect(lambda: _tray_send("hide"))
                _tray_menu.addSeparator()
                _tray_menu.addAction("Quit").triggered.connect(lambda: _tray_send("quit"))
                tray_icon.setContextMenu(_tray_menu)
                tray_icon._wfa_menu = _tray_menu  # keep a Python ref so the menu isn't GC'd
                tray_icon.activated.connect(
                    lambda reason: _tray_send("show")
                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
                tray_icon.show()
                tray_available = True
                _debug_log("tray: QSystemTrayIcon created and shown")
            else:
                _debug_log("tray: system tray not available (no StatusNotifierWatcher)")
        except Exception as _e:
            _debug_log(f"tray: setup failed: {_e}")
            tray_icon, tray_available = None, False

    # Setup command polling timer
    cmd_timer = QTimer()
    cmd_timer.timeout.connect(process_commands)
    cmd_timer.start(50)  # Check every 50ms (20Hz - responsive enough for state changes)

    # Send ready signal. Report tray availability so the main app's hide-to-tray guard
    # never withdraws the window when there is no tray to restore it from.
    print(json.dumps({"status": "ready", "tray_available": tray_available}), flush=True)
    
    sys.exit(app.exec())


# === Direct Test Mode ===

def test_overlay():
    """Test the overlay directly without IPC."""
    app = QApplication(sys.argv)
    
    overlay = GlassmorphicOverlay()
    overlay.set_state(OverlayState.LISTENING)
    
    # Simulate audio levels
    import random
    
    def simulate_audio():
        level = random.uniform(0.0, 0.8)
        overlay.set_audio_level(level)
    
    audio_timer = QTimer()
    audio_timer.timeout.connect(simulate_audio)
    audio_timer.start(50)
    
    # Cycle through states for testing
    states = [OverlayState.LISTENING, OverlayState.PROCESSING]
    state_index = [0]
    
    def cycle_state():
        state_index[0] = (state_index[0] + 1) % len(states)
        overlay.set_state(states[state_index[0]])
    
    state_timer = QTimer()
    state_timer.timeout.connect(cycle_state)
    state_timer.start(5000)  # Change every 5 seconds
    
    sys.exit(app.exec())


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_overlay()
    else:
        run_overlay()

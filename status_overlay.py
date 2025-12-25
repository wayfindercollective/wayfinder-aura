#!/usr/bin/env python3
"""
Wayfinder Voice - Glassmorphic Status Overlay

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
            if (client.caption && client.caption.indexOf("Wayfinder Voice Overlay") !== -1) {{
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
            if (w.caption && w.caption.indexOf("Wayfinder Voice Overlay") !== -1) {{
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
    QGradient,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QRegion,
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


# === Color Palettes ===

@dataclass
class StateColors:
    """Color palette for a given state."""
    border_top: str      # Bright highlight
    border_bottom: str   # Dark shadow
    glow: str            # Outer glow color
    wave: str            # Wave color
    text: str            # Text color


STATE_PALETTES = {
    OverlayState.READY: StateColors(
        border_top="#2D333B",      # GitHub Dark elevated
        border_bottom="#161B22",   # GitHub Dark surface
        glow="#21262D",            # Muted glow
        wave="#3D444D",            # Subtle wave
        text="#8B8B8F",            # Secondary text (2025 accessible)
    ),
    OverlayState.LISTENING: StateColors(
        border_top="#E8A0A8",      # Muted rose highlight
        border_bottom="#2D1520",   # Deep rose shadow
        glow="#E8707F",            # Muted rose glow
        wave="#E8707F",            # Muted rose wave
        text="#E8E8E8",            # Off-white (no pure white)
    ),
    OverlayState.PROCESSING: StateColors(
        border_top="#E8C86A",      # Muted gold highlight
        border_bottom="#2D2208",   # Deep bronze shadow
        glow="#E5AC2A",            # Muted gold glow
        wave="#E5AC2A",            # Muted gold wave
        text="#E8E8E8",            # Off-white (no pure white)
    ),
}

STATE_LABELS = {
    OverlayState.READY: "",
    OverlayState.LISTENING: "Listening...",
    OverlayState.PROCESSING: "Processing...",
}


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
        
        for freq, phase, alpha, thickness in wave_configs:
            amp = max_amp * amplitude_factor
            
            # Build wave path
            path = QPainterPath()
            first_point = True
            
            for x_pixel in range(int(rect.left()), int(rect.right()) + 1, 2):
                x = x_pixel - rect.left()
                
                # Combine sine waves for organic motion
                y = center_y + amp * math.sin(freq * x + self.time + phase)
                y += (amp * 0.4) * math.sin(freq * 2.3 * x + self.time * 1.6 + phase)
                y += (amp * 0.2) * math.sin(freq * 3.7 * x + self.time * 2.1 + phase * 0.5)
                
                # Clamp to bounds
                y = max(rect.top(), min(rect.bottom(), y))
                
                if first_point:
                    path.moveTo(x_pixel, y)
                    first_point = False
                else:
                    path.lineTo(x_pixel, y)
            
            # Create gradient for this wave layer
            wave_color = QColor(color)
            wave_color.setAlphaF(alpha)
            
            # Draw glow layer first (thicker, more transparent)
            glow_color = QColor(color)
            glow_color.setAlphaF(alpha * 0.3)
            glow_pen = QPen(glow_color, thickness + 4)
            glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(glow_pen)
            painter.drawPath(path)
            
            # Draw main wave
            pen = QPen(wave_color, thickness)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(path)
        
        # Draw bright center highlight wave
        highlight_amp = max_amp * amplitude_factor
        highlight_path = QPainterPath()
        first_point = True
        
        for x_pixel in range(int(rect.left()), int(rect.right()) + 1, 2):
            x = x_pixel - rect.left()
            y = center_y + highlight_amp * math.sin(0.13 * x + self.time * 1.4)
            y += (highlight_amp * 0.5) * math.sin(0.26 * x + self.time * 2.0 + 0.8)
            y = max(rect.top(), min(rect.bottom(), y))
            
            if first_point:
                highlight_path.moveTo(x_pixel, y)
                first_point = False
            else:
                highlight_path.lineTo(x_pixel, y)
        
        # Highlight glow
        highlight_glow = QColor(color)
        highlight_glow.setAlphaF(0.4)
        painter.setPen(QPen(highlight_glow, 6))
        painter.drawPath(highlight_path)
        
        # Bright highlight core
        painter.setPen(QPen(color, 2))
        painter.drawPath(highlight_path)
        
        painter.restore()


# === Border Chaser Animation ===

class BorderChaser:
    """
    Animates a bright light traveling around the border perimeter.
    Used for the Processing state.
    """
    
    def __init__(self):
        self.position = 0.0  # 0.0 to 1.0 around perimeter
        self.speed = 1.0 / 1.5  # Complete loop in 1.5 seconds
        
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
        
        # Get total path length and position
        path_length = path.length()
        current_pos = self.position * path_length
        
        # Get point on path
        point = path.pointAtPercent(self.position)
        
        # Draw gradient glow at position
        glow_radius = 30
        gradient = QRadialGradient(point, glow_radius)
        
        bright_color = QColor(color)
        bright_color.setAlphaF(0.9)
        gradient.setColorAt(0, bright_color)
        
        mid_color = QColor(color)
        mid_color.setAlphaF(0.4)
        gradient.setColorAt(0.4, mid_color)
        
        transparent = QColor(color)
        transparent.setAlphaF(0)
        gradient.setColorAt(1.0, transparent)
        
        painter.setBrush(QBrush(gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(point, glow_radius, glow_radius)
        
        # Draw bright core
        core_gradient = QRadialGradient(point, 8)
        white = QColor("#FFFFFF")
        white.setAlphaF(0.95)
        core_gradient.setColorAt(0, white)
        core_gradient.setColorAt(0.5, bright_color)
        core_gradient.setColorAt(1.0, transparent)
        
        painter.setBrush(QBrush(core_gradient))
        painter.drawEllipse(point, 8, 8)
        
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
    
    def animate_to(self, target: float, duration: int = 250):
        """Animate value to target over duration ms (250ms ease-out = engineered feel)."""
        if self._animation:
            self._animation.stop()
        
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(self._value)
        self._animation.setEndValue(target)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)  # 250ms OutCubic = premium
        self._animation.valueChanged.connect(lambda v: setattr(self, 'value', v))
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
    
    # Base dimensions - ultra compact for minimal intrusion
    BASE_HEIGHT = 20
    BASE_PADDING_H = 8
    BASE_WAVE_WIDTH = 40
    
    def __init__(self, scale: float = 1.0):
        # Scale factor must be set first (before any property access)
        self._scale = max(0.5, min(2.0, scale))
        
        # Setup KWin positioning rule BEFORE creating window
        # This ensures the window is positioned correctly from the first frame
        self._setup_kwin_positioning_rule()
        
        super().__init__()
        
        # State
        self._state = OverlayState.HIDDEN
        self._target_width = 200
        self._current_width = 200.0
        
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
        
        self._border_color_top = ColorAnimator(QColor("#2D333B"), self)
        self._border_color_bottom = ColorAnimator(QColor("#161B22"), self)
        self._glow_color = ColorAnimator(QColor("#21262D"), self)
        self._wave_color = ColorAnimator(QColor("#3D444D"), self)
        
        for animator in [self._border_color_top, self._border_color_bottom, 
                         self._glow_color, self._wave_color]:
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
    
    def set_scale(self, scale: float):
        """Update the overlay scale and resize."""
        if abs(scale - self._scale) < 0.01:
            return
        self._scale = max(0.5, min(2.0, scale))
        self._update_font()
        self._update_size()
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
        
        # Critical for ARGB channel on Wayland - must be set for transparency
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        
        # Ensure no background is painted by Qt
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        
        # Absolutely refuse to accept focus
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        
        # X11-specific: don't accept focus
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)
        except:
            pass
        
        # Try to enable blur on KDE Plasma
        self._request_blur()
        
        # Set initial size (minimal margin for subtle glow)
        self.setFixedHeight(self.scaled_height + 16)  # 8px margin each side
        self._update_size()
        
        # Timer to periodically raise window (Wayland focus protector)
        # 250ms is a good balance between responsiveness and CPU usage
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._ensure_on_top)
        self._raise_timer.setInterval(250)
        
        # Set window title for KWin script identification
        self.setWindowTitle("Wayfinder Voice Overlay")
        
        # Timer to periodically raise window (ensures stay-on-top on all compositors)
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._ensure_on_top)
        self._raise_timer.setInterval(100)  # Every 100ms for more aggressive stay-on-top
    
    def _ensure_on_top(self):
        """Ensure window stays on top of other windows (Wayland focus protector)."""
        if self.isVisible():
            # Only raise, never activate - we must not steal focus from text fields
            self.raise_()
    
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
        
        # Force position after window is shown (Wayland often ignores pre-show positioning)
        self._position_at_bottom()
        
        # Schedule another position attempt after window is fully mapped
        QTimer.singleShot(50, self._position_at_bottom)
        QTimer.singleShot(150, self._position_at_bottom)
        
        # Try KDE-specific always-on-top via D-Bus
        if os.environ.get("XDG_CURRENT_DESKTOP", "").upper() == "KDE":
            try:
                if self.windowHandle():
                    QTimer.singleShot(100, self._try_kde_keep_above)
            except Exception:
                pass
    
    def _try_kde_keep_above(self):
        """Attempt to set window position via KDE KWin scripting (only way that works on Wayland)."""
        try:
            screen = QApplication.primaryScreen()
            if screen:
                geom = screen.geometry()
                # Position: centered horizontally, just above taskbar
                # Taskbar is typically 44-48px on 1080p
                TASKBAR_HEIGHT = 44
                
                x = (geom.width() - self.width()) // 2
                y = geom.height() - self.height() - TASKBAR_HEIGHT
                
                # Use KWin script to force position (only way on Wayland)
                _force_kde_window_position(
                    "Wayfinder Voice Overlay",
                    x, y,
                    self.width(), self.height()
                )
        except Exception as e:
            print(f"KWin positioning failed: {e}", file=sys.stderr)
    
    def _apply_squircle_mask(self):
        """Apply window mask to clip black corners (Wayland fix)."""
        # Calculate the squircle bounds
        margin = 8
        bar_rect = QRectF(
            margin,
            margin,
            self._current_width,
            self.scaled_height
        )
        
        # Create expanded path for glow area (minimal)
        glow_margin = 6
        glow_rect = bar_rect.adjusted(-glow_margin, -glow_margin, glow_margin, glow_margin)
        glow_path = create_squircle_path(glow_rect, n=4.5)
        
        # Apply mask - physically clips black corners
        mask_region = QRegion(glow_path.toFillPolygon().toPolygon())
        self.setMask(mask_region)
    
    def _setup_kde_blur(self):
        """Setup KDE Plasma blur behind window."""
        try:
            from PyQt6.QtGui import QGuiApplication
            
            # Get the native window handle
            if self.windowHandle():
                # For X11/XWayland, try to set blur property
                # This is KDE-specific
                import subprocess
                # Check if we're on KDE
                desktop = subprocess.run(
                    ["echo", "$XDG_CURRENT_DESKTOP"],
                    capture_output=True, text=True, shell=True
                )
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
        base_size = 7  # Ultra compact base size
        scaled_size = max(6, int(base_size * self._scale))
        self._font = QFont(self._font_family, scaled_size)
        self._font.setWeight(QFont.Weight.Medium)
        self._font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3 * self._scale)
    
    def _setup_timers(self):
        """Setup animation timers."""
        # Main render timer (30 FPS - designer spec for CPU optimization)
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._on_frame)
        self._render_timer.setInterval(33)  # ~30 FPS (capped per designer spec)
    
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
                    geom = screen.geometry()
                    TASKBAR_HEIGHT = 0
                    GAP = 0
                    
                    # Calculate exact widget size
                    # Initial width for "Listening..." text (approximate)
                    estimated_content_width = 200  # Will be recalculated when shown
                    estimated_width = estimated_content_width + 16  # + glow margins
                    estimated_height = self.scaled_height + 16
                    
                    x = geom.x() + (geom.width() - estimated_width) // 2
                    y = geom.y() + geom.height() - estimated_height - TASKBAR_HEIGHT - GAP
                    
                    _setup_kwin_window_rule(x, y, estimated_width, estimated_height)
        except Exception as e:
            print(f"KWin positioning rule setup failed: {e}", file=sys.stderr)
    
    def _position_at_bottom(self):
        """Position overlay at bottom center of screen, just above taskbar."""
        screen = QApplication.primaryScreen()
        if not screen:
            return
        
        # Get screen geometry
        geom = screen.geometry()
        screen_width = geom.width()
        screen_height = geom.height()
        
        # Position above taskbar (44px is standard KDE Plasma taskbar height)
        TASKBAR_HEIGHT = 44
        GAP = 8  # Small gap above taskbar
        
        # Calculate target position - centered horizontally, above taskbar
        x = geom.x() + (screen_width - self.width()) // 2
        y = geom.y() + screen_height - self.height() - TASKBAR_HEIGHT - GAP
        
        # Try multiple methods to set position (Wayland workarounds)
        # Method 1: setGeometry with explicit size
        self.setGeometry(x, y, self.width(), self.height())
        
        # Method 2: move after geometry is set
        self.move(x, y)
        
        # Method 3: Set position via window handle (more direct on Wayland)
        try:
            if self.windowHandle():
                from PyQt6.QtCore import QPoint
                self.windowHandle().setPosition(QPoint(x, y))
        except Exception:
            pass
    
    def _update_size(self):
        """Update widget size based on current width."""
        width = int(self._current_width) + 16  # Extra for glow (8px each side)
        self.setFixedWidth(width)
        self._position_at_bottom()
        # Re-apply mask when size changes
        if self.isVisible():
            self._apply_squircle_mask()
    
    def _on_width_changed(self, width: float):
        """Handle width animation updates."""
        self._current_width = width
        self._update_size()
        self.update()
    
    def _on_opacity_changed(self, opacity: float):
        """Handle opacity animation updates."""
        self.setWindowOpacity(opacity)
        if opacity <= 0 and self._state == OverlayState.HIDDEN:
            self.hide()
    
    def _calculate_target_width(self, text: str) -> int:
        """Calculate required width for text and wave."""
        from PyQt6.QtGui import QFontMetrics
        
        if not text:
            # READY state: compact pill with just centered wave
            # Minimal padding around the wave
            return self.WAVE_WIDTH + self.PADDING_H
        
        # LISTENING/PROCESSING: full width with text + wave
        fm = QFontMetrics(self._font)
        text_width = fm.horizontalAdvance(text)
        return self.PADDING_H * 2 + text_width + 20 + self.WAVE_WIDTH
    
    def set_state(self, state: OverlayState, animate: bool = True):
        """
        Transition to a new state.
        
        Args:
            state: Target state
            animate: Whether to animate the transition
        """
        if state == self._state:
            return
        
        old_state = self._state
        self._state = state
        
        duration = 250 if animate else 0  # 250ms ease-out = engineered, not vibe-coded
        
        if state == OverlayState.HIDDEN:
            self._render_timer.stop()
            self._raise_timer.stop()
            
            # Check overlay mode
            mode = getattr(self, '_overlay_mode', 'persistent')
            # Check overlay mode
            mode = getattr(self, '_overlay_mode', 'persistent')
            if mode == "persistent":
                # Move off-screen instead of hiding (prevents focus stealing on show)
                self.setGeometry(-9999, -9999, self.width(), self.height())
            else:
                # Standard mode: actually hide
                self.hide()
            return
        
        # Get colors for new state
        palette = STATE_PALETTES.get(state, STATE_PALETTES[OverlayState.READY])
        label = STATE_LABELS.get(state, "")
        
        # Calculate new width
        target_width = self._calculate_target_width(label)
        
        # Animate properties
        self._width_animator.animate_to(float(target_width), duration)
        self._border_color_top.animate_to(QColor(palette.border_top), duration)
        self._border_color_bottom.animate_to(QColor(palette.border_bottom), duration)
        self._glow_color.animate_to(QColor(palette.glow), duration)
        self._wave_color.animate_to(QColor(palette.wave), duration)
        
        # Set glow intensity based on state
        if state == OverlayState.LISTENING:
            self._glow_intensity.animate_to(1.0, duration)
        elif state == OverlayState.PROCESSING:
            self._glow_intensity.animate_to(0.8, duration)
        else:
            self._glow_intensity.animate_to(0.3, duration)
        
        # Show and fade in if hidden
        if old_state == OverlayState.HIDDEN:
            self._render_timer.start()
            self._raise_timer.start()
            
            # Delay showing entirely - let KWin script prepare first
            # This completely eliminates the center flash
            QTimer.singleShot(50, self._delayed_show)
    
    def _delayed_show(self):
        """Show or move the overlay on-screen based on mode."""
        # Ensure we're still supposed to be visible
        if self._state == OverlayState.HIDDEN:
            return
        
        # Calculate final size based on current state's text
        label = STATE_LABELS.get(self._state, "")
        content_width = self._calculate_target_width(label)
        final_width = content_width + 16  # content + glow margins
        final_height = self.scaled_height + 16
        
        # Stop any running width animation and set final value immediately
        if self._width_animator._animation:
            self._width_animator._animation.stop()
        self._current_width = float(content_width)
        self._target_width = content_width
        self._width_animator._value = float(content_width)
        
        # Lock size to prevent "growing" animation
        self.setFixedSize(final_width, final_height)
        
        # Calculate position: centered, just above taskbar
        screen = QApplication.primaryScreen()
        if screen:
            geom = screen.geometry()
            TASKBAR_HEIGHT = 44  # Standard KDE taskbar height
            
            x = (geom.width() - final_width) // 2
            y = geom.height() - final_height - TASKBAR_HEIGHT
            
            # Try Qt first (won't work on Wayland but doesn't hurt)
            self.setGeometry(x, y, final_width, final_height)
            
            # Force position via KWin (the only way that works on Wayland)
            _force_kde_window_position(
                "Wayfinder Voice Overlay",
                x, y,
                final_width, final_height
            )
        
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
    
    def set_audio_level(self, level: float):
        """Update audio level for wave visualization."""
        self.wave_renderer.update_audio_level(level)
    
    def _on_frame(self):
        """Called each frame to update animations."""
        dt = 0.033  # 30 FPS (capped per designer spec for CPU optimization)
        
        # Update wave animation
        self.wave_renderer.advance_time(dt)
        
        # Update border chaser if processing
        if self._state == OverlayState.PROCESSING:
            self.border_chaser.advance(dt)
        
        self.update()
    
    def paintEvent(self, event):
        """Custom paint for glassmorphic overlay."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        
        # Calculate centered rect for the squircle
        # Margin provides room for subtle glow
        margin = 8  # Space for glow
        bar_rect = QRectF(
            margin,
            margin,
            self._current_width,
            self.scaled_height
        )
        
        # Create squircle path for main shape
        squircle = create_squircle_path(bar_rect, n=4.5)
        
        # Draw outer glow (fades into transparency)
        self._draw_outer_glow(painter, squircle, bar_rect)
        
        # Draw glass background
        self._draw_glass_background(painter, squircle)
        
        # Draw gradient border
        self._draw_gradient_border(painter, squircle, bar_rect)
        
        # Draw border chaser if processing
        if self._state == OverlayState.PROCESSING:
            self.border_chaser.render(painter, squircle, self._glow_color.color)
        
        # Clip to squircle for content (text, wave)
        painter.setClipPath(squircle)
        
        # Draw wave visualization
        label = STATE_LABELS.get(self._state, "")
        if label:
            # LISTENING/PROCESSING: wave on right side, text on left
            wave_rect = QRectF(
                bar_rect.right() - self.WAVE_WIDTH - self.PADDING_H + 10,
                bar_rect.top() + 4,
                self.WAVE_WIDTH - 10,
                bar_rect.height() - 8
            )
        else:
            # READY state: center the wave in the compact pill
            wave_width = self.WAVE_WIDTH - 10
            wave_x = bar_rect.left() + (bar_rect.width() - wave_width) / 2
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
        
        painter.end()
    
    def _draw_outer_glow(self, painter: QPainter, path: QPainterPath, rect: QRectF):
        """Draw the outer glow effect."""
        intensity = self._glow_intensity.value
        if intensity <= 0:
            return
        
        painter.save()
        
        glow_color = self._glow_color.color
        
        # Multiple glow layers for soft effect
        for i, (expand, alpha) in enumerate([
            (12, 0.1),
            (8, 0.15),
            (4, 0.2),
            (2, 0.25),
        ]):
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
        
        text_color = QColor("#FAFAFA")
        text_color.setAlphaF(0.92)
        painter.setPen(QPen(text_color))
        
        # Position text with padding
        text_rect = QRectF(
            rect.left() + self.PADDING_H,
            rect.top(),
            rect.width() - self.WAVE_WIDTH - self.PADDING_H * 2,
            rect.height()
        )
        
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, label)
        
        painter.restore()


# === IPC Command Handler ===

def run_overlay():
    """Run the overlay as a standalone application with stdin command handling."""
    import signal
    
    # Handle termination signals for clean shutdown
    def signal_handler(signum, frame):
        print(f"Overlay received signal {signum}, exiting...", file=sys.stderr)
        QApplication.quit()
        sys.exit(0)
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Parse mode argument
    mode = "persistent"  # default
    for arg in sys.argv:
        if arg.startswith("--mode="):
            mode = arg.split("=", 1)[1]
    
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    overlay = GlassmorphicOverlay()
    overlay._overlay_mode = mode  # Store mode for later use
    
    if mode == "persistent":
        # Start in READY state (visible, grey)
        overlay._overlay_mode = mode
        # Calculate position: centered, just above taskbar
        screen = app.primaryScreen()
        if screen:
            geom = screen.geometry()
            TASKBAR_HEIGHT = 44  # Standard KDE taskbar height
            
            # Calculate initial size for "Ready" text
            label = STATE_LABELS.get(OverlayState.READY, "Ready")
            content_width = overlay._calculate_target_width(label)
            final_width = content_width + 16
            final_height = overlay.scaled_height + 16
            
            x = (geom.width() - final_width) // 2
            y = geom.height() - final_height - TASKBAR_HEIGHT
            
            # Try Qt positioning (may not work on Wayland)
            overlay.setGeometry(x, y, final_width, final_height)
            overlay.show()
            overlay.set_state(OverlayState.READY, animate=False)
            
            # Force position via KWin after showing
            QTimer.singleShot(100, lambda: _force_kde_window_position(
                "Wayfinder Voice Overlay",
                x, y,
                final_width, final_height
            ))
    # In "standard" mode, window starts hidden
    
    # Command processing timer
    import select
    
    def process_commands():
        """Check for and process stdin commands."""
        try:
            # Non-blocking read from stdin
            if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
                line = sys.stdin.readline().strip()
                if line:
                    try:
                        cmd = json.loads(line)
                        handle_command(overlay, cmd)
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
    
    def handle_command(overlay: GlassmorphicOverlay, cmd: dict):
        """Handle a command from the main process."""
        command = cmd.get("cmd", "")
        
        if command == "show":
            state_name = cmd.get("state", "listening")
            state_map = {
                "ready": OverlayState.READY,
                "listening": OverlayState.LISTENING,
                "processing": OverlayState.PROCESSING,
            }
            state = state_map.get(state_name, OverlayState.LISTENING)
            overlay.set_state(state)
        
        elif command == "hide":
            overlay.set_state(OverlayState.HIDDEN)
        
        elif command == "level":
            level = cmd.get("value", 0.0)
            overlay.set_audio_level(level)
        
        elif command == "scale":
            scale = cmd.get("value", 1.0)
            overlay.set_scale(scale)
        
        elif command == "quit":
            app.quit()
    
    # Setup command polling timer
    cmd_timer = QTimer()
    cmd_timer.timeout.connect(process_commands)
    cmd_timer.start(16)  # Check every 16ms
    
    # Send ready signal
    print(json.dumps({"status": "ready"}), flush=True)
    
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


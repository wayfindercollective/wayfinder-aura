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

import json
import math
import sys
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
        border_top="#4B5563",      # Subtle gray highlight
        border_bottom="#1F2937",   # Dark gray shadow
        glow="#374151",            # Muted glow
        wave="#6B7280",            # Gray wave
        text="#9CA3AF",            # Muted text
    ),
    OverlayState.LISTENING: StateColors(
        border_top="#FF6B6B",      # Bright crimson highlight
        border_bottom="#7F1D1D",   # Dark maroon shadow
        glow="#F43F5E",            # Electric crimson glow
        wave="#F43F5E",            # Crimson wave
        text="#F8FAFC",            # Bright text
    ),
    OverlayState.PROCESSING: StateColors(
        border_top="#FCD34D",      # Bright gold highlight
        border_bottom="#78350F",   # Deep bronze shadow
        glow="#F59E0B",            # Golden amber glow
        wave="#F59E0B",            # Amber wave
        text="#F8FAFC",            # Bright text
    ),
}

STATE_LABELS = {
    OverlayState.READY: "",
    OverlayState.LISTENING: "Listening...",
    OverlayState.PROCESSING: "Processing...",
}


# === Squircle Path Generator ===

def create_squircle_path(rect: QRectF, n: float = 4.5) -> QPainterPath:
    """
    Create a superellipse (squircle) path.
    
    The squircle is defined by: |x/a|^n + |y/b|^n = 1
    where n=4-5 gives a pleasing blend between square and circle.
    
    Args:
        rect: Bounding rectangle for the squircle
        n: Superellipse exponent (4.5 recommended for squircle)
        
    Returns:
        QPainterPath representing the squircle
    """
    path = QPainterPath()
    
    cx = rect.center().x()
    cy = rect.center().y()
    a = rect.width() / 2
    b = rect.height() / 2
    
    # Generate points around the squircle
    num_points = 100
    points = []
    
    for i in range(num_points):
        theta = 2 * math.pi * i / num_points
        
        # Parametric form of superellipse
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        
        # Sign-preserving power function
        def signed_pow(base, exp):
            return math.copysign(abs(base) ** exp, base)
        
        x = cx + a * signed_pow(cos_t, 2 / n)
        y = cy + b * signed_pow(sin_t, 2 / n)
        
        points.append(QPointF(x, y))
    
    # Create smooth path through points
    if points:
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
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
    
    def animate_to(self, target: float, duration: int = 350):
        """Animate value to target over duration ms."""
        if self._animation:
            self._animation.stop()
        
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(self._value)
        self._animation.setEndValue(target)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
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
    
    def animate_to(self, target: QColor, duration: int = 350):
        """Animate color to target over duration ms."""
        if self._animation:
            self._animation.stop()
        
        start_color = self._color
        
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        
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
    
    # Base dimensions
    BASE_HEIGHT = 44
    PADDING_H = 20
    WAVE_WIDTH = 100
    
    def __init__(self):
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
        
        self._border_color_top = ColorAnimator(QColor("#4B5563"), self)
        self._border_color_bottom = ColorAnimator(QColor("#1F2937"), self)
        self._glow_color = ColorAnimator(QColor("#374151"), self)
        self._wave_color = ColorAnimator(QColor("#6B7280"), self)
        
        for animator in [self._border_color_top, self._border_color_bottom, 
                         self._glow_color, self._wave_color]:
            animator.colorChanged.connect(lambda _: self.update())
        
        # Setup window
        self._setup_window()
        self._setup_fonts()
        self._setup_timers()
        
        # Position at bottom center
        self._position_at_bottom()
    
    def _setup_window(self):
        """Configure window flags for overlay behavior."""
        # Simple flags that work reliably on Wayland/KDE
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.BypassWindowManagerHint  # Bypass WM for true always-on-top
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        
        # X11-specific: don't accept focus
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_X11DoNotAcceptFocus)
        except:
            pass
        
        # Try to enable blur on KDE Plasma
        self._request_blur()
        
        # Set initial size
        self.setFixedHeight(self.BASE_HEIGHT + 20)  # Extra for glow
        self._update_size()
        
        # Timer to periodically raise window (ensures stay-on-top on all compositors)
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._ensure_on_top)
        self._raise_timer.setInterval(100)  # Every 100ms for more aggressive stay-on-top
    
    def _ensure_on_top(self):
        """Ensure window stays on top of other windows."""
        if self.isVisible():
            # Re-apply stay on top hint and raise
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
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
        """Handle show event to setup blur."""
        super().showEvent(event)
        self._setup_kde_blur()
    
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
        """Setup premium typography."""
        # Try to load JetBrains Mono, fallback to system fonts
        font_families = ["JetBrains Mono", "SF Mono", "Fira Code", "monospace"]
        
        # Find first available font
        available_families = QFontDatabase.families()
        selected_family = "monospace"
        for family in font_families:
            if family in available_families:
                selected_family = family
                break
        
        self._font = QFont(selected_family, 11)
        self._font.setWeight(QFont.Weight.Medium)
        self._font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)
    
    def _setup_timers(self):
        """Setup animation timers."""
        # Main render timer (60 FPS)
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._on_frame)
        self._render_timer.setInterval(16)  # ~60 FPS
    
    def _position_at_bottom(self):
        """Position overlay at bottom center of screen, just above taskbar."""
        screen = QApplication.primaryScreen()
        if screen:
            # availableGeometry excludes taskbars/panels
            available = screen.availableGeometry()
            
            # Center horizontally in available area
            x = available.x() + (available.width() - self.width()) // 2
            
            # Position at the very bottom of available area (just above taskbar)
            # Small offset (8px) so it doesn't touch the taskbar
            y = available.y() + available.height() - self.height() - 8
            
            self.move(x, y)
    
    def _update_size(self):
        """Update widget size based on current width."""
        width = int(self._current_width) + 40  # Extra for glow
        self.setFixedWidth(width)
        self._position_at_bottom()
    
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
        fm = QFontMetrics(self._font)
        text_width = fm.horizontalAdvance(text) if text else 0
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
        
        duration = 350 if animate else 0
        
        if state == OverlayState.HIDDEN:
            # Fade out
            self._opacity.animate_to(0.0, 200)
            self._render_timer.stop()
            self._raise_timer.stop()
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
            self._position_at_bottom()  # Ensure correct position before showing
            self.show()
            self.raise_()  # Immediately raise to top
            self._opacity.animate_to(1.0, 200)
            self._render_timer.start()
            self._raise_timer.start()  # Keep raising to stay on top
        
        self.update()
    
    def set_audio_level(self, level: float):
        """Update audio level for wave visualization."""
        self.wave_renderer.update_audio_level(level)
    
    def _on_frame(self):
        """Called each frame to update animations."""
        dt = 0.016  # Assuming 60 FPS
        
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
        margin = 20  # Space for glow
        bar_rect = QRectF(
            margin,
            margin,
            self._current_width,
            self.BASE_HEIGHT
        )
        
        # Create squircle path
        squircle = create_squircle_path(bar_rect, n=4.5)
        
        # Draw outer glow
        self._draw_outer_glow(painter, squircle, bar_rect)
        
        # Draw glass background
        self._draw_glass_background(painter, squircle)
        
        # Draw gradient border
        self._draw_gradient_border(painter, squircle, bar_rect)
        
        # Draw border chaser if processing
        if self._state == OverlayState.PROCESSING:
            self.border_chaser.render(painter, squircle, self._glow_color.color)
        
        # Clip to squircle for content
        painter.setClipPath(squircle)
        
        # Draw wave visualization
        wave_rect = QRectF(
            bar_rect.right() - self.WAVE_WIDTH - self.PADDING_H + 10,
            bar_rect.top() + 4,
            self.WAVE_WIDTH - 10,
            bar_rect.height() - 8
        )
        self.wave_renderer.render(painter, wave_rect, self._wave_color.color)
        
        # Draw text
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
        """Draw the frosted glass background."""
        painter.save()
        
        # Smoked glass color with transparency
        glass_color = QColor(15, 23, 42, int(255 * 0.75))
        
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
        
        text_color = QColor("#F8FAFC")
        text_color.setAlphaF(0.95)
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
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    
    overlay = GlassmorphicOverlay()
    
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


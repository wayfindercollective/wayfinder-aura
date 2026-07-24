"""
Tests for wayfinder.ui.overlay module.

Since PyQt6 may not be available in CI, these tests focus on:
- IPC command JSON format validation
- Style badge color constants / palettes
- State name constants
- Scale parameter validation
- Pure helper functions (e.g., squircle geometry)

PyQt6-dependent tests are guarded with importorskip.
"""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


# =============================================================================
# IPC Command Format Tests (no PyQt6 required)
# =============================================================================


class TestIPCCommandFormat:
    """Validate the JSON command schema used for stdin IPC with the overlay process."""

    def test_show_command_format(self):
        """'show' command must have cmd and state fields."""
        cmd = {"cmd": "show", "state": "listening"}
        raw = json.dumps(cmd)
        parsed = json.loads(raw)
        assert parsed["cmd"] == "show"
        assert parsed["state"] in ("ready", "listening", "processing")

    def test_show_ack_protocol_shape(self):
        """Phase 1: critical show may carry nonce; Qt acks with nonce+state+ok."""
        cmd = {"cmd": "show", "state": "processing", "nonce": "1-abc"}
        raw = json.dumps(cmd)
        assert "\n" not in raw
        parsed = json.loads(raw)
        assert parsed["nonce"] == "1-abc"
        ack = {"ack": True, "nonce": parsed["nonce"], "state": "processing", "ok": True}
        assert json.loads(json.dumps(ack))["ack"] is True

    def test_ping_command_format(self):
        cmd = {"cmd": "ping", "nonce": "2-xyz"}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "ping"
        assert "nonce" in parsed

    def test_hide_command_format(self):
        cmd = {"cmd": "hide"}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "hide"

    def test_level_command_format(self):
        """'level' command carries a float value between 0.0 and 1.0."""
        cmd = {"cmd": "level", "value": 0.5}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "level"
        assert 0.0 <= parsed["value"] <= 1.0

    def test_scale_command_format(self):
        cmd = {"cmd": "scale", "value": 1.0}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "scale"
        assert isinstance(parsed["value"], (int, float))

    def test_style_command_format(self):
        cmd = {"cmd": "style", "value": "professional"}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "style"
        assert parsed["value"] in ("minimal", "professional", "casual", "dev", "personal")

    def test_quality_command_format(self):
        """'quality' command carries 'high' or 'performance' (live overlay-quality toggle)."""
        cmd = {"cmd": "quality", "value": "high"}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "quality"
        assert parsed["value"] in ("high", "performance")

    def test_quit_command_format(self):
        cmd = {"cmd": "quit"}
        parsed = json.loads(json.dumps(cmd))
        assert parsed["cmd"] == "quit"

    def test_all_commands_are_valid_json_strings(self):
        """All IPC commands must serialize to valid single-line JSON."""
        commands = [
            {"cmd": "show", "state": "listening"},
            {"cmd": "show", "state": "processing"},
            {"cmd": "show", "state": "ready"},
            {"cmd": "hide"},
            {"cmd": "level", "value": 0.0},
            {"cmd": "level", "value": 1.0},
            {"cmd": "scale", "value": 0.7},
            {"cmd": "style", "value": "dev"},
            {"cmd": "quit"},
        ]
        for cmd in commands:
            raw = json.dumps(cmd)
            assert "\n" not in raw, f"IPC commands must be single-line: {raw}"
            parsed = json.loads(raw)
            assert "cmd" in parsed


# =============================================================================
# Style Palette Constants (importable without PyQt6)
# =============================================================================

# Import the overlay module carefully -- the top-level import of PyQt6 will fail
# in headless CI, so we guard with importorskip on PyQt6 for anything that
# touches the widget classes, but the palette dicts are defined before the
# PyQt6 imports in the file (after the QPA env setup block).
# We import selectively to test what we can.

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@pytest.fixture(scope="module")
def overlay_module():
    """Try to import the overlay module; skip entire class if PyQt6 is missing."""
    return pytest.importorskip("wayfinder.ui.overlay")


def test_overlay_bare_script_import_bootstraps_wayfinder_package():
    """Desktop source mode launches ``overlay.py`` as a bare script.

    In that mode only ``src/wayfinder/ui`` is on ``sys.path``; without the
    bootstrap near the top of overlay.py, the Qt tray setup later fails with
    ``No module named 'wayfinder'`` and the notification tray dies on state
    changes. Run this in a child process so the current test runner's imported
    packages and ``PYTHONPATH=src`` cannot mask the failure.
    """
    pytest.importorskip("PyQt6")
    repo = Path(__file__).resolve().parent.parent
    overlay = repo / "src" / "wayfinder" / "ui" / "overlay.py"
    code = f"""
import importlib.util
import sys
from pathlib import Path

overlay = Path({str(overlay)!r})
src_root = overlay.parents[2]
repo_root = overlay.parents[3]
sys.path = [
    p for p in sys.path
    if p not in ('', str(src_root), str(repo_root))
]
sys.path.insert(0, str(overlay.parent))
spec = importlib.util.spec_from_file_location('wayfinder_overlay_bare', overlay)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
import wayfinder
import wayfinder.config
import wayfinder.ui.tray_icon
assert str(src_root) in sys.path
"""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_overlay_signal_handler_does_not_raise_through_qt():
    """SIGTERM during a Qt callback must stop the loop without aborting PyQt."""
    overlay = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "wayfinder"
        / "ui"
        / "overlay.py"
    )
    tree = ast.parse(overlay.read_text())
    run_overlay = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "run_overlay"
    )
    signal_handler = next(
        node
        for node in run_overlay.body
        if isinstance(node, ast.FunctionDef) and node.name == "signal_handler"
    )
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(signal_handler)
        if isinstance(node, ast.Call)
    }

    assert "QApplication.quit" in calls
    assert "sys.exit" not in calls
    assert "print" not in calls


class TestStylePalettes:
    """Tests for the STYLE_PALETTES dict and STYLE_CYCLE list."""

    def test_all_five_styles_exist(self, overlay_module):
        palettes = overlay_module.STYLE_PALETTES
        expected_styles = ["minimal", "professional", "casual", "dev", "personal"]
        for style in expected_styles:
            assert style in palettes, f"Missing style palette: {style}"

    def test_style_colors_are_valid_hex(self, overlay_module):
        """Each style's badge color must be valid hex (the dead `glow` field was removed)."""
        for name, palette in overlay_module.STYLE_PALETTES.items():
            assert HEX_COLOR_RE.match(palette.color), (
                f"STYLE_PALETTES['{name}'].color = '{palette.color}' is not valid hex"
            )

    def test_style_letters_are_non_empty(self, overlay_module):
        """Each style must have a non-empty letter/label."""
        for name, palette in overlay_module.STYLE_PALETTES.items():
            assert len(palette.letter) > 0, f"Empty letter for style: {name}"

    def test_style_cycle_order(self, overlay_module):
        """STYLE_CYCLE should contain all styles in the expected toggle order."""
        assert overlay_module.STYLE_CYCLE == [
            "minimal", "professional", "casual", "dev", "personal"
        ]

    def test_style_cycle_matches_palettes(self, overlay_module):
        """Every entry in STYLE_CYCLE must have a corresponding palette."""
        for style in overlay_module.STYLE_CYCLE:
            assert style in overlay_module.STYLE_PALETTES


# =============================================================================
# Overlay State Constants
# =============================================================================


class TestOverlayStateConstants:
    """Tests for OverlayState enum and STATE_PALETTES / STATE_LABELS."""

    def test_overlay_states_exist(self, overlay_module):
        OverlayState = overlay_module.OverlayState
        assert hasattr(OverlayState, "HIDDEN")
        assert hasattr(OverlayState, "READY")
        assert hasattr(OverlayState, "LISTENING")
        assert hasattr(OverlayState, "PROCESSING")

    def test_state_palettes_cover_visible_states(self, overlay_module):
        """STATE_PALETTES should have entries for READY, LISTENING, PROCESSING."""
        OverlayState = overlay_module.OverlayState
        palettes = overlay_module.STATE_PALETTES
        for state in [OverlayState.READY, OverlayState.LISTENING, OverlayState.PROCESSING]:
            assert state in palettes, f"Missing STATE_PALETTE for {state}"

    def test_state_palette_colors_are_hex(self, overlay_module):
        """All colors in state palettes must be valid hex."""
        for state, palette in overlay_module.STATE_PALETTES.items():
            for field in ["border_top", "border_bottom", "glow", "wave"]:
                value = getattr(palette, field)
                assert HEX_COLOR_RE.match(value), (
                    f"STATE_PALETTES[{state}].{field} = '{value}' is not valid hex"
                )

    def test_state_labels_defined(self, overlay_module):
        """STATE_LABELS should have entries for READY, LISTENING, PROCESSING."""
        OverlayState = overlay_module.OverlayState
        labels = overlay_module.STATE_LABELS
        assert labels[OverlayState.READY] == ""
        assert labels[OverlayState.LISTENING] == "Listening..."
        assert labels[OverlayState.PROCESSING] == "Processing..."


# =============================================================================
# Scale Validation
# =============================================================================


class TestScaleValidation:
    """Test that scale clamping works correctly in the overlay."""

    def test_scale_clamp_range(self, overlay_module):
        """The overlay clamps scale to [0.5, 2.0]."""
        # The clamping logic is: max(0.5, min(2.0, scale))
        clamp = lambda s: max(0.5, min(2.0, s))
        assert clamp(0.3) == 0.5
        assert clamp(0.5) == 0.5
        assert clamp(1.0) == 1.0
        assert clamp(2.0) == 2.0
        assert clamp(3.0) == 2.0

    def test_scale_affects_height_calculation(self, overlay_module):
        """Scaled height should be BASE_HEIGHT * scale."""
        base = overlay_module.GlassmorphicOverlay.BASE_HEIGHT
        # The formula is int(BASE_HEIGHT * scale)
        assert int(base * 0.7) == 16  # Default scale
        assert int(base * 1.0) == 24
        assert int(base * 2.0) == 48


# =============================================================================
# Pure Geometry Helper (squircle)
# =============================================================================


class TestSquircleGeometry:
    """Tests for the create_squircle_path() pure geometry function."""

    def test_squircle_path_creates_closed_path(self, overlay_module):
        """create_squircle_path() should return a closed QPainterPath."""
        from PyQt6.QtCore import QRectF
        path = overlay_module.create_squircle_path(QRectF(0, 0, 200, 30))
        # A closed path should have non-zero element count
        assert path.elementCount() > 0

    def test_squircle_bounding_rect(self, overlay_module):
        """Squircle path bounding rect should be within or equal to the input rect."""
        from PyQt6.QtCore import QRectF
        input_rect = QRectF(10, 10, 180, 24)
        path = overlay_module.create_squircle_path(input_rect)
        bounds = path.boundingRect()
        # The path should be contained within (or very close to) the input rect
        assert bounds.left() >= input_rect.left() - 1
        assert bounds.top() >= input_rect.top() - 1
        assert bounds.right() <= input_rect.right() + 1
        assert bounds.bottom() <= input_rect.bottom() + 1


# =============================================================================
# LiquidWaveRenderer (pure logic, no display)
# =============================================================================


class TestLiquidWaveRenderer:
    """Tests for LiquidWaveRenderer audio level logic (no painting)."""

    def test_audio_level_instant_attack(self, overlay_module):
        """Audio level should jump to peak immediately (instant attack)."""
        renderer = overlay_module.LiquidWaveRenderer()
        renderer.update_audio_level(0.8)
        assert renderer.audio_level == pytest.approx(0.8)

    def test_audio_level_slow_decay(self, overlay_module):
        """After peak, level should decay gradually (not instantly)."""
        renderer = overlay_module.LiquidWaveRenderer()
        renderer.update_audio_level(1.0)
        renderer.update_audio_level(0.0)
        # After one update with 0.0, the smoothed level should still be > 0
        assert renderer.audio_level > 0.5  # 1.0 * 0.75 + 0.0 * 0.25 = 0.75

    def test_advance_time_increments(self, overlay_module):
        """advance_time() should increase internal time counters."""
        renderer = overlay_module.LiquidWaveRenderer()
        initial_time = renderer.time
        renderer.advance_time(0.016)
        assert renderer.time > initial_time

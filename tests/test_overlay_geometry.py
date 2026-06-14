"""Unit tests for the pure overlay-position clamp (no Qt required).

The overlay rests gap-above the taskbar by default, but the user CAN push it (via a large
positive offset) all the way down so its bottom edge touches the bottom of the SCREEN (over
the taskbar) — requested behavior. The clamp's job is to keep the whole widget on the screen:
never off the top, and at most flush with the screen bottom.
"""

from wayfinder.ui.overlay_geometry import clamp_overlay_y


class TestReservedPanel:
    """availableGeometry() excludes the taskbar (the common case): screen 0..1080 with a
    30px reserved taskbar -> available 0..1050."""

    AVAIL_Y, AVAIL_H = 0, 1050
    FULL_Y, FULL_H = 0, 1080
    WIDGET_H, GAP = 40, 12

    def _y(self, offset):
        return clamp_overlay_y(
            self.AVAIL_Y, self.AVAIL_H, self.FULL_Y, self.FULL_H,
            self.WIDGET_H, offset, self.GAP,
        )

    def test_rest_position_sits_gap_above_taskbar(self):
        # bottom edge = y + widget_h should be gap px above the usable bottom (1050)
        y = self._y(0)
        assert y == 1050 - self.GAP - self.WIDGET_H  # 998
        assert y + self.WIDGET_H == 1050 - self.GAP

    def test_drag_fully_down_reaches_screen_bottom(self):
        # Large positive offset pushes the overlay down until its bottom edge touches the
        # bottom of the SCREEN (over the taskbar), not just the usable area.
        y = self._y(200)
        assert y == 1080 - self.WIDGET_H  # 1040 (FULL_H, not the 1050 usable bottom)
        assert y + self.WIDGET_H == 1080  # flush with the screen bottom

    def test_drag_fully_up_clamps_to_top_of_available(self):
        y = self._y(-5000)
        assert y == self.AVAIL_Y  # 0, never off the top

    def test_small_offset_moves_within_bounds(self):
        assert self._y(-50) == 998 - 50  # 948, unclamped
        assert self._y(5) == 998 + 5     # 1003, unclamped


class TestDockOverlayPanel:
    """availableGeometry() did NOT reserve the panel (auto-hide dock / some KDE-Wayland):
    available spans the full screen, so the 48px heuristic kicks in."""

    AVAIL_Y, AVAIL_H = 0, 1080
    FULL_Y, FULL_H = 0, 1080
    WIDGET_H, GAP, MIN_TB = 40, 12, 48

    def _y(self, offset):
        return clamp_overlay_y(
            self.AVAIL_Y, self.AVAIL_H, self.FULL_Y, self.FULL_H,
            self.WIDGET_H, offset, self.GAP, self.MIN_TB,
        )

    def test_rest_uses_heuristic_usable_bottom(self):
        # usable_bottom = 1080 - 48 = 1032; y = 1032 - 12 - 40 = 980
        assert self._y(0) == 1032 - self.GAP - self.WIDGET_H  # 980

    def test_drag_down_reaches_screen_bottom(self):
        # Even in the heuristic case, a large positive offset reaches the true screen bottom.
        y = self._y(500)
        assert y == 1080 - self.WIDGET_H  # 1040
        assert y + self.WIDGET_H == 1080  # flush with the screen bottom


class TestSizeAware:
    """A taller widget must shift the clamp up so the whole thing stays visible."""

    def test_taller_widget_clamps_higher(self):
        common = dict(avail_y=0, avail_h=1050, full_y=0, full_h=1080, offset=200, gap=12)
        short = clamp_overlay_y(widget_h=40, **common)
        tall = clamp_overlay_y(widget_h=80, **common)
        assert short == 1080 - 40  # 1040
        assert tall == 1080 - 80   # 1000, higher up
        # Both keep their bottom edge flush with the screen bottom (1080)
        assert short + 40 == 1080
        assert tall + 80 == 1080


class TestTopPanelOffset:
    """A non-zero available top (e.g. a top panel) must be respected by the upper clamp,
    proving the clamp uses avail_y rather than a hardcoded 0."""

    def test_clamps_to_available_top_not_zero(self):
        y = clamp_overlay_y(
            avail_y=40, avail_h=1000, full_y=0, full_h=1080,
            widget_h=40, offset=-5000, gap=12,
        )
        assert y == 40  # clamps to the top of the usable area, below the top panel

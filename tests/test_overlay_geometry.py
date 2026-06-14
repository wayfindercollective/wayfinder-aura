"""Unit tests for the pure overlay-position helpers (no Qt required).

clamp_overlay_y keeps the whole widget on the USABLE area — flush above the taskbar at most,
never off the top (placing it OVER the taskbar would need KWin scripting, which X11 avoids).
It also handles the 'top' anchor. anchor_x places the widget left/center/right of the usable
area. parse_anchor splits 'bottom-center' → ('bottom', 'center').
"""

from wayfinder.ui.overlay_geometry import clamp_overlay_y, anchor_x, parse_anchor


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

    def test_drag_fully_down_clamps_flush_above_taskbar(self):
        # Large positive offset clamps so the bottom edge rests at the usable bottom (1050),
        # above the taskbar (KWin would clamp native positioning here anyway).
        y = self._y(200)
        assert y == 1050 - self.WIDGET_H  # 1010
        assert y + self.WIDGET_H == 1050  # flush above the taskbar, whole widget visible

    def test_top_anchor_rests_below_top_edge(self):
        # The 'top' vertical anchor sits gap px below the usable top, offset nudges it down.
        y = clamp_overlay_y(self.AVAIL_Y, self.AVAIL_H, self.FULL_Y, self.FULL_H,
                            self.WIDGET_H, 0, self.GAP, vertical="top")
        assert y == self.AVAIL_Y + self.GAP  # 12

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

    def test_drag_down_clamps_above_assumed_taskbar(self):
        y = self._y(500)
        assert y == 1032 - self.WIDGET_H  # 992
        assert y + self.WIDGET_H == 1032  # just above the assumed 48px panel


class TestSizeAware:
    """A taller widget must shift the clamp up so the whole thing stays visible."""

    def test_taller_widget_clamps_higher(self):
        common = dict(avail_y=0, avail_h=1050, full_y=0, full_h=1080, offset=200, gap=12)
        short = clamp_overlay_y(widget_h=40, **common)
        tall = clamp_overlay_y(widget_h=80, **common)
        assert short == 1050 - 40  # 1010
        assert tall == 1050 - 80   # 970, higher up
        # Both keep their bottom edge flush at the usable bottom (1050)
        assert short + 40 == 1050
        assert tall + 80 == 1050


class TestAnchors:
    """anchor_x horizontal placement + parse_anchor splitting."""

    def test_anchor_x_left_center_right(self):
        # usable area x=0..1920, widget 100 wide, margin 12
        assert anchor_x(0, 1920, 100, "left", 12) == 12
        assert anchor_x(0, 1920, 100, "center", 12) == (1920 - 100) // 2  # 910
        assert anchor_x(0, 1920, 100, "right", 12) == 1920 - 100 - 12     # 1808

    def test_anchor_x_respects_offset_origin(self):
        # A non-zero usable-area x (e.g. a left panel) shifts left/center/right with it.
        assert anchor_x(50, 1000, 100, "left", 12) == 62
        assert anchor_x(50, 1000, 100, "right", 12) == 50 + 1000 - 100 - 12  # 938

    def test_anchor_x_clamps_oversize_widget(self):
        # A widget wider than the area never runs off the left edge.
        assert anchor_x(0, 100, 300, "right", 12) == 0

    def test_parse_anchor(self):
        assert parse_anchor("bottom-center") == ("bottom", "center")
        assert parse_anchor("top-left") == ("top", "left")
        assert parse_anchor("top-right") == ("top", "right")
        assert parse_anchor("bottom-left") == ("bottom", "left")
        # Garbage / empty falls back to the historical default.
        assert parse_anchor("") == ("bottom", "center")
        assert parse_anchor("nonsense") == ("bottom", "center")
        assert parse_anchor("top-banana") == ("top", "center")


class TestTopPanelOffset:
    """A non-zero available top (e.g. a top panel) must be respected by the upper clamp,
    proving the clamp uses avail_y rather than a hardcoded 0."""

    def test_clamps_to_available_top_not_zero(self):
        y = clamp_overlay_y(
            avail_y=40, avail_h=1000, full_y=0, full_h=1080,
            widget_h=40, offset=-5000, gap=12,
        )
        assert y == 40  # clamps to the top of the usable area, below the top panel

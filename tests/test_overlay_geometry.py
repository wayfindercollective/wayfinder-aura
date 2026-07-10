"""Unit tests for the pure overlay-position helpers (no Qt required).

clamp_overlay_y places the rest position above the taskbar, then lets positive
offset travel down toward/into the panel zone (lower clamp = physical screen
bottom). Negative offset raises. visual_inset accounts for transparent bottom glow.
"""

from wayfinder.ui.overlay_geometry import clamp_overlay_y, anchor_x, parse_anchor


class TestReservedPanel:
    """availableGeometry() excludes the taskbar (the common case): screen 0..1080 with a
    30px reserved taskbar -> available 0..1050."""

    AVAIL_Y, AVAIL_H = 0, 1050
    FULL_Y, FULL_H = 0, 1080
    WIDGET_H, GAP = 40, 12

    def _y(self, offset, visual_inset=0):
        return clamp_overlay_y(
            self.AVAIL_Y, self.AVAIL_H, self.FULL_Y, self.FULL_H,
            self.WIDGET_H, offset, self.GAP, visual_inset=visual_inset,
        )

    def test_rest_position_sits_gap_above_taskbar(self):
        # bottom edge of widget = usable_bottom - gap (no visual_inset)
        y = self._y(0)
        assert y == 1050 - self.GAP - self.WIDGET_H  # 998
        assert y + self.WIDGET_H == 1050 - self.GAP

    def test_positive_offset_moves_past_old_gap_ceiling(self):
        # Historical bug: +offset clamped after ~gap px so further motion looked stuck.
        y0 = self._y(0)
        y12 = self._y(12)   # old hard ceiling
        y30 = self._y(30)   # past old ceiling, still before screen bottom
        assert y12 == y0 + 12
        assert y30 > y12
        # Past usable_bottom: widget bottom enters the reserved panel strip
        assert y30 + self.WIDGET_H > 1050
        # Large values still move until the screen-bottom clamp
        assert self._y(200) == 1080 - self.WIDGET_H

    def test_large_positive_offset_clamps_to_screen_bottom(self):
        y = self._y(5000)
        assert y == 1080 - self.WIDGET_H  # 1040 — physical screen bottom
        assert y + self.WIDGET_H == 1080

    def test_top_anchor_rests_below_top_edge(self):
        y = clamp_overlay_y(self.AVAIL_Y, self.AVAIL_H, self.FULL_Y, self.FULL_H,
                            self.WIDGET_H, 0, self.GAP, vertical="top")
        assert y == self.AVAIL_Y + self.GAP  # 12

    def test_drag_fully_up_clamps_to_top_of_available(self):
        y = self._y(-5000)
        assert y == self.AVAIL_Y  # 0, never off the top

    def test_small_offset_moves_within_bounds(self):
        assert self._y(-50) == 998 - 50  # 948, unclamped
        assert self._y(5) == 998 + 5     # 1003, unclamped

    def test_visual_inset_lowers_rest_so_glow_does_not_fake_a_gap(self):
        # With 10px bottom glow, rest content sits gap above usable bottom; widget
        # top is higher by (widget_h - inset) only — glow may hang into the gap.
        y = self._y(0, visual_inset=10)
        assert y == 1050 - self.GAP - (self.WIDGET_H - 10)  # 1008
        # Content bottom (y + widget_h - inset) at usable - gap
        assert y + self.WIDGET_H - 10 == 1050 - self.GAP


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

    def test_drag_down_can_reach_screen_bottom(self):
        y = self._y(500)
        assert y == 1080 - self.WIDGET_H  # 1040
        assert y + self.WIDGET_H == 1080


class TestSizeAware:
    """A taller widget must shift the rest position up so the content stays above the gap."""

    def test_taller_widget_rests_higher(self):
        common = dict(avail_y=0, avail_h=1050, full_y=0, full_h=1080, offset=0, gap=12)
        short = clamp_overlay_y(widget_h=40, **common)
        tall = clamp_overlay_y(widget_h=80, **common)
        assert short == 1050 - 12 - 40  # 998
        assert tall == 1050 - 12 - 80   # 958
        assert short + 40 == 1050 - 12
        assert tall + 80 == 1050 - 12

    def test_taller_widget_same_screen_bottom_clamp(self):
        common = dict(avail_y=0, avail_h=1050, full_y=0, full_h=1080, offset=5000, gap=12)
        short = clamp_overlay_y(widget_h=40, **common)
        tall = clamp_overlay_y(widget_h=80, **common)
        assert short == 1080 - 40
        assert tall == 1080 - 80


class TestAnchors:
    """anchor_x horizontal placement + parse_anchor splitting."""

    def test_anchor_x_left_center_right(self):
        assert anchor_x(0, 1920, 100, "left", 12) == 12
        assert anchor_x(0, 1920, 100, "center", 12) == (1920 - 100) // 2  # 910
        assert anchor_x(0, 1920, 100, "right", 12) == 1920 - 100 - 12     # 1808

    def test_anchor_x_respects_offset_origin(self):
        assert anchor_x(50, 1000, 100, "left", 12) == 62
        assert anchor_x(50, 1000, 100, "right", 12) == 50 + 1000 - 100 - 12  # 938

    def test_anchor_x_clamps_oversize_widget(self):
        assert anchor_x(0, 100, 300, "right", 12) == 0

    def test_parse_anchor(self):
        assert parse_anchor("bottom-center") == ("bottom", "center")
        assert parse_anchor("top-left") == ("top", "left")
        assert parse_anchor("top-right") == ("top", "right")
        assert parse_anchor("bottom-left") == ("bottom", "left")
        assert parse_anchor("") == ("bottom", "center")
        assert parse_anchor("nonsense") == ("bottom", "center")
        assert parse_anchor("top-banana") == ("top", "center")


class TestTopPanelOffset:
    """A non-zero available top (e.g. a top panel) must be respected by the upper clamp."""

    def test_clamps_to_available_top_not_zero(self):
        y = clamp_overlay_y(
            avail_y=40, avail_h=1000, full_y=0, full_h=1080,
            widget_h=40, offset=-5000, gap=12,
        )
        assert y == 40

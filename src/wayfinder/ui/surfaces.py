"""Shared surface recipes, applied to standard CTkFrame widgets.

A broad workspace curve, secondary panes that fit their inset cards, and one
smaller card radius and hairline. The native glass backdrop remains owned by
the platform adapter; these are only the opaque foreground surfaces above it.
No OS-specific design forks.
"""

from __future__ import annotations

from collections.abc import Mapping

from wayfinder.ui.theme import COLORS, RADIUS


def panel_style(role: str = "section", colors: Mapping | None = None) -> dict:
    colors = COLORS if colors is None else colors
    if role not in ("workspace", "pane", "section", "inset"):
        raise ValueError(f"Unknown panel role: {role}")
    outer = role in ("workspace", "pane")
    return {
        "fg_color": colors["bg_input"] if role == "inset" else colors["bg_card"],
        "corner_radius": RADIUS["pane"] if role == "pane" else RADIUS["xl"] if outer else RADIUS["md"],
        "border_width": 1,
        "border_color": colors["border_rim"] if outer else colors["border_subtle"],
    }

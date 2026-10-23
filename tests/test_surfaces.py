"""A single panel language above the unchanged native glass backdrop."""

import pytest

from wayfinder.ui.surfaces import panel_style
from wayfinder.ui.theme import COLORS, RADIUS, SPACING


def test_workspace_edges_share_one_radius_and_one_border():
    style = panel_style("workspace")
    assert style == {
        "fg_color": COLORS["bg_card"], "corner_radius": RADIUS["xl"],
        "border_width": 1, "border_color": COLORS["border_rim"],
    }


def test_inset_cards_fit_the_container_curve_without_adding_padding():
    outer = panel_style("workspace")
    inner = panel_style("section")
    assert outer["corner_radius"] >= inner["corner_radius"] + SPACING["md"]
    assert inner["border_color"] == COLORS["border_subtle"]


def test_quiet_inset_uses_the_same_border_and_radius_as_a_section():
    section = panel_style("section")
    inset = panel_style("inset")
    assert inset["corner_radius"] == section["corner_radius"]
    assert inset["border_color"] == section["border_color"]
    assert inset["fg_color"] == COLORS["bg_input"]


def test_pane_curve_fits_cards_and_the_opaque_viewport_without_more_padding():
    from wayfinder.ui.macos_window import CONTENT_PANE_INSET

    radius = panel_style("pane")["corner_radius"]
    assert radius >= panel_style("section")["corner_radius"] + CONTENT_PANE_INSET
    # The square page's corner must be INSIDE the parent arc, not painting
    # over it. This preserves the existing inset instead of widening it.
    assert 2 * (radius - CONTENT_PANE_INSET) ** 2 <= radius ** 2


def test_palette_injection_does_not_mutate_shared_colors():
    colors = {**COLORS, "bg_card": "#101820", "border_rim": "#283440"}
    assert panel_style("workspace", colors)["fg_color"] == "#101820"
    assert COLORS["bg_card"] != "#101820"


def test_unknown_surface_roles_fail_explicitly():
    with pytest.raises(ValueError):
        panel_style("random-new-panel")

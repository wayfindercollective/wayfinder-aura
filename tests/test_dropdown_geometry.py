"""Unit tests for the in-window dropdown panel positioning math.

`dropdown_panel_geometry` (wayfinder_main.py) is a PURE function: given a
control's window-space rect, the desired list height, and the window size, it
returns where to place the open panel — below by default, flipped above when it
would clip, height-capped for long lists, x-clamped within the window. These
tests pin every branch headlessly (no Tk event loop).

Importing wayfinder_main pulls in customtkinter/numpy, so the file is skipped
where those deps are absent (matching tests/test_e2e_ui.py).
"""

import ast
import inspect
import textwrap

import pytest

pytest.importorskip("customtkinter")

from wayfinder_main import dropdown_panel_geometry  # noqa: E402


def test_panel_sized_via_constructor_not_place():
    """Regression: the open panel must be sized in the ``CTkFrame`` constructor,
    never via ``.place(width=, height=)``.

    CustomTkinter's ``CTkBaseClass.place()`` REJECTS ``width``/``height`` — it
    raises ``ValueError`` ("must be passed to the constructor … not the place
    method"). The original ``_open_dropdown_panel`` did
    ``panel.place(x=x, y=y, width=ctrl_w, height=h)``, so every open aborted with
    that exception and the list never appeared — the first-run setup pane's
    whisper-model dropdown was unselectable (the user-reported bug). Verified
    headlessly by parsing the method body: no ``.place()`` call may pass
    width/height, and the panel's ``CTkFrame`` must carry both.
    """
    from wayfinder_main import WayfinderApp

    src = textwrap.dedent(inspect.getsource(WayfinderApp._open_dropdown_panel))
    tree = ast.parse(src)

    place_kwargs: list[str] = []
    ctkframe_kwargs: list[set[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        kwargs = {k.arg for k in node.keywords if k.arg}
        if attr == "place":
            place_kwargs.extend(kwargs)
        elif attr == "CTkFrame":
            ctkframe_kwargs.append(kwargs)

    assert "width" not in place_kwargs and "height" not in place_kwargs, (
        "the panel must NOT pass width/height to CTk .place() — it raises "
        "ValueError and the dropdown list never opens"
    )
    assert any({"width", "height"} <= kw for kw in ctkframe_kwargs), (
        "the panel must be sized via the CTkFrame constructor (width= and height=)"
    )


def test_fits_below_opens_downward():
    # Plenty of room below → panel opens directly under the control.
    x, y, h, opens_up = dropdown_panel_geometry(
        ctrl_x=40, ctrl_y=100, ctrl_w=180, ctrl_h=36,
        list_h=200, win_w=900, win_h=800, margin=8,
    )
    assert opens_up is False
    assert y == 100 + 36 + 2   # control bottom + hairline gap
    assert h == 200            # full list height, uncapped
    assert x == 40             # aligned to the control's left edge


def test_flips_above_near_bottom_edge():
    # No room below, more room above → panel flips upward and sits above.
    x, y, h, opens_up = dropdown_panel_geometry(
        ctrl_x=40, ctrl_y=700, ctrl_w=180, ctrl_h=36,
        list_h=200, win_w=900, win_h=800, margin=8,
    )
    assert opens_up is True
    assert h == 200                     # room above (692) still fits the list
    assert y == 700 - 2 - 200           # control top - gap - height
    assert y >= 8                       # never above the top margin


def test_x_clamped_near_right_edge():
    # Control hugs the right edge → panel is pushed left so it stays in-window.
    x, y, h, opens_up = dropdown_panel_geometry(
        ctrl_x=500, ctrl_y=100, ctrl_w=200, ctrl_h=36,
        list_h=150, win_w=650, win_h=800, margin=8,
    )
    assert x == 650 - 8 - 200           # win_w - margin - panel width
    assert x + 200 <= 650 - 8           # right edge respects the margin


def test_x_clamped_never_below_left_margin():
    # Control starts left of the margin → x snaps to the margin, not negative.
    x, _y, _h, _up = dropdown_panel_geometry(
        ctrl_x=2, ctrl_y=100, ctrl_w=100, ctrl_h=36,
        list_h=150, win_w=500, win_h=800, margin=8,
    )
    assert x == 8


def test_height_capped_for_long_list_below():
    # A huge list can't fit → height is capped to the space below the control.
    _x, y, h, opens_up = dropdown_panel_geometry(
        ctrl_x=40, ctrl_y=100, ctrl_w=180, ctrl_h=36,
        list_h=5000, win_w=900, win_h=400, margin=8,
    )
    # avail_below = 400 - 8 - (100+36+2) = 254; more than avail_above (90) → stays below
    assert opens_up is False
    assert h == 254
    assert y == 100 + 36 + 2
    assert y + h <= 400 - 8   # capped panel bottom sits exactly on the margin


def test_height_capped_for_long_list_above():
    # No room below AND a long list → flips above and caps to the space above.
    _x, y, h, opens_up = dropdown_panel_geometry(
        ctrl_x=40, ctrl_y=380, ctrl_w=180, ctrl_h=16,
        list_h=5000, win_w=900, win_h=400, margin=8,
    )
    # avail_above = (380-2) - 8 = 370; capped height is 370, top lands on margin.
    assert opens_up is True
    assert h == 370
    assert y == 380 - 2 - 370
    assert y >= 8


def test_margin_respected_below():
    # The capped panel bottom must not cross the bottom margin.
    _x, y, h, opens_up = dropdown_panel_geometry(
        ctrl_x=40, ctrl_y=200, ctrl_w=180, ctrl_h=36,
        list_h=5000, win_w=900, win_h=600, margin=8,
    )
    assert opens_up is False
    assert y + h <= 600 - 8

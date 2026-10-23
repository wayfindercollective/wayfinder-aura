"""Real paint/overflow checks without any audio, hotkeys, or user-state writes."""

import pytest

pytestmark = pytest.mark.ui
ctk = pytest.importorskip("customtkinter")


@pytest.fixture
def root():
    import tkinter as tk

    import wayfinder_main  # noqa: F401 -- installs the existing CTk draw adapter

    from wayfinder.ui.scrollbars import apply_scrollbar_theme

    apply_scrollbar_theme(ctk.ThemeManager.theme)
    try:
        root = ctk.CTk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open here: {exc}")
    root.geometry("600x450+3000+3000")
    try:
        yield root
    finally:
        root.destroy()


def _thumb_states(scrollbar):
    canvas = scrollbar._canvas
    items = canvas.find_withtag("scrollbar_parts")
    assert items
    return {canvas.itemcget(item, "state") for item in items}


def test_thumb_follows_content_growth_shrink_and_window_resize(root):
    from wayfinder.ui.theme import COLORS

    scroll = ctk.CTkScrollableFrame(root)
    scroll.pack(fill="both", expand=True)
    content = ctk.CTkFrame(scroll, height=60)
    content.pack(fill="x")
    root.update()
    assert scroll._scrollbar._button_color == COLORS["scrollbar_thumb"]
    assert _thumb_states(scroll._scrollbar) == {"hidden"}
    scroll._scrollbar._on_enter()
    assert _thumb_states(scroll._scrollbar) == {"hidden"}
    content.configure(height=900)
    root.update()
    assert _thumb_states(scroll._scrollbar) == {"normal"}
    root.geometry("600x1100+3000+3000")
    root.update()
    assert _thumb_states(scroll._scrollbar) == {"hidden"}
    root.geometry("600x450+3000+3000")
    root.update()
    assert _thumb_states(scroll._scrollbar) == {"normal"}
    content.configure(height=60)
    root.update()
    assert _thumb_states(scroll._scrollbar) == {"hidden"}


def test_implicit_textbox_bars_share_colors_and_full_range_visibility(root):
    from wayfinder.ui.theme import COLORS

    box = ctk.CTkTextbox(root, wrap="none")
    box.pack(fill="both", expand=True)
    root.update()
    for bar in (box._x_scrollbar, box._y_scrollbar):
        assert bar._button_color == COLORS["scrollbar_thumb"]
        assert bar._button_hover_color == COLORS["scrollbar_thumb_hover"]
        bar.set(0, 1)
        assert _thumb_states(bar) == {"hidden"}
        bar.set(0.2, 0.5)
        assert _thumb_states(bar) == {"normal"}

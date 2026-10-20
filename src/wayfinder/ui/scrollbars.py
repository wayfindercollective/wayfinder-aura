"""Shared scrollbar colors and overflow-only thumbs for standard CTk widgets."""

from wayfinder.ui.theme import COLORS


def apply_scrollbar_theme(theme: dict) -> None:
    """Include implicit scrollbars in text boxes, setup, and model pickers."""
    theme["CTkScrollbar"].update(
        button_color=COLORS["scrollbar_thumb"],
        button_hover_color=COLORS["scrollbar_thumb_hover"],
    )
    theme["CTkTextbox"].update(
        scrollbar_button_color=COLORS["scrollbar_thumb"],
        scrollbar_button_hover_color=COLORS["scrollbar_thumb_hover"],
    )


def sync_scrollbar_visibility(scrollbar) -> None:
    """Hide a full-range thumb without reflowing the content or polling.

    CTk's set/resize draw path calls this after painting. Keeping its gutter
    stable avoids wrapping/layout oscillation at the overflow boundary. Hover
    only changes colors, so it cannot bring an unnecessary thumb back.
    """
    try:
        start, end = scrollbar.get()
        state = "normal" if end - start < 1 - 1e-9 else "hidden"
        scrollbar._canvas.itemconfigure("scrollbar_parts", state=state)
    except Exception:
        pass  # A pending scroll callback may outlive its Tk canvas.

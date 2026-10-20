"""Scrollbar appearance is shared, including widgets with no explicit colors."""

import ast
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wayfinder.ui.scrollbars import apply_scrollbar_theme, sync_scrollbar_visibility
from wayfinder.ui.theme import COLORS


def test_theme_covers_frames_and_both_textbox_scrollbars_without_changing_sizes():
    theme = {
        "CTkScrollbar": {"corner_radius": 9, "border_spacing": 4},
        "CTkTextbox": {"corner_radius": 6},
        "CTkButton": {"fg_color": "unchanged"},
    }
    before = deepcopy(theme)
    apply_scrollbar_theme(theme)
    assert theme["CTkScrollbar"]["button_color"] == COLORS["scrollbar_thumb"]
    assert theme["CTkScrollbar"]["button_hover_color"] == COLORS["scrollbar_thumb_hover"]
    assert theme["CTkTextbox"]["scrollbar_button_color"] == COLORS["scrollbar_thumb"]
    assert theme["CTkTextbox"]["scrollbar_button_hover_color"] == COLORS["scrollbar_thumb_hover"]
    for widget, values in before.items():
        for key, value in values.items():
            assert theme[widget][key] == value


@pytest.mark.parametrize("interval,expected", [
    ((0, 1), "hidden"), ((0, 0.25), "normal"), ((0.75, 1), "normal"),
])
def test_only_overflow_draws_a_thumb(interval, expected):
    canvas = Mock()
    sync_scrollbar_visibility(SimpleNamespace(get=lambda: interval, _canvas=canvas))
    canvas.itemconfigure.assert_called_once_with("scrollbar_parts", state=expected)


def test_late_draw_during_widget_destruction_is_safe():
    canvas = Mock()
    canvas.itemconfigure.side_effect = RuntimeError("canvas destroyed")
    sync_scrollbar_visibility(SimpleNamespace(get=lambda: (0, 1), _canvas=canvas))


def test_no_page_or_picker_overrides_the_shared_scrollbar_colors():
    repo = Path(__file__).resolve().parent.parent
    expected = {
        "scrollbar_button_color": "scrollbar_thumb",
        "scrollbar_button_hover_color": "scrollbar_thumb_hover",
    }
    for path in [repo / "wayfinder_main.py", *sorted((repo / "src" / "wayfinder").rglob("*.py"))]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.keyword) and node.arg in expected:
                assert ast.unparse(node.value) == f"COLORS['{expected[node.arg]}']", (
                    f"{path.relative_to(repo)}:{node.lineno} diverges from the shared scrollbar palette"
                )

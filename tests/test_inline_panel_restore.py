"""Inline-panel hide/restore must not compound CustomTkinter scaling."""

from __future__ import annotations

import sys

import pytest

from wayfinder_main import WayfinderApp, _logical_pack_info_for_restore


class _ScaledWidget:
    def __init__(self, scale: float):
        self.scale = scale

    def _get_widget_scaling(self) -> float:
        return self.scale


def test_pack_restore_converts_scaled_padding_back_to_design_units():
    info = {
        "fill": "x",
        "padx": 32,
        "pady": 20,
        "ipadx": 0,
        "ipady": 0,
    }

    restored = _logical_pack_info_for_restore(_ScaledWidget(2.0), info)

    assert restored == {
        "fill": "x",
        "padx": 16,
        "pady": 10,
        "ipadx": 0,
        "ipady": 0,
    }
    assert info["padx"] == 32  # caller-owned pack_info is never mutated


def test_pack_restore_handles_asymmetric_padding_without_rescaling_other_fields():
    restored = _logical_pack_info_for_restore(
        _ScaledWidget(2.0),
        {"padx": (0, 32), "pady": (8, 24), "side": "top", "expand": 0},
    )

    assert restored == {
        "padx": (0, 16),
        "pady": (4, 12),
        "side": "top",
        "expand": 0,
    }


def test_closing_model_panel_does_not_repack_raw_hidden_controls():
    class _Child:
        def __init__(self):
            self.pack_calls = []

        def pack(self, **kwargs):
            self.pack_calls.append(kwargs)

    class _Panel:
        def __init__(self, children):
            self._inline_hidden = children
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    visible, raw_hidden = _Child(), _Child()
    panel = _Panel([
        (visible, {"fill": "x", "padx": 4}),
        (raw_hidden, None),
    ])

    WayfinderApp._close_inline_panel(object(), None, panel)

    assert panel.destroyed is True
    assert visible.pack_calls == [{"fill": "x", "padx": 4}]
    assert raw_hidden.pack_calls == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Aqua Tk integration")
def test_real_inline_panel_restores_only_previously_packed_rows():
    import customtkinter as ctk

    class _Host:
        font_body = ("Arial",)
        font_header = ("Arial",)
        font_sizes = {"title": 16}
        _close_inline_panel = WayfinderApp._close_inline_panel

    root = ctk.CTk()
    root.withdraw()
    try:
        container = ctk.CTkFrame(root)
        container.pack()
        visible = ctk.CTkFrame(container)
        visible.pack(fill="x", padx=8)
        raw_hidden = ctk.CTkFrame(container)
        raw_hidden.pack(fill="x")
        raw_hidden.pack_forget()

        host = _Host()
        WayfinderApp._show_inline_panel(
            host, container, "Model", lambda _content, _close: None
        )
        assert visible.winfo_manager() == ""
        assert raw_hidden.winfo_manager() == ""

        host._active_inline_panel_close()
        assert visible.winfo_manager() == "pack"
        assert raw_hidden.winfo_manager() == ""
    finally:
        root.destroy()

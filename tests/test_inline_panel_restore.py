"""Inline-panel hide/restore must not compound CustomTkinter scaling."""

from __future__ import annotations

from wayfinder_main import _logical_pack_info_for_restore


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

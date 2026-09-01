"""Regression tests for Disappearing indicator input and Flatpak device metadata."""

import math

import numpy as np
import pytest

wayfinder_main = pytest.importorskip("wayfinder_main")


def test_disappearing_indicator_accepts_numpy_recorder_levels():
    assert wayfinder_main.normalize_indicator_audio_level(
        np.float32(0.375)
    ) == pytest.approx(0.375)


@pytest.mark.parametrize("value", [None, "bad", math.nan, math.inf, -1.0])
def test_disappearing_indicator_rejects_or_clamps_invalid_levels(value):
    assert wayfinder_main.normalize_indicator_audio_level(value) == 0.0


def test_proc_input_metadata_lists_physical_keyboard_and_mouse_once():
    mouse_bits = hex((1 << 272) | (1 << 275))[2:]
    keyboard_bits = hex((1 << 59) | (1 << 88))[2:]
    media_bits = hex(1 << 115)[2:]
    sample = f'''
N: Name="ydotoold virtual device"
H: Handlers=sysrq kbd mouse2 event21
B: KEY={mouse_bits}

N: Name="Corsair Gaming Mouse"
H: Handlers=mouse0 event2
B: KEY={mouse_bits}

N: Name="Corsair Gaming Mouse Keyboard"
H: Handlers=sysrq kbd event6
B: KEY={keyboard_bits}

N: Name="Keychron  Keychron Link  Keyboard"
H: Handlers=sysrq kbd leds event11
B: KEY={keyboard_bits}

N: Name="USB Microphone Media Controls"
H: Handlers=kbd event12
B: KEY={media_bits}

N: Name="Power Button"
H: Handlers=kbd event0
B: KEY={media_bits}
'''
    assert wayfinder_main.parse_proc_input_device_names(sample) == [
        "Corsair Gaming Mouse",
        "Keychron Keychron Link",
    ]

"""The Settings hotkey dropdown must only offer keys the listener can match.

Both tables are read as source (no pynput import, no Tk), so this runs
anywhere. ScrollLock and Pause shipped in the dropdown without ever being in
``_raw_evdev_map``: selecting either produced a hotkey that silently never
fired, which is indistinguishable from a broken trigger chain.
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# pynput is absent in some CI lanes. Importing the module still SUCCEEDS
# there — it sets Key = None, so every _k() yields None and the maps come out
# empty. Guard on the module's own availability flag, not on the import, or
# these tests "run" against empty maps and fail for the wrong reason.
try:
    from wayfinder.hotkeys.pynput_listener import (
        PYNPUT_AVAILABLE as _HAVE_PYNPUT,
        EVDEV_TO_PYNPUT,
        PYNPUT_TO_EVDEV,
    )
except Exception:  # pragma: no cover - depends on the environment
    _HAVE_PYNPUT = False
    EVDEV_TO_PYNPUT = PYNPUT_TO_EVDEV = {}

# Mouse buttons are handled by the mouse listener, not the keyboard key map.
MOUSE_BUTTON_CODES = {274, 275, 276, 277, 278}


def _assigned_dict(relative_path: str, name: str) -> ast.Dict:
    """Return the dict literal assigned to ``name`` (bare or ``self.``-scoped)."""
    tree = ast.parse((REPO / relative_path).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        for target in node.targets:
            ident = target.id if isinstance(target, ast.Name) else getattr(target, "attr", None)
            if ident == name:
                return node.value
    raise AssertionError(f"{name} not found in {relative_path}")


def _dropdown_codes() -> dict:
    node = _assigned_dict("wayfinder_main.py", "_hotkey_key_codes")
    return {k.value: v.value for k, v in zip(node.keys, node.values)}


def _mappable_codes() -> set:
    node = _assigned_dict("src/wayfinder/hotkeys/pynput_listener.py", "_raw_evdev_map")
    return {k.value for k in node.keys}


def test_every_dropdown_key_is_mappable_by_the_pynput_listener():
    mappable = _mappable_codes()
    unmatchable = {
        name: code
        for name, code in _dropdown_codes().items()
        if code not in MOUSE_BUTTON_CODES and code not in mappable
    }

    assert not unmatchable, (
        "Settings offers hotkeys the pynput listener has no mapping for, so "
        f"choosing one silently never fires: {unmatchable}"
    )


def test_scroll_lock_and_pause_are_mappable():
    """Regression guard for the two keys that shipped dead."""
    mappable = _mappable_codes()

    assert 70 in mappable, "ScrollLock (70) missing from _raw_evdev_map"
    assert 119 in mappable, "Pause (119) missing from _raw_evdev_map"


@pytest.mark.skipif(not _HAVE_PYNPUT, reason="needs pynput installed")
def test_dropdown_keys_survive_the_runtime_none_filter():
    """Presence in _raw_evdev_map is not enough to make a hotkey work.

    Entries are written as _k("name"); an unknown-to-pynput name yields None
    and is dropped when EVDEV_TO_PYNPUT is built, so a typo like
    _k("scrolllock") passes every source-level check while the hotkey never
    fires — exactly the failure this file exists to prevent.
    """
    unusable = {
        name: code
        for name, code in _dropdown_codes().items()
        if code not in MOUSE_BUTTON_CODES and code not in EVDEV_TO_PYNPUT
    }

    assert not unusable, (
        "dropdown keys resolved to None and were filtered out of the live "
        f"map — the pynput key name is wrong: {unusable}"
    )


@pytest.mark.skipif(not _HAVE_PYNPUT, reason="needs pynput installed")
def test_dropdown_keys_round_trip_for_detect_capture():
    """Settings -> Detect maps pynput key -> evdev code, so a one-way entry
    would bind the hotkey but silently fail to capture it."""
    for name, code in _dropdown_codes().items():
        if code in MOUSE_BUTTON_CODES:
            continue
        key = EVDEV_TO_PYNPUT.get(code)
        assert PYNPUT_TO_EVDEV.get(key) == code, (
            f"{name} ({code}) does not round-trip: Detect cannot capture it"
        )

"""Core Audio rate metadata: no stream or hardware reconfiguration."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wayfinder.utils import macos_audio as audio


@pytest.fixture
def hal(monkeypatch):
    monkeypatch.setattr(audio, "sys", SimpleNamespace(platform="darwin"))
    ca = Mock()

    def read(obj, address, qualifier_size, qualifier, size, output):
        assert address._obj.selector == audio._NOMINAL_RATE
        assert address._obj.scope == audio._SCOPE_GLOBAL
        ctypes.cast(output, ctypes.POINTER(ctypes.c_double)).contents.value = 48000.0
        return 0

    ca.AudioObjectGetPropertyData.side_effect = read
    monkeypatch.setattr(audio, "_load", lambda: (ca, Mock()))
    monkeypatch.setattr(audio, "_get_u32", lambda *args: 11)
    monkeypatch.setattr(audio, "_get_ids", lambda ca, obj, selector, scope=audio._SCOPE_GLOBAL:
                        [11, 12] if selector == audio._DEVICES else ([101] if obj == 11 else []))
    monkeypatch.setattr(audio, "_get_name", lambda ca, cf, obj: "Selected Mic")
    return ca


def test_default_input_rate_is_read_only(hal):
    assert audio.input_sample_rate() == 48000
    hal.AudioObjectSetPropertyData.assert_not_called()


def test_named_input_does_not_match_output_with_same_name(hal):
    assert audio.input_sample_rate("Selected Mic") == 48000
    assert hal.AudioObjectGetPropertyData.call_args.args[0] == 11


def test_missing_selected_input_never_substitutes_default(hal):
    assert audio.input_sample_rate("Missing Mic") is None
    hal.AudioObjectGetPropertyData.assert_not_called()


def test_duplicate_input_names_are_not_guessed(hal, monkeypatch):
    monkeypatch.setattr(audio, "_get_ids", lambda ca, obj, selector, scope=audio._SCOPE_GLOBAL:
                        [11, 12] if selector == audio._DEVICES else [101])
    assert audio.input_sample_rate("Selected Mic") is None
    hal.AudioObjectGetPropertyData.assert_not_called()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, 1000000])
def test_invalid_native_rate_returns_none(hal, value):
    def read(obj, address, qualifier_size, qualifier, size, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_double)).contents.value = value
        return 0
    hal.AudioObjectGetPropertyData.side_effect = read
    assert audio.input_sample_rate() is None


def test_property_error_uses_fallback(hal):
    hal.AudioObjectGetPropertyData.side_effect = lambda *args: -50
    assert audio.input_sample_rate() is None


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_never_load_core_audio(monkeypatch, platform):
    monkeypatch.setattr(audio, "sys", SimpleNamespace(platform=platform))
    load = Mock()
    monkeypatch.setattr(audio, "_load", load)
    assert audio.input_sample_rate() is None
    load.assert_not_called()

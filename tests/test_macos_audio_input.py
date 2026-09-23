"""macOS "Auto" microphone = the input chosen in System Settings, live."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wayfinder_main as wm  # noqa: E402
from wayfinder.utils import macos_audio  # noqa: E402

DEVICES = [
    {"name": "LG TV SSCR2", "max_input_channels": 0},
    {"name": "Wireless Stereo Headset", "max_input_channels": 1},
    {"name": "Wireless Stereo Headset", "max_input_channels": 0},
    {"name": "AirPods Pro", "max_input_channels": 1},
]


class _Warm:
    def __init__(self, device):
        self.device = device
        self.idle_secs = 30.0
        self.in_use = False
        self.rescans = 0
        self.set_to = "unset"

    def rescan(self):
        self.rescans += 1
        return True

    def set_device(self, device, preferred_name=None):
        self.set_to = device
        self.device = device


class _App:
    _MACOS_WARM_MIC_SECS = wm.WayfinderApp._MACOS_WARM_MIC_SECS
    _MACOS_BLUETOOTH_WARM_MIC_SECS = wm.WayfinderApp._MACOS_BLUETOOTH_WARM_MIC_SECS

    def __init__(self, device, config=None):
        self.warm_mic = _Warm(device)
        self.config = config or {}
        self.logs = []

    def log(self, message):
        self.logs.append(message)


@pytest.fixture
def mac(monkeypatch):
    import sounddevice as sd

    monkeypatch.setattr(wm, "IS_MACOS", True)

    def query(index=None, kind=None):
        if index is not None:
            return DEVICES[index]
        if kind == "input":
            return DEVICES[1]
        return DEVICES

    monkeypatch.setattr(sd, "query_devices", query)

    def set_default(name, bluetooth=False):
        monkeypatch.setattr(macos_audio, "default_input_device",
                            lambda: macos_audio.InputDevice(1, name, bluetooth))

    return set_default


def follow(app):
    wm.WayfinderApp._macos_follow_default_input(app)


def test_auto_resolves_to_the_macs_chosen_input(mac):
    mac("AirPods Pro", bluetooth=True)
    assert wm.resolve_audio_device({}) == 3  # not vetoed by a keyword score


def test_switching_input_in_control_center_is_followed(mac):
    mac("AirPods Pro", bluetooth=True)
    app = _App(device=1)
    follow(app)
    assert app.warm_mic.rescans == 1 and app.warm_mic.set_to == 3
    assert app.warm_mic.idle_secs == wm.WayfinderApp._MACOS_BLUETOOTH_WARM_MIC_SECS
    assert "AirPods Pro" in app.logs[-1]


def test_unchanged_input_does_nothing(mac):
    mac("Wireless Stereo Headset")
    app = _App(device=1)
    follow(app)
    assert app.warm_mic.rescans == 0 and app.warm_mic.set_to == "unset"
    assert app.warm_mic.idle_secs == wm.WayfinderApp._MACOS_WARM_MIC_SECS


def test_an_explicitly_chosen_mic_is_kept(mac):
    mac("AirPods Pro", bluetooth=True)
    app = _App(device=1, config={"audio_device_name": "Wireless Stereo Headset"})
    follow(app)
    assert app.warm_mic.rescans == 0


def test_never_yanks_a_mic_that_is_recording(mac):
    mac("AirPods Pro")
    app = _App(device=1)
    app.warm_mic.in_use = True
    follow(app)
    assert app.warm_mic.rescans == 0


def test_no_microphone_at_all_is_harmless(monkeypatch):
    monkeypatch.setattr(wm, "IS_MACOS", True)
    monkeypatch.setattr(macos_audio, "default_input_device", lambda: None)
    app = _App(device=None)
    follow(app)
    assert app.warm_mic.rescans == 0


@pytest.mark.skipif(sys.platform != "darwin", reason="Core Audio")
def test_core_audio_query_live():
    device = macos_audio.default_input_device()
    assert device is None or (device.name and isinstance(device.bluetooth, bool))

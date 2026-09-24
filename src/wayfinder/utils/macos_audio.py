"""Core Audio facts PortAudio cannot give us (macOS).

PortAudio's device table is a snapshot from initialisation, so it never sees
the user change their input in System Settings / Control Center, and it has no
notion of a device's transport. Both are one property read on the HAL:

* ``default_input_device()`` - the input the user chose on this Mac right now
  (name + whether it is Bluetooth), so "Auto" follows the Mac's own setting.
* ``output_volume()`` / ``set_output_volume()`` - the system output volume for
  audio ducking, in well under a millisecond (osascript took ~150 ms per call)
  and with an honest "this output has no volume control" (HDMI, many DACs).

ctypes only; every call is best-effort and returns None on failure.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass


def _fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


_SYSTEM_OBJECT = 1
_DEFAULT_INPUT = _fourcc("dIn ")
_DEFAULT_OUTPUT = _fourcc("dOut")
_VIRTUAL_MAIN_VOLUME = _fourcc("vmvc")
_SCOPE_OUTPUT = _fourcc("outp")
_DEVICE_NAME = _fourcc("lnam")
_TRANSPORT = _fourcc("tran")
_SCOPE_GLOBAL = _fourcc("glob")
_ELEMENT_MAIN = 0
_TRANSPORT_BLUETOOTH = {_fourcc("blue"), _fourcc("blea")}  # classic + LE
_UTF8 = 0x08000100


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


@dataclass(frozen=True)
class InputDevice:
    device_id: int
    name: str
    bluetooth: bool


_libs = None


def _load():
    global _libs
    if _libs is None:
        try:
            ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
            ca.AudioObjectGetPropertyData.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
            ]
            ca.AudioObjectHasProperty.restype = ctypes.c_bool
            ca.AudioObjectHasProperty.argtypes = [ctypes.c_uint32, ctypes.POINTER(_Address)]
            ca.AudioObjectIsPropertySettable.restype = ctypes.c_int32
            ca.AudioObjectIsPropertySettable.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.POINTER(ctypes.c_ubyte),
            ]
            ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
            ca.AudioObjectSetPropertyData.argtypes = [
                ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
                ctypes.c_uint32, ctypes.c_void_p,
            ]
            cf.CFStringGetCString.restype = ctypes.c_bool
            cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            _libs = (ca, cf)
        except Exception:
            _libs = False
    return _libs or None


def _get_u32(ca, obj: int, selector: int) -> int | None:
    value = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(value))
    address = _Address(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)
    status = ca.AudioObjectGetPropertyData(obj, ctypes.byref(address), 0, None,
                                           ctypes.byref(size), ctypes.byref(value))
    return int(value.value) if status == 0 else None


def _get_name(ca, cf, obj: int) -> str | None:
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    address = _Address(_DEVICE_NAME, _SCOPE_GLOBAL, _ELEMENT_MAIN)
    status = ca.AudioObjectGetPropertyData(obj, ctypes.byref(address), 0, None,
                                           ctypes.byref(size), ctypes.byref(ref))
    if status != 0 or not ref.value:
        return None
    try:
        buf = ctypes.create_string_buffer(512)
        if not cf.CFStringGetCString(ref.value, buf, len(buf), _UTF8):
            return None
        return buf.value.decode("utf-8", errors="replace")
    finally:
        cf.CFRelease(ref.value)


def default_input_device() -> InputDevice | None:
    """The Mac's current default input (as chosen in System Settings), or None."""
    if sys.platform != "darwin":
        return None
    libs = _load()
    if libs is None:
        return None
    ca, cf = libs
    try:
        device_id = _get_u32(ca, _SYSTEM_OBJECT, _DEFAULT_INPUT)
        if not device_id:
            return None  # e.g. a Mac Studio with no microphone attached
        name = _get_name(ca, cf, device_id)
        if not name:
            return None
        transport = _get_u32(ca, device_id, _TRANSPORT)
        return InputDevice(device_id, name, transport in _TRANSPORT_BLUETOOTH)
    except Exception:
        return None


def _output_volume_target(device_id: int | None = None):
    """(libs, device id, address) for an output's main volume (default output
    unless ``device_id`` is given), or None when it has no volume control."""
    if sys.platform != "darwin":
        return None
    libs = _load()
    if libs is None:
        return None
    ca, _cf = libs
    if device_id is None:
        device_id = _get_u32(ca, _SYSTEM_OBJECT, _DEFAULT_OUTPUT)
    if not device_id:
        return None
    address = _Address(_VIRTUAL_MAIN_VOLUME, _SCOPE_OUTPUT, _ELEMENT_MAIN)
    if not ca.AudioObjectHasProperty(device_id, ctypes.byref(address)):
        return None
    return ca, device_id, address


def output_volume_settable(device_id: int | None = None) -> bool:
    """False for outputs with no software volume (HDMI TVs, many USB DACs)."""
    try:
        target = _output_volume_target(device_id)
        if target is None:
            return False
        ca, device_id, address = target
        settable = ctypes.c_ubyte(0)
        status = ca.AudioObjectIsPropertySettable(device_id, ctypes.byref(address),
                                                  ctypes.byref(settable))
        return status == 0 and bool(settable.value)
    except Exception:
        return False


def output_volume(device_id: int | None = None) -> float | None:
    """System output volume 0.0-1.0, or None when the output has none."""
    try:
        target = _output_volume_target(device_id)
        if target is None:
            return None
        ca, device_id, address = target
        value = ctypes.c_float(0.0)
        size = ctypes.c_uint32(ctypes.sizeof(value))
        status = ca.AudioObjectGetPropertyData(device_id, ctypes.byref(address), 0, None,
                                               ctypes.byref(size), ctypes.byref(value))
        return float(value.value) if status == 0 else None
    except Exception:
        return None


def set_output_volume(volume: float, device_id: int | None = None) -> bool:
    try:
        target = _output_volume_target(device_id)
        if target is None:
            return False
        ca, device_id, address = target
        value = ctypes.c_float(max(0.0, min(1.0, float(volume))))
        status = ca.AudioObjectSetPropertyData(device_id, ctypes.byref(address), 0, None,
                                               ctypes.sizeof(value), ctypes.byref(value))
        return status == 0
    except Exception:
        return False

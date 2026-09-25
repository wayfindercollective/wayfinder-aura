"""Windows Core Audio (MMDevice API) through ctypes: the ``macos_audio`` counterpart.

* The default output's master volume, for ducking other audio while you
  dictate (``audio_ducker``), exactly like the Mac's virtual main volume.
* The default capture device's name and bus, so "Auto" follows the input the
  user picks in Windows Sound settings (PortAudio's device table is a startup
  snapshot) and Bluetooth headsets can be released early.

COM is reached through raw vtable calls, so nothing extra ships in the app.
Every function returns None/False on failure. Windows-only.
"""

from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import wintypes

_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_RPC_E_CHANGED_MODE = -2147417850
_E_RENDER, _E_CAPTURE, _E_CONSOLE = 0, 1, 0
_STGM_READ = 0
_VT_LPWSTR = 31

_HRESULT = ctypes.c_long


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> "_GUID":
        raw = uuid.UUID(text).bytes_le
        guid = cls()
        ctypes.memmove(ctypes.byref(guid), raw, 16)
        return guid


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("pwszVal", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


_CLSID_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_IID_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_IID_ENDPOINT_VOLUME = "5CDF2C82-841E-4546-9722-0CF74078229A"
_PKEY_DEVICE = "A45C254E-DF1C-4EFD-8020-67D146A850E0"
_PID_FRIENDLY_NAME = 14
_PID_ENUMERATOR_NAME = 24


def _method(ptr, index: int, restype, *argtypes):
    """Bind vtable slot *index* of COM object *ptr*."""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    fn = proto(vtbl[index])
    return lambda *args: fn(ptr, *args)


def _release(ptr) -> None:
    if ptr:
        try:
            _method(ptr, 2, ctypes.c_ulong)()
        except Exception:
            pass


class _Com:
    """CoInitializeEx for this thread (balanced on exit when we started it)."""

    def __enter__(self):
        ole32 = ctypes.windll.ole32
        ole32.CoInitializeEx.restype = _HRESULT
        hr = ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        self._uninit = hr in (0, 1)  # S_OK / S_FALSE: pair with CoUninitialize
        if hr not in (0, 1, _RPC_E_CHANGED_MODE):
            raise OSError(f"CoInitializeEx failed: {hr}")
        return self

    def __exit__(self, *exc):
        if self._uninit:
            ctypes.windll.ole32.CoUninitialize()
        return False


def _default_device(flow: int):
    """IMMDevice* for the default endpoint of *flow* (caller releases), or None."""
    ole32 = ctypes.windll.ole32
    ole32.CoCreateInstance.restype = _HRESULT
    enumerator = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(_GUID.of(_CLSID_ENUMERATOR)), None, _CLSCTX_ALL,
        ctypes.byref(_GUID.of(_IID_ENUMERATOR)), ctypes.byref(enumerator))
    if hr != 0 or not enumerator:
        return None
    try:
        device = ctypes.c_void_p()
        get_default = _method(enumerator, 4, _HRESULT, ctypes.c_int, ctypes.c_int,
                              ctypes.POINTER(ctypes.c_void_p))
        if get_default(flow, _E_CONSOLE, ctypes.byref(device)) != 0 or not device:
            return None
        return device
    finally:
        _release(enumerator)


def _endpoint_volume(device):
    volume = ctypes.c_void_p()
    activate = _method(device, 3, _HRESULT, ctypes.POINTER(_GUID), ctypes.c_uint32,
                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    if activate(ctypes.byref(_GUID.of(_IID_ENDPOINT_VOLUME)), _CLSCTX_ALL, None,
                ctypes.byref(volume)) != 0:
        return None
    return volume or None


def output_volume() -> float | None:
    """Default output's master volume, 0.0-1.0."""
    if sys.platform != "win32":
        return None
    try:
        with _Com():
            device = _default_device(_E_RENDER)
            if device is None:
                return None
            try:
                volume = _endpoint_volume(device)
                if volume is None:
                    return None
                try:
                    level = ctypes.c_float()
                    if _method(volume, 9, _HRESULT, ctypes.POINTER(ctypes.c_float))(
                            ctypes.byref(level)) != 0:
                        return None
                    return float(level.value)
                finally:
                    _release(volume)
            finally:
                _release(device)
    except Exception:
        return None


def set_output_volume(level: float) -> bool:
    """Set the default output's master volume (0.0-1.0). True on success."""
    if sys.platform != "win32":
        return False
    try:
        with _Com():
            device = _default_device(_E_RENDER)
            if device is None:
                return False
            try:
                volume = _endpoint_volume(device)
                if volume is None:
                    return False
                try:
                    setter = _method(volume, 7, _HRESULT, ctypes.c_float, ctypes.c_void_p)
                    return setter(max(0.0, min(1.0, float(level))), None) == 0
                finally:
                    _release(volume)
            finally:
                _release(device)
    except Exception:
        return False


def output_volume_settable() -> bool:
    """Whether the default output exposes a volume (HDMI sinks usually do on Windows)."""
    return output_volume() is not None


def _device_string(device, pid: int) -> str | None:
    store = ctypes.c_void_p()
    open_store = _method(device, 4, _HRESULT, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))
    if open_store(_STGM_READ, ctypes.byref(store)) != 0 or not store:
        return None
    try:
        key = _PROPERTYKEY(_GUID.of(_PKEY_DEVICE), pid)
        value = _PROPVARIANT()
        get_value = _method(store, 5, _HRESULT, ctypes.POINTER(_PROPERTYKEY),
                            ctypes.POINTER(_PROPVARIANT))
        if get_value(ctypes.byref(key), ctypes.byref(value)) != 0:
            return None
        try:
            if value.vt == _VT_LPWSTR and value.pwszVal:
                return ctypes.wstring_at(value.pwszVal)
            return None
        finally:
            ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
    finally:
        _release(store)


def default_input_device() -> dict | None:
    """{"name": friendly name, "transport": "bluetooth"|"usb"|"builtin"} for the default mic."""
    if sys.platform != "win32":
        return None
    try:
        with _Com():
            device = _default_device(_E_CAPTURE)
            if device is None:
                return None
            try:
                name = _device_string(device, _PID_FRIENDLY_NAME)
                bus = (_device_string(device, _PID_ENUMERATOR_NAME) or "").upper()
            finally:
                _release(device)
    except Exception:
        return None
    if not name:
        return None
    if bus.startswith("BTH"):
        transport = "bluetooth"
    elif bus == "USB":
        transport = "usb"
    else:
        transport = "builtin"
    return {"name": name, "transport": transport}


# PortAudio (MME) truncates device names to 31 characters.
_MME_NAME_LIMIT = 31


def names_match(portaudio_name: str, endpoint_name: str) -> bool:
    """Whether a PortAudio device name is this Windows endpoint's friendly name."""
    a, b = (portaudio_name or "").strip(), (endpoint_name or "").strip()
    if not a or not b:
        return False
    return a == b or (len(a) >= _MME_NAME_LIMIT - 1 and b.startswith(a))


__all__ = [
    "default_input_device",
    "names_match",
    "output_volume",
    "output_volume_settable",
    "set_output_volume",
]

_ = wintypes  # imported for callers that extend this module

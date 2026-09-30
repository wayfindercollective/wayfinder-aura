"""CPU, GPU and RAM for the Benchmark card on Windows (the Mac reads sysctl).

Registry and kernel32 only - no WMI/PowerShell round trip, so it is instant.
Windows-only: returns {} elsewhere.
"""

from __future__ import annotations

import ctypes
import os
import sys

_CPU_KEY = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
# Display adapters device class.
_GPU_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def _cpu() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU_KEY) as key:
            name = " ".join(str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).split())
    except OSError:
        return None
    cores = os.cpu_count()
    return f"{name} ({cores} threads)" if cores else name


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32), ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128), ("StateFlags", ctypes.c_uint32),
        ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128),
    ]


def _present_gpus() -> list[str]:
    """Physical display adapters that are present now (EnumDisplayDevices).

    The registry's adapter class key also keeps the drivers of removed cards (a
    GTX 1080 Ti long gone was reported as this PC's GPU), and virtual display
    drivers enumerate with a Root\\ device id, so only PCI devices count.
    """
    names: list[str] = []
    index = 0
    while True:
        device = _DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(device)
        if not ctypes.windll.user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        index += 1
        name = device.DeviceString.strip()
        if name and device.DeviceID.upper().startswith("PCI\\") and name not in names:
            names.append(name)
    return names


def _gpus() -> list[str]:
    try:
        present = _present_gpus()
    except Exception:
        present = []
    return present or _registry_gpus()


def _registry_gpus() -> list[str]:
    import winreg

    names: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _GPU_CLASS_KEY) as cls:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(cls, index)
                except OSError:
                    break
                index += 1
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, sub) as dev:
                        desc = str(winreg.QueryValueEx(dev, "DriverDesc")[0]).strip()
                except OSError:
                    continue
                # Skip remote-desktop / virtual display adapters.
                if desc and "basic display" not in desc.lower() and "remote" not in desc.lower() \
                        and desc not in names:
                    names.append(desc)
    except OSError:
        pass
    return names


def _ram() -> str | None:
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return f"{status.ullTotalPhys / (1024 ** 3):.0f} GB"


def gpu_vendor(name: str) -> str:
    """The vendor (nvidia, amd, intel or unknown) from an adapter name."""
    lowered = name.lower()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
        return "nvidia"
    if "amd" in lowered or "radeon" in lowered:
        return "amd"
    if "intel" in lowered:
        return "intel"
    return "unknown"


def pick_primary_gpu(names: list[str]) -> tuple[str, str] | None:
    """(vendor, name) worth reporting first: NVIDIA, then a discrete Radeon (RX /
    Pro), then Intel Arc, then any other adapter; None if there is none."""
    def rank(name: str) -> int:
        lowered, vendor = name.lower(), gpu_vendor(name)
        if vendor == "nvidia":
            return 0
        if vendor == "amd" and (" rx " in f" {lowered} " or " pro " in f" {lowered} "):
            return 1
        if vendor == "intel" and " arc" in f" {lowered}":
            return 2
        return 3 if vendor != "unknown" else 4

    ordered = sorted(names, key=rank)
    return (gpu_vendor(ordered[0]), ordered[0]) if ordered else None


def primary_gpu() -> tuple[str, str] | None:
    """The display adapter Windows reports that is most worth using, or None."""
    if sys.platform != "win32":
        return None
    try:
        return pick_primary_gpu(_gpus())
    except Exception:
        return None


def system_info() -> dict:
    """{"cpu", "gpu", "ram"} strings for what Windows reports (keys may be missing)."""
    if sys.platform != "win32":
        return {}
    info: dict = {}
    try:
        cpu = _cpu()
        if cpu:
            info["cpu"] = cpu
    except Exception:
        pass
    try:
        gpus = _gpus()
        if gpus:
            info["gpu"] = " · ".join(gpus)
    except Exception:
        pass
    try:
        ram = _ram()
        if ram:
            info["ram"] = ram
    except Exception:
        pass
    return info

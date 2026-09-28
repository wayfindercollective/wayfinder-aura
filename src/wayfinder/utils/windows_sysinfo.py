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


def _gpus() -> list[str]:
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

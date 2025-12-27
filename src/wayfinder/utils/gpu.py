"""
GPU detection utilities for Wayfinder Voice.

Detects GPU vendor, capabilities, and optimal settings.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class GPUInfo:
    """Detected GPU information."""
    vendor: str  # "nvidia", "amd", "intel", "unknown"
    name: str
    driver: str = ""
    
    @property
    def is_nvidia(self) -> bool:
        return self.vendor == "nvidia"
    
    @property
    def is_amd(self) -> bool:
        return self.vendor == "amd"
    
    @property
    def is_intel(self) -> bool:
        return self.vendor == "intel"
    
    @property
    def is_known(self) -> bool:
        return self.vendor != "unknown"


def detect_gpu() -> GPUInfo:
    """
    Detect the primary GPU on the system.
    
    Returns:
        GPUInfo with vendor, name, and driver information.
    """
    try:
        # Try lspci first (most reliable on Linux)
        result = subprocess.run(
            ["lspci", "-nn"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        
        if result.returncode == 0:
            # Check for discrete GPUs first (VGA/3D controllers)
            for line in result.stdout.split("\n"):
                line_lower = line.lower()
                if "vga" in line_lower or "3d" in line_lower or "display" in line_lower:
                    if "nvidia" in line_lower:
                        return GPUInfo("nvidia", line.split(":")[-1].strip(), "nvidia")
                    elif "amd" in line_lower or "radeon" in line_lower or "advanced micro" in line_lower:
                        return GPUInfo("amd", line.split(":")[-1].strip(), "amdgpu")
                    elif "intel" in line_lower:
                        return GPUInfo("intel", line.split(":")[-1].strip(), "i915")
        
        # Fallback: Check /sys for GPU info
        gpu_path = Path("/sys/class/drm")
        if gpu_path.exists():
            for card in gpu_path.iterdir():
                if card.name.startswith("card") and not card.name.endswith("-"):
                    vendor_file = card / "device" / "vendor"
                    if vendor_file.exists():
                        vendor_id = vendor_file.read_text().strip()
                        if vendor_id == "0x10de":  # NVIDIA
                            return GPUInfo("nvidia", "NVIDIA GPU", "nvidia")
                        elif vendor_id == "0x1002":  # AMD
                            return GPUInfo("amd", "AMD GPU", "amdgpu")
                        elif vendor_id == "0x8086":  # Intel
                            return GPUInfo("intel", "Intel GPU", "i915")
        
    except Exception:
        pass
    
    return GPUInfo("unknown", "Unknown GPU", "")


# Cache GPU info at module load
_cached_gpu_info: Optional[GPUInfo] = None


def get_gpu_info() -> GPUInfo:
    """Get cached GPU info (detected once at startup)."""
    global _cached_gpu_info
    if _cached_gpu_info is None:
        _cached_gpu_info = detect_gpu()
    return _cached_gpu_info


def get_optimal_thread_count() -> int:
    """
    Get optimal thread count based on CPU cores.
    
    Returns:
        Recommended thread count for whisper.cpp (75% of cores, 2-16 range).
    """
    try:
        cpu_count = os.cpu_count() or 4
        # Use ~75% of cores, minimum 2, maximum 16
        return max(2, min(16, int(cpu_count * 0.75)))
    except Exception:
        return 4


def get_system_info() -> dict[str, str]:
    """
    Detect system hardware: CPU, GPU, RAM.
    
    Returns:
        Dict with "cpu", "gpu", and "ram" keys.
    """
    info = {"cpu": "Unknown", "gpu": "Unknown", "ram": "Unknown"}
    
    # CPU detection
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    info["cpu"] = line.split(":")[1].strip()
                    break
    except Exception:
        pass
    
    # GPU detection
    gpu_info = get_gpu_info()
    if gpu_info.is_known:
        info["gpu"] = gpu_info.name
    
    # RAM detection
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem_kb = int(line.split()[1])
                    mem_gb = mem_kb / 1024 / 1024
                    info["ram"] = f"{mem_gb:.0f} GB"
                    break
    except Exception:
        pass
    
    return info




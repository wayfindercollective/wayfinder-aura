"""
GPU detection utilities for Wayfinder Aura.

Detects GPU vendor, capabilities, and optimal settings.
Handles Vulkan device selection for systems with multiple GPUs (iGPU + dGPU).
"""

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List


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


@dataclass
class VulkanDevice:
    """Information about a Vulkan-capable device."""
    index: int
    name: str
    device_type: str  # "discrete", "integrated", "cpu", "virtual", "other"
    vendor: str  # "nvidia", "amd", "intel", "other"
    device_id: str = ""
    
    @property
    def is_discrete(self) -> bool:
        return self.device_type == "discrete"
    
    @property
    def is_integrated(self) -> bool:
        return self.device_type == "integrated"
    
    @property
    def is_cpu(self) -> bool:
        return self.device_type == "cpu"
    
    def __str__(self) -> str:
        return f"[{self.index}] {self.name} ({self.device_type}, {self.vendor})"


@dataclass  
class VulkanInfo:
    """Complete Vulkan system information."""
    devices: List[VulkanDevice] = field(default_factory=list)
    recommended_device: int = 0
    has_discrete_gpu: bool = False
    has_multiple_gpus: bool = False
    detection_method: str = "none"
    error: str = ""
    
    def get_device(self, index: int) -> Optional[VulkanDevice]:
        """Get device by index."""
        for d in self.devices:
            if d.index == index:
                return d
        return None
    
    def get_discrete_devices(self) -> List[VulkanDevice]:
        """Get all discrete GPUs."""
        return [d for d in self.devices if d.is_discrete]
    
    def get_recommended_device(self) -> Optional[VulkanDevice]:
        """Get the recommended device."""
        return self.get_device(self.recommended_device)


def detect_vulkan_devices() -> VulkanInfo:
    """
    Detect all Vulkan-capable devices on the system.
    
    This is critical for systems with both integrated and discrete GPUs,
    where the default device may not be the fastest one.
    
    Returns:
        VulkanInfo with all devices and recommended selection.
    """
    info = VulkanInfo()
    
    # Method 1: Try vulkaninfo --summary (most reliable, structured output)
    try:
        result = subprocess.run(
            ["vulkaninfo", "--summary"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "VK_LOADER_DEBUG": ""},  # Suppress debug spam
        )
        
        if result.returncode == 0:
            info.detection_method = "vulkaninfo"
            devices = []
            current_device = None
            
            for line in result.stdout.split("\n"):
                # Look for GPU section headers (e.g., "GPU0:", "GPU1:")
                if line.startswith("GPU") and ":" in line:
                    # Save previous device if exists
                    if current_device:
                        devices.append(current_device)
                    
                    # Start new device
                    try:
                        idx = int(line.split(":")[0].replace("GPU", ""))
                    except ValueError:
                        idx = len(devices)
                    
                    current_device = VulkanDevice(
                        index=idx,
                        name="Unknown",
                        device_type="unknown",
                        vendor="other",
                    )
                    continue
                
                # Parse device properties (indented lines within GPU section)
                if current_device and "=" in line:
                    line = line.strip()
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    
                    if key == "deviceName":
                        current_device.name = value
                        # Determine vendor from name
                        name_lower = value.lower()
                        if "nvidia" in name_lower or "geforce" in name_lower or "rtx" in name_lower:
                            current_device.vendor = "nvidia"
                        elif "amd" in name_lower or "radeon" in name_lower or "rx " in name_lower:
                            current_device.vendor = "amd"
                        elif "intel" in name_lower:
                            current_device.vendor = "intel"
                        elif "llvmpipe" in name_lower or "swiftshader" in name_lower:
                            current_device.vendor = "software"
                    
                    elif key == "deviceType":
                        value_upper = value.upper()
                        if "DISCRETE" in value_upper:
                            current_device.device_type = "discrete"
                        elif "INTEGRATED" in value_upper:
                            current_device.device_type = "integrated"
                        elif "CPU" in value_upper:
                            current_device.device_type = "cpu"
                        elif "VIRTUAL" in value_upper:
                            current_device.device_type = "virtual"
                        else:
                            current_device.device_type = "other"
                    
                    elif key == "deviceID":
                        current_device.device_id = value
            
            # Don't forget the last device
            if current_device:
                devices.append(current_device)
            
            info.devices = devices
            
    except FileNotFoundError:
        info.error = "vulkaninfo not found"
    except subprocess.TimeoutExpired:
        info.error = "vulkaninfo timed out"
    except Exception as e:
        info.error = str(e)
    
    # Method 2: Fallback to /sys/class/drm parsing
    if not info.devices:
        info.detection_method = "sysfs"
        try:
            drm_path = Path("/sys/class/drm")
            if drm_path.exists():
                idx = 0
                for card in sorted(drm_path.iterdir()):
                    if card.name.startswith("card") and card.name[4:].isdigit():
                        vendor_file = card / "device" / "vendor"
                        device_file = card / "device" / "device"
                        
                        if vendor_file.exists():
                            vendor_id = vendor_file.read_text().strip()
                            device_id = device_file.read_text().strip() if device_file.exists() else ""
                            
                            # Map vendor IDs
                            vendor_map = {
                                "0x10de": "nvidia",
                                "0x1002": "amd", 
                                "0x8086": "intel",
                            }
                            vendor = vendor_map.get(vendor_id, "other")
                            
                            # Try to get device name from uevent
                            name = f"{vendor.upper()} GPU ({device_id})"
                            uevent_file = card / "device" / "uevent"
                            if uevent_file.exists():
                                uevent = uevent_file.read_text()
                                for line in uevent.split("\n"):
                                    if "PCI_SLOT_NAME" in line:
                                        name = f"{vendor.upper()} GPU @ {line.split('=')[1]}"
                            
                            # Guess device type based on common patterns
                            # Integrated GPUs typically have lower device IDs
                            device_type = "discrete"  # Default assumption
                            if vendor == "intel":
                                device_type = "integrated"  # Intel GPUs are usually integrated
                            elif vendor == "amd":
                                # AMD integrated GPUs often have specific device ID patterns
                                try:
                                    dev_num = int(device_id, 16)
                                    # Integrated AMD GPUs (APU graphics) are typically in certain ranges
                                    if dev_num >= 0x1300 and dev_num <= 0x17ff:
                                        device_type = "integrated"
                                except:
                                    pass
                            
                            info.devices.append(VulkanDevice(
                                index=idx,
                                name=name,
                                device_type=device_type,
                                vendor=vendor,
                                device_id=device_id,
                            ))
                            idx += 1
                            
        except Exception as e:
            if not info.error:
                info.error = f"sysfs detection failed: {e}"
    
    # Analyze results and pick recommended device
    if info.devices:
        discrete_gpus = info.get_discrete_devices()
        info.has_discrete_gpu = len(discrete_gpus) > 0
        info.has_multiple_gpus = len(info.devices) > 1
        
        # Recommendation logic:
        # 1. Prefer discrete GPU over integrated
        # 2. Prefer NVIDIA/AMD over Intel for compute
        # 3. Never recommend CPU/software renderers
        
        if discrete_gpus:
            # Pick the first discrete GPU (usually the primary one)
            info.recommended_device = discrete_gpus[0].index
        else:
            # No discrete GPU, pick the first non-CPU device
            for d in info.devices:
                if not d.is_cpu and d.vendor != "software":
                    info.recommended_device = d.index
                    break
    
    return info


@dataclass
class GgmlDevice:
    """Information about a device as seen by ggml/whisper.cpp."""
    index: int
    name: str
    is_uma: bool  # Unified Memory Architecture (integrated GPU)
    has_coopmat: bool  # Has matrix cores (fast for ML)
    
    @property
    def is_discrete(self) -> bool:
        """Discrete GPUs have uma=0 and usually have coopmat."""
        return not self.is_uma


def detect_ggml_devices() -> List[GgmlDevice]:
    """
    Detect GPU devices as seen by ggml/whisper.cpp.
    
    IMPORTANT: ggml's device ordering may differ from vulkaninfo!
    This runs a quick whisper-cli probe to get the actual ggml ordering.
    
    Returns:
        List of GgmlDevice in ggml's ordering.
    """
    devices = []
    
    # Try to find whisper-cli
    whisper_paths = [
        Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
        Path("/usr/bin/whisper-cli"),
        Path("/app/bin/whisper-cli"),
    ]
    
    whisper_cli = None
    for path in whisper_paths:
        if path.exists():
            whisper_cli = str(path)
            break
    
    if not whisper_cli:
        return devices
    
    try:
        # Run with debug to get device list (will fail without model, but that's ok)
        result = subprocess.run(
            [whisper_cli, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "GGML_VK_DEBUG": "1"},
        )
        
        # The device list appears in stderr
        output = result.stderr + result.stdout
        
        for line in output.split("\n"):
            if "ggml_vulkan:" in line and "=" in line:
                # Parse: "ggml_vulkan: 0 = Name (driver) | uma: 1 | ... | matrix cores: none"
                try:
                    parts = line.split("=", 1)
                    idx = int(parts[0].split(":")[-1].strip())
                    rest = parts[1]
                    
                    # Extract name (everything before first |)
                    name = rest.split("|")[0].strip()
                    
                    # Check for uma and coopmat
                    is_uma = "uma: 1" in rest
                    has_coopmat = "coopmat" in rest.lower() and "none" not in rest.lower().split("matrix cores")[-1][:20]
                    
                    devices.append(GgmlDevice(
                        index=idx,
                        name=name,
                        is_uma=is_uma,
                        has_coopmat=has_coopmat,
                    ))
                except (ValueError, IndexError):
                    continue
                    
    except Exception:
        pass
    
    return devices


def get_optimal_ggml_device() -> Optional[int]:
    """
    Get the optimal device index for ggml/whisper.cpp.
    
    This detects devices using ggml's own ordering (which may differ from vulkaninfo)
    and returns the index of the best device (discrete GPU with matrix cores).
    
    Returns:
        Optimal device index, or None if detection fails or no preference needed.
    """
    devices = detect_ggml_devices()
    
    if not devices:
        return None
    
    # If only one device, no preference needed
    if len(devices) == 1:
        return None
    
    # Find the best device:
    # 1. Prefer discrete (uma=0) over integrated (uma=1)
    # 2. Prefer devices with matrix cores (coopmat)
    
    discrete_with_coopmat = [d for d in devices if d.is_discrete and d.has_coopmat]
    discrete_only = [d for d in devices if d.is_discrete]
    
    if discrete_with_coopmat:
        return discrete_with_coopmat[0].index
    elif discrete_only:
        return discrete_only[0].index
    
    # No discrete GPU found, return None (use default)
    return None


def get_optimal_vulkan_device() -> int:
    """
    Get the optimal Vulkan device index for ggml/whisper.cpp.
    
    This handles the common issue where systems with both integrated
    and discrete GPUs may default to the slower integrated GPU.
    
    IMPORTANT: ggml_vulkan often orders devices differently than vulkaninfo!
    Common pattern: ggml puts integrated (UMA) GPUs first, discrete GPUs later.
    
    Uses ggml-specific detection when possible, falls back to heuristics.
    
    Returns:
        Recommended device index for GGML_VK_VISIBLE_DEVICES
    """
    # Try ggml-specific detection first (most accurate)
    ggml_device = get_optimal_ggml_device()
    if ggml_device is not None:
        return ggml_device
    
    # Fall back to vulkaninfo + heuristics
    info = detect_vulkan_devices()
    
    # If there's only one device or no discrete GPU, use default
    if not info.has_multiple_gpus or not info.has_discrete_gpu:
        return 0
    
    # Heuristic: ggml often orders devices differently than vulkaninfo
    # ggml typically puts integrated/UMA GPUs (device_type=integrated) first
    # If vulkaninfo shows discrete at index 0 but integrated exists,
    # ggml probably has them reversed
    
    discrete_devices = [d for d in info.devices if d.is_discrete]
    integrated_devices = [d for d in info.devices if d.is_integrated]
    
    if discrete_devices and integrated_devices:
        # Mixed system (iGPU + dGPU)
        # If vulkaninfo has discrete first, ggml probably has integrated first
        # So the discrete GPU is likely at a higher index in ggml
        
        vulkan_first_is_discrete = info.devices[0].is_discrete if info.devices else False
        
        if vulkan_first_is_discrete:
            # ggml probably reversed: discrete GPU is likely at index = number of integrated GPUs
            # This is a heuristic that works for common 2-GPU setups
            return len(integrated_devices)
        else:
            # vulkaninfo has integrated first, ggml ordering likely matches
            # Find discrete GPU index
            for d in info.devices:
                if d.is_discrete:
                    return d.index
    
    # Default: use vulkaninfo's recommendation
    return info.recommended_device


def get_vulkan_env_vars() -> dict[str, str]:
    """
    Get environment variables to set for optimal Vulkan device selection.
    
    Returns:
        Dict of environment variables to set (can be empty if not needed).
    """
    optimal = get_optimal_vulkan_device()
    env = {}
    
    # Only set if we need a non-default device
    if optimal and optimal != 0:
        env["GGML_VK_VISIBLE_DEVICES"] = str(optimal)
    
    return env


def get_gpu_diagnostics() -> dict:
    """
    Get comprehensive GPU diagnostics for troubleshooting.
    
    Returns:
        Dict with detailed GPU/Vulkan/ggml information.
    """
    gpu_info = detect_gpu()
    vulkan_info = detect_vulkan_devices()
    ggml_devices = detect_ggml_devices()
    optimal_ggml = get_optimal_ggml_device()
    
    return {
        "primary_gpu": {
            "vendor": gpu_info.vendor,
            "name": gpu_info.name,
            "driver": gpu_info.driver,
        },
        "vulkan": {
            "detection_method": vulkan_info.detection_method,
            "device_count": len(vulkan_info.devices),
            "has_discrete_gpu": vulkan_info.has_discrete_gpu,
            "has_multiple_gpus": vulkan_info.has_multiple_gpus,
            "vulkaninfo_recommended": vulkan_info.recommended_device,
            "devices": [
                {
                    "index": d.index,
                    "name": d.name,
                    "type": d.device_type,
                    "vendor": d.vendor,
                    "device_id": d.device_id,
                }
                for d in vulkan_info.devices
            ],
            "error": vulkan_info.error or None,
        },
        "ggml": {
            "device_count": len(ggml_devices),
            "optimal_device": optimal_ggml,
            "devices": [
                {
                    "index": d.index,
                    "name": d.name,
                    "is_uma": d.is_uma,
                    "is_discrete": d.is_discrete,
                    "has_coopmat": d.has_coopmat,
                    "is_recommended": d.index == optimal_ggml,
                }
                for d in ggml_devices
            ],
            "note": "ggml device ordering may differ from vulkaninfo!",
        },
        "recommendation": {
            "device_index": get_optimal_vulkan_device(),
            "env_vars": get_vulkan_env_vars(),
        },
    }


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





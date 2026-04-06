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
    
    # Find the smallest model available for quick probing
    model_dirs = [
        Path.home() / "whisper.cpp" / "models",
        Path.home() / ".local" / "share" / "whisper.cpp",
        Path("/app/share/whisper-models"),
    ]
    # Prefer smallest models for faster startup
    model_patterns = [
        "ggml-tiny.en.bin", "ggml-tiny.bin",
        "ggml-base.en.bin", "ggml-base.bin",
        "ggml-small.en.bin", "ggml-small.bin",
    ]
    
    model_path = None
    for model_dir in model_dirs:
        if not model_dir.exists():
            continue
        for pattern in model_patterns:
            path = model_dir / pattern
            if path.exists():
                model_path = str(path)
                break
        if model_path:
            break
    
    # Create a minimal silence WAV file for probing (avoids needing external files)
    import tempfile
    import wave
    import struct
    
    try:
        # Create 0.1 second of silence (minimal audio)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            temp_audio = f.name
            with wave.open(f.name, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                # 0.1 second of silence (1600 samples)
                wav.writeframes(struct.pack("<" + "h" * 1600, *([0] * 1600)))
        
        if model_path is None:
            # No model available, can't probe ggml devices
            return devices

        # Run whisper with the probe audio - GPU detection happens during init
        # We use --no-prints to minimize output but GPU info still appears
        cmd = [whisper_cli, "-m", model_path, "-f", temp_audio, "--no-timestamps"]
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,  # Should be fast with tiny model + minimal audio
        )
        
        # The device list appears in stdout during model init
        output = result.stdout + result.stderr
        
        for line in output.split("\n"):
            # Look for ggml_vulkan device lines
            # Format: "ggml_vulkan: 0 = Name (driver) | uma: 1 | fp16: 1 | ... | matrix cores: none"
            if "ggml_vulkan:" in line and "=" in line and "|" in line:
                try:
                    parts = line.split("=", 1)
                    idx_part = parts[0].split(":")[-1].strip()
                    # Handle "Found 2 Vulkan devices:" line
                    if "Found" in idx_part or not idx_part.isdigit():
                        continue
                    idx = int(idx_part)
                    rest = parts[1]
                    
                    # Extract name (everything before first |)
                    name = rest.split("|")[0].strip()
                    
                    # Check for uma (integrated GPU) and coopmat (matrix cores)
                    is_uma = "uma: 1" in rest
                    # Check for matrix core support (coopmat = cooperative matrix)
                    has_coopmat = ("coopmat" in rest.lower() or "KHR_coopmat" in rest) and \
                                  "none" not in rest.lower().split("matrix cores")[-1][:20] if "matrix cores" in rest else False
                    
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
    finally:
        # Clean up temp file
        try:
            os.unlink(temp_audio)
        except:
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


def benchmark_gpu_devices(
    whisper_cli: Optional[str] = None,
    model_path: Optional[str] = None,
    timeout: int = 30,
) -> dict[str, float]:
    """
    Benchmark all available GPU devices by running a quick transcription test.
    
    This is the most reliable way to detect the fastest GPU - actually test it!
    
    Args:
        whisper_cli: Path to whisper-cli binary (auto-detected if None)
        model_path: Path to model file (uses tiny.en if None)
        timeout: Max seconds per device test
        
    Returns:
        Dict mapping device index to time in seconds, plus "fastest" key.
        Example: {"0": 0.6, "1": 7.5, "2": 52.0, "fastest": "0"}
    """
    import tempfile
    import time
    import wave
    
    results: dict[str, float] = {}
    
    # Find whisper-cli
    if not whisper_cli:
        whisper_paths = [
            Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
            Path("/usr/bin/whisper-cli"),
            Path("/app/bin/whisper-cli"),
        ]
        for path in whisper_paths:
            if path.exists():
                whisper_cli = str(path)
                break
    
    if not whisper_cli or not Path(whisper_cli).exists():
        return {"error": "whisper-cli not found", "fastest": "0"}
    
    # Find a model to test with (prefer tiny for speed)
    if not model_path:
        model_dirs = [
            Path.home() / "whisper.cpp" / "models",
            Path.home() / ".local" / "share" / "whisper.cpp",
            Path("/app/share/whisper-models"),
        ]
        model_patterns = ["ggml-tiny.en.bin", "ggml-tiny.bin", "ggml-base.en.bin", "ggml-small.en.bin"]
        
        for model_dir in model_dirs:
            if not model_dir.exists():
                continue
            for pattern in model_patterns:
                path = model_dir / pattern
                if path.exists():
                    model_path = str(path)
                    break
            if model_path:
                break
    
    if not model_path or not Path(model_path).exists():
        return {"error": "no model found", "fastest": "0"}
    
    # IMPORTANT: Use ggml's device ordering, NOT vulkaninfo's!
    # ggml may order devices differently than vulkaninfo, so we must
    # detect devices the way ggml sees them to get correct benchmark results.
    ggml_devices = detect_ggml_devices()
    
    # Fallback to vulkaninfo if ggml detection fails
    if not ggml_devices:
        vulkan_info = detect_vulkan_devices()
        if not vulkan_info.devices:
            return {"error": "no vulkan devices", "fastest": "0"}
        # Convert vulkan devices to simple list of indices (might be wrong order!)
        device_indices = [d.index for d in vulkan_info.devices 
                         if not d.is_cpu and d.vendor != "software"]
    else:
        # Use ggml's device ordering - prefer discrete GPUs (uma=0)
        device_indices = [d.index for d in ggml_devices]
    
    if not device_indices:
        return {"error": "no GPU devices found", "fastest": "0"}
    
    # Create a short test audio file (3 seconds)
    try:
        import struct
        sample_rate = 16000
        duration = 3
        samples = int(duration * sample_rate)
        
        # Generate simple test tones without numpy
        import math
        audio_data = []
        for i in range(samples):
            t = i / sample_rate
            # Simple sine wave mix
            sample = int(32767 * 0.3 * (
                math.sin(2 * math.pi * 200 * t) +
                0.7 * math.sin(2 * math.pi * 400 * t)
            ))
            sample = max(-32767, min(32767, sample))
            audio_data.append(sample)
        
        temp_audio = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        with wave.open(temp_audio.name, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(struct.pack("<" + "h" * len(audio_data), *audio_data))
    except Exception as e:
        return {"error": f"audio creation failed: {e}", "fastest": "0"}
    
    try:
        # Test each GPU device using GGML's device indices
        for device_idx in device_indices:
            device_idx_str = str(device_idx)
            env = os.environ.copy()
            env["GGML_VK_VISIBLE_DEVICES"] = device_idx_str
            
            cmd = [
                whisper_cli,
                "-m", model_path,
                "-f", temp_audio.name,
                "-t", "4",
                "--no-timestamps",
                "--no-prints",
            ]
            
            try:
                start = time.perf_counter()
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                    env=env,
                )
                elapsed = time.perf_counter() - start
                
                if result.returncode == 0:
                    results[device_idx_str] = round(elapsed, 3)
            except subprocess.TimeoutExpired:
                results[device_idx_str] = float(timeout)  # Timed out = slow
            except Exception:
                pass  # Skip failed devices
        
        # Find fastest
        if results:
            fastest = min(results, key=results.get)
            results["fastest"] = fastest
        else:
            results["fastest"] = "0"  # Default fallback
            
    finally:
        # Cleanup temp file
        try:
            os.unlink(temp_audio.name)
        except:
            pass
    
    return results


def get_optimal_vulkan_device(config: Optional[dict] = None) -> int:
    """
    Get the optimal Vulkan device index for ggml/whisper.cpp.
    
    Priority order:
    1. Manual override from config (gpu_device setting)
    2. Cached benchmark result (gpu_benchmark_cache)
    3. Run new benchmark if needed
    4. Fallback to vulkaninfo-based detection
    
    Args:
        config: Optional config dict. If provided, checks for manual override
                and uses/updates benchmark cache.
    
    Returns:
        Recommended device index for GGML_VK_VISIBLE_DEVICES
    """
    # 1. Check for manual override
    if config:
        gpu_device = config.get("gpu_device", "auto")
        if gpu_device != "auto":
            try:
                return int(gpu_device)
            except (ValueError, TypeError):
                pass  # Invalid value, continue with auto
        
        # 2. Check cached benchmark results
        cache = config.get("gpu_benchmark_cache", {})
        if cache and "fastest" in cache:
            try:
                return int(cache["fastest"])
            except (ValueError, TypeError):
                pass  # Invalid cache, continue
    
    # 3. Try ggml-specific detection (if it works)
    ggml_device = get_optimal_ggml_device()
    if ggml_device is not None:
        return ggml_device
    
    # 4. Fall back to vulkaninfo detection
    # WARNING: ggml's device ordering often differs from vulkaninfo on AMD iGPU+dGPU systems!
    # If ggml detection failed, we can't reliably trust vulkaninfo ordering.
    info = detect_vulkan_devices()
    
    # If there's only one device or no discrete GPU, use default
    if not info.has_multiple_gpus or not info.has_discrete_gpu:
        return 0
    
    # Check if this is an AMD iGPU+dGPU system where ordering might differ
    has_amd_igpu = any(d.is_integrated and d.vendor == "amd" for d in info.devices)
    has_amd_dgpu = any(d.is_discrete and d.vendor == "amd" for d in info.devices)
    
    if has_amd_igpu and has_amd_dgpu:
        # AMD iGPU+dGPU system - ggml often sees integrated as device 0
        # Run a quick benchmark to determine the correct device
        print("[GPU] AMD iGPU+dGPU detected, running quick benchmark to find fastest device...")
        benchmark = benchmark_gpu_devices()
        if "fastest" in benchmark and benchmark["fastest"] != "error":
            fastest = benchmark["fastest"]
            print(f"[GPU] Benchmark complete: device {fastest} is fastest")
            # Cache this result for future use
            if config is not None:
                config["gpu_benchmark_cache"] = benchmark
            return int(fastest)
    
    # Find the first discrete GPU in vulkaninfo (might be wrong for ggml)
    for d in info.devices:
        if d.is_discrete:
            return d.index
    
    # Default
    return info.recommended_device


def get_vulkan_env_vars(config: Optional[dict] = None) -> dict[str, str]:
    """
    Get environment variables to set for optimal Vulkan device selection.
    
    Args:
        config: Optional config dict for manual override and cache.
    
    Returns:
        Dict of environment variables to set (can be empty if not needed).
    """
    # Detect available devices
    vulkan_info = detect_vulkan_devices()
    
    # If there's only one GPU, no need to set device
    if not vulkan_info.has_multiple_gpus:
        return {}
    
    optimal = get_optimal_vulkan_device(config)
    env = {}
    
    # Always set device explicitly when multiple GPUs exist
    # This ensures ggml uses the correct device regardless of its internal ordering
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


def get_gpu_choices() -> List[tuple[str, str]]:
    """
    Get a list of GPU choices for UI selection.
    
    Returns:
        List of (value, label) tuples for dropdown/combo box.
        Example: [("auto", "Auto (benchmark fastest)"), ("0", "AMD Radeon (discrete)"), ...]
    """
    choices = [("auto", "Auto (detect fastest)")]
    
    vulkan_info = detect_vulkan_devices()
    
    for d in vulkan_info.devices:
        # Skip software renderers
        if d.vendor == "software" or d.is_cpu:
            continue
        
        # Create descriptive label
        type_label = "discrete" if d.is_discrete else "integrated" if d.is_integrated else d.device_type
        label = f"{d.name} ({type_label})"
        
        choices.append((str(d.index), label))
    
    return choices


def run_gpu_benchmark_and_cache(config: dict) -> dict:
    """
    Run GPU benchmark and update config cache.
    
    This should be called on first run or when user requests re-benchmark.
    
    Args:
        config: Config dict to update with results.
        
    Returns:
        The benchmark results dict.
    """
    results = benchmark_gpu_devices()
    
    if "error" not in results:
        config["gpu_benchmark_cache"] = results
    
    return results


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

    On Apple Silicon, uses performance core count (not efficiency cores) capped at 8,
    since Metal handles GPU-heavy work and too many CPU threads cause contention.
    On other platforms, uses 75% of total cores (2-16 range).

    Returns:
        Recommended thread count for whisper.cpp.
    """
    try:
        import sys, platform
        if sys.platform == "darwin" and platform.machine() == "arm64":
            # Apple Silicon: use performance cores only, capped at 8
            try:
                perf_cores = int(subprocess.check_output(
                    ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                    text=True, timeout=5
                ).strip())
                return max(2, min(8, perf_cores))
            except Exception:
                pass  # Fall through to generic logic

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





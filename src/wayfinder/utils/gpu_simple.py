"""
Simplified GPU detection for Wayfinder Aura.

This replaces the complex multi-layer detection with a simple, reliable approach:
1. Run whisper-cli once to get ggml's view of devices
2. Pick the discrete GPU (uma=0)
3. Set GGML_VK_VISIBLE_DEVICES once at startup
4. Done. No caching, no fallbacks, no complexity.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List


@dataclass
class GpuDevice:
    """A GPU device as seen by ggml/whisper.cpp."""
    index: int
    name: str
    is_discrete: bool  # uma=0 means discrete, uma=1 means integrated
    has_matrix_cores: bool  # coopmat support for fast ML


def detect_gpu_devices() -> List[GpuDevice]:
    """
    Detect GPU devices using ggml's actual device ordering.
    
    This is the ONLY detection method we use - no vulkaninfo fallback.
    If this fails, we return empty list and use device 0 (default).
    
    Returns:
        List of GpuDevice in ggml's ordering.
    """
    devices = []
    
    # Find whisper-cli
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
    
    # Find smallest model for quick probe
    model_dirs = [
        Path.home() / "whisper.cpp" / "models",
        Path.home() / ".local" / "share" / "whisper.cpp",
        Path("/app/share/whisper-models"),
    ]
    model_patterns = ["ggml-tiny.en.bin", "ggml-tiny.bin", "ggml-base.en.bin"]
    
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
    
    if not model_path:
        return devices
    
    # Create minimal audio file (0.1s of silence)
    import tempfile
    import wave
    import struct
    
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            temp_audio = f.name
            with wave.open(f.name, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(struct.pack("<" + "h" * 1600, *([0] * 1600)))
        
        # Run whisper - GPU info appears during model init
        result = subprocess.run(
            [whisper_cli, "-m", model_path, "-f", temp_audio, "--no-timestamps"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        
        # Parse device list from output
        output = result.stdout + result.stderr
        
        for line in output.split("\n"):
            # Look for: "ggml_vulkan: 0 = Name | uma: 1 | ... | matrix cores: ..."
            if "ggml_vulkan:" not in line or "=" not in line or "|" not in line:
                continue
            
            try:
                parts = line.split("=", 1)
                idx_str = parts[0].split(":")[-1].strip()
                
                # Skip "Found N devices" line
                if not idx_str.isdigit():
                    continue
                
                idx = int(idx_str)
                rest = parts[1]
                name = rest.split("|")[0].strip()
                
                # uma: 1 = integrated (unified memory), uma: 0 = discrete
                is_discrete = "uma: 0" in rest
                
                # Check for matrix core support (fast for ML)
                has_matrix = "coopmat" in rest.lower() and "none" not in rest.lower().split("matrix cores")[-1][:20]
                
                devices.append(GpuDevice(
                    index=idx,
                    name=name,
                    is_discrete=is_discrete,
                    has_matrix_cores=has_matrix,
                ))
            except (ValueError, IndexError):
                continue
        
    except Exception:
        pass
    finally:
        try:
            os.unlink(temp_audio)
        except:
            pass
    
    return devices


def get_discrete_gpu() -> Optional[int]:
    """
    Get the discrete GPU device index.
    
    Simple logic:
    1. Detect devices
    2. Find first with is_discrete=True (prefer one with matrix cores)
    3. Return its index, or None if no discrete GPU found
    """
    devices = detect_gpu_devices()
    
    if not devices:
        return None
    
    # Prefer discrete GPU with matrix cores
    for d in devices:
        if d.is_discrete and d.has_matrix_cores:
            return d.index
    
    # Fall back to any discrete GPU
    for d in devices:
        if d.is_discrete:
            return d.index
    
    return None


def setup_gpu_environment(config: Optional[dict] = None) -> dict:
    """
    Set up GPU environment variables. Call this ONCE at app startup.
    
    Args:
        config: Optional config dict for manual override (gpu_device setting)
    
    Returns:
        Dict of environment variables that were set.
    """
    env_set = {}
    
    # 1. Check for manual override in config
    if config:
        gpu_device = config.get("gpu_device", "auto")
        if gpu_device != "auto":
            try:
                device_idx = int(gpu_device)
                os.environ["GGML_VK_VISIBLE_DEVICES"] = str(device_idx)
                env_set["GGML_VK_VISIBLE_DEVICES"] = str(device_idx)
                print(f"[GPU] Using manually configured device {device_idx}")
                return env_set
            except (ValueError, TypeError):
                pass
    
    # 2. Auto-detect discrete GPU
    discrete = get_discrete_gpu()
    
    if discrete is not None:
        os.environ["GGML_VK_VISIBLE_DEVICES"] = str(discrete)
        env_set["GGML_VK_VISIBLE_DEVICES"] = str(discrete)
        print(f"[GPU] Auto-detected discrete GPU: device {discrete}")
    else:
        print("[GPU] No discrete GPU detected, using default device 0")
    
    return env_set


def get_gpu_info() -> dict:
    """
    Get GPU information for display/debugging.
    """
    devices = detect_gpu_devices()
    discrete = get_discrete_gpu()
    current = os.environ.get("GGML_VK_VISIBLE_DEVICES", "not set")
    
    return {
        "devices": [
            {
                "index": d.index,
                "name": d.name,
                "is_discrete": d.is_discrete,
                "has_matrix_cores": d.has_matrix_cores,
            }
            for d in devices
        ],
        "recommended_device": discrete,
        "current_device": current,
    }


# For backwards compatibility with existing code
def get_vulkan_env_vars(config: Optional[dict] = None) -> dict:
    """
    Backwards-compatible function that returns env vars for GPU selection.
    
    NOTE: Prefer calling setup_gpu_environment() once at startup instead.
    This function is kept for compatibility with existing code.
    """
    # If already set in environment, just return that
    if "GGML_VK_VISIBLE_DEVICES" in os.environ:
        return {"GGML_VK_VISIBLE_DEVICES": os.environ["GGML_VK_VISIBLE_DEVICES"]}
    
    # Otherwise, detect and return (but don't set os.environ)
    if config:
        gpu_device = config.get("gpu_device", "auto")
        if gpu_device != "auto":
            try:
                return {"GGML_VK_VISIBLE_DEVICES": str(int(gpu_device))}
            except (ValueError, TypeError):
                pass
    
    discrete = get_discrete_gpu()
    if discrete is not None:
        return {"GGML_VK_VISIBLE_DEVICES": str(discrete)}
    
    return {}

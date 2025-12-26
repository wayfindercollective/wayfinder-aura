#!/usr/bin/env python3
"""
Benchmark script for Wayfinder Voice transcription performance.
Tests various models and configurations on your specific hardware.

Usage:
    python benchmark.py              # Run all benchmarks
    python benchmark.py --quick      # Quick test (tiny model only)
    python benchmark.py --gpu        # GPU benchmarks only
"""

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

# Track test files for cleanup on interrupt
_test_files_to_cleanup = []


def cleanup_and_exit(signum=None, frame=None):
    """Clean up test files and exit gracefully."""
    print("\n\nInterrupted! Cleaning up...")
    for path in _test_files_to_cleanup:
        try:
            os.unlink(path)
        except:
            pass
    sys.exit(1)

# Test audio durations
TEST_DURATIONS = [10, 30]  # seconds

# Models to test (if available)
MODELS_TO_TEST = [
    ("tiny.en", "Tiny English"),
    ("base.en", "Base English"),
    ("small.en", "Small English"),
    ("medium.en", "Medium English"),
    ("large-v3-turbo", "Large v3 Turbo"),
]

# Common model locations
MODEL_DIRS = [
    Path.home() / "whisper.cpp" / "models",
    Path.home() / ".local" / "share" / "whisper.cpp",
    Path("/app/share/whisper-models"),  # Flatpak
]

# whisper-cli locations
WHISPER_BINARIES = [
    Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
    Path("/usr/bin/whisper-cli"),
    Path("/app/bin/whisper-cli"),
]


def find_whisper_cli():
    """Find the whisper-cli binary."""
    for path in WHISPER_BINARIES:
        if path.exists():
            return str(path)
    # Try system PATH
    result = subprocess.run(["which", "whisper-cli"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def find_model(model_name: str) -> str | None:
    """Find a model file by name."""
    patterns = [
        f"ggml-{model_name}.bin",
        f"ggml-{model_name.replace('.', '-')}.bin",
        f"{model_name}.bin",
    ]
    
    for model_dir in MODEL_DIRS:
        if not model_dir.exists():
            continue
        for pattern in patterns:
            path = model_dir / pattern
            if path.exists():
                return str(path)
    return None


def create_test_audio(duration_seconds: int, sample_rate: int = 16000) -> str:
    """Create a test audio file with speech-like noise."""
    # Generate audio that simulates speech (not silence)
    samples = int(duration_seconds * sample_rate)
    
    # Create pseudo-speech: modulated noise with pauses
    t = np.linspace(0, duration_seconds, samples)
    
    # Base speech-like signal
    speech = np.sin(2 * np.pi * 200 * t) * 0.3  # Base tone
    speech += np.sin(2 * np.pi * 400 * t) * 0.2  # Harmonic
    speech += np.random.randn(samples) * 0.1    # Noise
    
    # Add envelope to simulate words
    envelope = np.abs(np.sin(2 * np.pi * 2 * t)) ** 0.5
    speech *= envelope
    
    # Normalize
    speech = speech / np.max(np.abs(speech)) * 0.7
    
    # Convert to int16
    audio_int16 = (speech * 32767).astype(np.int16)
    
    # Save to temp file
    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    with wave.open(temp_file.name, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(audio_int16.tobytes())
    
    return temp_file.name


def run_benchmark(whisper_cli: str, model_path: str, audio_file: str, 
                  use_gpu: bool = False, threads: int = 6) -> dict:
    """Run a single benchmark and return timing info."""
    
    cmd = [
        whisper_cli,
        "-m", model_path,
        "-f", audio_file,
        "-t", str(threads),
        "--no-timestamps",
    ]
    
    # GPU is ON by default in Vulkan builds of whisper.cpp
    # Use --no-gpu to disable it for CPU-only testing
    if not use_gpu:
        cmd.extend(["--no-gpu"])
    
    # Warm-up run (discard) - with proper exception handling
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"error": "Warm-up run timed out (>2min)"}
    except Exception as e:
        return {"error": f"Warm-up failed: {e}"}
    
    # Timed runs
    times = []
    errors = []
    for i in range(3):  # 3 runs for average
        try:
            start = time.perf_counter()
            result = subprocess.run(cmd, capture_output=True, timeout=180)
            elapsed = time.perf_counter() - start
            
            if result.returncode == 0:
                times.append(elapsed)
            else:
                stderr = result.stderr.decode('utf-8', errors='replace')[:200]
                errors.append(f"Run {i+1}: exit code {result.returncode}, {stderr}")
        except subprocess.TimeoutExpired:
            errors.append(f"Run {i+1}: timed out")
        except Exception as e:
            errors.append(f"Run {i+1}: {e}")
    
    if not times:
        error_summary = "; ".join(errors) if errors else "All runs failed"
        return {"error": error_summary}
    
    return {
        "min": min(times),
        "max": max(times),
        "avg": sum(times) / len(times),
        "runs": len(times),
    }


def format_time(seconds: float) -> str:
    """Format time nicely."""
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 10:
        return f"{seconds:.1f}s"
    else:
        return f"{seconds:.0f}s"


def print_system_info():
    """Print system hardware info."""
    print("=" * 60)
    print("SYSTEM INFORMATION")
    print("=" * 60)
    
    # CPU
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    cpu = line.split(":")[1].strip()
                    print(f"CPU: {cpu}")
                    break
    except:
        print("CPU: Unknown")
    
    # GPU
    try:
        result = subprocess.run(["lspci"], capture_output=True, text=True)
        for line in result.stdout.split("\n"):
            if "VGA" in line and "AMD" in line:
                gpu = line.split(":")[-1].strip()
                print(f"GPU: {gpu}")
    except:
        print("GPU: Unknown")
    
    # Memory
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem_kb = int(line.split()[1])
                    mem_gb = mem_kb / 1024 / 1024
                    print(f"RAM: {mem_gb:.0f} GB")
                    break
    except:
        pass
    
    print()


def main():
    global _test_files_to_cleanup
    
    # Register signal handler for clean interruption
    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)
    
    parser = argparse.ArgumentParser(description="Benchmark Wayfinder Voice transcription")
    parser.add_argument("--quick", action="store_true", help="Quick test (tiny model only)")
    parser.add_argument("--gpu", action="store_true", help="GPU benchmarks only")
    parser.add_argument("--cpu", action="store_true", help="CPU benchmarks only")
    parser.add_argument("--threads", type=int, default=6, help="CPU threads (default: 6)")
    args = parser.parse_args()
    
    print_system_info()
    
    # Find whisper-cli
    whisper_cli = find_whisper_cli()
    if not whisper_cli:
        print("ERROR: whisper-cli not found!")
        print("Install whisper.cpp first.")
        return
    print(f"Using: {whisper_cli}")
    print()
    
    # Find available models
    available_models = []
    for model_id, model_name in MODELS_TO_TEST:
        path = find_model(model_id)
        if path:
            available_models.append((model_id, model_name, path))
    
    if not available_models:
        print("ERROR: No models found!")
        print("Download models to ~/whisper.cpp/models/")
        return
    
    print(f"Found {len(available_models)} models:")
    for model_id, model_name, path in available_models:
        print(f"  • {model_name}: {path}")
    print()
    
    if args.quick:
        # Only test tiny model, or fall back to first available
        original_models = available_models.copy()
        available_models = [m for m in available_models if "tiny" in m[0]]
        if not available_models:
            available_models = [original_models[0]]  # First available
    
    # Create test audio
    print("Creating test audio files...")
    test_files = {}
    for duration in TEST_DURATIONS:
        test_files[duration] = create_test_audio(duration)
        _test_files_to_cleanup.append(test_files[duration])
        print(f"  • {duration}s test audio created")
    print()
    
    # Run benchmarks
    results = {}
    
    test_modes = []
    if not args.gpu:
        test_modes.append(("CPU", False))
    if not args.cpu:
        test_modes.append(("GPU", True))
    
    for mode_name, use_gpu in test_modes:
        print("=" * 60)
        print(f"{mode_name} BENCHMARKS")
        print("=" * 60)
        
        for model_id, model_name, model_path in available_models:
            print(f"\n{model_name} ({model_id}):")
            
            for duration in TEST_DURATIONS:
                audio_file = test_files[duration]
                
                try:
                    result = run_benchmark(
                        whisper_cli, model_path, audio_file,
                        use_gpu=use_gpu, threads=args.threads
                    )
                    
                    if "error" in result:
                        print(f"  {duration}s audio: FAILED - {result['error']}")
                    else:
                        avg_time = result["avg"]
                        rtf = avg_time / duration  # Real-time factor
                        
                        print(f"  {duration}s audio: {format_time(avg_time)} "
                              f"(RTF: {rtf:.2f}x, range: {format_time(result['min'])}-{format_time(result['max'])})")
                        
                        key = (model_id, mode_name, duration)
                        results[key] = result
                        
                except subprocess.TimeoutExpired:
                    print(f"  {duration}s audio: TIMEOUT (>5min)")
                except Exception as e:
                    print(f"  {duration}s audio: ERROR - {e}")
    
    # Cleanup test files
    for path in test_files.values():
        try:
            os.unlink(path)
        except:
            pass
    
    # Summary table
    print("\n")
    print("=" * 60)
    print("SUMMARY - 30 SECOND AUDIO")
    print("=" * 60)
    print()
    print("| Model | CPU Time | GPU Time | Speedup |")
    print("|-------|----------|----------|---------|")
    
    for model_id, model_name, _ in available_models:
        cpu_key = (model_id, "CPU", 30)
        gpu_key = (model_id, "GPU", 30)
        
        cpu_time = results.get(cpu_key, {}).get("avg", None)
        gpu_time = results.get(gpu_key, {}).get("avg", None)
        
        cpu_str = format_time(cpu_time) if cpu_time else "N/A"
        gpu_str = format_time(gpu_time) if gpu_time else "N/A"
        
        if cpu_time and gpu_time:
            speedup = f"{cpu_time/gpu_time:.1f}x"
        else:
            speedup = "N/A"
        
        print(f"| {model_name} | {cpu_str} | {gpu_str} | {speedup} |")
    
    print()
    print("RTF = Real-Time Factor (1.0 = processes audio in real-time)")
    print("Lower is better. RTF < 1.0 means faster than real-time.")


if __name__ == "__main__":
    main()


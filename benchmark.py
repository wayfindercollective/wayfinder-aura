#!/usr/bin/env python3
"""
Comprehensive Benchmark Suite for Wayfinder Aura

Tests all aspects of the transcription and post-processing pipeline:
- Transcription backends (whisper.cpp, Faster-Whisper)
- Processing modes (CPU, GPU)
- Accuracy modes (fast, balanced, high)
- Post-processing backends (Ollama, llama.cpp, cloud APIs)
- API latency for cloud services

Results are stored in config and used for intelligent recommendations.

Usage:
    python benchmark.py              # Run all benchmarks
    python benchmark.py --quick      # Quick test (minimal config)
    python benchmark.py --gpu        # GPU benchmarks only
    python benchmark.py --transcription  # Transcription only
    python benchmark.py --postprocessing # Post-processing only
    python benchmark.py --api        # API latency only
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


# =============================================================================
# Configuration
# =============================================================================

# Test audio durations
TEST_DURATIONS = [10, 30]  # seconds

# Whisper.cpp models to test (if available)
WHISPER_MODELS = [
    ("tiny.en", "Tiny English"),
    ("base.en", "Base English"),
    ("small.en", "Small English"),
    ("medium.en", "Medium English"),
    ("large-v3-turbo", "Large v3 Turbo"),
]

# Faster-Whisper models to test
FASTER_WHISPER_MODELS = [
    ("tiny.en", "Tiny English"),
    ("base.en", "Base English"),
    ("small.en", "Small English"),
    ("medium.en", "Medium English"),
    ("large-v3-turbo", "Large v3 Turbo"),
]

# Accuracy mode presets (beam_size, best_of)
ACCURACY_MODES = {
    "fast": {"beam_size": 1, "best_of": 1, "description": "Fastest, lower accuracy"},
    "balanced": {"beam_size": 3, "best_of": 2, "description": "Good balance"},
    "high": {"beam_size": 5, "best_of": 3, "description": "Best accuracy, slower"},
}

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

# Sample text for post-processing benchmarks
SAMPLE_TRANSCRIPTION = """
Um so basically I think that you know the main thing we need to focus on is like 
the performance of the application and also uh making sure that it actually works 
properly on different systems. So um what I was thinking is that we could like 
first run some benchmarks and then uh you know analyze the results and see where 
we can make improvements. Does that make sense?
"""


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class BenchmarkResult:
    """Single benchmark result."""
    name: str
    backend: str
    mode: str  # cpu, gpu, cloud
    duration_seconds: float
    avg_time: float
    min_time: float = 0.0
    max_time: float = 0.0
    rtf: float = 0.0  # Real-time factor
    error: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "backend": self.backend,
            "mode": self.mode,
            "duration_seconds": self.duration_seconds,
            "avg_time": self.avg_time,
            "min_time": self.min_time,
            "max_time": self.max_time,
            "rtf": self.rtf,
            "error": self.error,
            "extra": self.extra,
        }


@dataclass
class BenchmarkSuite:
    """Collection of all benchmark results."""
    transcription_results: list = field(default_factory=list)
    postprocessing_results: list = field(default_factory=list)
    api_results: list = field(default_factory=list)
    accuracy_results: list = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    system_info: dict = field(default_factory=dict)

    def get_fastest_transcription(self, duration: int = 30) -> tuple:
        """Get the fastest transcription config for given duration."""
        valid = [r for r in self.transcription_results 
                 if not r.error and r.duration_seconds == duration]
        if not valid:
            return None, None
        fastest = min(valid, key=lambda r: r.avg_time)
        return fastest.backend, fastest.mode

    def get_fastest_postprocessing(self) -> str | None:
        """Get the fastest post-processing backend."""
        valid = [r for r in self.postprocessing_results if not r.error]
        if not valid:
            return None
        fastest = min(valid, key=lambda r: r.avg_time)
        return fastest.backend

    def get_optimal_accuracy_mode(self, max_rtf: float = 0.5) -> str:
        """Get best accuracy mode that stays under RTF threshold."""
        valid = [r for r in self.accuracy_results 
                 if not r.error and r.rtf <= max_rtf]
        if not valid:
            return "fast"
        # Prefer higher accuracy if fast enough
        mode_priority = {"high": 0, "balanced": 1, "fast": 2}
        valid.sort(key=lambda r: mode_priority.get(r.extra.get("accuracy_mode", "fast"), 99))
        return valid[0].extra.get("accuracy_mode", "balanced")

    def get_ranked_results(self) -> list:
        """Get all results ranked from fastest to slowest."""
        all_results = (
            self.transcription_results + 
            self.postprocessing_results + 
            self.api_results +
            self.accuracy_results
        )
        valid = [r for r in all_results if not r.error]
        return sorted(valid, key=lambda r: r.avg_time)

    def to_config_format(self) -> dict:
        """Convert to format suitable for config file."""
        # Transcription benchmarks by model
        transcription = {}
        for r in self.transcription_results:
            if r.error:
                continue
            key = r.name.lower().replace(" ", "_")
            if key not in transcription:
                transcription[key] = {}
            transcription[key][f"{r.mode}_{int(r.duration_seconds)}s"] = round(r.avg_time, 3)
            transcription[key]["fastest"] = r.mode if r.avg_time == min(
                t.avg_time for t in self.transcription_results 
                if t.name == r.name and not t.error
            ) else transcription[key].get("fastest", r.mode)

        # Post-processing benchmarks
        postprocessing = {}
        for r in self.postprocessing_results:
            if r.error:
                continue
            postprocessing[r.backend] = {
                "avg_time": round(r.avg_time, 3),
                "available": True,
            }

        # API latencies
        api = {}
        for r in self.api_results:
            if r.error:
                continue
            api[r.backend] = {
                "latency": round(r.avg_time, 3),
                "available": True,
            }

        # Accuracy modes
        accuracy = {}
        for r in self.accuracy_results:
            if r.error:
                continue
            mode = r.extra.get("accuracy_mode", "unknown")
            accuracy[mode] = {
                "avg_time": round(r.avg_time, 3),
                "rtf": round(r.rtf, 3),
            }

        return {
            "benchmark_results": transcription,
            "postprocessing_benchmark_results": postprocessing,
            "api_benchmark_results": api,
            "accuracy_benchmark_results": accuracy,
            "benchmark_fastest_processor": self.get_fastest_transcription()[1],
            "benchmark_fastest_postprocessor": self.get_fastest_postprocessing(),
            "benchmark_optimal_accuracy_mode": self.get_optimal_accuracy_mode(),
            "benchmark_timestamp": int(self.timestamp),
            "benchmark_system_info": self.system_info,
        }


# =============================================================================
# Utility Functions
# =============================================================================

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


def format_time(seconds: float) -> str:
    """Format time nicely."""
    if seconds < 0.001:
        return f"{seconds*1000000:.0f}μs"
    elif seconds < 1:
        return f"{seconds*1000:.0f}ms"
    elif seconds < 10:
        return f"{seconds:.2f}s"
    else:
        return f"{seconds:.1f}s"


def get_system_info() -> dict:
    """Get system hardware info."""
    info = {}
    
    # CPU
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if "model name" in line:
                    info["cpu"] = line.split(":")[1].strip()
                    break
    except:
        info["cpu"] = "Unknown"
    
    # GPU
    try:
        result = subprocess.run(["lspci"], capture_output=True, text=True)
        for line in result.stdout.split("\n"):
            if "VGA" in line:
                # Extract GPU name
                if "AMD" in line or "NVIDIA" in line or "Intel" in line:
                    info["gpu"] = line.split(":")[-1].strip()
                    break
    except:
        info["gpu"] = "Unknown"
    
    # Memory
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if "MemTotal" in line:
                    mem_kb = int(line.split()[1])
                    info["ram_gb"] = round(mem_kb / 1024 / 1024, 1)
                    break
    except:
        pass
    
    return info


def print_system_info(info: dict):
    """Print system hardware info."""
    print("=" * 70)
    print("SYSTEM INFORMATION")
    print("=" * 70)
    print(f"CPU: {info.get('cpu', 'Unknown')}")
    print(f"GPU: {info.get('gpu', 'Unknown')}")
    if 'ram_gb' in info:
        print(f"RAM: {info['ram_gb']:.0f} GB")
    print()


# =============================================================================
# Transcription Benchmarks
# =============================================================================

def benchmark_whisper_cpp(
    whisper_cli: str, 
    model_path: str, 
    audio_file: str, 
    use_gpu: bool = False, 
    threads: int = 6,
    beam_size: int = 5,
    best_of: int = 3,
) -> dict:
    """Run a single whisper.cpp benchmark and return timing info."""
    
    cmd = [
        whisper_cli,
        "-m", model_path,
        "-f", audio_file,
        "-t", str(threads),
        "--no-timestamps",
        "--beam-size", str(beam_size),
        "--best-of", str(best_of),
    ]
    
    # GPU is ON by default in Vulkan builds
    if not use_gpu:
        cmd.extend(["--no-gpu"])
    
    # Warm-up run (discard)
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


def run_transcription_benchmarks(
    suite: BenchmarkSuite, 
    test_files: dict,
    whisper_cli: str | None,
    available_models: list,
    test_gpu: bool = True,
    test_cpu: bool = True,
    threads: int = 6,
    quick: bool = False,
) -> None:
    """Run all transcription benchmarks."""
    
    if not whisper_cli or not available_models:
        print("⚠ Skipping whisper.cpp benchmarks (not available)")
        return
    
    test_modes = []
    if test_cpu:
        test_modes.append(("CPU", False))
    if test_gpu:
        test_modes.append(("GPU", True))
    
    if quick:
        available_models = available_models[:1]  # Only test first model
        durations = [10]  # Only 10s test
    else:
        durations = TEST_DURATIONS
    
    for mode_name, use_gpu in test_modes:
        print("=" * 70)
        print(f"WHISPER.CPP {mode_name} BENCHMARKS")
        print("=" * 70)
        
        for model_id, model_name, model_path in available_models:
            print(f"\n{model_name} ({model_id}):")
            
            for duration in durations:
                audio_file = test_files[duration]
                
                try:
                    result = benchmark_whisper_cpp(
                        whisper_cli, model_path, audio_file,
                        use_gpu=use_gpu, threads=threads
                    )
                    
                    if "error" in result:
                        print(f"  {duration}s audio: FAILED - {result['error']}")
                        suite.transcription_results.append(BenchmarkResult(
                            name=model_name,
                            backend="whisper_cpp",
                            mode=mode_name.lower(),
                            duration_seconds=duration,
                            avg_time=0,
                            error=result["error"],
                        ))
                    else:
                        avg_time = result["avg"]
                        rtf = avg_time / duration
                        
                        print(f"  {duration}s audio: {format_time(avg_time)} "
                              f"(RTF: {rtf:.2f}x, range: {format_time(result['min'])}-{format_time(result['max'])})")
                        
                        suite.transcription_results.append(BenchmarkResult(
                            name=model_name,
                            backend="whisper_cpp",
                            mode=mode_name.lower(),
                            duration_seconds=duration,
                            avg_time=avg_time,
                            min_time=result["min"],
                            max_time=result["max"],
                            rtf=rtf,
                            extra={"model_id": model_id},
                        ))
                        
                except Exception as e:
                    print(f"  {duration}s audio: ERROR - {e}")


def run_faster_whisper_benchmarks(
    suite: BenchmarkSuite,
    test_files: dict,
    test_gpu: bool = True,
    test_cpu: bool = True,
    quick: bool = False,
) -> None:
    """Run Faster-Whisper benchmarks if available."""
    
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("⚠ Skipping Faster-Whisper benchmarks (not installed)")
        return
    
    # Check GPU availability
    gpu_available = False
    try:
        import torch
        gpu_available = torch.cuda.is_available()
    except ImportError:
        pass
    
    if quick:
        models_to_test = [("tiny.en", "Tiny English")]
        durations = [10]
    else:
        models_to_test = FASTER_WHISPER_MODELS[:3]  # Test up to small
        durations = TEST_DURATIONS
    
    test_modes = []
    if test_cpu:
        test_modes.append(("CPU", False))
    if test_gpu and gpu_available:
        test_modes.append(("GPU", True))
    
    for mode_name, use_gpu in test_modes:
        print("=" * 70)
        print(f"FASTER-WHISPER {mode_name} BENCHMARKS")
        print("=" * 70)
        
        for model_id, model_name in models_to_test:
            print(f"\n{model_name} ({model_id}):")
            
            try:
                # Load model once
                device = "cuda" if use_gpu else "cpu"
                compute_type = "float16" if use_gpu else "int8"
                
                model = WhisperModel(
                    model_id,
                    device=device,
                    compute_type=compute_type,
                )
                
                for duration in durations:
                    audio_file = test_files[duration]
                    
                    # Warm-up
                    try:
                        segments, _ = model.transcribe(audio_file, language="en")
                        list(segments)  # Consume iterator
                    except:
                        pass
                    
                    # Timed runs
                    times = []
                    for _ in range(3):
                        start = time.perf_counter()
                        segments, _ = model.transcribe(audio_file, language="en")
                        list(segments)  # Consume iterator
                        elapsed = time.perf_counter() - start
                        times.append(elapsed)
                    
                    avg_time = sum(times) / len(times)
                    rtf = avg_time / duration
                    
                    print(f"  {duration}s audio: {format_time(avg_time)} "
                          f"(RTF: {rtf:.2f}x, range: {format_time(min(times))}-{format_time(max(times))})")
                    
                    suite.transcription_results.append(BenchmarkResult(
                        name=f"FW {model_name}",
                        backend="faster_whisper",
                        mode=mode_name.lower(),
                        duration_seconds=duration,
                        avg_time=avg_time,
                        min_time=min(times),
                        max_time=max(times),
                        rtf=rtf,
                        extra={"model_id": model_id},
                    ))
                    
            except Exception as e:
                print(f"  ERROR: {e}")


# =============================================================================
# Accuracy Mode Benchmarks
# =============================================================================

def run_accuracy_mode_benchmarks(
    suite: BenchmarkSuite,
    test_files: dict,
    whisper_cli: str | None,
    model_path: str | None,
    use_gpu: bool = True,
    threads: int = 6,
) -> None:
    """Benchmark different accuracy modes (fast, balanced, high)."""
    
    if not whisper_cli or not model_path:
        print("⚠ Skipping accuracy mode benchmarks (whisper.cpp not available)")
        return
    
    print("=" * 70)
    print("ACCURACY MODE BENCHMARKS")
    print("=" * 70)
    
    audio_file = test_files[30]  # Use 30s audio for accuracy tests
    
    for mode_name, settings in ACCURACY_MODES.items():
        print(f"\n{mode_name.upper()} mode ({settings['description']}):")
        print(f"  beam_size={settings['beam_size']}, best_of={settings['best_of']}")
        
        result = benchmark_whisper_cpp(
            whisper_cli, model_path, audio_file,
            use_gpu=use_gpu, threads=threads,
            beam_size=settings["beam_size"],
            best_of=settings["best_of"],
        )
        
        if "error" in result:
            print(f"  Result: FAILED - {result['error']}")
            suite.accuracy_results.append(BenchmarkResult(
                name=f"Accuracy {mode_name}",
                backend="whisper_cpp",
                mode="gpu" if use_gpu else "cpu",
                duration_seconds=30,
                avg_time=0,
                error=result["error"],
                extra={"accuracy_mode": mode_name},
            ))
        else:
            avg_time = result["avg"]
            rtf = avg_time / 30
            
            print(f"  Result: {format_time(avg_time)} (RTF: {rtf:.2f}x)")
            
            suite.accuracy_results.append(BenchmarkResult(
                name=f"Accuracy {mode_name}",
                backend="whisper_cpp",
                mode="gpu" if use_gpu else "cpu",
                duration_seconds=30,
                avg_time=avg_time,
                min_time=result["min"],
                max_time=result["max"],
                rtf=rtf,
                extra={"accuracy_mode": mode_name, **settings},
            ))


# =============================================================================
# Post-Processing Benchmarks
# =============================================================================

def run_postprocessing_benchmarks(suite: BenchmarkSuite) -> None:
    """Benchmark post-processing backends."""
    
    print("=" * 70)
    print("POST-PROCESSING BENCHMARKS")
    print("=" * 70)
    
    test_text = SAMPLE_TRANSCRIPTION.strip()
    
    # Test Ollama
    print("\nOllama:")
    try:
        import requests
        
        # Check if Ollama is running
        try:
            response = requests.get("http://localhost:11434/api/tags", timeout=2)
            if response.status_code != 200:
                raise Exception("Ollama not responding")
        except:
            print("  ⚠ Ollama not running - skipping")
            suite.postprocessing_results.append(BenchmarkResult(
                name="Ollama",
                backend="ollama",
                mode="local",
                duration_seconds=0,
                avg_time=0,
                error="Ollama not running",
            ))
        else:
            # Get available models
            models = response.json().get("models", [])
            if not models:
                print("  ⚠ No Ollama models installed - skipping")
            else:
                # Use first small model (phi3, qwen, llama3.2, etc.)
                small_models = [m for m in models if any(
                    x in m.get("name", "").lower() 
                    for x in ["phi3:mini", "phi3.5:mini", "qwen2.5:1.5b", "llama3.2:1b", "smollm"]
                )]
                test_model = small_models[0]["name"] if small_models else models[0]["name"]
                
                print(f"  Testing with model: {test_model}")
                
                # Warm-up
                requests.post(
                    "http://localhost:11434/api/generate",
                    json={"model": test_model, "prompt": "Hello", "stream": False},
                    timeout=30,
                )
                
                # Timed runs
                times = []
                prompt = f"Clean up this transcription, remove filler words:\n{test_text}"
                
                for _ in range(3):
                    start = time.perf_counter()
                    response = requests.post(
                        "http://localhost:11434/api/generate",
                        json={"model": test_model, "prompt": prompt, "stream": False},
                        timeout=30,
                    )
                    elapsed = time.perf_counter() - start
                    if response.status_code == 200:
                        times.append(elapsed)
                
                if times:
                    avg_time = sum(times) / len(times)
                    print(f"  Result: {format_time(avg_time)} avg")
                    
                    suite.postprocessing_results.append(BenchmarkResult(
                        name=f"Ollama ({test_model})",
                        backend="ollama",
                        mode="local",
                        duration_seconds=0,
                        avg_time=avg_time,
                        min_time=min(times),
                        max_time=max(times),
                        extra={"model": test_model},
                    ))
                else:
                    print("  ⚠ All runs failed")
                    
    except ImportError:
        print("  ⚠ requests not installed - skipping Ollama")
    except Exception as e:
        print(f"  ⚠ Error: {e}")
    
    # Test llama-cpp-python
    print("\nllama-cpp-python:")
    try:
        from llama_cpp import Llama
        
        # Look for GGUF models
        model_dirs = [
            Path.home() / ".local" / "share" / "models",
            Path.home() / "models",
            Path.home() / "llama-models",
        ]
        
        gguf_models = []
        for model_dir in model_dirs:
            if model_dir.exists():
                gguf_models.extend(model_dir.glob("*.gguf"))
        
        if not gguf_models:
            print("  ⚠ No GGUF models found - skipping")
            suite.postprocessing_results.append(BenchmarkResult(
                name="llama.cpp",
                backend="llama_cpp",
                mode="local",
                duration_seconds=0,
                avg_time=0,
                error="No GGUF models found",
            ))
        else:
            # Use smallest model
            test_model = min(gguf_models, key=lambda p: p.stat().st_size)
            print(f"  Testing with: {test_model.name}")
            
            # Load model
            model = Llama(
                model_path=str(test_model),
                n_ctx=2048,
                n_threads=4,
                n_gpu_layers=-1,
                verbose=False,
            )
            
            prompt = f"Clean up this transcription:\n{test_text}\n\nCleaned:"
            
            # Warm-up
            model(prompt, max_tokens=100, temperature=0.1)
            
            # Timed runs
            times = []
            for _ in range(3):
                start = time.perf_counter()
                model(prompt, max_tokens=100, temperature=0.1)
                elapsed = time.perf_counter() - start
                times.append(elapsed)
            
            avg_time = sum(times) / len(times)
            print(f"  Result: {format_time(avg_time)} avg")
            
            suite.postprocessing_results.append(BenchmarkResult(
                name=f"llama.cpp ({test_model.name})",
                backend="llama_cpp",
                mode="local",
                duration_seconds=0,
                avg_time=avg_time,
                min_time=min(times),
                max_time=max(times),
                extra={"model": test_model.name},
            ))
            
    except ImportError:
        print("  ⚠ llama-cpp-python not installed - skipping")
    except Exception as e:
        print(f"  ⚠ Error: {e}")


# =============================================================================
# API Latency Benchmarks
# =============================================================================

def run_api_benchmarks(suite: BenchmarkSuite) -> None:
    """Benchmark API latency for cloud backends."""
    
    print("=" * 70)
    print("API LATENCY BENCHMARKS")
    print("=" * 70)
    
    test_text = SAMPLE_TRANSCRIPTION.strip()[:200]  # Shorter for API tests
    
    # Test OpenAI
    print("\nOpenAI GPT-4o-mini:")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("  ⚠ OPENAI_API_KEY not set - skipping")
    else:
        try:
            import openai
            client = openai.OpenAI(api_key=api_key, timeout=30.0)
            
            # Timed runs
            times = []
            for _ in range(3):
                start = time.perf_counter()
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    max_tokens=200,
                    messages=[{"role": "user", "content": f"Clean this: {test_text}"}],
                )
                elapsed = time.perf_counter() - start
                times.append(elapsed)
            
            avg_time = sum(times) / len(times)
            print(f"  Result: {format_time(avg_time)} avg latency")
            
            suite.api_results.append(BenchmarkResult(
                name="OpenAI GPT-4o-mini",
                backend="openai",
                mode="cloud",
                duration_seconds=0,
                avg_time=avg_time,
                min_time=min(times),
                max_time=max(times),
            ))
            
        except ImportError:
            print("  ⚠ openai package not installed")
        except Exception as e:
            print(f"  ⚠ Error: {e}")
    
    # Test Anthropic
    print("\nAnthropic Claude Haiku:")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("  ⚠ ANTHROPIC_API_KEY not set - skipping")
    else:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            
            # Timed runs
            times = []
            for _ in range(3):
                start = time.perf_counter()
                message = client.messages.create(
                    model="claude-3-haiku-20240307",
                    max_tokens=200,
                    messages=[{"role": "user", "content": f"Clean this: {test_text}"}],
                )
                elapsed = time.perf_counter() - start
                times.append(elapsed)
            
            avg_time = sum(times) / len(times)
            print(f"  Result: {format_time(avg_time)} avg latency")
            
            suite.api_results.append(BenchmarkResult(
                name="Anthropic Claude Haiku",
                backend="anthropic",
                mode="cloud",
                duration_seconds=0,
                avg_time=avg_time,
                min_time=min(times),
                max_time=max(times),
            ))
            
        except ImportError:
            print("  ⚠ anthropic package not installed")
        except Exception as e:
            print(f"  ⚠ Error: {e}")
    
    # Test Groq (for transcription API)
    print("\nGroq Whisper API (transcription):")
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        print("  ⚠ GROQ_API_KEY not set - skipping")
    else:
        try:
            import groq
            
            # Need a test audio file
            test_audio = create_test_audio(5)  # 5 second test
            _test_files_to_cleanup.append(test_audio)
            
            client = groq.Groq(api_key=api_key, timeout=30.0)
            
            # Timed runs
            times = []
            for _ in range(3):
                start = time.perf_counter()
                with open(test_audio, "rb") as f:
                    transcription = client.audio.transcriptions.create(
                        model="whisper-large-v3",
                        file=f,
                        response_format="text",
                    )
                elapsed = time.perf_counter() - start
                times.append(elapsed)
            
            avg_time = sum(times) / len(times)
            print(f"  Result: {format_time(avg_time)} avg for 5s audio")
            
            suite.api_results.append(BenchmarkResult(
                name="Groq Whisper",
                backend="groq_whisper",
                mode="cloud",
                duration_seconds=5,
                avg_time=avg_time,
                min_time=min(times),
                max_time=max(times),
                rtf=avg_time / 5,
            ))
            
            # Cleanup
            try:
                os.unlink(test_audio)
                _test_files_to_cleanup.remove(test_audio)
            except:
                pass
            
        except ImportError:
            print("  ⚠ groq package not installed")
        except Exception as e:
            print(f"  ⚠ Error: {e}")


# =============================================================================
# Results Summary
# =============================================================================

def print_summary(suite: BenchmarkSuite):
    """Print comprehensive results summary with rankings."""
    
    print("\n")
    print("=" * 70)
    print("BENCHMARK SUMMARY")
    print("=" * 70)
    
    # Transcription rankings (30s audio)
    print("\n📊 TRANSCRIPTION RANKINGS (30s audio, fastest to slowest):")
    print("-" * 60)
    
    transcription_30s = [r for r in suite.transcription_results 
                         if not r.error and r.duration_seconds == 30]
    transcription_30s.sort(key=lambda r: r.avg_time)
    
    if transcription_30s:
        for i, r in enumerate(transcription_30s, 1):
            status = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            rtf_status = "⚡" if r.rtf < 0.3 else "✓" if r.rtf < 1.0 else "⚠"
            print(f"  {status} {r.name} ({r.mode.upper()}): {format_time(r.avg_time)} "
                  f"[RTF: {r.rtf:.2f}x {rtf_status}]")
    else:
        print("  No transcription results available")
    
    # Accuracy mode comparison
    if suite.accuracy_results:
        print("\n🎯 ACCURACY MODE COMPARISON (30s audio):")
        print("-" * 60)
        
        for r in sorted(suite.accuracy_results, key=lambda x: x.avg_time):
            if r.error:
                continue
            mode = r.extra.get("accuracy_mode", "unknown")
            rtf_status = "⚡" if r.rtf < 0.3 else "✓" if r.rtf < 1.0 else "⚠"
            print(f"  {mode.upper():12} {format_time(r.avg_time):>10} [RTF: {r.rtf:.2f}x {rtf_status}]")
    
    # Post-processing rankings
    if suite.postprocessing_results:
        print("\n🔧 POST-PROCESSING RANKINGS:")
        print("-" * 60)
        
        pp_valid = [r for r in suite.postprocessing_results if not r.error]
        pp_valid.sort(key=lambda r: r.avg_time)
        
        for i, r in enumerate(pp_valid, 1):
            status = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            print(f"  {status} {r.name}: {format_time(r.avg_time)}")
    
    # API latency
    if suite.api_results:
        print("\n☁️ API LATENCY RANKINGS:")
        print("-" * 60)
        
        api_valid = [r for r in suite.api_results if not r.error]
        api_valid.sort(key=lambda r: r.avg_time)
        
        for i, r in enumerate(api_valid, 1):
            status = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            extra = f" (RTF: {r.rtf:.2f}x)" if r.rtf > 0 else ""
            print(f"  {status} {r.name}: {format_time(r.avg_time)}{extra}")
    
    # Intelligent recommendations
    print("\n💡 RECOMMENDATIONS:")
    print("-" * 60)
    
    fastest_backend, fastest_mode = suite.get_fastest_transcription(30)
    if fastest_backend:
        print(f"  • Fastest transcription: {fastest_backend} with {fastest_mode.upper()}")
    
    fastest_pp = suite.get_fastest_postprocessing()
    if fastest_pp:
        print(f"  • Fastest post-processing: {fastest_pp}")
    
    optimal_accuracy = suite.get_optimal_accuracy_mode(max_rtf=0.5)
    print(f"  • Optimal accuracy mode: {optimal_accuracy.upper()} (RTF < 0.5x)")
    
    # Check if GPU provides significant speedup
    gpu_results = [r for r in suite.transcription_results 
                   if r.mode == "gpu" and not r.error and r.duration_seconds == 30]
    cpu_results = [r for r in suite.transcription_results 
                   if r.mode == "cpu" and not r.error and r.duration_seconds == 30]
    
    if gpu_results and cpu_results:
        avg_gpu = sum(r.avg_time for r in gpu_results) / len(gpu_results)
        avg_cpu = sum(r.avg_time for r in cpu_results) / len(cpu_results)
        speedup = avg_cpu / avg_gpu if avg_gpu > 0 else 0
        
        if speedup > 1.5:
            print(f"  • GPU acceleration: {speedup:.1f}x speedup over CPU ✓")
        elif speedup > 1.0:
            print(f"  • GPU acceleration: {speedup:.1f}x speedup (marginal)")
        else:
            print(f"  • GPU acceleration: Not beneficial on this system")
    
    print()


def save_results(suite: BenchmarkSuite):
    """Save benchmark results to config file."""
    
    # Try to import config module
    try:
        sys.path.insert(0, str(Path(__file__).parent / "src"))
        from wayfinder.config import load_config, save_config, CONFIG_FILE
        
        config = load_config()
        results = suite.to_config_format()
        config.update(results)
        save_config(config)
        
        print(f"✓ Results saved to {CONFIG_FILE}")
        
    except ImportError:
        # Fallback: save to benchmark_results.json
        results_file = Path(__file__).parent / "benchmark_results.json"
        with open(results_file, "w") as f:
            json.dump(suite.to_config_format(), f, indent=2)
        print(f"✓ Results saved to {results_file}")


# =============================================================================
# Main
# =============================================================================

def main():
    global _test_files_to_cleanup
    
    # Register signal handler for clean interruption
    signal.signal(signal.SIGINT, cleanup_and_exit)
    signal.signal(signal.SIGTERM, cleanup_and_exit)
    
    parser = argparse.ArgumentParser(
        description="Comprehensive benchmark suite for Wayfinder Aura"
    )
    parser.add_argument("--quick", action="store_true", 
                        help="Quick test (minimal config)")
    parser.add_argument("--gpu", action="store_true", 
                        help="GPU benchmarks only")
    parser.add_argument("--cpu", action="store_true", 
                        help="CPU benchmarks only")
    parser.add_argument("--transcription", action="store_true", 
                        help="Transcription benchmarks only")
    parser.add_argument("--postprocessing", action="store_true", 
                        help="Post-processing benchmarks only")
    parser.add_argument("--api", action="store_true", 
                        help="API latency benchmarks only")
    parser.add_argument("--accuracy", action="store_true", 
                        help="Accuracy mode benchmarks only")
    parser.add_argument("--threads", type=int, default=6, 
                        help="CPU threads (default: 6)")
    parser.add_argument("--no-save", action="store_true", 
                        help="Don't save results to config")
    args = parser.parse_args()
    
    # Determine what to run
    run_all = not any([args.transcription, args.postprocessing, args.api, args.accuracy])
    run_transcription = run_all or args.transcription
    run_postprocessing = run_all or args.postprocessing
    run_api = run_all or args.api
    run_accuracy = run_all or args.accuracy
    
    test_cpu = not args.gpu
    test_gpu = not args.cpu
    
    # Initialize suite
    suite = BenchmarkSuite()
    suite.system_info = get_system_info()
    
    print_system_info(suite.system_info)
    
    # Find whisper-cli
    whisper_cli = find_whisper_cli()
    if whisper_cli:
        print(f"whisper-cli: {whisper_cli}")
    else:
        print("whisper-cli: Not found")
    
    # Find available models
    available_models = []
    for model_id, model_name in WHISPER_MODELS:
        path = find_model(model_id)
        if path:
            available_models.append((model_id, model_name, path))
    
    if available_models:
        print(f"\nFound {len(available_models)} whisper.cpp models:")
        for model_id, model_name, path in available_models:
            print(f"  • {model_name}: {path}")
    else:
        print("\nNo whisper.cpp models found")
    
    print()
    
    # Create test audio files
    print("Creating test audio files...")
    test_files = {}
    for duration in TEST_DURATIONS:
        test_files[duration] = create_test_audio(duration)
        _test_files_to_cleanup.append(test_files[duration])
        print(f"  • {duration}s test audio created")
    print()
    
    try:
        # Run benchmarks
        if run_transcription:
            run_transcription_benchmarks(
                suite, test_files, whisper_cli, available_models,
                test_gpu=test_gpu, test_cpu=test_cpu,
                threads=args.threads, quick=args.quick,
            )
            
            run_faster_whisper_benchmarks(
                suite, test_files,
                test_gpu=test_gpu, test_cpu=test_cpu,
                quick=args.quick,
            )
        
        if run_accuracy and whisper_cli and available_models:
            # Use the first available model for accuracy testing
            model_path = available_models[0][2]
            run_accuracy_mode_benchmarks(
                suite, test_files, whisper_cli, model_path,
                use_gpu=test_gpu, threads=args.threads,
            )
        
        if run_postprocessing:
            run_postprocessing_benchmarks(suite)
        
        if run_api:
            run_api_benchmarks(suite)
        
        # Print summary
        print_summary(suite)
        
        # Save results
        if not args.no_save:
            save_results(suite)
            
    finally:
        # Cleanup test files
        for path in test_files.values():
            try:
                os.unlink(path)
            except:
                pass


if __name__ == "__main__":
    main()

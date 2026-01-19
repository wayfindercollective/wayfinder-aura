#!/usr/bin/env python3
"""
llama.cpp Benchmark & Diagnostics for Wayfinder Aura

Tests llama.cpp post-processing with all available GGUF models:
- Lists all discovered models with size/tier info
- Benchmarks inference speed (GPU vs CPU)
- Tests actual transcription cleanup quality
- Provides recommendations

Usage:
    python scripts/benchmark_llama_cpp.py              # Full benchmark
    python scripts/benchmark_llama_cpp.py --quick      # Quick test (first model only)
    python scripts/benchmark_llama_cpp.py --list       # Just list available models
    python scripts/benchmark_llama_cpp.py --cpu        # CPU-only benchmark
"""

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# =============================================================================
# Configuration
# =============================================================================

# Common GGUF model locations
MODEL_SEARCH_PATHS = [
    Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models",
    Path.home() / ".local" / "share" / "wayfinder-voice" / "llm-models",
    Path.home() / "llama-models",
    Path.home() / "models",
    Path.home() / ".local" / "share" / "models",
    Path("/app/share/llm-models"),  # Flatpak
]

# llama.cpp binary locations
LLAMA_BINARY_PATHS = [
    Path.home() / "llama.cpp" / "build" / "bin" / "llama-cli",
    Path.home() / "llama.cpp" / "build" / "bin" / "llama-simple",
    Path("/usr/bin/llama-cli"),
    Path("/usr/bin/llama-simple"),
    Path("/app/bin/llama-cli"),
    Path("/app/bin/llama-simple"),
]

# Test transcription for cleanup
TEST_TRANSCRIPTION = """
Um so basically I think that you know the main thing we need to focus on is like 
the performance of the application and also uh making sure that it actually works 
properly on different systems. So um what I was thinking is that we could like 
first run some benchmarks and then uh you know analyze the results.
"""

# Model tier definitions for nice display
MODEL_TIERS = {
    "tiny": {"emoji": "🔹", "min_params": "0", "max_params": "500M", "color": "\033[94m"},
    "small": {"emoji": "🔸", "min_params": "500M", "max_params": "2B", "color": "\033[93m"},
    "standard": {"emoji": "🟢", "min_params": "2B", "max_params": "7B", "color": "\033[92m"},
    "large": {"emoji": "🟣", "min_params": "7B", "max_params": "∞", "color": "\033[95m"},
}


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class ModelInfo:
    """Information about a GGUF model."""
    path: Path
    name: str
    size_mb: float
    tier: str
    estimated_params: str
    
    def __str__(self):
        tier_info = MODEL_TIERS.get(self.tier, MODEL_TIERS["small"])
        return f"{tier_info['emoji']} {self.name} ({self.size_mb:.0f}MB, ~{self.estimated_params})"


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""
    model_name: str
    mode: str  # "gpu" or "cpu"
    inference_time: float
    output_text: str
    tokens_per_second: float = 0
    error: Optional[str] = None
    
    @property
    def is_success(self) -> bool:
        return self.error is None


# =============================================================================
# Discovery Functions
# =============================================================================

def find_llama_binary() -> Optional[Path]:
    """Find the llama-cli or llama-simple binary."""
    for path in LLAMA_BINARY_PATHS:
        if path.exists():
            return path
    return None


def estimate_params_from_size(size_mb: float, quant_level: str = "q4") -> str:
    """Estimate parameter count from file size and quantization."""
    # Rough estimates: Q4 = ~0.5 bytes/param, Q8 = ~1 byte/param, FP16 = ~2 bytes/param
    quant_multipliers = {
        "q2": 0.3,
        "q3": 0.4,
        "q4": 0.5,
        "q5": 0.6,
        "q6": 0.7,
        "q8": 1.0,
        "fp16": 2.0,
        "f16": 2.0,
    }
    
    multiplier = quant_multipliers.get(quant_level.lower(), 0.5)
    estimated_params = (size_mb * 1024 * 1024) / multiplier
    
    # Format as human readable
    if estimated_params >= 1e9:
        return f"{estimated_params / 1e9:.1f}B"
    elif estimated_params >= 1e6:
        return f"{estimated_params / 1e6:.0f}M"
    else:
        return f"{estimated_params / 1e3:.0f}K"


def detect_tier(size_mb: float, name: str) -> str:
    """Detect model tier based on size and name."""
    name_lower = name.lower()
    
    # Check name patterns first
    if any(x in name_lower for x in ["0.5b", "360m", "135m", "tiny"]):
        return "tiny"
    if any(x in name_lower for x in ["1b", "1.5b", "2b", "small", "mini"]):
        return "small"
    if any(x in name_lower for x in ["3b", "4b", "7b", "8b", "medium"]):
        return "standard"
    if any(x in name_lower for x in ["13b", "14b", "32b", "70b", "large"]):
        return "large"
    
    # Fallback to size-based detection (Q4 models)
    if size_mb < 400:
        return "tiny"
    elif size_mb < 1500:
        return "small"
    elif size_mb < 5000:
        return "standard"
    else:
        return "large"


def detect_quant_level(name: str) -> str:
    """Detect quantization level from filename."""
    name_lower = name.lower()
    
    for quant in ["q2", "q3", "q4", "q5", "q6", "q8", "fp16", "f16"]:
        if quant in name_lower:
            return quant
    
    return "q4"  # Default assumption


def find_models() -> list[ModelInfo]:
    """Find all available GGUF models."""
    models = []
    seen_paths = set()
    
    for search_path in MODEL_SEARCH_PATHS:
        if not search_path.exists():
            continue
        
        for gguf_file in search_path.glob("*.gguf"):
            if gguf_file in seen_paths:
                continue
            seen_paths.add(gguf_file)
            
            size_mb = gguf_file.stat().st_size / (1024 * 1024)
            quant = detect_quant_level(gguf_file.name)
            
            models.append(ModelInfo(
                path=gguf_file,
                name=gguf_file.stem,
                size_mb=size_mb,
                tier=detect_tier(size_mb, gguf_file.name),
                estimated_params=estimate_params_from_size(size_mb, quant),
            ))
    
    # Sort by size (smallest first for quick testing)
    models.sort(key=lambda m: m.size_mb)
    
    return models


# =============================================================================
# Benchmark Functions
# =============================================================================

def run_llama_benchmark(
    binary: Path,
    model_path: Path,
    prompt: str,
    use_gpu: bool = True,
    max_tokens: int = 150,
    timeout: int = 60,
) -> BenchmarkResult:
    """Run a single llama.cpp benchmark."""
    
    # Prefer llama-simple if available (better for non-interactive)
    simple_path = str(binary).replace("llama-cli", "llama-simple")
    if Path(simple_path).exists():
        binary = Path(simple_path)
    
    # Build command
    cmd = [
        str(binary),
        "-m", str(model_path),
        "-n", str(max_tokens),
        "-p", prompt,
    ]
    
    # GPU layers (-1 = all, 0 = none)
    if use_gpu:
        cmd.extend(["-ngl", "99"])  # Put all layers on GPU
    else:
        cmd.extend(["-ngl", "0"])
    
    mode = "gpu" if use_gpu else "cpu"
    model_name = model_path.stem
    
    try:
        start_time = time.perf_counter()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - start_time
        
        if result.returncode != 0:
            return BenchmarkResult(
                model_name=model_name,
                mode=mode,
                inference_time=elapsed,
                output_text="",
                error=f"Exit code {result.returncode}: {result.stderr[:200]}",
            )
        
        # Parse output - extract the generated text
        output = result.stdout
        
        # Remove llama.cpp info lines
        lines = []
        for line in output.split('\n'):
            # Skip info/debug lines
            if any(line.startswith(p) for p in [
                'llama_', 'ggml_', 'main:', 'sched_', 'graph_', 
                '~llama', 'Loading', 'WARNING', 'loaded',
            ]):
                continue
            if line.strip():
                lines.append(line.strip())
        
        generated_text = ' '.join(lines)
        
        # Estimate tokens/second (rough: ~1.3 chars per token)
        approx_tokens = len(generated_text) / 1.3
        tokens_per_sec = approx_tokens / elapsed if elapsed > 0 else 0
        
        return BenchmarkResult(
            model_name=model_name,
            mode=mode,
            inference_time=elapsed,
            output_text=generated_text,
            tokens_per_second=tokens_per_sec,
        )
        
    except subprocess.TimeoutExpired:
        return BenchmarkResult(
            model_name=model_name,
            mode=mode,
            inference_time=timeout,
            output_text="",
            error=f"Timed out after {timeout}s",
        )
    except Exception as e:
        return BenchmarkResult(
            model_name=model_name,
            mode=mode,
            inference_time=0,
            output_text="",
            error=str(e),
        )


def run_python_backend_test(model_path: Path, use_gpu: bool = True) -> BenchmarkResult:
    """Test using the llama-cpp-python backend (if available)."""
    mode = "gpu" if use_gpu else "cpu"
    model_name = model_path.stem
    
    try:
        from llama_cpp import Llama
    except ImportError:
        return BenchmarkResult(
            model_name=model_name,
            mode=mode,
            inference_time=0,
            output_text="",
            error="llama-cpp-python not installed",
        )
    
    try:
        n_gpu_layers = -1 if use_gpu else 0
        
        # Load model
        load_start = time.perf_counter()
        model = Llama(
            model_path=str(model_path),
            n_ctx=2048,
            n_threads=4,
            n_gpu_layers=n_gpu_layers,
            verbose=False,
        )
        load_time = time.perf_counter() - load_start
        
        # Run inference
        prompt = f"Clean up this text (remove um/uh): {TEST_TRANSCRIPTION.strip()}\n\nCleaned:"
        
        infer_start = time.perf_counter()
        response = model(
            prompt,
            max_tokens=150,
            temperature=0.1,
            stop=["Transcription:", "\n\n\n"],
            echo=False,
        )
        infer_time = time.perf_counter() - infer_start
        
        output = response["choices"][0]["text"].strip()
        
        # Estimate tokens/second
        tokens = response["usage"]["completion_tokens"]
        tokens_per_sec = tokens / infer_time if infer_time > 0 else 0
        
        return BenchmarkResult(
            model_name=model_name,
            mode=f"{mode}_python",
            inference_time=infer_time,
            output_text=output,
            tokens_per_second=tokens_per_sec,
        )
        
    except Exception as e:
        return BenchmarkResult(
            model_name=model_name,
            mode=mode,
            inference_time=0,
            output_text="",
            error=str(e),
        )


# =============================================================================
# Display Functions
# =============================================================================

def print_header(text: str, char: str = "="):
    """Print a formatted header."""
    width = 70
    print(f"\n{char * width}")
    print(f" {text}")
    print(f"{char * width}")


def print_model_diagram(models: list[ModelInfo]):
    """Print a nice diagram of available models."""
    print_header("📦 AVAILABLE GGUF MODELS", "═")
    
    if not models:
        print("\n  ⚠ No GGUF models found!")
        print("\n  Search paths checked:")
        for path in MODEL_SEARCH_PATHS:
            exists = "✓" if path.exists() else "✗"
            print(f"    {exists} {path}")
        print("\n  Download models from:")
        print("    https://huggingface.co/models?sort=trending&search=gguf")
        return
    
    # Group by tier
    by_tier = {}
    for model in models:
        if model.tier not in by_tier:
            by_tier[model.tier] = []
        by_tier[model.tier].append(model)
    
    print()
    print("  ┌─ Tier ─────────┬─ Model ────────────────────────────────────┐")
    
    for tier in ["tiny", "small", "standard", "large"]:
        if tier not in by_tier:
            continue
        
        tier_info = MODEL_TIERS[tier]
        tier_label = f"{tier_info['emoji']} {tier.upper()}"
        
        for i, model in enumerate(by_tier[tier]):
            prefix = tier_label if i == 0 else ""
            print(f"  │ {prefix:15} │ {model.name[:42]:42} │")
            if i == 0:
                params_str = f"~{model.estimated_params}"
                print(f"  │ {f'({params_str})':15} │ {f'  Size: {model.size_mb:.0f}MB':42} │")
    
    print("  └─────────────────┴────────────────────────────────────────────┘")
    
    # Print legend
    print("\n  Legend:")
    for tier, info in MODEL_TIERS.items():
        desc = {
            "tiny": "< 500M params - Fast but limited",
            "small": "500M - 2B params - Good balance",
            "standard": "2B - 7B params - Full capability",
            "large": "7B+ params - Best quality",
        }
        print(f"    {info['emoji']} {tier.upper():10} {desc[tier]}")


def print_benchmark_result(result: BenchmarkResult, index: int = 1):
    """Print a single benchmark result."""
    status = "✓" if result.is_success else "✗"
    mode_icon = "🚀" if "gpu" in result.mode else "💻"
    
    print(f"\n  {index}. {result.model_name}")
    print(f"     Mode: {mode_icon} {result.mode.upper()}")
    
    if result.is_success:
        print(f"     Time: {result.inference_time:.2f}s ({result.tokens_per_second:.1f} tokens/sec)")
        
        # Show truncated output
        output_preview = result.output_text[:100]
        if len(result.output_text) > 100:
            output_preview += "..."
        print(f"     Output: {output_preview}")
    else:
        print(f"     {status} Error: {result.error}")


def print_summary(results: list[BenchmarkResult]):
    """Print benchmark summary."""
    print_header("📊 BENCHMARK SUMMARY", "═")
    
    successful = [r for r in results if r.is_success]
    failed = [r for r in results if not r.is_success]
    
    if not successful:
        print("\n  ⚠ All benchmarks failed!")
        return
    
    # Find fastest
    fastest_gpu = [r for r in successful if "gpu" in r.mode]
    fastest_cpu = [r for r in successful if "cpu" in r.mode]
    
    if fastest_gpu:
        best_gpu = min(fastest_gpu, key=lambda r: r.inference_time)
        print(f"\n  🏆 Fastest GPU: {best_gpu.model_name}")
        print(f"     {best_gpu.inference_time:.2f}s ({best_gpu.tokens_per_second:.1f} tokens/sec)")
    
    if fastest_cpu:
        best_cpu = min(fastest_cpu, key=lambda r: r.inference_time)
        print(f"\n  💻 Fastest CPU: {best_cpu.model_name}")
        print(f"     {best_cpu.inference_time:.2f}s ({best_cpu.tokens_per_second:.1f} tokens/sec)")
    
    # GPU speedup
    if fastest_gpu and fastest_cpu:
        gpu_avg = sum(r.inference_time for r in fastest_gpu) / len(fastest_gpu)
        cpu_avg = sum(r.inference_time for r in fastest_cpu) / len(fastest_cpu)
        if gpu_avg > 0:
            speedup = cpu_avg / gpu_avg
            print(f"\n  ⚡ GPU Speedup: {speedup:.1f}x faster than CPU")
    
    if failed:
        print(f"\n  ⚠ {len(failed)} benchmark(s) failed")
    
    # Recommendations
    print("\n  💡 Recommendations:")
    if fastest_gpu:
        best = min(fastest_gpu, key=lambda r: r.inference_time)
        print(f"     • Use '{best.model_name}' for best GPU performance")
    print("     • qwen2.5-1.5b-instruct is recommended for quality/speed balance")
    print("     • Phi-3-mini is best for 'Strong' mode transformations")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="llama.cpp benchmark and diagnostics for Wayfinder Aura"
    )
    parser.add_argument("--quick", action="store_true",
                        help="Quick test (first model only)")
    parser.add_argument("--list", action="store_true",
                        help="Just list available models")
    parser.add_argument("--cpu", action="store_true",
                        help="CPU-only benchmark")
    parser.add_argument("--gpu", action="store_true",
                        help="GPU-only benchmark")
    parser.add_argument("--python", action="store_true",
                        help="Also test llama-cpp-python backend")
    args = parser.parse_args()
    
    # Find llama binary
    binary = find_llama_binary()
    if not binary:
        print("❌ llama.cpp binary not found!")
        print("\nExpected locations:")
        for path in LLAMA_BINARY_PATHS:
            print(f"  • {path}")
        print("\nTo install llama.cpp:")
        print("  git clone https://github.com/ggerganov/llama.cpp")
        print("  cd llama.cpp && cmake -B build -DGGML_VULKAN=ON && cmake --build build")
        return 1
    
    print(f"✓ Found llama.cpp: {binary}")
    
    # Check for Vulkan support
    if "vulkan" in str(binary.parent.parent).lower() or Path(binary.parent / "libggml-vulkan.so").exists():
        print("✓ Vulkan GPU support detected")
    
    # Find models
    models = find_models()
    
    # Print model diagram
    print_model_diagram(models)
    
    if args.list or not models:
        return 0 if models else 1
    
    # Determine what to test
    if args.quick:
        models = models[:1]
        print(f"\n🚀 Quick mode: testing only {models[0].name}")
    
    test_gpu = not args.cpu
    test_cpu = not args.gpu or args.cpu
    
    # Run benchmarks
    print_header("⏱ RUNNING BENCHMARKS", "═")
    
    prompt = f"Clean up this text (remove um/uh): {TEST_TRANSCRIPTION.strip()}\n\nCleaned:"
    results = []
    
    for i, model in enumerate(models, 1):
        print(f"\n[{i}/{len(models)}] Testing {model.name}...")
        
        if test_gpu:
            result = run_llama_benchmark(binary, model.path, prompt, use_gpu=True)
            results.append(result)
            print_benchmark_result(result, i)
        
        if test_cpu:
            result = run_llama_benchmark(binary, model.path, prompt, use_gpu=False)
            results.append(result)
            if not test_gpu:
                print_benchmark_result(result, i)
        
        if args.python:
            result = run_python_backend_test(model.path, use_gpu=test_gpu)
            results.append(result)
    
    # Print summary
    print_summary(results)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
First-run dependency detection and installation for Wayfinder Aura.

Checks for required external tools, offers automated installation,
and guides users through setup on first launch.

Supports Ubuntu/Debian (apt), with graceful fallback to manual instructions.
"""

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests


# ─── Model Catalog ───────────────────────────────────────────────

WHISPER_MODELS: dict[str, dict] = {
    "tiny.en": {
        "label": "Tiny (English)",
        "size": "75 MB",
        "bytes": 78_000_000,
        "note": "Fastest, lowest accuracy",
    },
    "base.en": {
        "label": "Base (English)",
        "size": "142 MB",
        "bytes": 148_000_000,
        "note": "Fast, basic accuracy",
    },
    "small.en": {
        "label": "Small (English)",
        "size": "466 MB",
        "bytes": 488_000_000,
        "note": "Good balance for CPU",
    },
    "medium.en": {
        "label": "Medium (English)",
        "size": "1.5 GB",
        "bytes": 1_530_000_000,
        "note": "High accuracy, needs GPU",
    },
    "large-v3-turbo": {
        "label": "Large v3 Turbo",
        "size": "1.6 GB",
        "bytes": 1_620_000_000,
        "note": "Best speed/accuracy (recommended for GPU)",
    },
    "large-v3": {
        "label": "Large v3",
        "size": "3.1 GB",
        "bytes": 3_090_000_000,
        "note": "Maximum accuracy, slower",
    },
}

MODEL_DOWNLOAD_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main"


# ─── Status Types ────────────────────────────────────────────────

@dataclass
class DependencyStatus:
    """Result of checking a single dependency."""
    installed: bool
    detail: str = ""          # e.g. "v1.2.3" or path
    warning: str = ""         # Non-blocking issue
    error: str = ""           # Blocking issue description


@dataclass
class Dependency:
    """A single dependency the app needs."""
    id: str
    name: str
    description: str
    required: bool = True
    status: Optional[DependencyStatus] = None
    # These are set by get_dependencies()
    _check: Optional[Callable] = field(default=None, repr=False)
    _install: Optional[Callable] = field(default=None, repr=False)

    def check(self) -> DependencyStatus:
        if self._check:
            self.status = self._check()
        else:
            self.status = DependencyStatus(installed=False, error="No check available")
        return self.status

    @property
    def can_install(self) -> bool:
        return self._install is not None

    @property
    def is_ok(self) -> bool:
        return self.status is not None and self.status.installed


# ─── Check Functions ─────────────────────────────────────────────

def check_audio() -> DependencyStatus:
    """Check for a working audio input device."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        if inputs:
            default = sd.query_devices(kind="input")
            name = default.get("name", "Unknown") if default else inputs[0].get("name", "Unknown")
            return DependencyStatus(True, detail=f"{name} ({len(inputs)} input{'s' if len(inputs) != 1 else ''})")
        return DependencyStatus(False, error="No audio input devices found")
    except Exception as e:
        return DependencyStatus(False, error=f"Audio system error: {e}")


def check_ydotool() -> DependencyStatus:
    """Check for ydotool binary and running daemon."""
    binary = shutil.which("ydotool")
    if not binary:
        return DependencyStatus(False, error="ydotool not installed")

    # Check if daemon is running (socket exists)
    socket_paths = ["/run/ydotool/ydotool.sock", "/tmp/.ydotool_socket"]
    daemon_running = any(Path(p).exists() for p in socket_paths)

    if not daemon_running:
        # Check if ydotoold process is running
        try:
            result = subprocess.run(["pgrep", "-x", "ydotoold"], capture_output=True, timeout=5)
            daemon_running = result.returncode == 0
        except Exception:
            pass

    if daemon_running:
        return DependencyStatus(True, detail="ydotool + daemon running")
    return DependencyStatus(True, detail="ydotool installed", warning="ydotoold daemon not running")


def check_gpu_driver() -> DependencyStatus:
    """Detect GPU and check if drivers are working."""
    gpu_vendor = _detect_gpu_vendor()

    if gpu_vendor == "nvidia":
        try:
            result = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                info = result.stdout.strip().split("\n")[0]
                return DependencyStatus(True, detail=f"NVIDIA: {info}")
        except FileNotFoundError:
            return DependencyStatus(False, error="NVIDIA GPU detected but nvidia-smi not found. Install drivers.")
        except Exception as e:
            return DependencyStatus(False, error=f"NVIDIA driver check failed: {e}")

    elif gpu_vendor == "amd":
        # AMD uses amdgpu kernel driver
        try:
            result = subprocess.run(["lspci", "-k"], capture_output=True, text=True, timeout=5)
            if "amdgpu" in result.stdout:
                return DependencyStatus(True, detail="AMD GPU with amdgpu driver")
        except Exception:
            pass
        return DependencyStatus(True, detail="AMD GPU detected", warning="Could not verify driver")

    elif gpu_vendor == "intel":
        return DependencyStatus(True, detail="Intel GPU (integrated)", warning="CPU transcription recommended")

    return DependencyStatus(False, error="No GPU detected (CPU mode will be used)")


def check_cuda_toolkit() -> DependencyStatus:
    """Check if NVIDIA CUDA toolkit is installed (only relevant for NVIDIA GPUs)."""
    if _detect_gpu_vendor() != "nvidia":
        return DependencyStatus(True, detail="Not needed (non-NVIDIA GPU)")

    nvcc = shutil.which("nvcc")
    if nvcc:
        try:
            result = subprocess.run(["nvcc", "--version"], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split("\n"):
                if "release" in line.lower():
                    return DependencyStatus(True, detail=line.strip())
            return DependencyStatus(True, detail="CUDA toolkit installed")
        except Exception:
            return DependencyStatus(True, detail="nvcc found")

    # Check if CUDA libs exist even without nvcc
    cuda_paths = ["/usr/local/cuda", "/usr/lib/cuda"]
    for p in cuda_paths:
        if Path(p).exists():
            return DependencyStatus(True, detail=f"CUDA found at {p}", warning="nvcc not in PATH")

    return DependencyStatus(False, error="CUDA toolkit not installed (needed for GPU acceleration)")


def check_build_tools() -> DependencyStatus:
    """Check for git, cmake, and a C++ compiler."""
    missing = []
    found = []

    for tool in ["git", "cmake", "make"]:
        if shutil.which(tool):
            found.append(tool)
        else:
            missing.append(tool)

    # Check for C++ compiler (g++ or clang++)
    has_cpp = shutil.which("g++") or shutil.which("clang++")
    if has_cpp:
        found.append("C++ compiler")
    else:
        missing.append("C++ compiler")

    if not missing:
        return DependencyStatus(True, detail=f"All build tools present")
    return DependencyStatus(False, error=f"Missing: {', '.join(missing)}")


def check_whisper_cpp(config: dict) -> DependencyStatus:
    """Check if whisper.cpp binary exists."""
    binary_path = os.path.expanduser(config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli"))

    if Path(binary_path).exists():
        # Check if it has GPU support
        try:
            result = subprocess.run([binary_path, "--help"], capture_output=True, text=True, timeout=5)
            help_text = result.stdout + result.stderr
            has_gpu = "--no-gpu" in help_text or "-ngl" in help_text
            gpu_note = " (GPU enabled)" if has_gpu else " (CPU only)"
            return DependencyStatus(True, detail=f"{binary_path}{gpu_note}")
        except Exception:
            return DependencyStatus(True, detail=binary_path)

    # Check common paths
    alt_paths = [
        Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
        Path("/usr/local/bin/whisper-cli"),
        Path("/usr/bin/whisper-cli"),
    ]
    for alt in alt_paths:
        if alt.exists():
            return DependencyStatus(True, detail=str(alt), warning="Found at non-configured path")

    return DependencyStatus(False, error="whisper.cpp not built. Required for transcription.")


def check_whisper_model(config: dict) -> DependencyStatus:
    """Check if a Whisper model file exists."""
    model_path = os.path.expanduser(config.get("model_path", "~/whisper.cpp/models/ggml-large-v3-turbo.bin"))

    if Path(model_path).exists():
        size_mb = Path(model_path).stat().st_size / 1_000_000
        return DependencyStatus(True, detail=f"{Path(model_path).name} ({size_mb:.0f} MB)")

    # Check model directory for any models
    model_dir = Path.home() / "whisper.cpp" / "models"
    if model_dir.exists():
        models = list(model_dir.glob("ggml-*.bin"))
        if models:
            best = max(models, key=lambda p: p.stat().st_size)
            size_mb = best.stat().st_size / 1_000_000
            return DependencyStatus(True, detail=f"{best.name} ({size_mb:.0f} MB)",
                                    warning="Configured model not found, but others available")

    return DependencyStatus(False, error="No Whisper model downloaded")


# ─── Install Functions ───────────────────────────────────────────

LogCallback = Callable[[str], None]
DoneCallback = Callable[[bool, str], None]


def install_system_packages(
    log: LogCallback,
    done: DoneCallback,
    packages: Optional[list[str]] = None,
) -> None:
    """
    Install system packages via apt with pkexec for authentication.
    Runs in a background thread. Calls done(success, message) when finished.
    """
    if packages is None:
        # Determine what's needed
        packages = []
        if not shutil.which("ydotool"):
            packages.append("ydotool")
        if not shutil.which("git"):
            packages.append("git")
        if not shutil.which("cmake"):
            packages.append("cmake")
        if not shutil.which("make") or not (shutil.which("g++") or shutil.which("clang++")):
            packages.append("build-essential")
        if _detect_gpu_vendor() == "nvidia" and not shutil.which("nvcc"):
            packages.append("nvidia-cuda-toolkit")
        # libfuse2 needed for AppImage on Ubuntu 22.04+
        packages.append("libfuse2")

    if not packages:
        done(True, "All system packages already installed")
        return

    def _run():
        log(f"Installing: {', '.join(packages)}")
        log("(A password dialog may appear)")
        log("")

        try:
            # Single pkexec call for all apt packages
            cmd = ["pkexec", "bash", "-c",
                   f"apt update -qq && apt install -y {' '.join(packages)}"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            for line in iter(proc.stdout.readline, ""):
                log(line.rstrip())
            proc.wait()

            if proc.returncode == 0:
                # Enable ydotoold if we installed ydotool
                if "ydotool" in packages:
                    log("")
                    log("Enabling ydotool daemon...")
                    subprocess.run(
                        ["pkexec", "bash", "-c",
                         "systemctl enable --now ydotoold && "
                         f"usermod -aG input {os.environ.get('USER', 'user')}"],
                        capture_output=True, timeout=30,
                    )
                    log("  ydotoold enabled (re-login needed for input group)")

                done(True, "System packages installed successfully")
            elif proc.returncode == 126 or proc.returncode == 127:
                # pkexec cancelled or not found
                log("")
                log("Authentication cancelled or pkexec not available.")
                log("Run manually:")
                log(f"  sudo apt install -y {' '.join(packages)}")
                done(False, "Authentication cancelled")
            else:
                done(False, f"apt exited with code {proc.returncode}")

        except FileNotFoundError:
            log("Error: pkexec not found.")
            log("Install packages manually:")
            log(f"  sudo apt install -y {' '.join(packages)}")
            done(False, "pkexec not available")
        except Exception as e:
            log(f"Error: {e}")
            done(False, str(e))

    threading.Thread(target=_run, daemon=True).start()


def build_whisper_cpp(
    log: LogCallback,
    done: DoneCallback,
    use_cuda: bool = False,
    use_vulkan: bool = False,
) -> None:
    """
    Clone and build whisper.cpp from source. No sudo needed.
    Runs in a background thread.
    """
    whisper_dir = Path.home() / "whisper.cpp"
    binary_path = whisper_dir / "build" / "bin" / "whisper-cli"

    def _run():
        try:
            # Step 1: Clone if needed
            if not whisper_dir.exists():
                log("Cloning whisper.cpp...")
                _run_cmd(["git", "clone", "https://github.com/ggerganov/whisper.cpp.git",
                          str(whisper_dir)], log)
            elif (whisper_dir / ".git").exists():
                log("Updating whisper.cpp...")
                _run_cmd(["git", "-C", str(whisper_dir), "pull", "--ff-only"], log)
            else:
                log(f"Using existing {whisper_dir}")

            # Step 2: Configure with cmake
            log("")
            build_flags = []
            if use_cuda:
                build_flags.append("-DGGML_CUDA=ON")
                log("Configuring with CUDA (NVIDIA GPU)...")
            elif use_vulkan:
                build_flags.append("-DGGML_VULKAN=ON")
                log("Configuring with Vulkan (AMD/Intel GPU)...")
            else:
                log("Configuring for CPU...")

            cmake_cmd = ["cmake", "-B", "build"] + build_flags
            _run_cmd(cmake_cmd, log, cwd=str(whisper_dir))

            # Step 3: Build
            log("")
            nproc = os.cpu_count() or 4
            log(f"Building whisper.cpp (using {nproc} cores)...")
            log("This may take a few minutes...")
            _run_cmd(["cmake", "--build", "build", "--config", "Release",
                       "-j", str(nproc)], log, cwd=str(whisper_dir))

            # Verify
            if binary_path.exists():
                log("")
                log(f"whisper-cli built at: {binary_path}")
                done(True, str(binary_path))
            else:
                done(False, "Build completed but binary not found")

        except subprocess.CalledProcessError as e:
            log(f"Build failed (exit code {e.returncode})")
            done(False, f"Build failed: {e}")
        except Exception as e:
            log(f"Error: {e}")
            done(False, str(e))

    threading.Thread(target=_run, daemon=True).start()


def download_whisper_model(
    model_name: str,
    log: LogCallback,
    done: DoneCallback,
    progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Download a Whisper model from Hugging Face. No sudo needed.
    Runs in a background thread.

    Args:
        model_name: Model identifier (e.g. "large-v3-turbo")
        log: Called with status messages
        done: Called with (success, path_or_error) when finished
        progress: Called with (downloaded_bytes, total_bytes) during download
    """
    url = f"{MODEL_DOWNLOAD_BASE}/ggml-{model_name}.bin"
    model_dir = Path.home() / "whisper.cpp" / "models"
    target = model_dir / f"ggml-{model_name}.bin"

    def _run():
        try:
            model_dir.mkdir(parents=True, exist_ok=True)

            model_info = WHISPER_MODELS.get(model_name, {})
            size_label = model_info.get("size", "unknown size")
            log(f"Downloading ggml-{model_name}.bin ({size_label})...")
            log(f"From: {url}")
            log("")

            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()

            total = int(response.headers.get("content-length", 0))
            downloaded = 0

            # Use a temp file to avoid partial downloads
            tmp_target = target.with_suffix(".bin.part")
            with open(tmp_target, "wb") as f:
                for chunk in response.iter_content(chunk_size=1_048_576):  # 1MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
                    # Log every ~50MB
                    if total > 0 and downloaded % (50 * 1_048_576) < 1_048_576:
                        pct = downloaded * 100 // total
                        log(f"  {pct}% ({downloaded // 1_000_000} / {total // 1_000_000} MB)")

            # Rename from .part to final
            tmp_target.rename(target)

            log("")
            log(f"Model saved to: {target}")
            done(True, str(target))

        except Exception as e:
            log(f"Download failed: {e}")
            # Clean up partial download
            tmp = target.with_suffix(".bin.part")
            if tmp.exists():
                tmp.unlink()
            done(False, str(e))

    threading.Thread(target=_run, daemon=True).start()


# ─── Dependency List Builder ─────────────────────────────────────

def get_dependencies(config: dict) -> list[Dependency]:
    """
    Build the full dependency list with check/install functions bound.

    Args:
        config: App configuration dict (for path settings)

    Returns:
        List of Dependency objects ready to check()
    """
    gpu_vendor = _detect_gpu_vendor()
    needs_cuda = gpu_vendor == "nvidia"
    needs_vulkan = gpu_vendor == "amd"

    deps = [
        Dependency(
            id="audio",
            name="Audio Input",
            description="Microphone for voice recording",
            required=True,
            _check=check_audio,
            _install=None,  # Can't auto-install hardware
        ),
        Dependency(
            id="ydotool",
            name="Text Injection (ydotool)",
            description="Types transcribed text at your cursor",
            required=True,
            _check=check_ydotool,
            # install handled by install_system_packages
        ),
        Dependency(
            id="gpu_driver",
            name="GPU Driver",
            description="Graphics driver for hardware acceleration",
            required=False,
            _check=check_gpu_driver,
        ),
    ]

    if needs_cuda:
        deps.append(Dependency(
            id="cuda",
            name="CUDA Toolkit",
            description="NVIDIA GPU compute libraries for fast transcription",
            required=False,
            _check=check_cuda_toolkit,
            # install handled by install_system_packages
        ))

    deps.extend([
        Dependency(
            id="build_tools",
            name="Build Tools",
            description="git, cmake, C++ compiler for building whisper.cpp",
            required=True,
            _check=check_build_tools,
            # install handled by install_system_packages
        ),
        Dependency(
            id="whisper_cpp",
            name="Speech Engine (whisper.cpp)",
            description="Local speech-to-text engine with GPU acceleration",
            required=True,
            _check=lambda: check_whisper_cpp(config),
        ),
        Dependency(
            id="whisper_model",
            name="Whisper Model",
            description="AI model for speech recognition",
            required=True,
            _check=lambda: check_whisper_model(config),
        ),
    ])

    return deps


def get_recommended_model() -> str:
    """Return the best model name for this system's GPU."""
    vendor = _detect_gpu_vendor()
    if vendor in ("nvidia", "amd"):
        return "large-v3-turbo"
    return "small.en"


def get_missing_system_packages() -> list[str]:
    """Return list of apt package names that need installing."""
    packages = []
    if not shutil.which("ydotool"):
        packages.append("ydotool")
    if not shutil.which("git"):
        packages.append("git")
    if not shutil.which("cmake"):
        packages.append("cmake")
    if not shutil.which("make") or not (shutil.which("g++") or shutil.which("clang++")):
        packages.append("build-essential")
    if _detect_gpu_vendor() == "nvidia" and not shutil.which("nvcc"):
        packages.append("nvidia-cuda-toolkit")
    return packages


# ─── Helpers ─────────────────────────────────────────────────────

def _detect_gpu_vendor() -> str:
    """Quick GPU vendor detection. Returns 'nvidia', 'amd', 'intel', or 'unknown'."""
    # Fast check: nvidia-smi exists = NVIDIA
    if shutil.which("nvidia-smi"):
        return "nvidia"

    # Check /sys for vendor IDs
    drm_path = Path("/sys/class/drm")
    if drm_path.exists():
        vendor_map = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}
        for card in sorted(drm_path.iterdir()):
            if card.name.startswith("card") and card.name[4:].isdigit():
                vendor_file = card / "device" / "vendor"
                if vendor_file.exists():
                    vendor_id = vendor_file.read_text().strip()
                    vendor = vendor_map.get(vendor_id)
                    if vendor in ("nvidia", "amd"):
                        return vendor

    # Fallback: lspci
    try:
        result = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
        lower = result.stdout.lower()
        if "nvidia" in lower:
            return "nvidia"
        if "amd" in lower or "radeon" in lower:
            return "amd"
        if "intel" in lower:
            return "intel"
    except Exception:
        pass

    return "unknown"


def _run_cmd(cmd: list[str], log: LogCallback, cwd: Optional[str] = None) -> None:
    """Run a command, streaming output to log callback. Raises on failure."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
    )
    for line in iter(proc.stdout.readline, ""):
        log(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)

"""
First-run dependency detection and installation for Wayfinder Aura.

Checks for required external tools, offers automated installation,
and guides users through setup on first launch.

Supports Ubuntu/Debian (apt), with graceful fallback to manual instructions.
"""

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

from ..config import IS_APPIMAGE, IS_FLATPAK, APPDIR


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

# LLM models for post-processing (dictation cleanup)
LLM_MODELS: dict[str, dict] = {
    "google_gemma-3-1b-it-Q4_K_M": {
        "label": "Gemma 3 1B (Recommended)",
        "size": "~806 MB",
        "url": "https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF/resolve/main/google_gemma-3-1b-it-Q4_K_M.gguf",
        "filename": "google_gemma-3-1b-it-Q4_K_M.gguf",
        "note": "Most consistent gentle-guide cleanup across tones; smaller and faster than Qwen 3.5",
    },
    "Qwen3.5-2B-Q4_K_M": {
        "label": "Qwen 3.5 2B",
        "size": "~1.3 GB",
        "url": "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/main/Qwen3.5-2B-Q4_K_M.gguf",
        "filename": "Qwen3.5-2B-Q4_K_M.gguf",
        "note": "Capable reasoning model; less consistent than Gemma 3 for light cleanup",
    },
    "qwen2.5-1.5b-instruct-q4_k_m": {
        "label": "Qwen 2.5 1.5B (Legacy)",
        "size": "~1.0 GB",
        "url": "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "filename": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "note": "Previous default, still works well",
    },
}


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


def check_text_injection() -> DependencyStatus:
    """Check for an available text-injection backend (platform/session aware).

    Mirrors the injector's dispatch (wayfinder.utils.platform.get_text_injector) so the setup
    UI doesn't report injection broken when a working backend exists — or OK when it doesn't.
    Linux/Wayland prefers wtype (sandbox-safe, no uinput); X11 prefers xdotool; ydotool is the
    fallback and additionally needs its daemon + /dev/uinput (the old code wrongly treated a
    bundled ydotool binary as sufficient — Codex review / STEAMDECK-INSTALL-LOG Issue 4).
    """
    if sys.platform == "darwin":
        try:
            import pyautogui
            return DependencyStatus(True, detail="pyautogui (macOS)")
        except ImportError:
            return DependencyStatus(False, error="pyautogui not installed. Run: pip install pyautogui")

    # AppImage bundles ydotool at a fixed path that may not be on PATH.
    if IS_APPIMAGE and APPDIR and os.path.exists(os.path.join(APPDIR, "usr", "bin", "ydotool")):
        return DependencyStatus(True, detail="Bundled ydotool")

    from wayfinder.utils.platform import get_text_injector
    tool = get_text_injector()
    if tool == "xdotool":
        return DependencyStatus(True, detail="xdotool (X11)")
    if tool == "wtype":
        return DependencyStatus(True, detail="wtype (Wayland)")
    if tool == "ydotool":
        # ydotool needs its daemon + /dev/uinput — having the binary is not enough.
        socket_paths = ["/run/ydotool/ydotool.sock", "/tmp/.ydotool_socket",
                        f"/run/user/{os.getuid()}/.ydotool_socket"]
        daemon_running = any(Path(p).exists() for p in socket_paths)
        if not daemon_running:
            try:
                result = subprocess.run(["pgrep", "-x", "ydotoold"], capture_output=True, timeout=5)
                daemon_running = result.returncode == 0
            except Exception:
                pass
        if daemon_running:
            return DependencyStatus(True, detail="ydotool + daemon running")
        return DependencyStatus(True, detail="ydotool installed", warning="ydotoold daemon not running")
    return DependencyStatus(False, error="No text injection backend (install wtype or xdotool)")


# Keep old name for backwards compat (tests, etc.)
check_ydotool = check_text_injection


def install_pyautogui(log: 'LogCallback', done: 'DoneCallback') -> None:
    """Install pyautogui via pip (macOS text injection dependency)."""
    def _run():
        try:
            log("Installing pyautogui via pip...")
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "pyautogui"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            for line in iter(proc.stdout.readline, ""):
                log(line.rstrip())
            proc.wait()
            if proc.returncode == 0:
                done(True, "pyautogui installed successfully")
            else:
                done(False, f"pip install failed (exit {proc.returncode})")
        except Exception as e:
            log(f"Error: {e}")
            done(False, str(e))
    threading.Thread(target=_run, daemon=True).start()


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

    elif gpu_vendor == "apple":
        return DependencyStatus(True, detail="Apple Silicon (Metal GPU acceleration)")

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
    # In bundled environments, check for the bundled binary first
    if IS_APPIMAGE and APPDIR:
        bundled = os.path.join(APPDIR, "usr", "bin", "whisper-cli")
        if os.path.exists(bundled):
            return DependencyStatus(True, detail=f"Bundled whisper-cli")
    if IS_FLATPAK and os.path.exists("/app/bin/whisper-cli"):
        return DependencyStatus(True, detail=f"Bundled whisper-cli")

    binary_path = os.path.expanduser(config.get("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli"))

    if Path(binary_path).exists():
        try:
            result = subprocess.run([binary_path, "--help"], capture_output=True, text=True, timeout=5)
            help_text = result.stdout + result.stderr
            has_gpu = "--no-gpu" in help_text or "-ngl" in help_text
            gpu_note = " (GPU enabled)" if has_gpu else " (CPU only)"
            return DependencyStatus(True, detail=f"{binary_path}{gpu_note}")
        except Exception:
            return DependencyStatus(True, detail=binary_path)

    # Check common paths (including Homebrew ARM on macOS)
    alt_paths = [
        Path.home() / "whisper.cpp" / "build" / "bin" / "whisper-cli",
        Path("/opt/homebrew/bin/whisper-cli"),  # macOS ARM Homebrew
        Path("/usr/local/bin/whisper-cli"),     # macOS Intel Homebrew / Linux
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


def _detect_package_manager() -> str:
    """Detect the system package manager. Returns 'brew', 'dnf', 'apt', or 'unknown'."""
    if sys.platform == "darwin" and shutil.which("brew"):
        return "brew"
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("apt"):
        return "apt"
    if shutil.which("pacman"):
        return "pacman"
    return "unknown"


# Package name mapping: generic name -> {pkg_manager: actual_package_name}
_PACKAGE_MAP = {
    "ydotool":          {"apt": "ydotool",          "dnf": "ydotool",          "pacman": "ydotool"},
    "git":              {"apt": "git",              "dnf": "git",              "brew": "git",       "pacman": "git"},
    "cmake":            {"apt": "cmake",            "dnf": "cmake",            "brew": "cmake",     "pacman": "cmake"},
    "build-essential":  {"apt": "build-essential",  "dnf": "gcc-c++ make",     "brew": "gcc",       "pacman": "base-devel"},
    "nvidia-cuda-toolkit": {"apt": "nvidia-cuda-toolkit", "dnf": "cuda-toolkit", "pacman": "cuda"},
    "libfuse2":         {"apt": "libfuse2",         "dnf": "fuse-libs",        "pacman": "fuse2"},
    "whisper-cpp":      {"brew": "whisper-cpp"},
}


def _get_install_hint(generic_name: str) -> str:
    """One-line, package-manager-appropriate install hint for a single dependency.

    The hardcoded 'sudo dnf install …' strings were Fedora-only — wrong on Arch/SteamOS
    (the larger Deck user base) and actively caused failed installs because users ran the
    printed command and got 'dnf: command not found' (STEAMDECK-INSTALL-LOG Issue 4 +
    the 2026-05-08 post-mortem). Detect the manager and emit the right command.
    """
    pkg_mgr = _detect_package_manager()
    resolved = _resolve_packages([generic_name], pkg_mgr)
    pkg_str = " ".join(resolved) if resolved else generic_name
    if pkg_mgr == "brew":
        return f"brew install {pkg_str}"
    if pkg_mgr == "pacman":
        return f"sudo pacman -S {pkg_str}   (on SteamOS run 'sudo steamos-readonly disable' first)"
    if pkg_mgr == "dnf":
        return f"sudo dnf install {pkg_str}"
    if pkg_mgr == "apt":
        return f"sudo apt install {pkg_str}"
    return f"install '{pkg_str}' with your system package manager"


def _resolve_packages(generic_names: list[str], pkg_mgr: str) -> list[str]:
    """Map generic package names to the actual names for the detected package manager."""
    resolved = []
    for name in generic_names:
        mapped = _PACKAGE_MAP.get(name, {}).get(pkg_mgr, name)
        # Some mappings have multiple packages (e.g., "gcc-c++ make")
        resolved.extend(mapped.split())
    return resolved


def install_system_packages(
    log: LogCallback,
    done: DoneCallback,
    packages: Optional[list[str]] = None,
) -> None:
    """
    Install system packages using the detected package manager (apt or dnf).
    Runs in a background thread. Calls done(success, message) when finished.
    """
    pkg_mgr = _detect_package_manager()

    if packages is None:
        # Determine what's needed (generic names)
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

    if not packages:
        done(True, "All system packages already installed")
        return

    # Map to actual package names for this distro
    resolved = _resolve_packages(packages, pkg_mgr)

    def _run():
        log(f"Installing: {', '.join(resolved)}")
        log(f"(Using {pkg_mgr} — a password dialog may appear)")
        log("")

        if pkg_mgr == "unknown":
            log("Could not detect package manager.")
            log("Install these manually:")
            log(f"  {', '.join(resolved)}")
            done(False, "Unknown package manager")
            return

        try:
            if pkg_mgr == "brew":
                # macOS: Homebrew doesn't need sudo
                install_cmd = f"brew install {' '.join(resolved)}"
                cmd = ["bash", "-c", install_cmd]
            elif pkg_mgr == "dnf":
                install_cmd = f"dnf install -y {' '.join(resolved)}"
                cmd = ["pkexec", "bash", "-c", install_cmd]
            elif pkg_mgr == "pacman":
                # SteamOS/Arch. NB: SteamOS also needs `steamos-readonly disable` + an
                # initialized keyring first — most Deck users should use the Flatpak.
                install_cmd = f"pacman -S --needed --noconfirm {' '.join(resolved)}"
                cmd = ["pkexec", "bash", "-c", install_cmd]
            else:  # apt
                install_cmd = f"apt update -qq && apt install -y {' '.join(resolved)}"
                cmd = ["pkexec", "bash", "-c", install_cmd]

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            for line in iter(proc.stdout.readline, ""):
                log(line.rstrip())
            proc.wait()

            if proc.returncode == 0:
                # Linux-only: enable ydotool daemon after install
                if "ydotool" in packages and sys.platform != "darwin":
                    log("")
                    log("Enabling ydotool daemon...")
                    user = os.environ.get('USER', 'user')
                    if pkg_mgr == "pacman":
                        # Arch/SteamOS ship a USER-level unit named ydotool.service (no sudo),
                        # NOT the system 'ydotoold' (STEAMDECK-INSTALL-LOG Issue 4). The input
                        # group needs a full logout to take effect — Issue 10.
                        subprocess.run(["systemctl", "--user", "enable", "--now", "ydotool.service"],
                                       capture_output=True, timeout=30)
                        subprocess.run(["pkexec", "bash", "-c", f"usermod -aG input {user}"],
                                       capture_output=True, timeout=30)
                        log("  ydotool.service (user) enabled; FULL logout needed for input group")
                    else:
                        subprocess.run(
                            ["pkexec", "bash", "-c",
                             "systemctl enable --now ydotoold && "
                             f"usermod -aG input {user}"],
                            capture_output=True, timeout=30,
                        )
                        log("  ydotoold enabled (re-login needed for input group)")

                done(True, "System packages installed successfully")
            elif proc.returncode in (126, 127):
                log("")
                log("No worries — you can install these yourself instead.")
                log("Open a terminal and run:")
                if pkg_mgr == "brew":
                    log(f"  {install_cmd}")
                else:
                    log(f"  sudo {install_cmd}")
                log("")
                log("Then restart Wayfinder Aura.")
                done(False, "Manual install needed")
            else:
                log(f"Package manager returned exit code {proc.returncode}.")
                log("You can install manually:")
                if pkg_mgr == "brew":
                    log(f"  {install_cmd}")
                else:
                    log(f"  sudo {install_cmd}")
                done(False, f"{pkg_mgr} exited with code {proc.returncode}")

        except FileNotFoundError:
            log("Automatic install is not available on this system.")
            log("Install packages manually in a terminal:")
            if pkg_mgr == "brew":
                log(f"  brew install {' '.join(resolved)}")
            elif pkg_mgr == "dnf":
                log(f"  sudo dnf install -y {' '.join(resolved)}")
            else:
                log(f"  sudo apt install -y {' '.join(resolved)}")
            done(False, "Manual install needed")
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


def download_llm_model(
    model_key: str,
    log: Callable[[str], None],
    done: Callable[[bool, str], None],
    progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """
    Download an LLM model (GGUF) from Hugging Face for post-processing.

    Args:
        model_key: Key into LLM_MODELS (e.g. "Qwen3.5-2B-Q4_K_M")
        log: Called with status messages
        done: Called with (success, path_or_error) when finished
        progress: Called with (downloaded_bytes, total_bytes) during download
    """
    model_info = LLM_MODELS.get(model_key)
    if not model_info:
        done(False, f"Unknown LLM model: {model_key}")
        return

    url = model_info["url"]
    filename = model_info["filename"]
    model_dir = Path.home() / ".local" / "share" / "wayfinder-aura" / "llm-models"
    target = model_dir / filename

    def _run():
        try:
            model_dir.mkdir(parents=True, exist_ok=True)

            size_label = model_info.get("size", "unknown size")
            log(f"Downloading {filename} ({size_label})...")
            log(f"From: {url}")
            log("")

            response = requests.get(url, stream=True, timeout=30)
            response.raise_for_status()

            total = int(response.headers.get("content-length", 0))
            downloaded = 0

            tmp_target = target.with_suffix(".gguf.part")
            with open(tmp_target, "wb") as f:
                for chunk in response.iter_content(chunk_size=1_048_576):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)
                    if total > 0 and downloaded % (50 * 1_048_576) < 1_048_576:
                        pct = downloaded * 100 // total
                        log(f"  {pct}% ({downloaded // 1_000_000} / {total // 1_000_000} MB)")

            tmp_target.rename(target)

            log("")
            log(f"Model saved to: {target}")
            done(True, str(target))

        except Exception as e:
            log(f"Download failed: {e}")
            tmp = target.with_suffix(".gguf.part")
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
            name="Text Injection (pyautogui)" if sys.platform == "darwin" else "Text Injection (wtype/xdotool/ydotool)",
            description="Types transcribed text at your cursor",
            required=True,
            _check=check_text_injection,
            _install=install_pyautogui if sys.platform == "darwin" else None,
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

    # Build tools (git/cmake/compiler) are only needed to compile whisper.cpp from source.
    # Bundled environments (AppImage/Flatpak) ship a pre-built binary, and once whisper.cpp is
    # already built there is nothing left to compile — so don't surface a Build Tools entry at
    # all (no "Missing: cmake" row, no "Install" button). That button runs `dnf install`, which
    # is blocked on immutable distros (Bazzite/SteamOS use rpm-ostree) and can never succeed
    # there, so it's pure noise on a working install.
    if not (IS_APPIMAGE or IS_FLATPAK) and not check_whisper_cpp(config).installed:
        deps.append(Dependency(
            id="build_tools",
            name="Build Tools",
            description="git, cmake, C++ compiler for building whisper.cpp",
            required=True,
            _check=check_build_tools,
        ))

    deps.extend([
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


def is_steam_deck() -> bool:
    """True on Steam Deck hardware (LCD 'Jupiter' or OLED 'Galileo').

    Kept separate from _detect_gpu_vendor() so its callers that branch on vendor values
    ('amd', etc.) are unaffected (Codex review). Used only to pick a default model.
    """
    try:
        product = Path("/sys/class/dmi/id/product_name").read_text().strip().lower()
        return "jupiter" in product or "galileo" in product
    except Exception:
        return False


def get_recommended_model() -> str:
    """Return the best whisper model name for this system's hardware class."""
    # Steam Deck (Zen 2 APU): large-v3-turbo runs ~10x slower than real-time and is unusable
    # for live dictation — default to base.en (STEAMDECK-INSTALL-LOG Issues 11/17).
    if is_steam_deck():
        return "base.en"
    vendor = _detect_gpu_vendor()
    if vendor in ("nvidia", "amd", "apple"):
        return "large-v3-turbo"
    return "small.en"


def get_missing_system_packages() -> list[str]:
    """Return list of generic package names that need installing.
    
    Returns generic names (e.g., 'build-essential') which get mapped
    to the actual package manager names during installation.
    """
    packages = []
    # ydotool is Linux-only; macOS uses pyautogui (pip package, not system)
    if sys.platform != "darwin" and not shutil.which("ydotool"):
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
    """Quick GPU vendor detection.

    Returns 'nvidia', 'amd', 'intel', 'apple' (Metal), or 'unknown'.
    """
    import platform as plat

    # macOS: Apple Silicon has Metal GPU, Intel Macs also support Metal
    if sys.platform == "darwin":
        machine = plat.machine().lower()
        if machine in ("arm64", "aarch64"):
            return "apple"  # Apple Silicon — Metal GPU
        return "intel"  # Intel Mac — still supports Metal

    # Linux: Fast check: nvidia-smi exists = NVIDIA
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

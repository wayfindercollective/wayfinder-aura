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
from ..utils.hostexec import host_env


# ─── Model Catalog ───────────────────────────────────────────────

WHISPER_MODELS: dict[str, dict] = {
    "tiny.en": {
        "label": "Tiny (English)",
        "size": "78 MB",
        "bytes": 77_704_715,
        "sha256": "921e4cf8686fdd993dcd081a5da5b6c365bfde1162e72b08d75ac75289920b1f",
        "note": "Fastest, lowest accuracy",
    },
    "base.en": {
        "label": "Base (English)",
        "size": "148 MB",
        "bytes": 147_964_211,
        "sha256": "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002",
        "note": "Fast, basic accuracy",
    },
    "small.en": {
        "label": "Small (English)",
        "size": "488 MB",
        "bytes": 487_614_201,
        "sha256": "c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d",
        "note": "Good balance for CPU",
    },
    "medium.en": {
        "label": "Medium (English)",
        "size": "1.5 GB",
        "bytes": 1_533_774_781,
        "sha256": "cc37e93478338ec7700281a7ac30a10128929eb8f427dda2e865faa8f6da4356",
        "note": "High accuracy, needs GPU",
    },
    "large-v3-turbo": {
        "label": "Large v3 Turbo",
        "size": "1.6 GB",
        "bytes": 1_624_555_275,
        "sha256": "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        "note": "Best speed/accuracy (recommended for GPU)",
    },
    "large-v3": {
        "label": "Large v3",
        "size": "3.1 GB",
        "bytes": 3_095_033_483,
        "sha256": "64d182b440b98d5203c4f9bd541544d84c605196c4f7b845dfa11fb23594d1e2",
        "note": "Maximum accuracy, slower",
    },
}

# Pinned revision of ggerganov/whisper.cpp. `main` is mutable: an upstream
# re-upload or a repo compromise would silently change the weights a
# first-run Setup downloads (security audit 2026-08-17, F-C). Bump this
# together with the per-model sha256 values above.
MODEL_DOWNLOAD_REVISION = "5359861c739e955e79d9a303bcbc70fb988958b1"
MODEL_DOWNLOAD_BASE = (
    f"https://huggingface.co/ggerganov/whisper.cpp/resolve/{MODEL_DOWNLOAD_REVISION}"
)

# LLM models for post-processing (dictation cleanup)
LLM_MODELS: dict[str, dict] = {
    "google_gemma-3-1b-it-Q4_K_M": {
        "label": "Gemma 3 1B (Recommended)",
        "size": "806 MB",
        "url": "https://huggingface.co/bartowski/google_gemma-3-1b-it-GGUF/resolve/116f76234503685a98f572982177b11d44ec8ff1/google_gemma-3-1b-it-Q4_K_M.gguf",
        "bytes": 806_058_496,
        "filename": "google_gemma-3-1b-it-Q4_K_M.gguf",
        "sha256": "12bf0fff8815d5f73a3c9b586bd8fee8e7b248c935de70dec367679873d0f29d",
        "note": "Most consistent gentle-guide cleanup across tones; smaller and faster than Qwen 3.5",
    },
    "Qwen3.5-2B-Q4_K_M": {
        "label": "Qwen 3.5 2B",
        "size": "1.3 GB",
        "url": "https://huggingface.co/unsloth/Qwen3.5-2B-GGUF/resolve/f6d5376be1edb4d416d56da11e5397a961aca8ae/Qwen3.5-2B-Q4_K_M.gguf",
        "bytes": 1_280_835_840,
        "filename": "Qwen3.5-2B-Q4_K_M.gguf",
        "sha256": "aaf42c8b7c3cab2bf3d69c355048d4a0ee9973d48f16c731c0520ee914699223",
        "note": "Capable reasoning model; less consistent than Gemma 3 for light cleanup",
    },
    "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M": {
        "label": "Qwen3 4B Instruct 2507 (Strong/Caricature)",
        "size": "2.5 GB",
        "url": "https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/ae44f08e1392f39c0e474af10c3ff8355c8b6688/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "bytes": 2_497_280_736,
        "filename": "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "sha256": "2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e",
        "note": "Best local model for Strong & Caricature intensity; sharpest instruction follower at 4B",
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
    blocking_warning: bool = False  # warning that still blocks the "All set!" gate


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

    if sys.platform == "win32":
        # Native Win32 SendInput injection needs no external tool or daemon.
        return DependencyStatus(True, detail="Windows native (SendInput)")

    # NOTE: a bundled ydotool CLIENT is deliberately NOT treated as sufficient —
    # it must protocol-match a running host daemon, and without one it silently
    # types into a throwaway uinput device nothing receives (2026-07 field bug).

    from wayfinder.utils.platform import get_text_injector, is_wayland
    tool = get_text_injector()
    if tool == "xdotool":
        return DependencyStatus(True, detail="xdotool (X11)")
    if tool == "wtype":
        # wtype works only while the compositor honors the virtual-keyboard
        # protocol (GNOME never does; KWin can revoke it mid-session). When we
        # can self-provision the robust backend (host ydotoold + user-writable
        # /dev/uinput + systemd --user), report not-ready so the Setup wizard
        # auto-installs the user service — zero-terminal typing that keeps
        # working when wtype doesn't.
        if is_wayland() and can_self_provision_ydotoold():
            return DependencyStatus(
                False,
                error="Installing background typing service (one-click, no password)",
            )
        # Honest status: with wtype as the ONLY backend there is no automatic
        # desktop-mode fallback if the compositor refuses it (GNOME always
        # does; KWin can revoke mid-session) — say so instead of promising a
        # clipboard path that only Game Mode has (Codex review).
        return DependencyStatus(True, detail="wtype (Wayland)",
                                warning="wtype only — some desktops refuse it; "
                                        "install ydotool for reliable typing")
    if tool == "ydotool":
        # ydotool needs its daemon + /dev/uinput — having the binary is not
        # enough. Delegate to check_ydotool_ready: it also rejects STALE socket
        # files from a dead daemon, which a bare exists() check reported as
        # "running" while every connect got refused.
        from wayfinder.core.injector import check_ydotool_ready
        daemon_running = check_ydotool_ready()[0]
        if daemon_running:
            return DependencyStatus(True, detail="ydotool + daemon running")
        # Daemon down -> injection will actually fail. If we can self-provision
        # the user service, report not-ready so the wizard installs it hands-free.
        if can_self_provision_ydotoold():
            return DependencyStatus(
                False,
                error="Installing background typing service (one-click, no password)",
            )
        # Keep the gold warning badge, but flag it blocking so Setup won't claim
        # "All set!" while typing is broken (Codex review F11).
        return DependencyStatus(True, detail="ydotool installed",
                                warning="ydotoold daemon not running", blocking_warning=True)
    return DependencyStatus(False, error="No text injection backend (install wtype or xdotool)")


# Keep old name for backwards compat (tests, etc.)
check_ydotool = check_text_injection


def _find_host_ydotoold() -> Optional[str]:
    """Host ydotoold daemon binary — PATH walk skipping AppImage bundle dirs."""
    appdir = os.environ.get("APPDIR", "")
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or (appdir and d.startswith(appdir)):
            continue
        cand = os.path.join(d, "ydotoold")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def can_self_provision_ydotoold() -> bool:
    """True when the user-level ydotoold service can be installed with NO root.

    Needs: a host ydotoold binary, user-writable /dev/uinput (Bazzite/Fedora
    grant this via ACL), and systemd --user. When all three hold, Setup can
    make typing robust with zero terminal work.
    """
    if not sys.platform.startswith("linux"):
        return False
    try:
        return bool(
            _find_host_ydotoold()
            and os.access("/dev/uinput", os.W_OK)
            and shutil.which("systemctl")
        )
    except OSError:
        return False


def install_ydotoold_user_service(log: 'LogCallback', done: 'DoneCallback') -> None:
    """Self-provision a USER-level ydotoold service — the zero-terminal typing fix.

    Writes a 5-line systemd user unit pointing at the HOST ydotoold (client and
    daemon must protocol-match; see injector._get_ydotool_binary), enables it,
    waits for the socket, and verifies delivery with a harmless Shift
    press+release (types nothing). No root, no password: /dev/uinput access
    comes from the distro's ACL (verified by can_self_provision_ydotoold).
    """
    def _run():
        try:
            ydotoold = _find_host_ydotoold()
            if not ydotoold:
                done(False, "No ydotoold on this system — typing will rely on wtype "
                            "(install ydotool for the robust backend)")
                return
            if not os.access("/dev/uinput", os.W_OK):
                done(False, "No /dev/uinput permission. Run: sudo systemctl enable --now ydotoold")
                return
            unit_dir = Path(os.path.expanduser("~/.config/systemd/user"))
            unit_dir.mkdir(parents=True, exist_ok=True)
            sock = Path(f"/run/user/{os.getuid()}/.ydotool_socket")

            # Respect an EXISTING user ydotoold.service (theirs, not ours):
            # never overwrite it — try starting it first; only if that fails do
            # we install our own APP-OWNED unit under a distinct name, which is
            # always safe to (re)write (Codex review). NOTE: no socket cleanup
            # before this — the unit may already be ACTIVE (enable --now won't
            # restart it) and unlinking its live socket would orphan it; it may
            # also use a different socket path entirely.
            existing = unit_dir / "ydotoold.service"
            if existing.exists():
                log("Found an existing user ydotoold.service — starting it…")
                r = subprocess.run(
                    ["systemctl", "--user", "enable", "--now", "ydotoold.service"],
                    capture_output=True, text=True, timeout=15, env=host_env(),
                )
                if r.returncode == 0:
                    unit_name = "ydotoold.service"
                else:
                    log("Existing unit failed to start — installing Wayfinder's own unit…")
                    unit_name = "wayfinder-ydotoold.service"
            else:
                unit_name = "wayfinder-ydotoold.service"

            if unit_name == "wayfinder-ydotoold.service":
                log("Installing user-level ydotoold service (no root needed)…")
                # OUR unit binds the user socket path below: clear a STALE file
                # there first (a dead daemon's leftover makes existence checks
                # lie and connects fail with "Connection refused").
                try:
                    sock.unlink()
                except OSError:
                    pass
                (unit_dir / unit_name).write_text(
                    "[Unit]\n"
                    "Description=ydotool daemon (user) — virtual input for Wayfinder Aura dictation\n"
                    "Conflicts=ydotoold.service\n\n"
                    "[Service]\n"
                    f"ExecStart={ydotoold} --socket-path=%t/.ydotool_socket --socket-own=%U:%G\n"
                    "Restart=on-failure\n\n"
                    "[Install]\n"
                    "WantedBy=default.target\n"
                )
                for cmd in (["systemctl", "--user", "daemon-reload"],
                            ["systemctl", "--user", "enable", "--now", unit_name]):
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                       env=host_env())
                    if r.returncode != 0:
                        detail = r.stderr.strip() or r.stdout.strip() or "(no output)"
                        done(False, f"{' '.join(cmd)} failed: {detail}")
                        return
            # Delivery check with retries: verify by CONNECTING, not by socket-file
            # existence (a file can exist before the daemon binds it). Shift
            # press+release exercises the daemon path and types nothing; the
            # injector checker also rejects the silent "backend unavailable"
            # direct-uinput fallback. The socket is FORCED to the one OUR
            # daemon owns — _get_ydotool_env's priority order could otherwise
            # select an inherited/system socket and verify the wrong daemon
            # (Codex review).
            log("Verifying typing backend…")
            import time as _time
            from wayfinder.core.injector import (
                InjectionError, _check_ydotool_result, _get_ydotool_binary,
                _get_ydotool_env, _probe_unix_socket,
            )
            ydotool_client = _get_ydotool_binary()
            if not ydotool_client:
                done(False, "ydotoold installed, but no host ydotool client found to verify with")
                return
            last_err = "ydotoold started but never became reachable"
            for _ in range(20):
                if unit_name == "wayfinder-ydotoold.service":
                    # OUR daemon owns the user socket: verify exactly that one —
                    # env priority could otherwise select an inherited/system
                    # socket and verify the wrong daemon (Codex review).
                    if not _probe_unix_socket(str(sock)):
                        _time.sleep(0.25)
                        continue
                    verify_env = _get_ydotool_env()
                    verify_env["YDOTOOL_SOCKET"] = str(sock)
                else:
                    # Pre-existing unit: it may bind ANY socket path. Selection
                    # is connectable-aware, so wait for whichever socket its
                    # daemon actually serves.
                    verify_env = _get_ydotool_env()
                    vsock = verify_env.get("YDOTOOL_SOCKET")
                    if not vsock or not _probe_unix_socket(vsock):
                        _time.sleep(0.25)
                        continue
                r = subprocess.run(
                    [ydotool_client, "key", "42:1", "42:0"],
                    capture_output=True, text=True, timeout=10, env=verify_env,
                )
                try:
                    _check_ydotool_result(r, "verify")
                    done(True, "Background typing service installed and verified")
                    return
                except InjectionError as e:
                    last_err = str(e)
                    _time.sleep(0.25)
            done(False, last_err)
        except Exception as e:
            done(False, str(e))
    threading.Thread(target=_run, daemon=True).start()


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
    if sys.platform == "win32":
        # Windows provisions whisper.cpp from a prebuilt binary download
        # (build_whisper_cpp), so no local C toolchain is required.
        return DependencyStatus(True, detail="Prebuilt binary (no toolchain needed)")
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
    """Check if a USABLE Whisper model exists — on disk AND permitted by license.

    A weight only satisfies the dependency if the current license lets the
    transcriber load it. A license-blind check saw a leftover Ultra weight,
    reported the model "installed", skipped the wizard's download step — and
    the user finished Setup with a model dictation then refused: recordings
    silently produced nothing.
    """
    def _usable(path: str) -> bool:
        try:
            from wayfinder.license import get_feature_gate, transcription_model_allowed
            return transcription_model_allowed(path, get_feature_gate())
        except Exception:
            return Path(path).name.lower() in ("ggml-base.bin", "ggml-base.en.bin")

    model_path = os.path.expanduser(config.get("model_path", "~/whisper.cpp/models/ggml-base.en.bin"))

    if Path(model_path).exists() and _usable(model_path):
        size_mb = Path(model_path).stat().st_size / 1_000_000
        return DependencyStatus(True, detail=f"{Path(model_path).name} ({size_mb:.0f} MB)")

    # Check model directory for any usable models
    model_dir = Path.home() / "whisper.cpp" / "models"
    if model_dir.exists():
        models = [p for p in model_dir.glob("ggml-*.bin") if _usable(str(p))]
        if models:
            best = max(models, key=lambda p: p.stat().st_size)
            size_mb = best.stat().st_size / 1_000_000
            return DependencyStatus(True, detail=f"{best.name} ({size_mb:.0f} MB)",
                                    warning="Configured model not found, but others available")

    return DependencyStatus(False, error="No usable Whisper model — download the Free Base model")


# ─── Install Functions ───────────────────────────────────────────

LogCallback = Callable[[str], None]
DoneCallback = Callable[[bool, str], None]


def _is_atomic_host() -> bool:
    """True on image-based Fedora (Bazzite/Silverblue/Kinoite/SteamOS-like ostree).

    These hosts ship a `dnf` shim that refuses to install ("Fedora Atomic images
    utilize rpm-ostree instead") — running it just paints a red error wall in
    Setup. /run/ostree-booted is the canonical runtime marker.
    """
    return Path("/run/ostree-booted").exists()


def _detect_package_manager() -> str:
    """Detect the system package manager. Returns 'brew', 'ostree', 'dnf', 'apt', or 'unknown'."""
    if sys.platform == "darwin" and shutil.which("brew"):
        return "brew"
    # Must precede the dnf check: Atomic hosts have a dnf binary that refuses to run.
    if _is_atomic_host():
        return "ostree"
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
    if pkg_mgr == "ostree":
        return f"rpm-ostree install {pkg_str}   (immutable Fedora — layers the package, reboot required)"
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
        # Single source of truth for what's needed (bundle-aware, Atomic-aware).
        packages = get_missing_system_packages()

    if not packages:
        done(True, "All system packages already installed")
        return

    # Map to actual package names for this distro
    resolved = _resolve_packages(packages, pkg_mgr)

    def _run():
        if pkg_mgr == "ostree":
            # Never invoke dnf here — Atomic's dnf shim refuses with an error wall
            # (observed on Bazzite: "Fedora Atomic images utilize rpm-ostree instead").
            log("Immutable Fedora detected (Bazzite/Silverblue) — system packages are")
            log("managed by rpm-ostree and can't be installed live from Setup.")
            log("")
            log("These tools are optional here. To add them anyway:")
            log(f"  rpm-ostree install {' '.join(resolved)}")
            log("(layers the packages; takes effect after a reboot)")
            done(False, "Immutable OS — skipped automatic install (see log)")
            return

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

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=host_env())

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
                                       capture_output=True, timeout=30, env=host_env())
                        subprocess.run(["pkexec", "bash", "-c", f"usermod -aG input {user}"],
                                       capture_output=True, timeout=30, env=host_env())
                        log("  ydotool.service (user) enabled; FULL logout needed for input group")
                    else:
                        subprocess.run(
                            ["pkexec", "bash", "-c",
                             "systemctl enable --now ydotoold && "
                             f"usermod -aG input {user}"],
                            capture_output=True, timeout=30, env=host_env(),
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


# Pinned prebuilt whisper.cpp Windows CPU build (no toolchain needed on Windows).
# Bump together with any whisper-cli flag/model-format change the app relies on.
WHISPER_WINDOWS_BUILD = "b4938"
WHISPER_WINDOWS_URL = (
    "https://github.com/ggml-org/whisper.cpp/releases/download/"
    f"{WHISPER_WINDOWS_BUILD}/whisper-bin-x64.zip"
)


def _download_whisper_binary_windows(log: LogCallback, done: DoneCallback) -> None:
    """Windows whisper provisioning: fetch the prebuilt CPU binary, no compiler.

    Extracts whisper-cli.exe and its DLLs to ~/whisper.cpp/build/bin/ — the same
    path the config resolves to — so the setup wizard's whisper step works on a
    Windows box without git/cmake/a C++ toolchain. Runs in a background thread.
    """
    import io
    import threading
    import zipfile

    dest = Path.home() / "whisper.cpp" / "build" / "bin"
    binary_path = dest / "whisper-cli.exe"

    def _run():
        if binary_path.exists():
            log(f"whisper-cli already present at {binary_path}")
            done(True, str(binary_path))
            return
        try:
            import requests
            dest.mkdir(parents=True, exist_ok=True)
            log(f"Downloading prebuilt whisper.cpp ({WHISPER_WINDOWS_BUILD}) for Windows…")
            resp = requests.get(WHISPER_WINDOWS_URL, timeout=180)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                for member in zf.namelist():
                    name = os.path.basename(member)
                    # whisper-cli.exe plus every DLL it loads; skip the other tools.
                    if name == "whisper-cli.exe" or name.endswith(".dll"):
                        with zf.open(member) as src, open(dest / name, "wb") as out:
                            shutil.copyfileobj(src, out)
            if binary_path.exists():
                log(f"whisper-cli installed at: {binary_path}")
                done(True, str(binary_path))
            else:
                done(False, "whisper-cli.exe not found in the downloaded archive")
        except Exception as e:
            done(False, f"Windows whisper download failed: {e}")

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
    if sys.platform == "win32":
        # No default C toolchain on Windows — fetch the prebuilt binary instead
        # of building. Same result path (~/whisper.cpp/build/bin/whisper-cli.exe).
        _download_whisper_binary_windows(log, done)
        return

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
        model_info = WHISPER_MODELS.get(model_name, {})
        size_label = model_info.get("size", "unknown size")
        _download_model_file(url, target, ".bin.part", size_label, log, done, progress,
                             sha256=model_info.get("sha256"),
                             expected_bytes=model_info.get("bytes"))

    threading.Thread(target=_run, daemon=True).start()


def _download_model_file(
    url: str,
    target: Path,
    part_suffix: str,
    size_label: str,
    log: Callable[[str], None],
    done: Callable[[bool, str], None],
    progress: Optional[Callable[[int, int], None]] = None,
    min_bytes: int = 10_000_000,
    sha256: Optional[str] = None,
    expected_bytes: Optional[int] = None,
) -> None:
    """Shared model downloader: atomic .part file, integrity checks, clear errors.

    Integrity: the byte count must match content-length (a CDN/proxy can serve a
    short 200 body without raising), and the file must clear min_bytes so an HTML
    error page never gets renamed into a "model" that fails hours later with a
    cryptic load error. When ``sha256`` is given, the content must also match it
    before the file is renamed into place — size checks alone cannot tell a
    swapped model from the real one (security audit 2026-08-17, F-C).
    """
    tmp_target = target.with_suffix(part_suffix)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)

        log(f"Downloading {target.name} ({size_label})...")
        log(f"From: {url}")
        log("")

        from wayfinder.core.download_guard import (
            DownloadBounds,
            DownloadSizeMismatch,
            https_only_get,
        )

        def _attempt():
            """One bounded, https-only transfer into tmp_target."""
            bounds = DownloadBounds(expected_bytes)
            # Pass the module, not a Session: this path has no auth headers to
            # pool and `requests.get` stays the single seam the setup tests patch.
            response = https_only_get(requests, url, timeout=30)
            if response.status_code == 429:
                return None, None  # caller reports the rate-limit message
            response.raise_for_status()

            total = bounds.check_header(int(response.headers.get("content-length", 0))) or 0
            downloaded = 0

            with open(tmp_target, "wb") as f:
                for chunk in response.iter_content(chunk_size=1_048_576):  # 1MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    bounds.check_progress(downloaded)
                    if progress:
                        progress(downloaded, total)
                    # Log every ~50MB
                    if total > 0 and downloaded % (50 * 1_048_576) < 1_048_576:
                        pct = downloaded * 100 // total
                        log(f"  {pct}% ({downloaded // 1_000_000} / {total // 1_000_000} MB)")

            bounds.check_complete(downloaded)
            return total, downloaded

        # One retry, for transport failures only — a dropped connection part-way
        # through a 3 GB model is the common case and the user should not have to
        # start over by hand. A digest mismatch is handled below and never
        # retried: identical bytes would come back.
        try:
            total, downloaded = _attempt()
        except (DownloadSizeMismatch, requests.RequestException, OSError) as first:
            log(f"Download interrupted ({first}); retrying once...")
            try:
                tmp_target.unlink(missing_ok=True)
            except OSError:
                pass
            total, downloaded = _attempt()

        if total is None:
            done(False, "Hugging Face is rate-limiting downloads — try again in a few minutes")
            return

        if total > 0 and downloaded != total:
            raise IOError(
                f"incomplete download ({downloaded // 1_000_000} of {total // 1_000_000} MB) — "
                "connection dropped, please retry"
            )
        if downloaded < min_bytes:
            raise IOError(
                f"downloaded file is too small ({downloaded} bytes) — "
                "the server returned an error page instead of the model"
            )

        if sha256:
            log("Verifying checksum...")
            from wayfinder.model_catalog import ModelDigestMismatch, verify_model_digest

            try:
                verify_model_digest(tmp_target, sha256)
            except ModelDigestMismatch as e:
                log(f"checksum mismatch: expected {e.expected}, got {e.actual}")
                raise IOError(e.user_message)

        # Rename from .part to final
        tmp_target.rename(target)

        log("")
        log(f"Model saved to: {target}")
        done(True, str(target))

    except Exception as e:
        log(f"Download failed: {e}")
        # Clean up partial download
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except OSError:
                pass
        done(False, str(e))


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
        size_label = model_info.get("size", "unknown size")
        _download_model_file(url, target, ".gguf.part", size_label, log, done, progress,
                             min_bytes=100_000_000, sha256=model_info.get("sha256"),
                             expected_bytes=model_info.get("bytes"))

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
            # Linux: one-click, root-free ydotoold user-service install — makes
            # typing survive compositors that refuse wtype (see check).
            _install=install_pyautogui if sys.platform == "darwin"
            else (install_ydotoold_user_service if sys.platform.startswith("linux") else None),
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
    """Return the best whisper model name for this system's hardware class AND license.

    Never recommend an Ultra-gated model to an unlicensed install: the transcriber
    refuses `large_models` weights without a license, so a free user who accepts the
    default would finish Setup with no working model at all.
    """
    # Steam Deck (Zen 2 APU): large-v3-turbo runs ~10x slower than real-time and is unusable
    # for live dictation — default to base.en (STEAMDECK-INSTALL-LOG Issues 11/17).
    if is_steam_deck():
        return "base.en"
    try:
        from wayfinder.license import get_feature_gate
        has_more_models = get_feature_gate().has_feature("large_models")
    except Exception:
        has_more_models = False
    if not has_more_models:
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
    # ydotool is Linux-only; macOS uses pyautogui (pip package, not system).
    # Bundled runs (AppImage/Flatpak) don't demand it: wtype ships in the
    # bundle for Wayland, X11 uses xdotool, and where a host ydotool exists
    # Setup self-provisions its daemon. (ydotool is deliberately NOT bundled —
    # the client must protocol-match the HOST daemon.)
    if (
        sys.platform != "darwin"
        and not (IS_APPIMAGE or IS_FLATPAK)
        and not shutil.which("ydotool")
    ):
        packages.append("ydotool")
    # git/cmake/compilers/CUDA exist only to build whisper.cpp/llama.cpp from
    # source — a step get_dependencies already skips for AppImage/Flatpak runs
    # (bundled binaries ship in the package). Demanding them anyway turned the
    # Bazzite first run into a dnf error wall for tools that would never be used.
    if not (IS_APPIMAGE or IS_FLATPAK):
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
    """Run a host command (git/cmake/make), streaming output. Raises on failure.

    Host toolchain binaries must not see the bundle's library path.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=cwd,
        env=host_env(),
    )
    for line in iter(proc.stdout.readline, ""):
        log(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)

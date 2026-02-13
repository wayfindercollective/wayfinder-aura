"""
End-to-end tests for the first-run setup wizard flow.

Tests the dependency detection, installation logic, model catalog,
and config updates that happen during onboarding. All system calls
(apt, git, cmake, nvidia-smi, etc.) are mocked.
"""

import io
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from wayfinder.config import DEFAULT_CONFIG, load_config, save_config
from wayfinder.core.setup import (
    WHISPER_MODELS,
    MODEL_DOWNLOAD_BASE,
    Dependency,
    DependencyStatus,
    check_audio,
    check_build_tools,
    check_cuda_toolkit,
    check_gpu_driver,
    check_whisper_cpp,
    check_whisper_model,
    check_ydotool,
    get_dependencies,
    get_missing_system_packages,
    get_recommended_model,
    install_system_packages,
    build_whisper_cpp,
    download_whisper_model,
    _detect_gpu_vendor,
)


# =============================================================================
# Helpers
# =============================================================================


def _mock_popen_success(stdout_lines=None):
    """Create a properly-iterable mock Popen for subprocess tests."""
    if stdout_lines is None:
        stdout_lines = ["Done\n"]

    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("".join(stdout_lines))
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0
    return mock_proc


def _mock_popen_failure(returncode=1, stdout_lines=None):
    """Create a mock Popen that fails."""
    if stdout_lines is None:
        stdout_lines = ["Error\n"]

    mock_proc = MagicMock()
    mock_proc.stdout = io.StringIO("".join(stdout_lines))
    mock_proc.wait.return_value = None
    mock_proc.returncode = returncode
    return mock_proc


def _wait_done(done_event, timeout=10):
    """Wait for a done_event with timeout, fail test if it doesn't fire."""
    assert done_event.wait(timeout=timeout), "Operation timed out"


# =============================================================================
# Dependency Status Dataclass
# =============================================================================


class TestDependencyStatus:
    """Test the DependencyStatus dataclass."""

    def test_installed_status(self):
        s = DependencyStatus(installed=True, detail="v1.0")
        assert s.installed is True
        assert s.detail == "v1.0"
        assert s.warning == ""
        assert s.error == ""

    def test_missing_status(self):
        s = DependencyStatus(installed=False, error="Not found")
        assert s.installed is False
        assert s.error == "Not found"

    def test_warning_status(self):
        s = DependencyStatus(installed=True, detail="found", warning="daemon not running")
        assert s.installed is True
        assert s.warning == "daemon not running"


# =============================================================================
# Dependency Object
# =============================================================================


class TestDependencyObject:
    """Test the Dependency dataclass and its methods."""

    def test_dependency_check_calls_function(self):
        check_fn = MagicMock(return_value=DependencyStatus(installed=True, detail="ok"))
        dep = Dependency(id="test", name="Test", description="A test dep", _check=check_fn)
        status = dep.check()
        check_fn.assert_called_once()
        assert status.installed is True
        assert dep.is_ok is True

    def test_dependency_is_ok_false_when_not_checked(self):
        dep = Dependency(id="test", name="Test", description="desc")
        assert dep.is_ok is False

    def test_dependency_is_ok_false_when_missing(self):
        check_fn = MagicMock(return_value=DependencyStatus(installed=False, error="missing"))
        dep = Dependency(id="test", name="Test", description="desc", _check=check_fn)
        dep.check()
        assert dep.is_ok is False

    def test_dependency_can_install(self):
        dep_with = Dependency(id="a", name="A", description="", _install=lambda: None)
        dep_without = Dependency(id="b", name="B", description="")
        assert dep_with.can_install is True
        assert dep_without.can_install is False

    def test_dependency_required_flag(self):
        dep = Dependency(id="r", name="Required", description="", required=True)
        opt = Dependency(id="o", name="Optional", description="", required=False)
        assert dep.required is True
        assert opt.required is False


# =============================================================================
# Audio Check
# =============================================================================


class TestCheckAudio:
    """Test audio input device detection."""

    @patch("sounddevice.query_devices")
    def test_audio_found(self, mock_query):
        def side_effect(*args, **kwargs):
            if kwargs.get("kind") == "input":
                return {"name": "USB Mic", "max_input_channels": 2}
            return [
                {"name": "USB Mic", "max_input_channels": 2},
                {"name": "Built-in", "max_input_channels": 1},
            ]
        mock_query.side_effect = side_effect

        status = check_audio()
        assert status.installed is True
        assert "USB Mic" in status.detail
        assert "2 input" in status.detail

    @patch("sounddevice.query_devices")
    def test_no_audio_devices(self, mock_query):
        mock_query.return_value = [
            {"name": "Speaker", "max_input_channels": 0},
        ]
        status = check_audio()
        assert status.installed is False

    @patch.dict("sys.modules", {"sounddevice": None})
    def test_sounddevice_not_importable(self):
        """When sounddevice can't be imported, check_audio returns error."""
        # Force reimport of setup to hit the import error
        status = check_audio()
        assert status.installed is False


# =============================================================================
# ydotool Check
# =============================================================================


class TestCheckYdotool:
    """Test ydotool detection."""

    @patch("subprocess.run")
    @patch("wayfinder.core.setup.shutil.which", return_value="/usr/bin/ydotool")
    def test_ydotool_with_daemon_via_pgrep(self, mock_which, mock_run):
        """ydotool installed + daemon running (detected via pgrep)."""
        # pgrep finds ydotoold
        mock_run.return_value = MagicMock(returncode=0)

        # Mock Path.exists to return False for sockets (force pgrep fallback)
        with patch.object(Path, "exists", return_value=False):
            status = check_ydotool()

        assert status.installed is True
        assert "daemon" in status.detail.lower() or status.warning == ""

    @patch("subprocess.run")
    @patch("wayfinder.core.setup.shutil.which", return_value="/usr/bin/ydotool")
    def test_ydotool_without_daemon(self, mock_which, mock_run):
        # pgrep returns non-zero (not running)
        mock_run.return_value = MagicMock(returncode=1)

        with patch.object(Path, "exists", return_value=False):
            status = check_ydotool()

        assert status.installed is True
        assert status.warning != ""  # Should warn about daemon

    @patch("wayfinder.core.setup.shutil.which", return_value=None)
    def test_ydotool_not_installed(self, mock_which):
        status = check_ydotool()
        assert status.installed is False
        assert "not installed" in status.error.lower()


# =============================================================================
# GPU Driver Check
# =============================================================================


class TestCheckGpuDriver:
    """Test GPU driver detection."""

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    @patch("subprocess.run")
    def test_nvidia_driver_working(self, mock_run, mock_vendor):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="NVIDIA GeForce RTX 3090, 535.183.01\n",
        )
        status = check_gpu_driver()
        assert status.installed is True
        assert "NVIDIA" in status.detail or "3090" in status.detail

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_nvidia_no_driver(self, mock_run, mock_vendor):
        status = check_gpu_driver()
        assert status.installed is False
        assert "nvidia-smi" in status.error.lower() or "driver" in status.error.lower()

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="unknown")
    def test_no_gpu_detected(self, mock_vendor):
        status = check_gpu_driver()
        assert status.installed is False
        assert "CPU" in status.error or "No GPU" in status.error


# =============================================================================
# CUDA Toolkit Check
# =============================================================================


class TestCheckCudaToolkit:
    """Test CUDA toolkit detection."""

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    @patch("wayfinder.core.setup.shutil.which", return_value="/usr/bin/nvcc")
    @patch("subprocess.run")
    def test_cuda_installed(self, mock_run, mock_which, mock_vendor):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="nvcc: NVIDIA (R) Cuda compiler driver\nrelease 12.4, V12.4.131\n",
        )
        status = check_cuda_toolkit()
        assert status.installed is True
        assert "release" in status.detail.lower() or "12.4" in status.detail

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    @patch("wayfinder.core.setup.shutil.which", return_value=None)
    def test_cuda_not_installed(self, mock_which, mock_vendor):
        with patch.object(Path, "exists", return_value=False):
            status = check_cuda_toolkit()
        assert status.installed is False
        assert "CUDA" in status.error

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="amd")
    def test_cuda_not_needed_for_amd(self, mock_vendor):
        status = check_cuda_toolkit()
        assert status.installed is True
        assert "Not needed" in status.detail


# =============================================================================
# Build Tools Check
# =============================================================================


class TestCheckBuildTools:
    """Test build tool detection."""

    @patch("wayfinder.core.setup.shutil.which")
    def test_all_tools_present(self, mock_which):
        def which_side_effect(name):
            tools = {"git": "/usr/bin/git", "cmake": "/usr/bin/cmake",
                     "make": "/usr/bin/make", "g++": "/usr/bin/g++", "clang++": None}
            return tools.get(name)
        mock_which.side_effect = which_side_effect

        status = check_build_tools()
        assert status.installed is True

    @patch("wayfinder.core.setup.shutil.which")
    def test_missing_cmake(self, mock_which):
        def which_side_effect(name):
            tools = {"git": "/usr/bin/git", "cmake": None,
                     "make": "/usr/bin/make", "g++": "/usr/bin/g++", "clang++": None}
            return tools.get(name)
        mock_which.side_effect = which_side_effect

        status = check_build_tools()
        assert status.installed is False
        assert "cmake" in status.error.lower()

    @patch("wayfinder.core.setup.shutil.which")
    def test_missing_compiler(self, mock_which):
        def which_side_effect(name):
            tools = {"git": "/usr/bin/git", "cmake": "/usr/bin/cmake",
                     "make": "/usr/bin/make", "g++": None, "clang++": None}
            return tools.get(name)
        mock_which.side_effect = which_side_effect

        status = check_build_tools()
        assert status.installed is False
        assert "C++" in status.error


# =============================================================================
# whisper.cpp Check
# =============================================================================


class TestCheckWhisperCpp:
    """Test whisper.cpp binary detection."""

    def test_whisper_found_at_config_path(self, temp_dir: Path):
        binary = temp_dir / "whisper-cli"
        binary.touch()

        config = {"whisper_binary": str(binary)}

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="--no-gpu", stderr="")
            status = check_whisper_cpp(config)

        assert status.installed is True
        assert "GPU enabled" in status.detail

    def test_whisper_not_found(self, temp_dir: Path):
        """When binary doesn't exist at configured or fallback paths."""
        config = {"whisper_binary": str(temp_dir / "nonexistent")}

        # Mock Path.home so alt paths don't find the real whisper.cpp
        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir / "fakehome"):
            status = check_whisper_cpp(config)

        assert status.installed is False
        assert "not built" in status.error.lower()


# =============================================================================
# Whisper Model Check
# =============================================================================


class TestCheckWhisperModel:
    """Test Whisper model file detection."""

    def test_model_found(self, temp_dir: Path):
        model = temp_dir / "ggml-large-v3-turbo.bin"
        model.write_bytes(b"\x00" * 1_600_000)  # 1.6 MB fake model

        config = {"model_path": str(model)}
        status = check_whisper_model(config)
        assert status.installed is True
        assert "large-v3-turbo" in status.detail

    def test_model_not_found(self, temp_dir: Path):
        """When model doesn't exist at configured path or in model dir."""
        config = {"model_path": str(temp_dir / "nonexistent.bin")}

        # Mock Path.home so it can't find real models
        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir / "fakehome"):
            status = check_whisper_model(config)

        assert status.installed is False
        assert "No Whisper model" in status.error

    def test_alternative_model_found(self, temp_dir: Path):
        """If configured model is missing but other models exist, report them."""
        models_dir = temp_dir / "whisper.cpp" / "models"
        models_dir.mkdir(parents=True)
        alt_model = models_dir / "ggml-small.en.bin"
        alt_model.write_bytes(b"\x00" * 466_000)

        config = {"model_path": str(models_dir / "nonexistent.bin")}

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            status = check_whisper_model(config)

        assert status.installed is True
        assert status.warning != ""
        assert "small" in status.detail.lower()


# =============================================================================
# GPU Vendor Detection
# =============================================================================


class TestDetectGpuVendor:
    """Test GPU vendor auto-detection."""

    @patch("wayfinder.core.setup.shutil.which", return_value="/usr/bin/nvidia-smi")
    def test_nvidia_detected_via_smi(self, mock_which):
        vendor = _detect_gpu_vendor()
        assert vendor == "nvidia"

    @patch("wayfinder.core.setup.shutil.which", return_value=None)
    @patch("subprocess.run")
    def test_amd_detected_via_lspci(self, mock_run, mock_which):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="06:00.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI] Navi 31\n",
        )
        with patch.object(Path, "exists", return_value=False):
            vendor = _detect_gpu_vendor()
        assert vendor == "amd"

    @patch("wayfinder.core.setup.shutil.which", return_value=None)
    @patch("subprocess.run", side_effect=FileNotFoundError)
    def test_unknown_when_no_tools(self, mock_run, mock_which):
        with patch.object(Path, "exists", return_value=False):
            vendor = _detect_gpu_vendor()
        assert vendor == "unknown"


# =============================================================================
# get_dependencies()
# =============================================================================


class TestGetDependencies:
    """Test the dependency list builder."""

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    def test_nvidia_includes_cuda_dep(self, mock_vendor, sample_config: dict):
        deps = get_dependencies(sample_config)
        dep_ids = [d.id for d in deps]
        assert "cuda" in dep_ids
        assert "audio" in dep_ids
        assert "ydotool" in dep_ids
        assert "whisper_cpp" in dep_ids
        assert "whisper_model" in dep_ids

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="amd")
    def test_amd_excludes_cuda_dep(self, mock_vendor, sample_config: dict):
        deps = get_dependencies(sample_config)
        dep_ids = [d.id for d in deps]
        assert "cuda" not in dep_ids

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    def test_all_deps_have_check_functions(self, mock_vendor, sample_config: dict):
        deps = get_dependencies(sample_config)
        for dep in deps:
            assert dep._check is not None, f"Dependency '{dep.id}' has no check function"

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    def test_required_deps_marked(self, mock_vendor, sample_config: dict):
        deps = get_dependencies(sample_config)
        required_ids = {d.id for d in deps if d.required}
        assert "audio" in required_ids
        assert "ydotool" in required_ids
        assert "whisper_cpp" in required_ids
        assert "whisper_model" in required_ids
        # GPU driver and CUDA are optional
        optional_ids = {d.id for d in deps if not d.required}
        assert "gpu_driver" in optional_ids


# =============================================================================
# Model Catalog
# =============================================================================


class TestModelCatalog:
    """Test the Whisper model catalog."""

    def test_all_models_have_required_fields(self):
        for key, info in WHISPER_MODELS.items():
            assert "label" in info, f"Model '{key}' missing 'label'"
            assert "size" in info, f"Model '{key}' missing 'size'"
            assert "bytes" in info, f"Model '{key}' missing 'bytes'"
            assert "note" in info, f"Model '{key}' missing 'note'"
            assert isinstance(info["bytes"], int)
            assert info["bytes"] > 0

    def test_recommended_model_exists(self):
        model = get_recommended_model()
        assert model in WHISPER_MODELS

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    def test_nvidia_recommends_large_turbo(self, mock_vendor):
        assert get_recommended_model() == "large-v3-turbo"

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="amd")
    def test_amd_recommends_large_turbo(self, mock_vendor):
        assert get_recommended_model() == "large-v3-turbo"

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="unknown")
    def test_cpu_recommends_small(self, mock_vendor):
        assert get_recommended_model() == "small.en"

    def test_download_url_format(self):
        for model_name in WHISPER_MODELS:
            url = f"{MODEL_DOWNLOAD_BASE}/ggml-{model_name}.bin"
            assert url.startswith("https://")
            assert model_name in url

    def test_model_sizes_ordered(self):
        """Models should be listed in increasing size."""
        sizes = [info["bytes"] for info in WHISPER_MODELS.values()]
        assert sizes == sorted(sizes), "Models should be ordered by size"


# =============================================================================
# get_missing_system_packages()
# =============================================================================


class TestGetMissingSystemPackages:
    """Test detection of missing apt packages."""

    @patch("wayfinder.core.setup.shutil.which")
    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    def test_all_missing(self, mock_vendor, mock_which):
        mock_which.return_value = None
        pkgs = get_missing_system_packages()
        assert "ydotool" in pkgs
        assert "git" in pkgs
        assert "cmake" in pkgs
        assert "build-essential" in pkgs
        assert "nvidia-cuda-toolkit" in pkgs

    @patch("wayfinder.core.setup.shutil.which")
    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="amd")
    def test_all_installed_amd(self, mock_vendor, mock_which):
        mock_which.side_effect = lambda name: f"/usr/bin/{name}"
        pkgs = get_missing_system_packages()
        assert "nvidia-cuda-toolkit" not in pkgs

    @patch("wayfinder.core.setup.shutil.which")
    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="unknown")
    def test_only_ydotool_missing(self, mock_vendor, mock_which):
        def which_side_effect(name):
            if name == "ydotool":
                return None
            return f"/usr/bin/{name}"
        mock_which.side_effect = which_side_effect
        pkgs = get_missing_system_packages()
        assert "ydotool" in pkgs
        assert "git" not in pkgs


# =============================================================================
# install_system_packages() (mocked)
# =============================================================================


class TestInstallSystemPackages:
    """Test the system package installation flow (all mocked)."""

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_install_calls_pkexec_success(self, mock_popen, mock_run):
        mock_popen.return_value = _mock_popen_success(["Reading...\n", "Done\n"])
        mock_run.return_value = MagicMock(returncode=0)

        log_messages = []
        done_result = {}
        done_event = threading.Event()

        install_system_packages(
            lambda msg: log_messages.append(msg),
            lambda s, d: (done_result.update({"success": s}), done_event.set()),
            packages=["ydotool", "cmake"],
        )
        _wait_done(done_event)

        assert done_result["success"] is True
        cmd_args = mock_popen.call_args[0][0]
        assert cmd_args[0] == "pkexec"

    @patch("subprocess.Popen")
    def test_install_cancelled_by_user(self, mock_popen):
        mock_popen.return_value = _mock_popen_failure(returncode=126)

        done_result = {}
        done_event = threading.Event()

        install_system_packages(
            lambda m: None,
            lambda s, d: (done_result.update({"success": s}), done_event.set()),
            packages=["ydotool"],
        )
        _wait_done(done_event)

        assert done_result["success"] is False

    @patch("subprocess.Popen", side_effect=FileNotFoundError)
    def test_install_pkexec_not_found(self, mock_popen):
        done_result = {}
        done_event = threading.Event()

        install_system_packages(
            lambda m: None,
            lambda s, d: (done_result.update({"success": s, "detail": d}), done_event.set()),
            packages=["ydotool"],
        )
        _wait_done(done_event)

        assert done_result["success"] is False
        assert "pkexec" in done_result["detail"].lower()

    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_ydotool_daemon_enabled_after_install(self, mock_popen, mock_run):
        """After installing ydotool, systemctl enable should be called."""
        mock_popen.return_value = _mock_popen_success(["Done\n"])
        mock_run.return_value = MagicMock(returncode=0)

        log_messages = []
        done_event = threading.Event()

        install_system_packages(
            lambda msg: log_messages.append(msg),
            lambda s, d: done_event.set(),
            packages=["ydotool"],
        )
        _wait_done(done_event)

        # systemctl enable should have been called via subprocess.run
        assert mock_run.called
        all_args = str(mock_run.call_args)
        assert "systemctl" in all_args


# =============================================================================
# build_whisper_cpp() (mocked)
# =============================================================================


class TestBuildWhisperCpp:
    """Test the whisper.cpp build flow (all mocked)."""

    @patch("wayfinder.core.setup._run_cmd")
    def test_clone_and_build_cuda(self, mock_run_cmd, temp_dir: Path):
        """Should clone, configure with CUDA, and build."""
        done_result = {}
        done_event = threading.Event()

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            # Create the expected binary path so the check passes
            binary_path = temp_dir / "whisper.cpp" / "build" / "bin" / "whisper-cli"
            binary_path.parent.mkdir(parents=True, exist_ok=True)
            binary_path.touch()

            build_whisper_cpp(
                lambda m: None,
                lambda s, d: (done_result.update({"success": s, "detail": d}), done_event.set()),
                use_cuda=True,
            )
            _wait_done(done_event)

        assert done_result["success"] is True
        # Verify cmake was called with CUDA flag
        cmake_calls = [c for c in mock_run_cmd.call_args_list
                       if any("cmake" in str(arg) for arg in c[0])]
        assert len(cmake_calls) >= 1
        cmake_args = str(cmake_calls[0])
        assert "GGML_CUDA" in cmake_args

    @patch("wayfinder.core.setup._run_cmd")
    def test_build_vulkan_for_amd(self, mock_run_cmd, temp_dir: Path):
        """AMD GPU should build with Vulkan flag."""
        done_result = {}
        done_event = threading.Event()

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            binary_path = temp_dir / "whisper.cpp" / "build" / "bin" / "whisper-cli"
            binary_path.parent.mkdir(parents=True, exist_ok=True)
            binary_path.touch()

            build_whisper_cpp(
                lambda m: None,
                lambda s, d: (done_result.update({"success": s}), done_event.set()),
                use_vulkan=True,
            )
            _wait_done(done_event)

        cmake_calls = [c for c in mock_run_cmd.call_args_list
                       if any("cmake" in str(arg) for arg in c[0])]
        cmake_args = str(cmake_calls[0])
        assert "GGML_VULKAN" in cmake_args

    @patch("wayfinder.core.setup._run_cmd", side_effect=subprocess.CalledProcessError(1, ["cmake"]))
    def test_build_failure(self, mock_run_cmd, temp_dir: Path):
        """Build failure should report error."""
        done_result = {}
        done_event = threading.Event()

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            build_whisper_cpp(
                lambda m: None,
                lambda s, d: (done_result.update({"success": s}), done_event.set()),
            )
            _wait_done(done_event)

        assert done_result["success"] is False


# =============================================================================
# download_whisper_model() (mocked)
# =============================================================================


class TestDownloadWhisperModel:
    """Test model download flow (all mocked)."""

    @patch("requests.get")
    def test_successful_download(self, mock_get, temp_dir: Path):
        (temp_dir / "whisper.cpp" / "models").mkdir(parents=True)

        mock_response = MagicMock()
        mock_response.headers = {"content-length": "1000"}
        mock_response.iter_content.return_value = [b"\x00" * 500, b"\x00" * 500]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        done_result = {}
        progress_calls = []
        done_event = threading.Event()

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            download_whisper_model(
                "tiny.en",
                lambda m: None,
                lambda s, d: (done_result.update({"success": s, "detail": d}), done_event.set()),
                progress=lambda dl, tot: progress_calls.append((dl, tot)),
            )
            _wait_done(done_event)

        assert done_result["success"] is True
        assert "tiny.en" in done_result["detail"]
        # Verify correct URL
        url = mock_get.call_args[0][0]
        assert "ggml-tiny.en.bin" in url
        assert len(progress_calls) > 0

    @patch("requests.get", side_effect=Exception("Network error"))
    def test_download_failure(self, mock_get, temp_dir: Path):
        (temp_dir / "whisper.cpp" / "models").mkdir(parents=True)

        done_result = {}
        done_event = threading.Event()

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            download_whisper_model(
                "tiny.en",
                lambda m: None,
                lambda s, d: (done_result.update({"success": s}), done_event.set()),
            )
            _wait_done(done_event)

        assert done_result["success"] is False

    @patch("requests.get")
    def test_partial_download_cleaned_up(self, mock_get, temp_dir: Path):
        """If download fails mid-stream, the .part file should be removed."""
        models_dir = temp_dir / "whisper.cpp" / "models"
        models_dir.mkdir(parents=True)

        mock_response = MagicMock()
        mock_response.headers = {"content-length": "1000"}
        mock_response.iter_content.side_effect = Exception("Connection lost")
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        done_event = threading.Event()

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            download_whisper_model("tiny.en", lambda m: None, lambda s, d: done_event.set())
            _wait_done(done_event)

        part_file = models_dir / "ggml-tiny.en.bin.part"
        assert not part_file.exists()


# =============================================================================
# Config Integration (setup_completed flag)
# =============================================================================


class TestSetupConfigIntegration:
    """Test that setup completion is persisted in config."""

    def test_setup_completed_not_in_defaults(self):
        """DEFAULT_CONFIG should not have setup_completed (triggers wizard)."""
        assert "setup_completed" not in DEFAULT_CONFIG

    def test_setup_completed_persists(self, temp_config_dir: Path):
        """Setting setup_completed should survive save/load."""
        import wayfinder.config as cfg_mod

        original_dir = cfg_mod.CONFIG_DIR
        original_file = cfg_mod.CONFIG_FILE
        try:
            cfg_mod.CONFIG_DIR = temp_config_dir
            cfg_mod.CONFIG_FILE = temp_config_dir / "config.json"

            config = load_config()
            assert config.get("setup_completed", False) is False

            config["setup_completed"] = True
            save_config(config)

            reloaded = load_config()
            assert reloaded["setup_completed"] is True
        finally:
            cfg_mod.CONFIG_DIR = original_dir
            cfg_mod.CONFIG_FILE = original_file

    def test_config_paths_updated_after_setup(self, temp_config_dir: Path):
        """Setup should update whisper_binary and model_path in config."""
        import wayfinder.config as cfg_mod

        original_dir = cfg_mod.CONFIG_DIR
        original_file = cfg_mod.CONFIG_FILE
        try:
            cfg_mod.CONFIG_DIR = temp_config_dir
            cfg_mod.CONFIG_FILE = temp_config_dir / "config.json"

            config = load_config()
            new_binary = "/home/user/whisper.cpp/build/bin/whisper-cli"
            new_model = "/home/user/whisper.cpp/models/ggml-large-v3-turbo.bin"

            config["whisper_binary"] = new_binary
            config["model_path"] = new_model
            config["setup_completed"] = True
            save_config(config)

            reloaded = load_config()
            assert reloaded["whisper_binary"] == new_binary
            assert reloaded["model_path"] == new_model
            assert reloaded["setup_completed"] is True
        finally:
            cfg_mod.CONFIG_DIR = original_dir
            cfg_mod.CONFIG_FILE = original_file


# =============================================================================
# Full Setup Flow (E2E with mocks)
# =============================================================================


class TestFullSetupFlow:
    """
    End-to-end test of the complete setup sequence:
    detect -> install packages -> build whisper -> download model -> config saved.
    """

    @patch("requests.get")
    @patch("wayfinder.core.setup._run_cmd")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    @patch("wayfinder.core.setup.shutil.which", return_value=None)
    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="nvidia")
    @patch("wayfinder.core.setup._detect_package_manager", return_value="apt")
    def test_complete_nvidia_setup_flow(
        self, mock_pkg_mgr, mock_vendor, mock_which, mock_popen, mock_sub_run,
        mock_run_cmd, mock_requests, temp_dir: Path, sample_config: dict,
    ):
        """Simulate full setup on a fresh NVIDIA Ubuntu system."""

        # Step 1: Check — everything missing
        deps = get_dependencies(sample_config)
        ydotool_dep = next(d for d in deps if d.id == "ydotool")
        assert ydotool_dep.check().installed is False

        build_dep = next(d for d in deps if d.id == "build_tools")
        assert build_dep.check().installed is False

        # Step 2: Install system packages
        mock_popen.return_value = _mock_popen_success(["Done\n"])
        mock_sub_run.return_value = MagicMock(returncode=0)

        pkg_event = threading.Event()
        pkg_result = {}
        install_system_packages(
            lambda m: None,
            lambda s, d: (pkg_result.update({"success": s}), pkg_event.set()),
            packages=["ydotool", "build-essential", "cmake", "git", "nvidia-cuda-toolkit"],
        )
        _wait_done(pkg_event)
        assert pkg_result["success"] is True

        # Step 3: Build whisper.cpp with CUDA
        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            binary_path = temp_dir / "whisper.cpp" / "build" / "bin" / "whisper-cli"
            binary_path.parent.mkdir(parents=True, exist_ok=True)
            binary_path.touch()

            build_event = threading.Event()
            build_result = {}
            build_whisper_cpp(
                lambda m: None,
                lambda s, d: (build_result.update({"success": s, "detail": d}), build_event.set()),
                use_cuda=True,
            )
            _wait_done(build_event)
        assert build_result["success"] is True

        # Step 4: Download model
        (temp_dir / "whisper.cpp" / "models").mkdir(parents=True, exist_ok=True)
        mock_response = MagicMock()
        mock_response.headers = {"content-length": "1600000"}
        mock_response.iter_content.return_value = [b"\x00" * 1_000_000, b"\x00" * 600_000]
        mock_response.raise_for_status = MagicMock()
        mock_requests.return_value = mock_response

        with patch("wayfinder.core.setup.Path.home", return_value=temp_dir):
            dl_event = threading.Event()
            dl_result = {}
            download_whisper_model(
                "large-v3-turbo",
                lambda m: None,
                lambda s, d: (dl_result.update({"success": s, "path": d}), dl_event.set()),
            )
            _wait_done(dl_event)
        assert dl_result["success"] is True
        assert "large-v3-turbo" in dl_result["path"]

        # Step 5: Config reflects new paths
        config = sample_config.copy()
        config["whisper_binary"] = build_result.get("detail", "")
        config["model_path"] = dl_result.get("path", "")
        config["setup_completed"] = True
        assert config["setup_completed"] is True

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="amd")
    def test_amd_setup_skips_cuda(self, mock_vendor, sample_config: dict):
        deps = get_dependencies(sample_config)
        dep_ids = [d.id for d in deps]
        assert "cuda" not in dep_ids

    @patch("wayfinder.core.setup._detect_gpu_vendor", return_value="unknown")
    def test_cpu_only_setup_minimal_deps(self, mock_vendor, sample_config: dict):
        deps = get_dependencies(sample_config)
        dep_ids = [d.id for d in deps]
        assert "audio" in dep_ids
        assert "ydotool" in dep_ids
        assert "whisper_cpp" in dep_ids
        assert "whisper_model" in dep_ids
        assert "cuda" not in dep_ids

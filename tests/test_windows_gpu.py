"""Windows GPU transcription (bundled Vulkan whisper build), GPU detection,
model folder, idle-animation cover check and installer signing.

Everything here mocks the OS: no GPU, subprocess or registry access.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _bundle(tmp_path, gpu=True):
    """A packaged layout: _internal/whisper(+ -vulkan)/whisper-{cli,server}.exe."""
    root = tmp_path / "_internal"
    dirs = ["whisper"] + (["whisper-vulkan"] if gpu else [])
    for folder in dirs:
        (root / folder).mkdir(parents=True)
        for exe in ("whisper-cli.exe", "whisper-server.exe"):
            (root / folder / exe).write_bytes(b"MZ")
    return root


# --- utils/windows_whisper ------------------------------------------------------

def test_twins_map_between_the_cpu_and_vulkan_folders(tmp_path):
    from wayfinder.utils.windows_whisper import cpu_twin, gpu_twin

    root = _bundle(tmp_path)
    cpu = str(root / "whisper" / "whisper-server.exe")
    gpu = str(root / "whisper-vulkan" / "whisper-server.exe")
    assert gpu_twin(cpu) == gpu
    assert cpu_twin(gpu) == cpu
    assert gpu_twin(gpu) is None and cpu_twin(cpu) is None
    # Dev staging folders follow the same rule.
    dev = tmp_path / "build"
    for folder in ("windows-whisper", "windows-whisper-vulkan"):
        (dev / folder).mkdir(parents=True)
        (dev / folder / "whisper-cli.exe").write_bytes(b"MZ")
    assert gpu_twin(str(dev / "windows-whisper" / "whisper-cli.exe")).endswith(
        str(Path("windows-whisper-vulkan") / "whisper-cli.exe"))


def test_no_twin_without_a_vulkan_build(tmp_path):
    from wayfinder.utils.windows_whisper import gpu_twin

    root = _bundle(tmp_path, gpu=False)
    assert gpu_twin(str(root / "whisper" / "whisper-cli.exe")) is None
    assert gpu_twin("") is None


@pytest.mark.parametrize("code,crashed", [
    (0, False), (1, False), (2, False), (-11, True),
    (0xC0000005, True), (0xC0000409, True), (None, False),
])
def test_crash_codes(code, crashed):
    from wayfinder.utils.windows_whisper import crashed as is_crash

    assert is_crash(code) is crashed


# --- transcriber: GPU mode picks the Vulkan build, gate unchanged ---------------

def _backend_config(root, use_gpu=True):
    return {
        "transcription_backend": "whisper_cpp",
        "whisper_binary": str(root / "whisper" / "whisper-cli.exe"),
        "model_path": str(root / "ggml-small.en.bin"),
        "use_gpu": use_gpu,
        "whisper_server_mode": True,
    }


def _patch_backend_env(monkeypatch, root, gpu_licensed):
    from wayfinder.core import transcriber

    (root / "ggml-small.en.bin").write_bytes(b"x")
    monkeypatch.setattr(transcriber.sys, "platform", "win32")
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda b: b)
    monkeypatch.setattr(
        "wayfinder.license.FeatureGate.has_feature",
        lambda self, f: gpu_licensed and f in ("gpu_acceleration", "large_models"),
    )
    monkeypatch.setattr(transcriber.WhisperServerBackend, "is_available", lambda self: True)
    return transcriber


def test_ultra_gpu_mode_runs_the_vulkan_build(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    transcriber = _patch_backend_env(monkeypatch, root, gpu_licensed=True)
    backend = transcriber.get_backend(_backend_config(root))
    assert backend.use_gpu is True
    assert Path(backend.whisper_server_binary).parent.name == "whisper-vulkan"
    # Its CPU safety net is the CPU build's server, tried last.
    attempts = backend._server_cmd_attempts(8178)
    assert Path(attempts[-1][0]).parent.name == "whisper"


def test_free_gpu_request_stays_on_the_cpu_build(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    transcriber = _patch_backend_env(monkeypatch, root, gpu_licensed=False)
    backend = transcriber.get_backend(_backend_config(root, use_gpu=True))
    assert backend.use_gpu is False
    assert Path(backend.whisper_server_binary).parent.name == "whisper"


def test_cpu_mode_stays_on_the_cpu_build(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    transcriber = _patch_backend_env(monkeypatch, root, gpu_licensed=True)
    backend = transcriber.get_backend(_backend_config(root, use_gpu=False))
    assert Path(backend.whisper_server_binary).parent.name == "whisper"


def test_cli_cpu_fallback_uses_the_cpu_build_on_windows(tmp_path, monkeypatch):
    from wayfinder.core import transcriber

    root = _bundle(tmp_path)
    monkeypatch.setattr(transcriber.sys, "platform", "win32")
    assert transcriber._cpu_fallback_binary(str(root / "whisper-vulkan" / "whisper-cli.exe")) == \
        str(root / "whisper" / "whisper-cli.exe")
    assert transcriber._cpu_fallback_binary(str(root / "whisper" / "whisper-cli.exe")) is None


# --- utils/gpu_simple: device probe --------------------------------------------

_HELP = (
    "ggml_vulkan: Found 2 Vulkan devices:\n"
    "ggml_vulkan: 0 = AMD Radeon RX 7900 XTX (AMD proprietary driver) | uma: 0 | fp16: 1 | "
    "matrix cores: KHR_coopmat\n"
    "ggml_vulkan: 1 = AMD Radeon 780M Graphics (AMD proprietary driver) | uma: 1 | fp16: 1 | "
    "matrix cores: KHR_coopmat\n"
)


def test_windows_probe_asks_the_vulkan_build_without_a_window(tmp_path, monkeypatch):
    from wayfinder.utils import gpu_simple

    root = _bundle(tmp_path)
    calls = []

    class Result:
        stdout, stderr = "", _HELP

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Result()

    monkeypatch.setattr(gpu_simple.sys, "platform", "win32")
    monkeypatch.setattr(gpu_simple.subprocess, "run", fake_run)
    monkeypatch.setenv("GGML_VK_VISIBLE_DEVICES", "1")
    devices = gpu_simple.detect_gpu_devices({"whisper_binary": str(root / "whisper" / "whisper-cli.exe")})
    assert [d.name for d in devices][0].startswith("AMD Radeon RX 7900 XTX")
    assert devices[0].is_discrete and not devices[1].is_discrete
    cmd, kwargs = calls[0]
    assert Path(cmd[0]).parent.name == "whisper-vulkan" and cmd[1:] == ["--help"]
    assert "GGML_VK_VISIBLE_DEVICES" not in kwargs["env"]  # list every device
    assert kwargs.get("creationflags")  # CREATE_NO_WINDOW


def test_windows_cpu_mode_skips_the_gpu_probe(tmp_path, monkeypatch):
    from wayfinder.utils import gpu_simple

    root = _bundle(tmp_path)
    monkeypatch.setattr(gpu_simple.sys, "platform", "win32")
    monkeypatch.setattr(gpu_simple, "get_discrete_gpu",
                        lambda config=None: pytest.fail("probed in CPU mode"))
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    config = {"whisper_binary": str(root / "whisper" / "whisper-cli.exe"), "use_gpu": False}
    assert gpu_simple.setup_gpu_environment(config) == {}


def test_windows_gpu_mode_picks_the_discrete_gpu(tmp_path, monkeypatch):
    from wayfinder.utils import gpu_simple

    root = _bundle(tmp_path)
    monkeypatch.setattr(gpu_simple.sys, "platform", "win32")
    monkeypatch.setattr(gpu_simple, "detect_gpu_devices",
                        lambda config=None: gpu_simple.parse_ggml_vulkan_devices(_HELP))
    monkeypatch.delenv("GGML_VK_VISIBLE_DEVICES", raising=False)
    config = {"whisper_binary": str(root / "whisper" / "whisper-cli.exe"), "use_gpu": True}
    assert gpu_simple.setup_gpu_environment(config) == {"GGML_VK_VISIBLE_DEVICES": "0"}


# --- utils/windows_sysinfo: which GPU to report --------------------------------

@pytest.mark.parametrize("names,expected", [
    (["AMD Radeon 780M Graphics", "AMD Radeon RX 7900 XTX"], ("amd", "AMD Radeon RX 7900 XTX")),
    (["Intel(R) UHD Graphics", "NVIDIA GeForce RTX 4070 Laptop GPU"],
     ("nvidia", "NVIDIA GeForce RTX 4070 Laptop GPU")),
    (["Intel(R) Iris(R) Xe Graphics", "Intel(R) Arc(TM) A770"], ("intel", "Intel(R) Arc(TM) A770")),
    (["AMD Radeon 780M Graphics"], ("amd", "AMD Radeon 780M Graphics")),
    ([], None),
])
def test_primary_gpu_prefers_a_dedicated_card(names, expected):
    from wayfinder.utils.windows_sysinfo import pick_primary_gpu

    assert pick_primary_gpu(names) == expected


# --- ui/windows_window: is Aura covered? ---------------------------------------

def test_exposure_detects_a_covering_window_but_not_the_desktop():
    from wayfinder.ui.windows_window import exposure

    rects = {1: (100, 100, 900, 700), 2: (0, 0, 2560, 1400), 3: (200, 200, 600, 500), 4: (0, 0, 2560, 1440)}
    classes = {2: "Chrome_WidgetWin_1", 3: "Notepad", 4: "Progman"}
    owner = {5: 1}

    def expo(foreground):
        return exposure(1, foreground, rects.get, lambda h: classes.get(h, ""),
                        lambda h: owner.get(h, h))

    assert expo(1) == "foreground"
    assert expo(5) == "foreground"   # Aura's own tooltip/dialog in front
    assert expo(2) == "covered"      # a maximized app hides Aura entirely
    assert expo(3) == "background"   # a small window overlaps only part
    assert expo(4) == "background"   # clicking the desktop never hides Aura
    assert expo(0) == "foreground"   # nothing in front (lock screen, unknown)


# --- utils/platform: Windows speech-model folder -------------------------------

def test_windows_downloads_speech_models_to_local_appdata(tmp_path, monkeypatch):
    from wayfinder.utils import platform as plat

    monkeypatch.setattr(plat.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert plat.get_whisper_download_dir() == tmp_path / "Local" / "wayfinder-aura" / "whisper-models"
    dirs = plat.get_whisper_host_model_dirs()
    assert dirs[0] == plat.get_whisper_download_dir()
    assert dirs[1] == Path.home() / "whisper.cpp" / "models"  # older downloads still found


def test_linux_keeps_its_speech_model_folder(monkeypatch):
    from wayfinder.utils import platform as plat

    monkeypatch.setattr(plat.sys, "platform", "linux")
    assert plat.get_whisper_download_dir() == Path.home() / "whisper.cpp" / "models"
    assert plat.get_whisper_host_model_dirs() == [Path.home() / "whisper.cpp" / "models"]


# --- packaging/windows/build.py: Authenticode ----------------------------------

def _build_module():
    spec = importlib.util.spec_from_file_location("win_build", REPO / "packaging" / "windows" / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signing_is_off_without_an_identity(monkeypatch):
    build = _build_module()
    for var in ("WAYFINDER_SIGN_CERT_SHA1", "WAYFINDER_SIGN_DLIB", "WAYFINDER_SIGN_METADATA"):
        monkeypatch.delenv(var, raising=False)
    assert build._sign_command() is None


def test_signing_with_a_certificate_thumbprint(tmp_path, monkeypatch):
    build = _build_module()
    tool = tmp_path / "signtool.exe"
    tool.write_bytes(b"MZ")
    monkeypatch.setenv("SIGNTOOL", str(tool))
    monkeypatch.setenv("WAYFINDER_SIGN_CERT_SHA1", "ABC123")
    monkeypatch.delenv("WAYFINDER_SIGN_TIMESTAMP_URL", raising=False)
    cmd = build._sign_command()
    assert cmd[:4] == [str(tool), "sign", "/sha1", "ABC123"]
    assert "/fd" in cmd and "SHA256" in cmd and "/tr" in cmd


def test_signing_with_azure_trusted_signing(tmp_path, monkeypatch):
    build = _build_module()
    tool = tmp_path / "signtool.exe"
    tool.write_bytes(b"MZ")
    monkeypatch.setenv("SIGNTOOL", str(tool))
    monkeypatch.delenv("WAYFINDER_SIGN_CERT_SHA1", raising=False)
    monkeypatch.setenv("WAYFINDER_SIGN_DLIB", "C:/sign/Azure.CodeSigning.Dlib.dll")
    monkeypatch.setenv("WAYFINDER_SIGN_METADATA", "C:/sign/metadata.json")
    cmd = build._sign_command()
    assert cmd[cmd.index("/dlib") + 1].endswith("Azure.CodeSigning.Dlib.dll")
    assert cmd[cmd.index("/dmdf") + 1].endswith("metadata.json")


def test_installer_signs_setup_and_uninstaller_when_asked():
    iss = (REPO / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    block = iss.split("#ifdef SignAura", 1)[1].split("#endif", 1)[0]
    assert "SignTool=aura" in block and "SignedUninstaller=yes" in block


def test_windows_spec_bundles_the_vulkan_build_and_leaves_scipy_out():
    spec = (REPO / "packaging" / "windows" / "wayfinder-aura-windows.spec").read_text(encoding="utf-8")
    assert '"whisper-vulkan"' in spec
    assert '"scipy",' in spec.split("excludes=[", 1)[1].split("]", 1)[0]
    assert "opengl32sw.dll" in spec and "Qt6Pdf.dll" in spec


@pytest.mark.skipif(sys.platform != "win32", reason="MSBuild path limit check is Windows-specific")
def test_vulkan_build_folder_is_short():
    build = _build_module()
    # The nested shader-generator project overran MAX_PATH under build/whisper.cpp-src/.
    assert len(str(build.ROOT / "build" / "wvk")) < len(str(build.WHISPER_SRC / "build-vulkan"))


def test_upgrade_replaces_the_whole_payload():
    """Files an older version shipped must not survive an upgrade (a leftover
    scipy/ would still be importable, and 125 MB piled up on one PC)."""
    iss = (REPO / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
    block = iss.split("[InstallDelete]", 1)[1].split("[", 1)[0]
    assert 'Type: filesandordirs; Name: "{app}\_internal"' in block

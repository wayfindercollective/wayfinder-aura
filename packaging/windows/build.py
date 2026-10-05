#!/usr/bin/env python3
"""Build the Wayfinder Aura Windows installer, end to end.

    python packaging/windows/build.py

Step 1 runs PyInstaller (packaging/windows/wayfinder-aura-windows.spec) to produce
the onedir bundle at dist/Wayfinder Aura/. Step 2 runs Inno Setup (ISCC) on
installer.iss to wrap that into dist/installer/WayfinderAura-Setup-<version>.exe.

Windows-only; it never touches the Linux AppImage/Flatpak or macOS .app builds.
Prerequisites: the app's venv with `pyinstaller` installed, and (for the
installer step) Inno Setup 6. If ISCC isn't found, the bundle is still produced
unless --require-installer is passed (CI uses it so a missing ISCC fails loudly).
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def _find_iscc() -> Path | None:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for c in candidates:
        if c.exists():
            return c
    found = shutil.which("ISCC")
    return Path(found) if found else None


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit("could not read version from pyproject.toml")
    return match.group(1)


# The pinned prebuilt whisper.cpp CPU build (same one the setup flow downloads,
# see WHISPER_WINDOWS_URL in src/wayfinder/core/setup.py). Bundled so a fresh
# install transcribes without a first-run download, like the AppImage and .app.
WHISPER_ZIP_SHA256 = "c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d"
WHISPER_STAGE = ROOT / "build" / "windows-whisper"
WHISPER_FILES = ("whisper-cli.exe", "whisper-server.exe")


def _whisper_build_tag() -> str:
    text = (ROOT / "src" / "wayfinder" / "core" / "setup.py").read_text(encoding="utf-8")
    build = re.search(r'^WHISPER_WINDOWS_BUILD = "([^"]+)"', text, re.MULTILINE)
    if build is None:
        raise SystemExit("could not read WHISPER_WINDOWS_BUILD from setup.py")
    return build.group(1)


def _whisper_zip_url() -> str:
    return ("https://github.com/ggml-org/whisper.cpp/releases/download/"
            f"{_whisper_build_tag()}/whisper-bin-x64.zip")


# The same pinned whisper.cpp built with the Vulkan backend (AMD, Intel and
# NVIDIA GPUs) for Ultra's GPU acceleration. whisper.cpp publishes no Vulkan
# build for Windows x64, so it is compiled here with its release job's own
# Windows flags (MSVC, shared libs, dynamically loaded backends with every CPU
# variant) plus GGML_VULKAN. Loading backends dynamically matters: on a PC
# without a Vulkan driver the GPU backend is skipped instead of the exe failing
# to start. Bundled beside the CPU build, which stays the default.
WHISPER_VULKAN_STAGE = ROOT / "build" / "windows-whisper-vulkan"
WHISPER_SRC = ROOT / "build" / "whisper.cpp-src"
WHISPER_REPO = "https://github.com/ggml-org/whisper.cpp.git"


def _vulkan_sdk() -> Path | None:
    """The Vulkan SDK (glslc compiles the backend's shaders), newest first."""
    env = os.environ.get("VULKAN_SDK")
    if env and (Path(env) / "Bin" / "glslc.exe").exists():
        return Path(env)
    found = sorted(Path("C:/VulkanSDK").glob("*/Bin/glslc.exe"), reverse=True)
    return found[0].parent.parent if found else None


def _stage_whisper_vulkan(required: bool) -> bool:
    """Build and stage the Vulkan whisper-cli/whisper-server + DLLs. True if staged."""
    wanted = (*WHISPER_FILES, "ggml-vulkan.dll")
    if all((WHISPER_VULKAN_STAGE / name).exists() for name in wanted):
        return True
    sdk = _vulkan_sdk()
    if sdk is None:
        message = "Vulkan SDK not found (set VULKAN_SDK): GPU whisper build skipped"
        if required:
            raise SystemExit(message)
        print(message)
        return False
    tag = _whisper_build_tag()
    if not (WHISPER_SRC / ".git").exists():
        print(f"== Cloning whisper.cpp {tag} ==")
        subprocess.run(["git", "clone", "--depth", "1", "--branch", tag, WHISPER_REPO,
                        str(WHISPER_SRC)], check=True)
    def _rev(ref: str) -> str:
        return subprocess.run(["git", "-C", str(WHISPER_SRC), "rev-parse", "--verify", "-q", ref],
                              capture_output=True, text=True).stdout.strip()

    if not _rev(f"{tag}^{{commit}}") or _rev("HEAD") != _rev(f"{tag}^{{commit}}"):
        raise SystemExit(f"{WHISPER_SRC} is not at whisper.cpp {tag}; delete it to re-clone")
    # Short on purpose: the Vulkan shader generator is a nested CMake project
    # whose MSBuild logs overrun Windows' 260-character path limit otherwise.
    build_dir = ROOT / "build" / "wvk"
    env = dict(os.environ, VULKAN_SDK=str(sdk))
    print(f"== Building whisper.cpp {tag} with Vulkan (SDK {sdk.name}) ==")
    subprocess.run([
        "cmake", "-S", str(WHISPER_SRC), "-B", str(build_dir), "-A", "x64",
        "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=ON", "-DWHISPER_SDL2=OFF",
        "-DGGML_NATIVE=OFF", "-DGGML_BACKEND_DL=ON", "-DGGML_CPU_ALL_VARIANTS=ON",
        "-DGGML_VULKAN=ON", "-DWHISPER_BUILD_TESTS=OFF",
    ], check=True, env=env)
    # Bare --parallel ignores the environment default and lets MSBuild choose.
    subprocess.run(["cmake", "--build", str(build_dir), "--config", "Release",
                    "--parallel", os.environ.get("CMAKE_BUILD_PARALLEL_LEVEL") or "2"],
                   check=True, env=env)
    out = build_dir / "bin" / "Release"
    WHISPER_VULKAN_STAGE.mkdir(parents=True, exist_ok=True)
    for path in out.iterdir():
        if path.name in WHISPER_FILES or path.suffix == ".dll":
            shutil.copy2(path, WHISPER_VULKAN_STAGE / path.name)
    missing = [name for name in wanted if not (WHISPER_VULKAN_STAGE / name).exists()]
    if missing:
        raise SystemExit(f"Vulkan whisper build is missing {', '.join(missing)}")
    return True


def _stage_whisper() -> None:
    """Download, verify, and extract whisper-cli/whisper-server + DLLs for the spec."""
    if all((WHISPER_STAGE / name).exists() for name in WHISPER_FILES):
        return
    import hashlib
    import io
    import urllib.request
    import zipfile

    url = _whisper_zip_url()
    print(f"== Fetching pinned whisper.cpp: {url} ==")
    with urllib.request.urlopen(url, timeout=180) as resp:
        payload = resp.read()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != WHISPER_ZIP_SHA256:
        raise SystemExit(f"whisper.cpp zip digest mismatch: {digest} != {WHISPER_ZIP_SHA256}")
    WHISPER_STAGE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for member in zf.namelist():
            name = os.path.basename(member)
            if name in WHISPER_FILES or name.endswith(".dll"):
                (WHISPER_STAGE / name).write_bytes(zf.read(member))


# Authenticode. An unsigned installer draws a SmartScreen warning, so a release
# is signed whenever the build machine provides a code-signing identity:
#   WAYFINDER_SIGN_CERT_SHA1   thumbprint of a code-signing certificate in the
#                              Windows certificate store (OV/EV), or
#   WAYFINDER_SIGN_DLIB and WAYFINDER_SIGN_METADATA
#                              Azure Trusted Signing (Azure.CodeSigning.Dlib.dll
#                              and its metadata.json).
# WAYFINDER_SIGN_TIMESTAMP_URL overrides the RFC 3161 timestamp server, SIGNTOOL
# the signtool.exe path. WAYFINDER_REQUIRE_SIGNING=1 fails a build without one.
def _signtool() -> Path | None:
    if os.environ.get("SIGNTOOL"):
        return Path(os.environ["SIGNTOOL"])
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10"
    found = sorted((kits / "bin").glob("*/x64/signtool.exe"), reverse=True)
    return found[0] if found else None


def _sign_command() -> list[str] | None:
    """signtool argv without the file names, or None when signing isn't set up."""
    thumbprint = os.environ.get("WAYFINDER_SIGN_CERT_SHA1", "").strip()
    dlib = os.environ.get("WAYFINDER_SIGN_DLIB", "").strip()
    metadata = os.environ.get("WAYFINDER_SIGN_METADATA", "").strip()
    if not thumbprint and not (dlib and metadata):
        return None
    tool = _signtool()
    if tool is None or not tool.exists():
        raise SystemExit("A signing identity is set but signtool.exe was not found "
                         "(install the Windows SDK or set SIGNTOOL)")
    if thumbprint:
        stamp = os.environ.get("WAYFINDER_SIGN_TIMESTAMP_URL", "http://timestamp.digicert.com")
        return [str(tool), "sign", "/sha1", thumbprint, "/fd", "SHA256",
                "/tr", stamp, "/td", "SHA256"]
    stamp = os.environ.get("WAYFINDER_SIGN_TIMESTAMP_URL", "http://timestamp.acs.microsoft.com")
    return [str(tool), "sign", "/fd", "SHA256", "/tr", stamp, "/td", "SHA256",
            "/dlib", dlib, "/dmdf", metadata]


def main() -> int:
    require_installer = "--require-installer" in sys.argv[1:]
    if sys.platform != "win32":
        print("The Windows installer build runs on Windows only.")
        return 1

    if require_installer and importlib.util.find_spec("llama_cpp") is None:
        print("llama_cpp is required for release builds (--require-installer). "
              "Install it with: python -m pip install -r requirements-windows.txt")
        return 1

    # The app icon (.exe, installer, window/taskbar): re-rendered every build
    # from the shared app-icon artwork, so it can never go stale again.
    sys.path.insert(0, str(HERE))
    import make_icon

    make_icon.main()

    _stage_whisper()
    # Release builds must carry the GPU build; a dev build without the Vulkan
    # SDK still produces a working (CPU-only) app.
    _stage_whisper_vulkan(required=require_installer)

    sign = _sign_command()
    if sign is None and os.environ.get("WAYFINDER_REQUIRE_SIGNING") == "1":
        print("WAYFINDER_REQUIRE_SIGNING=1 but no signing identity is configured.")
        return 1

    print("== PyInstaller: building the onedir bundle ==")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller",
         str(HERE / "wayfinder-aura-windows.spec"), "--clean", "--noconfirm"],
        cwd=str(ROOT), check=True,
    )

    if sign is not None:
        bundle = ROOT / "dist" / "Wayfinder Aura"
        exes = [bundle / "Wayfinder Aura.exe", *sorted((bundle / "_internal").glob("whisper*/*.exe"))]
        print(f"== Signing {len(exes)} executables ==")
        subprocess.run(sign + [str(path) for path in exes], check=True)
    else:
        print("(unsigned build: no signing identity configured)")

    iscc = _find_iscc()
    if iscc is None:
        if require_installer:
            print("Inno Setup (ISCC.exe) not found and --require-installer was passed.")
            return 1
        print("\nInno Setup (ISCC.exe) not found — the app bundle is ready at "
              "dist/Wayfinder Aura/. Install Inno Setup 6 to build the installer, "
              "or run: winget install JRSoftware.InnoSetup")
        return 0

    print("== Inno Setup: building the installer ==")
    version = _project_version()
    iscc_args = [str(iscc), f"/DMyAppVersion={version}"]
    if sign is not None:
        # Inno Setup signs Setup.exe and the uninstaller with the same identity
        # ($f is the file it hands over, already quoted).
        iscc_args += ["/DSignAura", f"/Saura={subprocess.list2cmdline(sign)} $f"]
    subprocess.run(iscc_args + [str(HERE / "installer.iss")], cwd=str(ROOT), check=True)
    print("\nDone → dist/installer/WayfinderAura-Setup-*.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

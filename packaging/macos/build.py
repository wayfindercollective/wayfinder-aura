#!/usr/bin/env python3
"""Build, validate, and optionally notarize the Wayfinder Aura macOS app."""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = PROJECT_ROOT / "build" / "macos-native"
NATIVE_BIN_DIR = BUILD_ROOT / "bin"
NATIVE_LIB_DIR = BUILD_ROOT / "lib"
WHISPER_SOURCE_DIR = BUILD_ROOT / "src" / "whisper.cpp"
WHISPER_BUILD_DIR = BUILD_ROOT / "whisper-build"
LLAMA_SOURCE_DIR = BUILD_ROOT / "src" / "llama.cpp"
LLAMA_BUILD_DIR = BUILD_ROOT / "llama-build"
APP_PATH = PROJECT_ROOT / "dist" / "Wayfinder Aura.app"
REQUIRED_PYTHON = (3, 12)
MINIMUM_TK = (8, 6, 16)
MINIMUM_MACOS = "14.0"

WHISPER_REPOSITORY = "https://github.com/ggerganov/whisper.cpp.git"
WHISPER_TAG = "v1.9.1"
WHISPER_COMMIT = "f049fff95a089aa9969deb009cdd4892b3e74916"
WHISPER_BINARIES = ("whisper-cli", "whisper-server")
LLAMA_REPOSITORY = "https://github.com/ggml-org/llama.cpp.git"
LLAMA_TAG = "b9608"
LLAMA_COMMIT = "70b54e140c90a92285ba699d77e1e32e0868a0e2"
LLAMA_BINARIES = ("llama-simple", "llama-server")
NATIVE_BINARIES = (*WHISPER_BINARIES, *LLAMA_BINARIES)


def run(args: list[str | Path], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    rendered = [str(value) for value in args]
    print("+", " ".join(rendered), flush=True)
    subprocess.run(rendered, cwd=cwd, env=env, check=True)


def output(args: list[str | Path]) -> str:
    return subprocess.check_output([str(value) for value in args], text=True).strip()


def checked_output(args: list[str | Path]) -> str:
    rendered = [str(value) for value in args]
    print("+", " ".join(rendered), flush=True)
    result = subprocess.run(rendered, check=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout.rstrip(), flush=True)
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr, flush=True)
    return result.stdout.strip()


def require_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit("The macOS bundle must be built on macOS.")
    if platform.machine() not in {"arm64", "x86_64"}:
        raise SystemExit(f"Unsupported build architecture: {platform.machine()}")
    if sys.version_info[:2] != REQUIRED_PYTHON:
        raise SystemExit(
            "The macOS UI release is pinned to Python 3.12; "
            f"this interpreter is {sys.version_info.major}.{sys.version_info.minor}. "
            "Create the build environment with python3.12."
        )
    try:
        import tkinter

        tcl = tkinter.Tcl()
        tk_version = tuple(
            int(part) for part in str(tcl.call("info", "patchlevel")).split(".")
        )
    except Exception as exc:
        raise SystemExit(f"A working Tk-enabled Python 3.12 build is required: {exc}") from exc
    if tk_version[:2] != MINIMUM_TK[:2] or tk_version < MINIMUM_TK:
        rendered = ".".join(str(part) for part in tk_version)
        required = ".".join(str(part) for part in MINIMUM_TK)
        raise SystemExit(
            f"The macOS UI requires Aqua Tk {required}+ in the 8.6 line; found {rendered}. "
            "Use the official Python.org Python 3.12 framework build."
        )
    for tool in ("cmake", "git", "hdiutil", "codesign", "xcrun", "file"):
        if shutil.which(tool) is None:
            raise SystemExit(f"Missing required build tool: {tool}")


def build_whisper() -> None:
    """Build the pinned whisper.cpp CLI and resident server with Metal."""
    if not WHISPER_SOURCE_DIR.exists():
        WHISPER_SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
        run([
            "git", "clone", "--filter=blob:none", "--branch", WHISPER_TAG,
            WHISPER_REPOSITORY, WHISPER_SOURCE_DIR,
        ])

    actual_commit = output(["git", "-C", WHISPER_SOURCE_DIR, "rev-parse", "HEAD"])
    if actual_commit != WHISPER_COMMIT:
        raise SystemExit(
            "Pinned whisper.cpp checkout does not match the expected commit: "
            f"{actual_commit} != {WHISPER_COMMIT}. Remove {WHISPER_SOURCE_DIR} and rebuild."
        )

    run([
        "cmake", "-S", WHISPER_SOURCE_DIR, "-B", WHISPER_BUILD_DIR,
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_OSX_DEPLOYMENT_TARGET={MINIMUM_MACOS}",
        "-DBUILD_SHARED_LIBS=OFF",
        # Never inherit the builder's CPU generation. An app compiled on a new
        # Apple chip must continue to launch on every supported arm64 Mac.
        "-DGGML_NATIVE=OFF",
        "-DGGML_METAL=ON",
        "-DWHISPER_BUILD_EXAMPLES=ON",
        "-DWHISPER_BUILD_TESTS=OFF",
    ])
    run([
        "cmake", "--build", WHISPER_BUILD_DIR, "--config", "Release",
        "--parallel", str(os.cpu_count() or 4),
    ])

    NATIVE_BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name in WHISPER_BINARIES:
        source = WHISPER_BUILD_DIR / "bin" / name
        if not source.is_file():
            raise SystemExit(f"whisper.cpp build did not produce {source}")
        shutil.copy2(source, NATIVE_BIN_DIR / name)


def build_llama() -> None:
    """Build the pinned llama.cpp cleanup CLI and resident server with Metal."""
    if not LLAMA_SOURCE_DIR.exists():
        LLAMA_SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
        run([
            "git", "clone", "--filter=blob:none", "--branch", LLAMA_TAG,
            LLAMA_REPOSITORY, LLAMA_SOURCE_DIR,
        ])

    actual_commit = output(["git", "-C", LLAMA_SOURCE_DIR, "rev-parse", "HEAD"])
    if actual_commit != LLAMA_COMMIT:
        raise SystemExit(
            "Pinned llama.cpp checkout does not match the expected commit: "
            f"{actual_commit} != {LLAMA_COMMIT}. Remove {LLAMA_SOURCE_DIR} and rebuild."
        )

    run([
        "cmake", "-S", LLAMA_SOURCE_DIR, "-B", LLAMA_BUILD_DIR,
        "-DCMAKE_BUILD_TYPE=Release",
        f"-DCMAKE_OSX_DEPLOYMENT_TARGET={MINIMUM_MACOS}",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DGGML_NATIVE=OFF",
        "-DGGML_METAL=ON",
        "-DLLAMA_BUILD_EXAMPLES=ON",
        # The resident llama-server keeps the cleanup model loaded (~0.2-0.5s
        # per dictation instead of ~2s reloading it through llama-simple). Its
        # web UI stays off (below), so the build needs no npm/network access.
        "-DLLAMA_BUILD_SERVER=ON",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_CURL=OFF",
        "-DLLAMA_BUILD_UI=OFF",
        "-DLLAMA_USE_PREBUILT_UI=OFF",
        # The resident server only binds loopback HTTP. Linking Homebrew
        # OpenSSL would make the app depend on a library absent on clean Macs.
        "-DLLAMA_OPENSSL=OFF",
    ])
    run([
        "cmake", "--build", LLAMA_BUILD_DIR, "--config", "Release",
        "--parallel", str(os.cpu_count() or 4),
        "--target", *LLAMA_BINARIES,
    ])

    NATIVE_BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name in LLAMA_BINARIES:
        source = LLAMA_BUILD_DIR / "bin" / name
        if not source.is_file():
            raise SystemExit(f"llama.cpp build did not produce {source}")
        shutil.copy2(source, NATIVE_BIN_DIR / name)


def build_hero_renderer() -> None:
    """Build native Metal schedulers used by the Mac hero and floating pill."""
    source = PROJECT_ROOT / "packaging" / "macos" / "hero_renderer.m"
    overlay_source = PROJECT_ROOT / "packaging" / "macos" / "overlay_renderer.m"
    output_path = NATIVE_LIB_DIR / "libwayfinder_hero.dylib"
    NATIVE_LIB_DIR.mkdir(parents=True, exist_ok=True)
    run([
        "xcrun", "clang",
        "-dynamiclib", "-fobjc-arc", "-O2",
        f"-mmacosx-version-min={MINIMUM_MACOS}",
        "-framework", "Cocoa",
        "-framework", "QuartzCore",
        "-framework", "Metal",
        source, overlay_source,
        "-o", output_path,
    ])


def remove_previous_app() -> None:
    """Clear the last bundle before PyInstaller rebuilds it.

    Once a bundle has been launched, macOS App Management protection can refuse
    to delete files inside it (EPERM), which aborts PyInstaller's cleanup. The
    bundle as a whole can still be moved, so fall back to the user's Trash.
    """
    if not APP_PATH.exists():
        return
    try:
        shutil.rmtree(APP_PATH)
        return
    except PermissionError:
        pass
    trash = Path.home() / ".Trash"
    target = trash / f"{APP_PATH.stem} (previous build {os.getpid()}){APP_PATH.suffix}"
    shutil.move(str(APP_PATH), str(target))
    print(f"Previous app bundle is protected by macOS; moved it to {target}", flush=True)


def build_app() -> None:
    pyinstaller = Path(sys.executable).with_name("pyinstaller")
    if not pyinstaller.is_file():
        raise SystemExit(
            f"PyInstaller is not installed for {sys.executable}. "
            "Run: python -m pip install -e '.[dev]'"
        )
    remove_previous_app()
    env = os.environ.copy()
    env.setdefault("AURA_MACOS_ARCH", platform.machine())
    run([
        pyinstaller,
        PROJECT_ROOT / "wayfinder-aura-macos.spec",
        "--clean",
        "--noconfirm",
    ], cwd=PROJECT_ROOT, env=env)


def broken_symlinks(root: Path) -> list[str]:
    broken: list[str] = []
    for path in root.rglob("*"):
        if path.is_symlink() and not path.exists():
            broken.append(f"{path.relative_to(root)} -> {os.readlink(path)}")
    return broken


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def validate_native_deployment_targets(root: Path, declared: str) -> None:
    """Fail when any bundled Mach-O requires a newer OS than Info.plist."""
    declared_version = _version_tuple(declared)
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            if "Mach-O" not in output(["file", "-b", path]):
                continue
            details = output(["xcrun", "vtool", "-show-build", path])
        except (OSError, subprocess.CalledProcessError):
            continue
        match = re.search(r"^\s*minos\s+([0-9.]+)\s*$", details, re.MULTILINE)
        if match and _version_tuple(match.group(1)) > declared_version:
            offenders.append(
                f"{path.relative_to(root)} requires {match.group(1)}"
            )
    if offenders:
        raise SystemExit(
            f"Bundle declares macOS {declared}, but native files require newer:\n  "
            + "\n  ".join(offenders[:40])
        )


def validate_app() -> None:
    if not APP_PATH.is_dir():
        raise SystemExit(f"Bundle was not created: {APP_PATH}")

    plist_path = APP_PATH / "Contents" / "Info.plist"
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    expected = {
        "CFBundleIdentifier": "io.wayfindercollective.WayfinderAura",
        "NSMicrophoneUsageDescription": "Wayfinder Aura needs microphone access for voice dictation.",
    }
    for key, value in expected.items():
        if plist.get(key) != value:
            raise SystemExit(f"Info.plist {key} is {plist.get(key)!r}, expected {value!r}")
    if plist.get("LSMinimumSystemVersion") != MINIMUM_MACOS:
        raise SystemExit(
            "Info.plist minimum macOS version does not match the release stack: "
            f"{plist.get('LSMinimumSystemVersion')!r} != {MINIMUM_MACOS!r}"
        )

    broken = broken_symlinks(APP_PATH)
    if broken:
        raise SystemExit("Broken bundle symlinks:\n  " + "\n  ".join(broken))
    validate_native_deployment_targets(APP_PATH / "Contents", MINIMUM_MACOS)

    executable = APP_PATH / "Contents" / "MacOS" / "Wayfinder Aura"
    resources = APP_PATH / "Contents" / "Resources"
    for name in NATIVE_BINARIES:
        bundled = resources / "bin" / name
        if not bundled.is_file() or not os.access(bundled, os.X_OK):
            raise SystemExit(f"Bundle is missing executable {bundled}")
    hero_renderer = resources / "lib" / "libwayfinder_hero.dylib"
    if not hero_renderer.is_file():
        raise SystemExit(f"Bundle is missing native hero renderer {hero_renderer}")

    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", APP_PATH])
    run([executable, "--audio-processing-self-test"])
    run([executable, "--app-import-self-test"])
    renderer = checked_output([executable, "--ui-renderer-self-test"])
    if "family='DejaVu Sans'" not in renderer:
        raise SystemExit(
            "Bundled UI font registration failed; renderer did not resolve DejaVu Sans:\n"
            + renderer
        )
    run([executable, "--runtime-assets-self-test"])
    run([executable, "--macos-native-renderers-self-test"])
    run([resources / "bin" / "whisper-cli", "--help"])


def create_dmg() -> Path:
    version = "0.0.0"
    with (APP_PATH / "Contents" / "Info.plist").open("rb") as handle:
        version = str(plistlib.load(handle)["CFBundleShortVersionString"])
    architecture = platform.machine()
    dmg_path = PROJECT_ROOT / "dist" / f"Wayfinder_Aura-{version}-macOS-{architecture}.dmg"
    if dmg_path.exists():
        dmg_path.unlink()

    with tempfile.TemporaryDirectory(prefix="wayfinder-aura-dmg-") as temp_dir:
        stage = Path(temp_dir)
        try:
            import dmgbuild
        except ImportError:
            dmgbuild = None
        if dmgbuild is not None:
            # Branded Finder window: dark background, guide arrow, fixed
            # icon layout and the app icon as the volume icon.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from dmg_assets import dmgbuild_settings, write_background

            background = write_background(stage / "art")
            settings = dmgbuild_settings(
                APP_PATH, background, PROJECT_ROOT / "assets" / "icon.icns"
            )
            print(f"+ dmgbuild {dmg_path}", flush=True)
            dmgbuild.build_dmg(str(dmg_path), "Wayfinder Aura", settings=settings)
        else:
            print("dmgbuild not installed; building a plain disk image", flush=True)
            shutil.copytree(APP_PATH, stage / APP_PATH.name, symlinks=True)
            os.symlink("/Applications", stage / "Applications")
            run([
                "hdiutil", "create", "-volname", "Wayfinder Aura",
                "-srcfolder", stage, "-ov", "-format", "UDZO", dmg_path,
            ])
    identity = os.environ.get("MACOS_CODESIGN_IDENTITY")
    if identity:
        run(["codesign", "--force", "--timestamp", "--sign", identity, dmg_path])
    run(["hdiutil", "verify", dmg_path])
    return dmg_path


def notarize(dmg_path: Path, profile: str) -> None:
    run(["xcrun", "notarytool", "submit", dmg_path, "--keychain-profile", profile, "--wait"])
    run(["xcrun", "stapler", "staple", APP_PATH])
    run(["xcrun", "stapler", "staple", dmg_path])
    run(["spctl", "--assess", "--type", "execute", "--verbose=2", APP_PATH])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-native-build", action="store_true", help="reuse build/macos-native/bin")
    parser.add_argument("--no-dmg", action="store_true", help="stop after validating the .app")
    parser.add_argument(
        "--notarize-profile",
        default=os.environ.get("MACOS_NOTARY_PROFILE", ""),
        help="notarytool keychain profile (or MACOS_NOTARY_PROFILE)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_macos()
    if not args.skip_native_build:
        build_whisper()
        build_llama()
    build_hero_renderer()
    for name in NATIVE_BINARIES:
        if not (NATIVE_BIN_DIR / name).is_file():
            raise SystemExit(f"Missing {NATIVE_BIN_DIR / name}; build without --skip-native-build")
    build_app()
    validate_app()
    dmg_path = None if args.no_dmg else create_dmg()
    if args.notarize_profile:
        if dmg_path is None:
            raise SystemExit("Notarization requires a DMG; remove --no-dmg")
        if not os.environ.get("MACOS_CODESIGN_IDENTITY"):
            raise SystemExit("Notarization requires MACOS_CODESIGN_IDENTITY")
        notarize(dmg_path, args.notarize_profile)

    print(f"macOS app ready: {APP_PATH}")
    if dmg_path:
        print(f"macOS disk image ready: {dmg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

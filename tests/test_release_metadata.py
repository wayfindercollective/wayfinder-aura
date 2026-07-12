"""Static checks for release-facing packaging metadata."""

from __future__ import annotations

import re
import json
import importlib.util
import shutil
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
APP_ID = "io.wayfindercollective.WayfinderAura"
FLATPAK_MANIFEST = REPO / "flatpak" / f"{APP_ID}.yml"
FLATPAK_METAINFO = REPO / "flatpak" / f"{APP_ID}.metainfo.xml"
SOURCE_DESKTOP = REPO / "wayfinder-aura.desktop"
FLATPAK_DESKTOP = REPO / "flatpak" / f"{APP_ID}.desktop"
PYTHON_DEPS = REPO / "flatpak" / "python-deps.json"
FLATPAK_REQUIREMENTS = REPO / "flatpak" / "flatpak-requirements.txt"
RELEASE_SHELL_SCRIPTS = [
    REPO / "build-appimage.sh",
    REPO / "build.sh",
    REPO / "scripts" / "ci" / "install-glslc-if-needed.sh",
    REPO / "flatpak" / "generate-pip-sources.sh",
    REPO / "flatpak" / "wayfinder-aura-launcher.sh",
    REPO / "scripts" / "build-appimage.sh",
    REPO / "scripts" / "steamdeck" / "install-steamdeck.sh",
    REPO / "scripts" / "steamdeck" / "uninstall-steamdeck.sh",
    REPO / "scripts" / "wayfinder-aura-launcher-steamdeck.sh",
]
RELEASE_PYTHON_SCRIPTS = [
    REPO / "scripts" / "ci" / "check-release-license-defaults.py",
    REPO / "scripts" / "ci" / "check-storefront-readiness.py",
    REPO / "scripts" / "ship_preflight.py",
    REPO / "flatpak" / "prepare-release-manifest.py",
    REPO / "scripts" / "steamdeck" / "wayfinder-aura-show-or-start.py",
]


def _manifest_text() -> str:
    return FLATPAK_MANIFEST.read_text(encoding="utf-8")


def _workflow_text() -> str:
    return (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _workflow_job_body(name: str) -> str:
    workflow = _workflow_text()
    match = re.search(
        rf"(?ms)^  {re.escape(name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)",
        workflow,
    )
    assert match, f"workflow must define the {name} job"
    return match.group("body")


def test_appimage_metadata_copies_authoritative_desktop_and_metainfo():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")
    metainfo = ET.parse(FLATPAK_METAINFO).getroot()

    assert f'APP_ID="{APP_ID}"' in script
    assert 'SOURCE_DESKTOP="flatpak/${APP_ID}.desktop"' in script
    assert 'SOURCE_METAINFO="flatpak/${APP_ID}.metainfo.xml"' in script
    assert 'cp "$SOURCE_DESKTOP" "$APPDIR/${APP_ID}.desktop"' in script
    assert 'cp "$SOURCE_DESKTOP" "$APPDIR/usr/share/applications/${APP_ID}.desktop"' in script
    assert 'cp "$SOURCE_METAINFO" "$APPDIR/usr/share/metainfo/${APP_ID}.metainfo.xml"' in script
    assert "io.github.wayfinder.Aura" not in script

    assert metainfo.findtext("id") == APP_ID
    assert metainfo.findtext("metadata_license") == "CC0-1.0"
    assert metainfo.findtext("project_license") == "Elastic-2.0"
    assert metainfo.findtext("launchable") == f"{APP_ID}.desktop"
    assert metainfo.find("content_rating") is not None
    assert metainfo.find("branding") is not None
    assert metainfo.find("screenshots") is not None
    assert metainfo.find("releases/release") is not None
    assert "<metadata_license>MIT</metadata_license>" not in script
    assert "<project_license>MIT</project_license>" not in script


def test_root_appimage_builder_delegates_to_maintained_script():
    root_path = REPO / "build-appimage.sh"
    root_script = root_path.read_text(encoding="utf-8")
    maintained = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    assert root_path.stat().st_mode & 0o111, "root AppImage wrapper must be executable"
    assert 'exec "$SCRIPT_DIR/scripts/build-appimage.sh" "$@"' in root_script
    assert "APPIMAGETOOL_URL" not in root_script
    assert "cat > \"$APPDIR/usr/share/applications" not in root_script
    assert "--skip-build" in maintained
    assert "SKIP_BUILD=1" in maintained


def test_release_shell_scripts_parse_with_bash():
    for script in RELEASE_SHELL_SCRIPTS:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"{script.relative_to(REPO)}: {result.stderr}"


def test_release_python_scripts_compile():
    for script in RELEASE_PYTHON_SCRIPTS:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(script)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"{script.relative_to(REPO)}: {result.stderr}"


def test_desktop_entries_validate_when_tool_is_available():
    validator = shutil.which("desktop-file-validate")
    if not validator:
        return

    # Flatpak desktop is install-ready as checked in.
    result = subprocess.run(
        [validator, str(FLATPAK_DESKTOP)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"{FLATPAK_DESKTOP.relative_to(REPO)}: {result.stderr}"

    # Source desktop is a template (@WAYFINDER_ROOT@); validate after rewrite.
    import tempfile
    from pathlib import Path

    rewritten = SOURCE_DESKTOP.read_text(encoding="utf-8").replace(
        "@WAYFINDER_ROOT@", "/opt/wayfinder-aura"
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".desktop", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(rewritten)
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            [validator, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"rewritten source desktop: {result.stderr}"
    finally:
        tmp_path.unlink(missing_ok=True)


def test_source_desktop_actions_match_build_rewrite_contract():
    desktop = SOURCE_DESKTOP.read_text(encoding="utf-8")
    build = (REPO / "build.sh").read_text(encoding="utf-8")

    assert "Actions=toggle-recording;cycle-style;hide-to-tray;" in desktop
    assert "Exec=@WAYFINDER_ROOT@/launch-wayfinder-aura.sh" in desktop
    assert "Exec=python3 @WAYFINDER_ROOT@/trigger_record.py" in desktop
    assert "Exec=python3 @WAYFINDER_ROOT@/trigger_style.py" in desktop
    assert "Exec=python3 @WAYFINDER_ROOT@/trigger_hide.py" in desktop
    assert "Icon=@WAYFINDER_ROOT@/assets/icon.png" in desktop
    assert "/home/bazzite/" not in desktop

    assert 'DESKTOP_DEST="$HOME/.local/share/applications/wayfinder-aura.desktop"' in build
    assert 'sed "s|@WAYFINDER_ROOT@|$SCRIPT_DIR|g"' in build
    assert '"$SCRIPT_DIR/wayfinder-aura.desktop" > "$DESKTOP_DEST"' in build


def test_flatpak_desktop_actions_use_current_app_id_and_cli_hooks():
    desktop = FLATPAK_DESKTOP.read_text(encoding="utf-8")

    assert "Exec=wayfinder-aura" in desktop
    assert "Exec=wayfinder-aura --toggle" in desktop
    assert "Exec=wayfinder-aura --cycle-style" in desktop
    assert "Exec=wayfinder-aura --hide" in desktop
    assert "hide-to-tray" in desktop
    assert f"Icon={APP_ID}" in desktop
    assert "/home/bazzite/Dev/wayfinder-aura" not in desktop
    assert "io.github.wayfinder" not in desktop


def test_macos_bundle_identifier_uses_current_app_id():
    spec = (REPO / "wayfinder-aura-macos.spec").read_text(encoding="utf-8")

    assert f"bundle_identifier='{APP_ID}'" in spec
    assert f"'CFBundleIdentifier': '{APP_ID}'" in spec
    assert "io.github.wayfindercollective" not in spec


def test_dev_unlock_backdoor_strings_are_not_shipped():
    release_surfaces = [
        REPO / "src" / "wayfinder" / "license.py",
        REPO / "src" / "wayfinder" / "config.py",
        REPO / "wayfinder_main.py",
    ]
    forbidden = ("DEV-UNLOCK", "WAYFINDER_DEV_UNLOCK", "dev_unlock")

    for path in release_surfaces:
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{needle!r} remains in {path.relative_to(REPO)}"


def test_global_shortcut_trigger_scripts_notify_on_unreachable_service():
    for script_name, command in (
        ("trigger_record.py", b"toggle"),
        ("trigger_style.py", b"style"),
    ):
        script = (REPO / script_name).read_text(encoding="utf-8")

        assert "notify-send" in script
        assert "Wayfinder Aura" in script
        assert "Service not running" in script
        assert "wayfinder-aura.sock" in script
        assert "XDG_RUNTIME_DIR" in script
        assert repr(command.decode("utf-8")).strip("'") in script


def test_legacy_app_socket_listener_has_health_probe_restart_path():
    main = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")
    listener = (REPO / "src" / "wayfinder" / "hotkeys" / "socket.py").read_text(
        encoding="utf-8"
    )

    assert 'data_str == "ping"' in listener
    assert 'conn.sendall(b"pong")' in listener
    assert "def _socket_listener_healthy" in main
    assert 'client.sendall(b"ping")' in main
    assert 'client.recv(16) == b"pong"' in main
    assert "Socket listener unreachable - restarting" in main
    assert "force_restart=True" in main


def test_appimage_native_dependency_sources_are_commit_pinned():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")
    manifest = _manifest_text()

    for commit in (
        "f049fff95a089aa9969deb009cdd4892b3e74916",  # whisper.cpp v1.9.1
        "70b54e140c90a92285ba699d77e1e32e0868a0e2",  # llama.cpp b9608
    ):
        assert commit in script
        assert commit in manifest

    assert 'WHISPER_TAG="v1.9.1"' in script
    assert 'LLAMA_TAG="b9608"' in script
    assert "https://github.com/ggml-org/llama.cpp.git" in script
    assert "https://github.com/ggerganov/llama.cpp.git" not in script
    assert "git clone --depth 1 --branch" in script
    assert "git -C \"$dest\" checkout --detach \"$commit\"" in script


def test_appimage_full_build_has_cpu_fallback_when_vulkan_toolchain_is_missing():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    assert "cmake_native_build()" in script
    assert '-DGGML_VULKAN="$vulkan"' in script
    assert 'WHISPER_BUILD="$WHISPER_DIR/build-vulkan"' in script
    assert 'WHISPER_BUILD="$WHISPER_DIR/build-cpu"' in script
    assert "Falling back to CPU-only whisper.cpp" in script
    assert 'LLAMA_BUILD="$LLAMA_DIR/build-vulkan"' in script
    assert 'LLAMA_BUILD="$LLAMA_DIR/build-cpu"' in script
    assert "Falling back to CPU-only llama.cpp" in script


def test_appimage_builder_prints_build_mode_after_argument_parsing():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    parse_loop = script.index("# ─── Parse arguments")
    banner = script.index("Wayfinder Aura - AppImage Builder")
    assert parse_loop < banner


def test_appimage_builder_fails_fast_without_tkinter():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    assert "import tkinter" in script
    assert "python3 tkinter support not found" in script
    assert "sudo dnf install python3-tkinter" in script
    assert "sudo apt install python3-tk" in script


def test_premium_storefront_defaults_are_consistent_across_release_surfaces():
    from wayfinder.config import DEFAULT_CONFIG

    readme = (REPO / "README.md").read_text(encoding="utf-8")
    main = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")

    checkout = DEFAULT_CONFIG["premium_url"]
    info_url = DEFAULT_CONFIG["premium_info_url"]
    launch_price = DEFAULT_CONFIG["premium_price"]
    regular_price = DEFAULT_CONFIG["premium_price_regular"]

    assert checkout.startswith("https://wayfindercollective.io/checkout/")
    assert info_url == "https://wayfindercollective.io/aura"
    assert launch_price in readme
    assert regular_price in readme
    assert "wayfinder.dev" not in readme

    for key, fallback in (
        ("premium_url", checkout),
        ("premium_info_url", info_url),
        ("premium_price", launch_price),
        ("premium_price_regular", regular_price),
    ):
        assert f'self.config.get("{key}", "{fallback}")' in main


def test_pyinstaller_spec_keeps_linux_tray_dbus_integrations_optional():
    spec = (REPO / "wayfinder-aura.spec").read_text(encoding="utf-8")

    assert "HAS_GLIB_DBUS" in spec
    assert "_distribution_available('pygobject')" in spec
    assert "OPTIONAL_HIDDENIMPORTS" in spec
    assert "OPTIONAL_EXCLUDES" in spec
    assert "OPTIONAL_EXCLUDES += ['dbus', 'gi']" in spec
    assert "sys.platform.startswith('linux')" in spec
    assert "OPTIONAL_EXCLUDES.append('pystray')" in spec

    hiddenimports_block = re.search(
        r"hiddenimports=\[(?P<body>.*?)\]\s*\+ OPTIONAL_HIDDENIMPORTS",
        spec,
        re.DOTALL,
    )
    assert hiddenimports_block, "spec must append optional hidden imports"
    for module in ("pystray", "dbus", "gi", "gi.repository.GLib"):
        assert f"'{module}'" not in hiddenimports_block.group("body")


def test_tagged_github_release_is_gated_by_release_readiness_check():
    workflow = _workflow_text()

    assert re.search(r"(?m)^    tags:\n      - \"v\*\"$", workflow)
    assert "release-readiness:" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "Release readiness check only runs for version tags and manual artifact builds." in workflow
    assert "python scripts/ci/check-release-license-defaults.py" in workflow
    assert "python -m pip install playwright" in workflow
    assert "python -m playwright install --with-deps chromium" in workflow
    assert "python scripts/ci/check-storefront-readiness.py --browser --timeout 30" in workflow
    assert "python flatpak/prepare-release-manifest.py" in workflow
    assert '--tag "${GITHUB_REF_NAME}"' in workflow
    assert "--output /tmp/wayfinder-flathub/io.wayfindercollective.WayfinderAura.yml" in workflow
    assert "--allow-dev-license" not in workflow

    release_job = _workflow_job_body("release")
    for job_name in ("release", "build-pyinstaller", "build-appimage", "build-flatpak"):
        assert "release-readiness" in _workflow_job_body(job_name)

    assert release_job, "workflow must define the tag release job"
    assert "softprops/action-gh-release@v3" in release_job


def test_release_artifact_jobs_do_not_build_dev_license_artifacts_on_normal_pushes():
    pyinstaller_job = _workflow_job_body("build-pyinstaller")
    appimage_job = _workflow_job_body("build-appimage")

    artifact_condition = "startsWith(github.ref, 'refs/tags/v') || github.event_name == 'workflow_dispatch'"
    assert f"if: {artifact_condition}" in pyinstaller_job
    assert f"if: {artifact_condition}" in appimage_job


def test_release_license_defaults_checker_rejects_dev_and_accepts_non_dev(tmp_path):
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    assert checker.stat().st_mode & 0o111, "license defaults checker must be executable"

    dev_license = tmp_path / "license-dev.py"
    dev_license.write_text(
        """
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "https://valuable-stoat-578.convex.site/activate")
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(dev_license)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "release artifacts require production license defaults" in result.stderr

    prod_license = tmp_path / "license-prod.py"
    prod_license.write_text(
        """
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "https://license.example.com/activate")
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(prod_license)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "release license defaults are production-ready" in result.stdout


def _load_storefront_checker():
    checker = REPO / "scripts" / "ci" / "check-storefront-readiness.py"
    spec = importlib.util.spec_from_file_location("check_storefront_readiness", checker)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storefront_readiness_checker_rejects_unproven_checkout_payload():
    checker = _load_storefront_checker()
    defaults = {
        "premium_url": "https://wayfindercollective.io/checkout/product",
        "premium_info_url": "https://wayfindercollective.io/aura",
        "premium_price": "$29.99",
        "premium_price_regular": "$60",
    }
    pages = {
        defaults["premium_info_url"]: "Wayfinder Aura - Your voice, turned to text",
        defaults["premium_url"]: "Wayfinder Loading checkout...",
    }

    errors = checker.live_readiness_errors(
        defaults=defaults,
        fetcher=lambda url, _timeout: (200, pages[url]),
    )

    assert any("Wayfinder Aura" in error for error in errors)
    assert any("Pay with card" in error for error in errors)
    assert any("$29.99" in error for error in errors)


def test_storefront_readiness_checker_accepts_client_rendered_checkout_markers():
    checker = _load_storefront_checker()
    defaults = {
        "premium_url": "https://wayfindercollective.io/checkout/product",
        "premium_info_url": "https://wayfindercollective.io/aura",
        "premium_price": "$29.99",
        "premium_price_regular": "$60",
    }
    html_pages = {
        defaults["premium_info_url"]: "Loading the latest release...",
        defaults["premium_url"]: "Loading checkout...",
    }
    rendered_pages = {
        defaults["premium_info_url"]: "Wayfinder Aura\nYour voice, turned to text",
        defaults["premium_url"]: "Wayfinder Aura\nOne-time license\nPay with card\n$29.99 launch",
    }

    errors = checker.live_readiness_errors(
        defaults=defaults,
        fetcher=lambda url, _timeout: (200, html_pages[url]),
        renderer=lambda url, _timeout: rendered_pages[url],
        browser=True,
    )

    assert errors == []


def test_storefront_readiness_checker_accepts_release_markers(tmp_path):
    checker = REPO / "scripts" / "ci" / "check-storefront-readiness.py"
    assert checker.stat().st_mode & 0o111, "storefront readiness checker must be executable"

    config = tmp_path / "config.py"
    readme = tmp_path / "README.md"
    main = tmp_path / "wayfinder_main.py"
    config.write_text(
        """
DEFAULT_CONFIG = {
    "premium_url": "https://wayfindercollective.io/checkout/product",
    "premium_info_url": "https://wayfindercollective.io/aura",
    "premium_price": "$29.99",
    "premium_price_regular": "$60",
}
""",
        encoding="utf-8",
    )
    readme.write_text("Ultra is $29.99 at launch and $60 regularly.", encoding="utf-8")
    main.write_text(
        """
self.config.get("premium_url", "https://wayfindercollective.io/checkout/product")
self.config.get("premium_info_url", "https://wayfindercollective.io/aura")
self.config.get("premium_price", "$29.99")
self.config.get("premium_price_regular", "$60")
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(checker),
            "--config-file",
            str(config),
            "--readme-file",
            str(readme),
            "--main-file",
            str(main),
            "--skip-network",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "storefront release surfaces are ready" in result.stdout


def test_appimage_ci_build_uses_older_glibc_runner_and_smoke_test():
    job = _workflow_job_body("build-appimage")

    assert "runs-on: ubuntu-22.04" in job
    assert "libvulkan-dev vulkan-tools" in job
    assert "scripts/ci/install-glslc-if-needed.sh" in job
    assert "ydotool wtype" in job
    assert "desktop-file-utils appstream" in job
    assert "./scripts/build-appimage.sh --full" in job
    assert "./Wayfinder_Aura-*.AppImage --appimage-extract" in job

    for binary in (
        "wayfinder-aura",
        "whisper-cli",
        "llama-cli",
        "llama-simple",
        "wtype",
        "ydotool",
    ):
        assert f"test -x squashfs-root/usr/bin/{binary}" in job

    assert "desktop-file-validate squashfs-root/io.wayfindercollective.WayfinderAura.desktop" in job
    assert (
        "appstreamcli validate --no-net "
        "squashfs-root/usr/share/metainfo/io.wayfindercollective.WayfinderAura.metainfo.xml"
    ) in job


def test_appimage_ci_glslc_helper_is_pinned_and_builds_shaderc_when_needed():
    script_path = REPO / "scripts" / "ci" / "install-glslc-if-needed.sh"
    script = script_path.read_text(encoding="utf-8")

    assert script_path.stat().st_mode & 0o111, "glslc helper must be executable"
    assert 'SHADERC_TAG="${SHADERC_TAG:-v2024.4}"' in script
    assert 'SHADERC_COMMIT="${SHADERC_COMMIT:-caa54d9779d5605aca4e1a0c0c962a3d8f4aeb31}"' in script
    assert "https://github.com/google/shaderc.git" in script
    assert "git clone --depth 1 --branch \"$SHADERC_TAG\"" in script
    assert "git -C \"$SOURCE_DIR\" checkout --detach \"$SHADERC_COMMIT\"" in script
    assert "python3 \"$SOURCE_DIR/utils/git-sync-deps\"" in script
    assert "-DSHADERC_SKIP_TESTS=ON" in script
    assert "-DSHADERC_SKIP_EXAMPLES=ON" in script
    assert "cmake --build \"$BUILD_DIR\" --target glslc" in script
    assert "glslc --version" in script


def test_flatpak_ci_builds_tag_sourced_release_manifest_on_tags():
    job = _workflow_job_body("build-flatpak")

    assert "fetch-depth: 0" in job
    assert "actions/setup-python@v5" in job
    assert "id: flatpak-manifest" in job
    assert 'if [[ "$GITHUB_REF" == refs/tags/v* ]]; then' in job
    assert "python3 flatpak/prepare-release-manifest.py" in job
    assert '--tag "$GITHUB_REF_NAME"' in job
    assert "--output flatpak/release/io.wayfindercollective.WayfinderAura.yml" in job
    assert "manifest=release/io.wayfindercollective.WayfinderAura.yml" in job
    assert "manifest=io.wayfindercollective.WayfinderAura.yml" in job
    assert 'flatpak-builder --user --force-clean --jobs=2 --repo=repo build-dir "${{ steps.flatpak-manifest.outputs.manifest }}"' in job


def test_appimage_version_matches_pyproject():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")
    match = re.search(r'^VERSION="([^"]+)"$', script, re.MULTILINE)
    assert match, "scripts/build-appimage.sh must declare VERSION"

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert match.group(1) == pyproject["project"]["version"]


def test_metainfo_screenshots_are_local_pngs_with_release_sized_dimensions():
    metainfo = ET.parse(FLATPAK_METAINFO)
    urls = [image.text or "" for image in metainfo.findall(".//image")]

    assert urls, "AppStream metainfo must include screenshots"
    assert len(urls) == 5, f"expected 5 store screenshots, found {len(urls)}"

    basenames = set()
    for url in urls:
        prefix = "https://raw.githubusercontent.com/wayfindercollective/wayfinder-aura/main/"
        assert url.startswith(prefix)

        rel_path = Path(url.removeprefix(prefix))
        assert rel_path.parts[0] == "screenshots"
        basenames.add(rel_path.name)
        screenshot = REPO / rel_path
        assert screenshot.exists(), f"missing screenshot referenced by metainfo: {rel_path}"

        with Image.open(screenshot) as image:
            assert image.format == "PNG"
            width, height = image.size

        assert width >= 1248
        assert height >= 702

    required = {
        "main-window.png",
        "settings.png",
        "overlay.png",
        "style.png",
        "welcome.png",
    }
    assert required <= basenames, f"missing required screenshots: {required - basenames}"



def test_metainfo_branding_matches_icon_blue():
    """Store brand primary must match tray/icon blue (not violet or cyan)."""
    root = ET.parse(FLATPAK_METAINFO).getroot()
    colors = [c.text for c in root.findall(".//branding/color") if c.text]
    assert colors, "AppStream branding colors required"
    banned = {
        "#00D4FF", "#0099CC", "#00d4ff", "#0099cc",  # legacy cyan
        "#A78BFA", "#6D28D9", "#a78bfa", "#6d28d9",  # short-lived violet
    }
    for color in colors:
        assert color not in banned, f"branding color regression: {color}"
    dark = root.find('.//branding/color[@scheme_preference="dark"]')
    assert dark is not None and dark.text
    assert dark.text.upper() == "#4682DC"


def test_public_docs_use_ultra_not_premium_tier_name():
    """Public product tier is Ultra; internal premium_* API names stay."""
    docs = [
        REPO / "README.md",
        REPO / "PRIVACY.md",
        REPO / "SUPPORT.md",
        REPO / "WEBSITE_COPY_BRIEF.md",
    ]
    # Word "Premium" as a product tier (not substrings of premium_url etc. — those
    # files should not contain the bare marketing word).
    for path in docs:
        text = path.read_text(encoding="utf-8")
        assert "Premium" not in text, f"{path.name} still uses public tier name Premium"


def test_metainfo_summary_is_outcome_led():
    summary = ET.parse(FLATPAK_METAINFO).getroot().findtext("summary") or ""
    assert "cursor" in summary.lower() or "dictat" in summary.lower()
    assert len(summary) >= 20


def test_screenshot_capture_recipe_documents_welcome_profile_split():
    readme = (REPO / "screenshots" / "README.md").read_text(encoding="utf-8")
    assert "welcome_completed" in readme
    assert "overlay.png" in readme
    assert "base.en" in readme or "ggml-base.en" in readme
    assert "false" in readme.lower()


def test_metainfo_release_date_is_iso8601():
    release = ET.parse(FLATPAK_METAINFO).getroot().find("releases/release")
    assert release is not None
    assert date.fromisoformat(release.attrib["date"])


def test_flatpak_runtime_baseapp_and_permissions_are_release_safe():
    manifest = _manifest_text()

    for expected in (
        f"app-id: {APP_ID}",
        "runtime: org.kde.Platform",
        "runtime-version: '6.10'",
        "sdk: org.kde.Sdk",
        "base: com.riverbankcomputing.PyQt.BaseApp",
        "base-version: '6.10'",
        "BASEAPP_REMOVE_WEBENGINE: '1'",
        "- --socket=wayland",
        "- --socket=fallback-x11",
        "- --socket=pulseaudio",
        "- --device=dri",
        "- --filesystem=xdg-run/wayfinder-aura:create",
        "- --talk-name=org.kde.StatusNotifierWatcher",
        "- --talk-name=org.freedesktop.Notifications",
    ):
        assert expected in manifest

    assert "--filesystem=home" not in manifest
    assert "--filesystem=host" not in manifest
    assert "--filesystem=xdg-config/wayfinder-aura" not in manifest
    assert "--filesystem=xdg-cache/wayfinder-aura:create" not in manifest
    assert "--talk-name=org.freedesktop.Flatpak" not in manifest
    assert "--talk-name=org.freedesktop.portal." not in manifest
    assert re.search(r"(?m)^  - name: ydotool$", manifest) is None
    assert re.search(r"(?m)^  - name: wtype$", manifest)
    assert re.search(r"(?m)^  - name: xdotool$", manifest)
    assert "whisper-cli-cpu" in manifest
    assert "whisper-server-cpu" in manifest
    assert "llama-simple-cpu" in manifest


def test_flatpak_git_sources_are_all_tag_and_commit_pinned():
    manifest = _manifest_text()
    blocks = re.findall(r"(?m)^      - type: git\n((?:        .+\n)+)", manifest)

    assert len(blocks) >= 8, "expected all native git sources to be visible to this guard"
    for block in blocks:
        assert re.search(r"^        url: https://", block, re.MULTILINE), block
        assert re.search(r"^        tag: .+", block, re.MULTILINE), block
        assert re.search(r"^        commit: [0-9a-f]{40}$", block, re.MULTILINE), block


def test_flatpak_python_deps_are_offline_hashed_and_exclude_pyqt():
    deps_text = PYTHON_DEPS.read_text(encoding="utf-8")
    requirements = FLATPAK_REQUIREMENTS.read_text(encoding="utf-8")
    deps = json.loads(deps_text)

    assert deps["name"] == "python-deps"
    assert "PyQt6" not in deps_text
    assert "PyQt6" not in requirements
    assert "--share=network" not in deps_text
    assert "evdev" not in requirements
    assert "pyautogui" not in requirements
    assert "llama-cpp-python" not in requirements

    modules = deps.get("modules", [])
    assert modules, "python-deps.json must contain generated pip modules"
    for module in modules:
        for command in module.get("build-commands", []):
            assert "--no-index" in command
            assert "--find-links=\"file://${PWD}\"" in command
        for source in module.get("sources", []):
            assert source.get("type") == "file"
            assert source.get("url", "").startswith("https://")
            assert re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))

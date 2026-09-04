"""Static checks for release-facing packaging metadata."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pytest
import tomllib
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
    REPO / "scripts" / "ci" / "build-flatpak-candidate.sh",
    REPO / "scripts" / "ci" / "build-flatpak-on-mini-inf.sh",
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
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPO / ".github" / "workflows" / "release.yml"
FLATPAK_BUILD_WORKFLOW = REPO / ".github" / "workflows" / "flatpak-build.yml"
FLATPAK_BUILD_SCRIPT = REPO / "scripts" / "ci" / "build-flatpak-candidate.sh"
MINI_INF_BUILD_SCRIPT = REPO / "scripts" / "ci" / "build-flatpak-on-mini-inf.sh"


def _manifest_text() -> str:
    return FLATPAK_MANIFEST.read_text(encoding="utf-8")


def _ci_workflow_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def _release_workflow_text() -> str:
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def _flatpak_build_workflow_text() -> str:
    return FLATPAK_BUILD_WORKFLOW.read_text(encoding="utf-8")


def _flatpak_build_script_text() -> str:
    return FLATPAK_BUILD_SCRIPT.read_text(encoding="utf-8")


def _workflow_text() -> str:
    return (
        f"{_ci_workflow_text()}\n"
        f"{_release_workflow_text()}\n"
        f"{_flatpak_build_workflow_text()}"
    )


def _workflow_job_body(name: str) -> str:
    workflow = (
        _flatpak_build_workflow_text()
        if name == "build-flatpak"
        else _workflow_text()
    )
    workflow_name = "build" if name == "build-flatpak" else name
    match = re.search(
        rf"(?ms)^  {re.escape(workflow_name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)",
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


@pytest.mark.linux_only
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


def test_release_artifacts_bundle_and_probe_portable_tls_trust():
    spec = (REPO / "wayfinder-aura.spec").read_text(encoding="utf-8")
    release_builds = f"{_workflow_text()}\n{_flatpak_build_script_text()}"
    requirements = (REPO / "requirements.txt").read_text(encoding="utf-8")
    flatpak_sources = PYTHON_DEPS.read_text(encoding="utf-8")

    assert "CERTIFI_CA_BUNDLE" in spec
    assert "(str(CERTIFI_CA_BUNDLE), 'certifi')" in spec
    assert "certifi>=2024.0.0" in requirements
    assert "certifi-" in flatpak_sources
    assert release_builds.count("--tls-self-test") >= 2
    assert release_builds.count("TLS_SELF_TEST_OK") >= 2
    appimage_builder = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")
    assert 'export TMPDIR="$WF_RUNTIME_TMP"' in appimage_builder


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
    assert 'whisper-cli-cpu' in script
    assert 'whisper-server-cpu' in script
    assert 'llama-simple-cpu' in script
    assert '--target "${targets[@]}"' in script

    job = _workflow_job_body("build-appimage")
    assert "test -x squashfs-root/usr/bin/whisper-server" in job
    assert "test -x squashfs-root/usr/bin/whisper-cli-cpu" in job
    assert "test -x squashfs-root/usr/bin/whisper-server-cpu" in job
    assert "test -x squashfs-root/usr/bin/llama-simple-cpu" in job
    assert "CPU safety twin unexpectedly links the Vulkan backend" in job


def test_appimage_native_binaries_use_the_steam_deck_cpu_baseline():
    """Release binaries must not inherit AVX-512 from their build worker."""
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    assert 'GGML_CPU_BASELINE_OPTS=(' in script
    assert '-DGGML_NATIVE=OFF' in script
    assert '-DGGML_SSE42=ON' in script
    assert '-DGGML_AVX=ON' in script
    assert '-DGGML_AVX2=ON' in script
    assert '-DGGML_FMA=ON' in script
    assert '-DGGML_F16C=ON' in script
    assert '-DGGML_BMI2=ON' in script
    assert '-DGGML_AVX512=OFF' in script
    assert '"${GGML_CPU_BASELINE_OPTS[@]}"' in script


def test_appimage_uses_host_player_without_mixing_recording_libraries():
    """Playback crosses the bundle boundary via a host-clean process."""
    spec = (REPO / "wayfinder-aura.spec").read_text(encoding="utf-8")
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")
    output = (REPO / "src" / "wayfinder" / "utils" / "audio_output.py").read_text(encoding="utf-8")
    job = _workflow_job_body("build-appimage")

    assert "use_host_linux_audio_abi" not in spec
    assert 'rm -f "$APPDIR/usr/lib/libasound.so"' not in script
    assert 'shutil.which("paplay"' in output
    assert "host_env()" in output
    assert '"--raw"' in output
    assert "--audio-output-self-test" in job
    assert "AUDIO_OUTPUT_SELF_TEST_OK" in job
    assert 'grep -Fq "PulseAudio/PipeWire"' in job
    assert "host PulseAudio/PipeWire" not in job
    assert "--audio-input-self-test" in job
    assert "AUDIO_INPUT_SELF_TEST_OK" in job
    assert "--audio-processing-self-test" in job
    assert "AUDIO_PROCESSING_SELF_TEST_OK" in job


def test_appimage_import_probe_and_desktop_audio_graph_are_release_gated():
    job = _workflow_job_body("build-appimage")
    entrypoint = (REPO / "main.py").read_text(encoding="utf-8")

    assert "module-null-sink" in job
    assert "module-null-source" in job
    assert "--app-import-self-test" in job
    assert "APP_IMPORT_SELF_TEST_OK" in job
    assert 'if "--app-import-self-test" in sys.argv:' in entrypoint
    assert "from wayfinder_main import WayfinderApp" in entrypoint


def test_flatpak_audio_stack_is_runtime_coherent_and_release_probed():
    manifest = _manifest_text()
    build_script = _flatpak_build_script_text()

    assert "--socket=pulseaudio" in manifest
    assert "-DPA_USE_ALSA=ON" in manifest
    assert "-DPA_USE_JACK=OFF" in manifest
    assert 'flatpak run "$APP_ID"' in build_script
    assert "--audio-output-self-test" in build_script
    assert "AUDIO_OUTPUT_SELF_TEST_OK" in build_script
    assert "--audio-input-self-test" in build_script
    assert "AUDIO_INPUT_SELF_TEST_OK" in build_script
    assert "--audio-processing-self-test" in build_script
    assert "AUDIO_PROCESSING_SELF_TEST_OK" in build_script
    assert "started_pulseaudio=1" in build_script
    assert "pulseaudio --kill" in build_script


def test_appimage_builder_prints_build_mode_after_argument_parsing():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    parse_loop = script.index("# ─── Parse arguments")
    banner = script.index("Wayfinder Aura - AppImage Builder")
    assert parse_loop < banner


def test_appimage_builder_allows_non_destructive_local_output_name():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    assert 'OUTPUT_NAME="${OUTPUT_NAME:-Wayfinder_Aura-${VERSION}-${ARCH}.AppImage}"' in script


def test_appimage_builder_fails_fast_without_tkinter():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")

    assert 'PYTHON_BIN="${PYTHON_BIN:-python3}"' in script
    assert '"$PYTHON_BIN" -m PyInstaller' in script
    assert "import tkinter" in script
    assert "Tkinter support not found for $PYTHON_BIN" in script
    assert "sudo dnf install python3-tkinter" in script
    assert "sudo apt install python3-tk" in script
    assert "require_compatible_tk_renderer" in script
    assert "xvfb-run" in script
    assert "Tk must be built with Xft" in script


def test_pyinstaller_spec_collects_portable_python_tk_libraries():
    """uv Python's combined Tcl/Tk 9 library evades normal hook discovery."""
    spec = (REPO / "wayfinder-aura.spec").read_text(encoding="utf-8")

    assert "def _python_standalone_tk_binaries" in spec
    assert "Path(sys.base_prefix) / 'lib'" in spec
    assert "'libtcl*.so*'" in spec
    assert "'libtk*.so*'" in spec
    assert "binaries=PYTHON_STANDALONE_TK_BINARIES" in spec


def test_release_artifacts_probe_real_xft_renderer():
    workflow = _workflow_text()
    appimage = _workflow_job_body("build-appimage")
    flatpak = _flatpak_build_script_text()

    assert "--ui-renderer-self-test" in appimage
    assert "--ui-renderer-self-test" in flatpak
    assert appimage.count("UI_RENDERER_SELF_TEST_OK") >= 1
    assert flatpak.count("UI_RENDERER_SELF_TEST_OK") >= 1
    assert "xvfb" in workflow


def test_public_tier_copy_matches_runtime_gpu_and_model_gates():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    support = (REPO / "SUPPORT.md").read_text(encoding="utf-8")
    website_brief = (REPO / "WEBSITE_COPY_BRIEF.md").read_text(encoding="utf-8")
    marketing_handoff = (
        REPO / "docs" / "STOREFRONT-MARKETING-HANDOFF.md"
    ).read_text(encoding="utf-8")
    metainfo = (
        REPO / "flatpak" / "io.wayfindercollective.WayfinderAura.metainfo.xml"
    ).read_text(encoding="utf-8")

    assert "| Base/Base.en transcription on CPU | ✅ | ✅ |" in readme
    assert "| GPU acceleration (Vulkan) | — | ✅ |" in readme
    assert "Free tier includes Base/Base.en transcription on CPU" in metainfo
    assert "Free tier includes local GPU-accelerated" not in metainfo
    assert "free tier included" not in readme.lower()
    assert "free tier runs Base/Base.en on CPU" in support
    assert "| Additional speech models | — | ✅ |" in website_brief
    assert "Optional local cleanup, Ultra GPU acceleration" in website_brief
    assert "Post-processing: **Off** by default" in website_brief
    assert "Standard models (tiny / base / small)" not in marketing_handoff
    assert "| Base/Base.en speech model | Tiny, Small, Medium, Turbo, and Large speech models |" in marketing_handoff


def test_documented_dictation_defaults_and_auto_enter_warning_match_runtime():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    support = (REPO / "SUPPORT.md").read_text(encoding="utf-8")
    main = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")
    normalized_docs = [" ".join(text.split()) for text in (readme, support, main)]

    assert "Post-processing starts off on new installs" in readme
    assert "Ultra Chunk Processing defaults to Auto" in readme
    assert "Auto press Enter starts off" in readme
    for warning_term in ("terminal commands", "AI prompts"):
        for document in normalized_docs:
            assert warning_term in document


def test_support_documents_activation_slot_recovery_without_full_key_disclosure():
    support = (REPO / "SUPPORT.md").read_text(encoding="utf-8")
    normalized = " ".join(support.split())

    assert "up to three activated devices" in normalized
    assert "does not release a server activation slot" in normalized
    assert "support@wayfindercoaching.net" in normalized
    assert "last four characters" in normalized
    assert "Never post your purchase email or full license key" in normalized
    assert "Support verifies ownership before any slot-recovery action" in normalized
    for response in ("activation_limit", "revoked", "refunded", "offline"):
        assert f"`{response}`" in support


def test_premium_storefront_defaults_are_consistent_across_release_surfaces():
    from wayfinder.config import DEFAULT_CONFIG

    readme = (REPO / "README.md").read_text(encoding="utf-8")
    main = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")

    checkout = DEFAULT_CONFIG["premium_url"]
    info_url = DEFAULT_CONFIG["premium_info_url"]
    launch_price = DEFAULT_CONFIG["premium_price"]
    regular_price = DEFAULT_CONFIG["premium_price_regular"]

    assert checkout.startswith("https://wayfindercollective.io/checkout/")
    assert checkout.endswith("/aura-ultra")
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
    workflow = _release_workflow_text()

    assert re.search(r"(?m)^    tags:\n      - \"v\*\"$", workflow)
    assert "release-readiness:" in workflow
    assert "if: startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "python scripts/ci/check-release-license-defaults.py" in workflow
    assert "python -m pip install playwright" in workflow
    assert "python -m playwright install --with-deps chromium" in workflow
    assert "python scripts/ci/check-storefront-readiness.py --browser --timeout 30" in workflow
    assert "python flatpak/prepare-release-manifest.py" in workflow
    assert '--tag "${GITHUB_REF_NAME}"' in workflow
    assert "--output /tmp/wayfinder-flathub/io.wayfindercollective.WayfinderAura.yml" in workflow
    assert "--allow-dev-license" not in workflow

    release_job = _workflow_job_body("release")
    for job_name in ("release", "build-appimage"):
        assert "release-readiness" in _workflow_job_body(job_name)
    assert "release-readiness" in workflow.split("  build-flatpak:", 1)[1]

    assert release_job, "workflow must define the tag release job"
    assert "softprops/action-gh-release@v3" in release_job


def test_release_artifact_jobs_are_manual_or_tag_only_and_raw_binary_is_removed():
    workflow = _release_workflow_text()
    flatpak_workflow = _flatpak_build_workflow_text()
    appimage_job = _workflow_job_body("build-appimage")
    release_job = _workflow_job_body("release")

    assert "build-pyinstaller:" not in workflow
    assert "wayfinder-aura-linux" not in workflow
    assert "dist/wayfinder-aura" not in release_job
    assert "inputs.artifacts == 'appimage' || inputs.artifacts == 'all'" in appimage_job
    assert "inputs.artifacts == 'hosted-flatpak' || inputs.artifacts == 'all'" in workflow
    assert "uses: ./.github/workflows/flatpak-build.yml" in workflow
    assert "pull_request:" not in workflow.split("jobs:", 1)[0]
    assert "pull_request:" not in flatpak_workflow.split("jobs:", 1)[0]
    assert "if: startsWith(github.ref, 'refs/tags/v') || github.event_name == 'workflow_dispatch'" in flatpak_workflow
    assert "runs-on: ubuntu-latest" in flatpak_workflow
    assert "self-hosted" not in workflow
    assert "self-hosted" not in flatpak_workflow
    assert "uses: ./.github/workflows/ci.yml" in workflow


def test_normal_ci_keeps_cached_quality_platform_smoke_and_cancels_stale_pushes():
    workflow = _ci_workflow_text()
    quality_job = _workflow_job_body("quality")

    for removed_job in ("lint:", "type-check:", "test:", "structure-check:"):
        assert f"  {removed_job}" not in workflow
    assert "cache: pip" in quality_job
    assert "Run high-signal Ruff checks" in quality_job
    assert "python scripts/verify_structure.py" in quality_job
    assert "pytest tests/" in quality_job
    assert "group: aura-ci-${{ github.event_name }}-${{ github.ref }}" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "aura-flatpak" not in workflow
    assert "workflow_call:" in workflow
    platform_job = _workflow_job_body("platform-smoke")
    assert "macos-latest" in platform_job
    assert "windows-latest" in platform_job
    assert "python scripts/platform_smoke.py --expected ${{ matrix.expected }}" in platform_job


def test_model_pin_drift_is_scheduled_and_only_pushes_for_pin_surfaces():
    workflow = (REPO / ".github" / "workflows" / "model-pin-drift.yml").read_text(
        encoding="utf-8"
    )

    trigger_block = workflow.split("jobs:", 1)[0]
    assert "schedule:" in trigger_block
    assert "workflow_dispatch:" in trigger_block
    assert "push:" in trigger_block
    for path in (
        "wayfinder_main.py",
        "src/wayfinder/core/setup.py",
        "scripts/verify-model-digests.py",
        "tests/test_catalog_ratchet.py",
        ".github/workflows/model-pin-drift.yml",
    ):
        assert f'- "{path}"' in trigger_block


def test_mini_inf_flatpak_build_is_resource_capped_below_live_inference():
    limits = MINI_INF_BUILD_SCRIPT.read_text(encoding="utf-8")

    for setting in (
        "CPUQuota=200%",
        "MemoryHigh=16G",
        "MemoryMax=24G",
        "nice -n 10 ionice -c 3",
        'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"',
    ):
        assert setting in limits


def test_mini_inf_build_accepts_only_pushed_main_history_and_uses_no_runner():
    script = MINI_INF_BUILD_SCRIPT.read_text(encoding="utf-8")
    docs = (REPO / "docs" / "CI.md").read_text(encoding="utf-8")

    assert 'SOURCE_REF="origin/main"' in script
    assert 'merge-base --is-ancestor "$source_sha" origin/main' in script
    assert 'merge-base --is-ancestor "$source_sha" refs/heads/main' in script
    assert "https://github.com/wayfindercollective/wayfinder-aura.git" in script
    assert "systemd-run --user --scope" in script
    assert 'remote_tag=${TAG:--}' in script
    assert "scp --" in script
    assert "permanently registered repository runner" in docs
    assert "**not** registered" in docs


@pytest.mark.linux_only
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
        defaults["premium_info_url"]: "Wayfinder Aura - Press a key. Speak. Your words land at your cursor.",
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
        defaults["premium_info_url"]: (
            "Wayfinder Aura\nPress a key. Speak. Your words land at your cursor."
        ),
        defaults["premium_url"]: "Wayfinder Aura\nOne-time license\nPay with card\n$29.99 launch",
    }

    errors = checker.live_readiness_errors(
        defaults=defaults,
        fetcher=lambda url, _timeout: (200, html_pages[url]),
        renderer=lambda url, _timeout: rendered_pages[url],
        browser=True,
    )

    assert errors == []


def test_storefront_readiness_checker_rejects_free_gpu_claim():
    checker = _load_storefront_checker()
    defaults = {
        "premium_url": "https://wayfindercollective.io/checkout/product",
        "premium_info_url": "https://wayfindercollective.io/aura",
        "premium_price": "$29.99",
        "premium_price_regular": "$60",
    }
    pages = {
        defaults["premium_info_url"]: (
            "Wayfinder Aura - Press a key. Speak. Your words land at your cursor. "
            "GPU support on the lighter models"
        ),
        defaults["premium_url"]: (
            "Wayfinder Aura One-time license Pay with card $29.99"
        ),
    }

    errors = checker.live_readiness_errors(
        defaults=defaults,
        fetcher=lambda url, _timeout: (200, pages[url]),
    )

    assert any("stale release claims" in error for error in errors)
    assert any("GPU support on the lighter models" in error for error in errors)


@pytest.mark.linux_only
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
    # ydotool is deliberately NOT installed/bundled at build: the client must
    # protocol-match the HOST daemon (jammy's 0.1.8 doesn't speak the app CLI).
    assert "ydotool" not in job.split("Smoke-test")[0]
    assert "wtype" in job
    # Vulkan configure hard-requires SPIRV-Headers; without it the build
    # silently ships CPU-only inference binaries.
    assert "spirv-headers" in job
    # Hermetic setup-python needs pip PyGObject/dbus-python or the bundle
    # ships without gi/dbus (no portal shortcuts, no GI typelibs).
    assert "PyGObject==3.50.2" in job
    assert "dbus-python==1.4.0" in job
    assert "from gi.repository import GLib" in job
    assert "desktop-file-utils appstream" in job
    assert "./scripts/build-appimage.sh --full" in job
    assert "./Wayfinder_Aura-*.AppImage --appimage-extract" in job

    for binary in (
        "wayfinder-aura",
        "whisper-cli",
        "llama-cli",
        "llama-simple",
        "wtype",
    ):
        assert f"test -x squashfs-root/usr/bin/{binary}" in job

    # The bundle must NOT contain a ydotool client, and the release must be
    # provably Vulkan-native with gi/dbus bundled.
    assert "test ! -e squashfs-root/usr/bin/ydotool" in job
    assert 'grep -aFq "ggml_vulkan" squashfs-root/usr/bin/whisper-cli' in job
    assert "gi_typelibs/GLib-2.0.typelib" in job
    assert "_dbus_bindings" in job

    assert "desktop-file-validate squashfs-root/io.wayfindercollective.WayfinderAura.desktop" in job
    assert "appstreamcli validate --no-net" in job
    # Jammy appstreamcli exits non-zero on modern tags; CI only fails on hard E: lines.
    assert 'grep -qE \'^E:\'' in job or 'grep -qE "^E:"' in job
    assert "glslang-tools" in job


@pytest.mark.linux_only
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
    assert "cmake --build \"$BUILD_DIR\" --target glslc_exe" in script
    assert "glslc --version" in script


def test_flatpak_ci_builds_tag_sourced_release_manifest_on_tags():
    job = _workflow_job_body("build-flatpak")
    build_script = _flatpak_build_script_text()

    assert "fetch-depth: 0" in job
    assert "actions/setup-python@v7" in job
    assert 'if [[ "$GITHUB_REF" == refs/tags/v* ]]; then' in job
    assert '--tag "$GITHUB_REF_NAME"' in job
    assert "scripts/ci/build-flatpak-candidate.sh" in job
    assert 'python3 "$REPO_ROOT/flatpak/prepare-release-manifest.py"' in build_script
    assert '--tag "$TAG"' in build_script
    assert '--output "$REPO_ROOT/flatpak/release/io.wayfindercollective.WayfinderAura.yml"' in build_script
    assert 'manifest="release/io.wayfindercollective.WayfinderAura.yml"' in build_script
    assert 'manifest="io.wayfindercollective.WayfinderAura.yml"' in build_script
    assert 'flatpak-builder --user --force-clean --ccache --jobs=2' in build_script
    assert 'build-dir "$manifest"' in build_script


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
        "runtime-version: '6.11'",
        "sdk: org.kde.Sdk",
        "- org.freedesktop.Sdk.Extension.rust-stable",
        "base: com.riverbankcomputing.PyQt.BaseApp",
        "base-version: '6.11'",
        "BASEAPP_REMOVE_WEBENGINE: '1'",
        "- --socket=x11",
        "- --env=XDG_SESSION_TYPE=x11",
        "- --env=QT_QPA_PLATFORM=xcb",
        "- --socket=pulseaudio",
        "- --device=dri",
        "- --filesystem=xdg-run/wayfinder-aura:create",
        "- --talk-name=org.kde.StatusNotifierWatcher",
        "- --talk-name=org.freedesktop.Notifications",
    ):
        assert expected in manifest

    assert "--filesystem=home" not in manifest
    assert "--socket=fallback-x11" not in manifest
    assert "--socket=wayland" not in manifest
    assert "--filesystem=host" not in manifest
    assert "--filesystem=xdg-config/wayfinder-aura" not in manifest
    assert "--filesystem=xdg-cache/wayfinder-aura:create" not in manifest
    assert "--talk-name=org.freedesktop.Flatpak" not in manifest
    assert "pipewire-0" not in manifest
    assert "--talk-name=org.freedesktop.portal." not in manifest
    assert re.search(r"(?m)^  - name: ydotool$", manifest) is None
    assert re.search(r"(?m)^  - name: wtype$", manifest)
    assert re.search(r"(?m)^  - name: xdotool$", manifest)
    assert "whisper-cli-cpu" in manifest
    assert "whisper-server-cpu" in manifest
    assert "llama-simple-cpu" in manifest
    # Vulkan ON for the primary whisper/llama modules (Ultra needs GPU and the
    # Free benchmark shows a GPU upgrade preview). The historic CI failure
    # ("Failed to fork" at mul_mm shader gen) was fork-ENOMEM under default
    # heuristic overcommit on runners, fixed by the CI memory-headroom step
    # (overcommit=1 + swap) — verified by a resource-rich local build linking
    # the same manifest cleanly. CPU fallback modules stay OFF by design.
    assert manifest.count("-DGGML_VULKAN=ON") == 2
    assert manifest.count("-DGGML_VULKAN=OFF") >= 2


def test_audio_ducking_uses_narrow_manager_client_and_full_public_range():
    ducker = (REPO / "src" / "wayfinder" / "utils" / "audio_ducker.py").read_text(
        encoding="utf-8"
    )
    main = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")

    assert '"PULSE_PROP_media.category": "Manager"' in ducker
    assert "manager=True" in ducker
    assert "--talk-name=org.freedesktop.Flatpak" not in _manifest_text()
    assert "pipewire-0" not in _manifest_text()
    slider = main[main.index("def _create_audio_ducking_slider_row"):]
    slider = slider[:slider.index("def _on_audio_ducking_toggled")]
    assert "from_=0" in slider
    assert "to=100" in slider


def test_flatpak_git_sources_are_all_tag_and_commit_pinned():
    manifest = _manifest_text()
    blocks = re.findall(r"(?m)^      - type: git\n((?:        .+\n)+)", manifest)

    # PortAudio, SPIRV-Headers, whisper×2, wtype, xdotool, llama×2.
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


def test_flatpak_compiled_python_dependencies_build_from_source():
    manifest = _manifest_text()
    for module in (
        "openblas",
        "python-maturin",
        "python-pycparser",
        "python-cffi",
        "python-cryptography",
        "python-numpy",
        "python-scipy",
        "python-pillow",
        "python-jiter",
        "python-pydantic-core",
    ):
        assert re.search(rf"(?m)^  - name: {re.escape(module)}$", manifest)

    dependency_manifests = [
        PYTHON_DEPS,
        REPO / "flatpak" / "python-numpy-build-tools.json",
        REPO / "flatpak" / "python-scipy-build-tools.json",
    ]
    for path in dependency_manifests:
        text = path.read_text(encoding="utf-8")
        assert "manylinux" not in text, path
        assert "musllinux" not in text, path

    # A build-only wildcard cleanup can claim native dependencies that the
    # later build-tool module imports. Runtime artifact smokes caught this when
    # NumPy's extensions disappeared only during the final cleanup stage.
    for path in dependency_manifests[1:]:
        assert '"cleanup"' not in path.read_text(encoding="utf-8"), path
    assert "/lib/python*/site-packages/pythran*" in manifest

    assert "-DBUILD_SHARED_LIBS:BOOL=ON" in manifest
    assert "-DBUILD_WITHOUT_LAPACKE:BOOL=ON" in manifest
    assert "-DDYNAMIC_OLDER" not in manifest
    assert manifest.count("maturin build --release --locked --offline") == 3
    assert manifest.count("CARGO_NET_OFFLINE: 'true'") == 4

    for filename in (
        "cargo-sources-maturin.json",
        "cargo-sources-cryptography.json",
        "cargo-sources-jiter.json",
        "cargo-sources-pydantic-core.json",
    ):
        assert filename in manifest
        cargo_sources = json.loads((REPO / "flatpak" / filename).read_text(encoding="utf-8"))
        assert cargo_sources
        for source in cargo_sources:
            assert source.get("type") in {"archive", "inline"}
            if source["type"] == "archive":
                assert source.get("url", "").startswith("https://static.crates.io/")
                assert re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", ""))
            else:
                assert source.get("contents")


# ── 1.1.5 rendering + host-spawn invariants ──────────────────────────────────


def test_design_fonts_are_committed_with_licenses():
    """DejaVu Sans is the product typeface BY DECISION (2026-07-30) — the
    approved look always rendered as DejaVu via substitution; bundling makes
    it deterministic. JetBrains Mono ships for the overlay pill only."""
    fonts = REPO / "assets" / "fonts"
    for path in (
        fonts / "dejavu" / "DejaVuSans.ttf",
        fonts / "dejavu" / "DejaVuSans-Bold.ttf",
        fonts / "dejavu" / "LICENSE.txt",
        fonts / "jetbrains-mono" / "JetBrainsMono-Regular.ttf",
        fonts / "jetbrains-mono" / "JetBrainsMono-Bold.ttf",
        fonts / "jetbrains-mono" / "OFL.txt",
    ):
        assert path.is_file(), f"missing bundled font asset: {path}"


def test_ui_font_stacks_are_dejavu_first():
    """Owner decision 2026-07-30: Inter/JetBrains-first stacks were rejected
    in the field ('styling is SUPER off') — the approved product typeface is
    DejaVu Sans everywhere, including the mono value chips."""
    theme = (REPO / "src" / "wayfinder" / "ui" / "theme.py").read_text(encoding="utf-8")
    main = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")
    for key in ("display", "header", "body", "mono"):
        assert re.search(rf'"{key}": \("DejaVu Sans"', theme), f"theme FONTS[{key}] not DejaVu-first"
    for attr in ("font_display", "font_header", "font_body", "font_mono"):
        assert re.search(rf'self\.{attr} = \("DejaVu Sans"', main), f"{attr} not DejaVu-first"
    assert 'font=("Inter"' not in main, "literal Inter font site survived the DejaVu decision"


def test_appimage_bundles_fonts_and_fontconfig():
    script = (REPO / "scripts" / "build-appimage.sh").read_text(encoding="utf-8")
    spec = (REPO / "wayfinder-aura.spec").read_text(encoding="utf-8")
    entrypoint = (REPO / "main.py").read_text(encoding="utf-8")
    assert "usr/share/fonts/wayfinder-aura" in script
    assert "assets/fonts/." in script
    # AppRun must expose the fonts to fontconfig without hiding host fonts
    assert "FONTCONFIG_FILE" in script
    assert '<include ignore_missing="yes">/etc/fonts/fonts.conf</include>' in script
    # A directly-run PyInstaller binary has no AppRun wrapper; it must still
    # expose the same product fonts so dogfood does not silently use Noto/fixed.
    assert "('assets/fonts', 'assets/fonts')" in spec
    assert "def _configure_frozen_fontconfig" in entrypoint
    assert 'os.environ["FONTCONFIG_FILE"]' in entrypoint


def test_flatpak_installs_design_fonts():
    assert "share/fonts" in _manifest_text()


def test_spec_ships_qtdbus_for_kwin_placement():
    """Overlay placement talks to KWin in-process via QtDBus — host qdbus is
    unusable from the bundle (env/libstdc++ mismatch, proven live)."""
    spec = (REPO / "wayfinder-aura.spec").read_text(encoding="utf-8")
    assert "'PyQt6.QtDBus'" in spec


def test_overlay_uses_qtdbus_with_scrubbed_fallback():
    overlay = (REPO / "src" / "wayfinder" / "ui" / "overlay.py").read_text(encoding="utf-8")
    assert "QDBusInterface" in overlay
    assert "qdbus-qt6" in overlay, "Fedora names the qdbus binary qdbus-qt6"
    assert "host_env" in overlay


def test_font_size_token_mirrors_stay_in_sync():
    """theme.py FONT_SIZES and wayfinder_main self.font_sizes are mirrors."""
    def extract(text: str, anchor: str) -> dict[str, int]:
        block = re.search(anchor + r"\s*=\s*\{(.*?)\}", text, re.S)
        assert block, f"could not locate token dict via {anchor!r}"
        return {k: int(v) for k, v in re.findall(r'"(\w+)":\s*(\d+)', block.group(1))}

    theme = extract(
        (REPO / "src" / "wayfinder" / "ui" / "theme.py").read_text(encoding="utf-8"),
        r"FONT_SIZES: dict\[str, int\]",
    )
    main = extract(
        (REPO / "wayfinder_main.py").read_text(encoding="utf-8"),
        r"self\.font_sizes",
    )
    assert theme == main, "font token mirrors drifted"
    # Owner-approved original scale (the 2x recalibration was rejected)
    assert theme["body"] == 13


def test_release_license_checker_rejects_a_pubkey_that_is_not_productions(tmp_path):
    """Presence of a pubkey is not enough — it must be PRODUCTION's.

    The activate URL and the embedded Ed25519 pubkey must both belong to the
    same deployment. A wrong-but-present pubkey passes a presence check, then
    fails at token verification after the server has already said the key is
    valid: a second silent cross-system divergence, which is exactly the class
    of bug that let the storefront mint against dev for three weeks while the
    app activated against prod.
    """
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    wrong_pubkey = tmp_path / "license-wrong-pubkey.py"
    wrong_pubkey.write_text(
        """
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "https://shiny-goshawk-432.convex.site/activate")
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(wrong_pubkey)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "LICENSE_PUBLIC_KEY_HEX" in result.stderr


def test_release_license_checker_accepts_the_real_shipping_defaults():
    """Guard against the pin being tightened into something the repo fails."""
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"

    result = subprocess.run(
        [sys.executable, str(checker)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_license_checker_rejects_other_convex_deployments(tmp_path):
    """A Convex deployment that is neither dev nor prod must not ship."""
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    lic = tmp_path / "license-other-convex.py"
    lic.write_text(
        '''
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "https://some-other-deploy-999.convex.site/activate")
''',
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(lic)],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 2, result.stdout + result.stderr


def test_pubkey_pin_applies_only_to_the_exact_production_url(tmp_path):
    """The pin matched the deployment name as a SUBSTRING, so a lookalike host
    like shiny-goshawk-432.convex.site.evil was treated as production and
    pinned against production's keypair. Match exactly instead."""
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("relcheck", checker)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.PROD_LICENSE_PUBLIC_KEY_HEX, "production pubkey must be pinned"
    # Exact production URL + wrong pubkey => rejected.
    lic = tmp_path / "license-prod-wrong-key.py"
    lic.write_text(
        '''
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "https://shiny-goshawk-432.convex.site/activate")
''',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(lic)],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "LICENSE_PUBLIC_KEY_HEX" in result.stderr


@pytest.mark.parametrize(
    "prod_variant",
    [
        "https://shiny-goshawk-432.convex.site/activate?build=1",
        "https://shiny-goshawk-432.convex.site/activate/",
        "https://shiny-goshawk-432.convex.site/activate#frag",
    ],
)
def test_pubkey_pin_covers_equivalent_production_urls(tmp_path, prod_variant):
    """Exact string equality let a URL that still hits PRODUCTION skip the pin,
    so a release could ship the production endpoint with a foreign keypair."""
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    lic = tmp_path / "license-variant.py"
    lic.write_text(
        f'''
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "{prod_variant}")
''',
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(lic)],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 2, (
        f"{prod_variant} hits production but skipped the pubkey pin: "
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize(
    "routable_variant",
    [
        # Explicit default port — same host, same request.
        "https://shiny-goshawk-432.convex.site:443/activate",
        # Userinfo does not change where the request goes.
        "https://user:pass@shiny-goshawk-432.convex.site/activate",
        "https://SHINY-GOSHAWK-432.CONVEX.SITE/activate",
    ],
)
def test_pubkey_pin_covers_production_urls_with_port_or_userinfo(tmp_path, routable_variant):
    """netloc includes userinfo and port, so comparing it raw let URLs that
    still reach production skip the keypair pin."""
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    lic = tmp_path / "license-routable.py"
    lic.write_text(
        f'''
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "{routable_variant}")
''',
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(lic)],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 2, (
        f"{routable_variant} reaches production but skipped the pin: "
        f"{result.stdout}{result.stderr}"
    )


@pytest.mark.parametrize(
    "dotted",
    [
        "https://shiny-goshawk-432.convex.site/x/../activate",
        "https://shiny-goshawk-432.convex.site/activate/.",
    ],
)
def test_pubkey_pin_covers_dot_segment_paths(tmp_path, dotted):
    """requests resolves dot segments before sending, so these still reach
    production and must be held to production's keypair."""
    checker = REPO / "scripts" / "ci" / "check-release-license-defaults.py"
    lic = tmp_path / "license-dotted.py"
    lic.write_text(
        f'''
import os
LICENSE_PUBLIC_KEY_HEX = os.environ.get("WAYFINDER_LICENSE_PUBKEY", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
LICENSE_API_URL = os.environ.get("WAYFINDER_LICENSE_API_URL", "{dotted}")
''',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(checker), "--license-file", str(lic)],
        cwd=REPO, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2, result.stdout + result.stderr

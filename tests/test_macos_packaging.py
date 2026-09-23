import plistlib
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def test_macos_spec_preserves_pyinstaller_framework_symlinks():
    spec = (REPO / "wayfinder-aura-macos.spec").read_text(encoding="utf-8")

    assert "remove_duplicate_entries" not in spec
    assert "AURA_MACOS_ARCH" in spec
    assert "MACOS_CODESIGN_IDENTITY" in spec
    assert "packaging' / 'macos' / 'entitlements.plist" in spec


def test_macos_hardened_runtime_entitlements_are_minimal():
    with (REPO / "packaging" / "macos" / "entitlements.plist").open("rb") as handle:
        entitlements = plistlib.load(handle)

    assert entitlements["com.apple.security.device.audio-input"] is True
    assert "com.apple.security.automation.apple-events" not in entitlements


def test_macos_builder_pins_and_verifies_offline_runtimes():
    builder = (REPO / "packaging" / "macos" / "build.py").read_text(encoding="utf-8")

    assert 'WHISPER_COMMIT = "f049fff95a089aa9969deb009cdd4892b3e74916"' in builder
    assert 'LLAMA_COMMIT = "70b54e140c90a92285ba699d77e1e32e0868a0e2"' in builder
    assert '"-DGGML_NATIVE=OFF"' in builder
    assert '"-DLLAMA_OPENSSL=OFF"' in builder
    assert 'run([executable, "--runtime-assets-self-test"])' in builder
    assert '"family=\'DejaVu Sans\'"' in builder
    assert "def build_hero_renderer()" in builder
    assert '"-framework", "Metal"' in builder
    assert 'resources / "lib" / "libwayfinder_hero.dylib"' in builder
    assert 'overlay_renderer.m' in builder
    assert 'MINIMUM_MACOS = "14.0"' in builder
    assert "validate_native_deployment_targets" in builder


def test_manual_macos_candidate_workflow_uses_packaging_helper():
    workflow = (REPO / ".github" / "workflows" / "macos-build.yml").read_text(encoding="utf-8")

    assert "runs-on: macos-latest" in workflow
    assert "python packaging/macos/build.py" in workflow
    assert "Wayfinder_Aura-*-macOS-*.dmg" in workflow
    assert 'PYTHON_VERSION: "3.12.10"' in workflow
    assert "packaging/macos/constraints.txt" in workflow


def test_macos_bundle_declares_truthful_minimum_os():
    spec = (REPO / "wayfinder-aura-macos.spec").read_text(encoding="utf-8")
    assert "'LSMinimumSystemVersion': '14.0'" in spec


def test_metal_compute_layers_allow_shader_writes():
    # Compute kernels write straight into the CAMetalLayer drawable. A
    # framebufferOnly layer vends render-target-only textures, so the write is
    # invalid and M3 GPUs display solid magenta instead of the waveform.
    for name in ("hero_renderer.m", "overlay_renderer.m"):
        source = (REPO / "packaging" / "macos" / name).read_text(encoding="utf-8")
        assert "computeCommandEncoder" in source
        assert "framebufferOnly = NO;" in source
        assert "framebufferOnly = YES" not in source

    hero = (REPO / "src" / "wayfinder" / "ui" / "macos_hero_metal.py").read_text(encoding="utf-8")
    assert "setFramebufferOnly_(False)" in hero
    assert "setFramebufferOnly_(True)" not in hero


def test_dmg_layout_places_app_and_applications_without_finder_chrome():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "dmg_assets", REPO / "packaging" / "macos" / "dmg_assets.py"
    )
    dmg_assets = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dmg_assets)

    app = Path("/tmp/dist/Wayfinder Aura.app")
    settings = dmg_assets.dmgbuild_settings(app, Path("bg.tiff"), Path("icon.icns"))

    assert settings["icon_locations"] == {
        "Wayfinder Aura.app": dmg_assets.APP_POSITION,
        "Applications": dmg_assets.APPLICATIONS_POSITION,
    }
    assert settings["symlinks"] == {"Applications": "/Applications"}
    assert not any(settings[k] for k in ("show_toolbar", "show_sidebar", "show_status_bar"))
    assert settings["window_rect"][1] == dmg_assets.WINDOW_SIZE

    builder = (REPO / "packaging" / "macos" / "build.py").read_text(encoding="utf-8")
    assert "dmgbuild.build_dmg(" in builder
    assert '"hdiutil", "create"' in builder  # fallback when dmgbuild is absent

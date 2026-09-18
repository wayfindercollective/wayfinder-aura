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

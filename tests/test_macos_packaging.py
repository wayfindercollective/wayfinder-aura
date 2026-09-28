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


def test_native_renderers_settle_when_idle_and_stop_when_hidden():
    """The idle pill/hero stop issuing Metal work (GPU memory + wakeups) and
    let Core Animation breathe the last frame; a hidden hero stops its timer."""
    root = Path(__file__).resolve().parent.parent / "packaging" / "macos"
    overlay = (root / "overlay_renderer.m").read_text()
    hero = (root / "hero_renderer.m").read_text()
    assert "wf_overlay_set_idle" in overlay and "enterVectorIdle" in overlay
    assert "animationWithKeyPath:@\"path\"" in overlay  # vector idle flow
    assert "NSWindowDidChangeOcclusionStateNotification" in hero
    assert "enterVectorIdle" in hero and "animationWithKeyPath:@\"path\"" in hero
    assert "[self stopTimer];  // a hidden layer" in hero


def test_overlay_bridge_tolerates_an_older_dylib():
    import types
    from wayfinder.ui.macos_overlay_metal import MacOSOverlayMetalLayer

    layer = MacOSOverlayMetalLayer.__new__(MacOSOverlayMetalLayer)
    layer.handle = 1
    layer.library = types.SimpleNamespace()  # no wf_overlay_set_idle symbol
    layer.set_idle(True)  # must not raise


def _load_builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wayfinder_macos_build", REPO / "packaging" / "macos" / "build.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder


def test_macos_builder_notarizes_the_app_before_building_the_dmg():
    # Apple's order: notarize + staple the .app, put THAT app in the DMG, then
    # notarize + staple the DMG. Stapling only after the DMG exists leaves the
    # app inside the image without a ticket.
    import inspect

    builder = _load_builder()
    main = inspect.getsource(builder.main)
    assert main.index("notarize_app(notary_auth)") < main.index("create_dmg()")
    assert main.index("create_dmg()") < main.index("notarize_dmg(dmg_path, notary_auth)")
    stapled_app = inspect.getsource(builder.notarize_app)
    assert '"ditto", "-c", "-k", "--sequesterRsrc", "--keepParent"' in stapled_app
    assert '"stapler", "staple", APP_PATH' in stapled_app
    stapled_dmg = inspect.getsource(builder.notarize_dmg)
    assert '"stapler", "staple", dmg_path' in stapled_dmg


def test_macos_notary_credentials_profile_api_key_or_none(monkeypatch, tmp_path):
    import pytest

    builder = _load_builder()
    for name in builder.NOTARY_API_KEY_ENV:
        monkeypatch.delenv(name, raising=False)

    # Ad-hoc/local builds: nothing configured, nothing notarized.
    assert builder.notary_credentials("") is None
    assert builder.notary_credentials("wayfinder-aura-notary") == [
        "--keychain-profile", "wayfinder-aura-notary",
    ]

    key = tmp_path / "AuthKey_TEST.p8"
    key.write_text("not a real key")
    monkeypatch.setenv("MACOS_NOTARY_API_KEY_PATH", str(key))
    with pytest.raises(SystemExit, match="partially configured"):
        builder.notary_credentials("")
    monkeypatch.setenv("MACOS_NOTARY_API_KEY_ID", "KEYID")
    monkeypatch.setenv("MACOS_NOTARY_API_ISSUER", "issuer-uuid")
    assert builder.notary_credentials("") == [
        "--key", str(key), "--key-id", "KEYID", "--issuer", "issuer-uuid",
    ]


def test_macos_notary_submit_requires_an_accepted_verdict(monkeypatch, tmp_path):
    import subprocess

    import pytest

    builder = _load_builder()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        stdout = '{"id": "sub-1", "status": "%s", "message": "done"}' % status
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(builder.subprocess, "run", fake_run)
    auth = ["--keychain-profile", "p"]

    status = "Accepted"
    builder.notary_submit(tmp_path / "a.dmg", auth)
    assert calls[-1][:3] == ["xcrun", "notarytool", "submit"]
    assert "--wait" in calls[-1]

    status = "Invalid"
    with pytest.raises(SystemExit, match="Invalid"):
        builder.notary_submit(tmp_path / "a.dmg", auth)
    assert calls[-1] == ["xcrun", "notarytool", "log", "sub-1", *auth]


def test_release_workflow_macos_job_never_gates_the_linux_release():
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    mac = workflow.split("\n  build-macos:\n", 1)[1].split("\n  release:\n", 1)[0]
    release = workflow.split("\n  release:\n", 1)[1].split("\n  publish-macos:\n", 1)[0]
    publish = workflow.split("\n  publish-macos:\n", 1)[1]

    # Linux assets are published without waiting on (or failing because of) macOS.
    assert "needs: [quality, release-readiness, build-appimage, build-flatpak]" in release
    assert "build-macos" not in release.split("steps:", 1)[0]
    assert "options: [appimage, hosted-flatpak, windows, macos, all]" in workflow

    # Same toolchain as the manual candidate workflow.
    assert "runs-on: macos-latest" in mac
    assert 'MACOS_PYTHON_VERSION: "3.12.10"' in mac
    assert "-c packaging/macos/constraints.txt -e '.[dev]'" in mac
    assert "python packaging/macos/build.py" in mac
    # Signed DMGs are verified before they get the publishable artifact name;
    # the temporary keychain is always removed.
    assert mac.index("spctl --assess --type open --context context:primary-signature") \
        < mac.index("name: wayfinder-aura-macos-dmg")
    assert "xcrun stapler validate" in mac
    assert "if: always()" in mac and "security delete-keychain" in mac

    # Only a signed build, after the Linux release exists, reaches the release.
    assert "needs: [release, build-macos]" in publish
    assert "needs.build-macos.outputs.signed == 'true'" in publish
    assert "name: wayfinder-aura-macos-dmg" in publish
    assert "wayfinder-aura-macos-unsigned" not in publish
    assert "gh release upload" in publish


def test_info_plist_polish_and_accessibility_hooks():
    root = Path(__file__).resolve().parent.parent
    spec = (root / "wayfinder-aura-macos.spec").read_text()
    build = (root / "packaging" / "macos" / "build.py").read_text()
    mic = "Wayfinder Aura uses the microphone only while you dictate, to turn your speech into text."
    assert mic in spec and mic in build  # validator and bundle must agree
    assert "NSHumanReadableCopyright" in spec
    for native in ("overlay_renderer.m", "hero_renderer.m"):
        assert "accessibilityDisplayShouldReduceMotion" in (root / "packaging" / "macos" / native).read_text()
    main = (root / "wayfinder_main.py").read_text()
    assert "::tk::mac::ShowPreferences" in main and "<Command-w>" in main


def test_reduce_transparency_turns_glass_off(monkeypatch):
    from wayfinder.ui import macos_window
    monkeypatch.setattr(macos_window.sys, "platform", "darwin")
    monkeypatch.delenv("WAYFINDER_MACOS_GLASS", raising=False)
    monkeypatch.setattr(macos_window, "_reduce_transparency", lambda: True)
    assert macos_window.glass_enabled() is False
    monkeypatch.setattr(macos_window, "_reduce_transparency", lambda: False)
    assert macos_window.glass_enabled() is True


def test_metal_waves_are_display_synced_off_the_main_thread():
    """Active waves render from a CADisplayLink on a private thread: frames land
    on every refresh (no NSTimer judder) and a busy main thread can't skip them."""
    root = REPO / "packaging" / "macos"
    clock = (root / "wf_render_clock.m").read_text()
    assert "displayLinkWithTarget:" in clock
    assert "addToRunLoop:WFRenderRunLoop()" in clock
    assert "NSQualityOfServiceUserInteractive" in clock
    for name in ("hero_renderer.m", "overlay_renderer.m"):
        source = (root / name).read_text()
        assert "NSTimer" not in source, name
        assert "renderClockTickAt:" in source and "WFRenderClock" in source, name
        assert "os_unfair_lock" in source, name  # state shared with the render thread
    overlay = (root / "overlay_renderer.m").read_text()
    assert "kWFLevelAttack" in overlay and "_targetAudioLevel" in overlay  # eased level
    builder = (root / "build.py").read_text()
    assert "wf_render_clock.m" in builder

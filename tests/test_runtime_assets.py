from pathlib import Path

from wayfinder.utils import runtime_assets


def _make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_appimage_whisper_cli_is_resolved_from_the_current_mount(tmp_path, monkeypatch):
    appdir = tmp_path / "mount"
    primary = _make_executable(appdir / "usr" / "bin" / "whisper-cli")
    stale = _make_executable(tmp_path / ".mount_stale" / "usr" / "bin" / "whisper-cli")
    monkeypatch.setattr(runtime_assets, "get_wayfinder_appimage_dir", lambda: appdir)
    monkeypatch.setattr(runtime_assets, "is_wayfinder_flatpak_env", lambda: False)
    monkeypatch.setattr(runtime_assets.shutil, "which", lambda _name: None)

    found = runtime_assets.find_whisper_binary(
        {"whisper_binary": str(stale)}
    )

    assert found == str(primary)


def test_cpu_lookup_prefers_packaged_cpu_safety_twin(tmp_path, monkeypatch):
    appdir = tmp_path / "mount"
    primary = _make_executable(appdir / "usr" / "bin" / "whisper-cli")
    cpu = _make_executable(appdir / "usr" / "bin" / "whisper-cli-cpu")
    monkeypatch.setattr(runtime_assets, "get_wayfinder_appimage_dir", lambda: appdir)
    monkeypatch.setattr(runtime_assets, "is_wayfinder_flatpak_env", lambda: False)
    monkeypatch.setattr(runtime_assets.shutil, "which", lambda _name: None)

    assert runtime_assets.find_whisper_binary({"whisper_binary": str(primary)}, cpu=True) == str(cpu)


def test_cpu_lookup_falls_back_to_primary_when_twin_is_absent(tmp_path, monkeypatch):
    primary = _make_executable(tmp_path / "whisper-cli")
    monkeypatch.setattr(runtime_assets, "get_wayfinder_appimage_dir", lambda: None)
    monkeypatch.setattr(runtime_assets, "is_wayfinder_flatpak_env", lambda: False)
    monkeypatch.setattr(runtime_assets.shutil, "which", lambda _name: None)

    assert runtime_assets.find_whisper_binary({"whisper_binary": str(primary)}, cpu=True) == str(primary)


def test_non_executable_candidate_is_rejected(tmp_path, monkeypatch):
    candidate = tmp_path / "whisper-cli"
    candidate.write_text("not executable")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr(runtime_assets, "get_wayfinder_appimage_dir", lambda: None)
    monkeypatch.setattr(runtime_assets, "is_wayfinder_flatpak_env", lambda: False)
    monkeypatch.setattr(runtime_assets.shutil, "which", lambda _name: None)

    assert runtime_assets.find_whisper_binary({"whisper_binary": str(candidate)}) is None

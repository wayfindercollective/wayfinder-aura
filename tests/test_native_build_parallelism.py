"""Native packaging must preserve the runner's configured compile concurrency."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_builder(platform):
    spec = importlib.util.spec_from_file_location(
        f"build_{platform}", ROOT / "packaging" / platform / "build.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("configured,expected", [("2", "2"), ("5", "5"), (None, "2"), ("", "2")])
@pytest.mark.parametrize("target", ["macos-whisper", "macos-llama", "windows-vulkan"])
def test_native_build_invocations_honor_job_limit(monkeypatch, tmp_path, target, configured, expected):
    if configured is None:
        monkeypatch.delenv("CMAKE_BUILD_PARALLEL_LEVEL", raising=False)
    else:
        monkeypatch.setenv("CMAKE_BUILD_PARALLEL_LEVEL", configured)
    commands = []
    platform, component = target.split("-")
    builder = load_builder(platform)
    # A large host must not silently replace the owner's smaller CI allocation.
    monkeypatch.setattr(builder.os, "cpu_count", lambda: 28)
    if platform == "macos":
        prefix = component.upper()
        source = tmp_path / "source"
        source.mkdir()
        build = tmp_path / "build"
        (build / "bin").mkdir(parents=True)
        monkeypatch.setattr(builder, f"{prefix}_SOURCE_DIR", source)
        monkeypatch.setattr(builder, f"{prefix}_BUILD_DIR", build)
        monkeypatch.setattr(builder, "NATIVE_BIN_DIR", tmp_path / "staged")
        monkeypatch.setattr(builder, "output", lambda args: getattr(builder, f"{prefix}_COMMIT"))
        monkeypatch.setattr(builder, "run", lambda args, **kw: commands.append([str(a) for a in args]))
        for name in getattr(builder, f"{prefix}_BINARIES"):
            (build / "bin" / name).write_bytes(b"fixture")
        getattr(builder, f"build_{component}")()
    else:
        source = tmp_path / "source"
        (source / ".git").mkdir(parents=True)
        output = tmp_path / "build/wvk/bin/Release"
        output.mkdir(parents=True)
        monkeypatch.setattr(builder, "ROOT", tmp_path)
        monkeypatch.setattr(builder, "WHISPER_SRC", source)
        monkeypatch.setattr(builder, "WHISPER_VULKAN_STAGE", tmp_path / "staged")
        monkeypatch.setattr(builder, "_vulkan_sdk", lambda: tmp_path / "sdk")
        monkeypatch.setattr(builder, "_whisper_build_tag", lambda: "fixture-tag")

        def run(args, **kwargs):
            commands.append([str(a) for a in args])
            return SimpleNamespace(stdout="fixture-commit", returncode=0)

        monkeypatch.setattr(builder, "subprocess", SimpleNamespace(run=run))
        for name in (*builder.WHISPER_FILES, "ggml-vulkan.dll"):
            (output / name).write_bytes(b"fixture")
        assert builder._stage_whisper_vulkan(required=True)

    build_commands = [args for args in commands if args[:2] == ["cmake", "--build"]]
    assert len(build_commands) == 1
    args = build_commands[0]
    index = args.index("--parallel")
    assert args[index + 1:index + 2] == [expected]

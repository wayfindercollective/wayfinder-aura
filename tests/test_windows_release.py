"""Windows release preflight and headless engine probe checks."""

import ast
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("result_text, expected", [(None, 0), (" Paris", 0), (" \n", 1)])
def test_llm_engine_probe(monkeypatch, capsys, result_text, expected):
    engine = MagicMock()
    monkeypatch.setitem(sys.modules, "llama_cpp", engine)
    monkeypatch.setattr(sys, "argv", ["main.py", "--llm-engine-self-test"])
    monkeypatch.delenv("WAYFINDER_LLM_SELFTEST_MODEL", raising=False)
    if result_text is not None:
        monkeypatch.setenv("WAYFINDER_LLM_SELFTEST_MODEL", "test.gguf")
        engine.Llama.return_value.create_completion.return_value = {
            "choices": [{"text": result_text}]
        }

    _run_engine_probe(expected)
    engine.llama_print_system_info.assert_called_once_with()
    if result_text is None:
        engine.Llama.assert_not_called()
    else:
        engine.Llama.assert_called_once_with(model_path="test.gguf", n_ctx=512, verbose=False)
        engine.Llama.return_value.create_completion.assert_called_once_with(
            prompt="The capital of France is", max_tokens=8
        )
        engine.Llama.return_value.close.assert_called_once_with()
    output = capsys.readouterr()
    assert ("LLM_ENGINE_SELF_TEST_OK" if expected == 0 else "LLM_ENGINE_SELF_TEST_FAILED") in (
        output.out + output.err
    )


@pytest.mark.parametrize("missing", [True, False])
def test_llm_engine_probe_reports_missing_engine_or_dll(monkeypatch, capsys, missing):
    engine = MagicMock()
    engine.llama_print_system_info.side_effect = OSError("native DLL unavailable")
    monkeypatch.setitem(sys.modules, "llama_cpp", None if missing else engine)
    monkeypatch.setattr(sys, "argv", ["main.py", "--llm-engine-self-test"])
    _run_engine_probe(1)
    assert "LLM_ENGINE_SELF_TEST_FAILED" in capsys.readouterr().err


def _run_engine_probe(expected):
    # Execute the actual entry-point branch without initializing desktop services.
    tree = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))
    probe = next(
        node for node in tree.body
        if isinstance(node, ast.If) and any(
            isinstance(value, ast.Constant) and value.value == "--llm-engine-self-test"
            for value in ast.walk(node.test)
        )
    )
    with pytest.raises(SystemExit) as exit_info:
        exec(compile(ast.Module(body=[probe], type_ignores=[]), "main.py", "exec"), {"sys": sys, "os": os})
    assert exit_info.value.code == expected


def test_required_installer_rejects_missing_llama_before_build(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("windows_build", REPO / "packaging/windows/build.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["build.py", "--require-installer"])
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: None)
    run = MagicMock()
    monkeypatch.setattr(build.subprocess, "run", run)

    assert build.main() == 1
    assert "llama_cpp is required" in capsys.readouterr().out
    run.assert_not_called()


def test_frozen_windows_prefers_bundled_whisper(monkeypatch, tmp_path):
    from wayfinder import config

    cli = tmp_path / "whisper" / "whisper-cli.exe"
    cli.parent.mkdir()
    cli.write_bytes(b"")
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "_MEIPASS", str(tmp_path), raising=False)

    assert config._windows_bundled_whisper() == str(cli)
    # A config saved by an older build still pointing at ~/whisper.cpp is repaired.
    assert config._repair_config_path("whisper_binary", "~/whisper.cpp/build/bin/whisper-cli.exe") == str(cli)


def test_source_runs_do_not_use_bundled_whisper(monkeypatch, tmp_path):
    from wayfinder import config

    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.delattr(config.sys, "frozen", raising=False)
    monkeypatch.setattr(config.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert config._windows_bundled_whisper() is None

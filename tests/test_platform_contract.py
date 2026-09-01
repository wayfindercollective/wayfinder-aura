"""Static and native checks for Aura's cross-platform development contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import tomllib

REPO = Path(__file__).resolve().parent.parent
PLATFORM_DOC = REPO / "docs" / "PLATFORM-DEVELOPMENT.md"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO / "pyproject.toml"


def test_platform_contract_declares_one_repo_and_honest_support_levels():
    contract = PLATFORM_DOC.read_text(encoding="utf-8")

    assert "one repository" in contract
    assert "Linux | Production baseline" in contract
    assert "macOS | Active port" in contract
    assert "Windows | Planned" in contract
    assert "Linux suite even for a macOS/Windows-only change" in contract
    assert "Do not advertise macOS or Windows" in contract


def test_platform_dependencies_remain_os_scoped():
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    dependencies = project["dependencies"]

    assert "evdev>=1.6.0; sys_platform == 'linux'" in dependencies
    assert "pyautogui>=0.9.54; sys_platform == 'darwin'" in dependencies


def test_ci_runs_native_macos_and_windows_platform_smoke():
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "platform-smoke:" in workflow
    assert "os: macos-latest" in workflow
    assert "expected: darwin" in workflow
    assert "os: windows-latest" in workflow
    assert "expected: windows" in workflow
    assert "python scripts/platform_smoke.py --expected ${{ matrix.expected }}" in workflow


def test_platform_smoke_passes_on_the_current_native_host():
    expected = "darwin" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else "linux"
    result = subprocess.run(
        [sys.executable, "scripts/platform_smoke.py", "--expected", expected],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert f"PLATFORM_SMOKE_OK {expected}" in result.stdout

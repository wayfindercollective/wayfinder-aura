"""Slack routing mirrors Wayfinder OS: main and stable releases -> prod, all else -> dev."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "slack-notify.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or not shutil.which("bash") or not shutil.which("jq"),
    reason="needs bash and jq (the GitHub Actions ubuntu runner has both)",
)


def _run(env_extra, tmp_path=None):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SLACK_", "LITELLM_", "OPENAI_", "GITHUB_"))}
    env.update(env_extra)
    return subprocess.run(["bash", str(SCRIPT)], env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


def _head():
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()


@pytest.mark.parametrize("branch,channel,tag", [
    ("main", "prod", "all platforms"),
    ("macos", "dev", "macOS"),
    ("feature/styles-vocabulary", "dev", ""),
])
def test_push_routes_main_to_prod_and_everything_else_to_dev(branch, channel, tag):
    out = _run({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_NAME": branch, "PUSH_AFTER": _head()})
    assert out.returncode == 0, out.stderr
    assert f"=== DRY RUN -> {channel} ===" in out.stdout
    assert f"*Wayfinder Aura* · " in out.stdout and f"pushed to `{branch}`" in out.stdout
    if tag:
        assert tag in out.stdout


def _release(prerelease, name="v1.2.0"):
    return {
        "tag_name": name, "name": name, "prerelease": prerelease,
        "html_url": f"https://github.com/x/releases/tag/{name}",
        "assets": [
            {"name": "Wayfinder_Aura-1.2.0-macOS-arm64.dmg", "browser_download_url": "https://d/a.dmg"},
            {"name": "Wayfinder_Aura-1.2.0-x86_64.AppImage", "browser_download_url": "https://d/a.AppImage"},
            {"name": "WayfinderAura-Setup.exe", "browser_download_url": "https://d/a.exe"},
        ],
    }


@pytest.mark.parametrize("prerelease,channel", [(False, "prod"), (True, "dev")])
def test_published_release_lists_every_platform(tmp_path, prerelease, channel):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"action": "published", "release": _release(prerelease),
                                 "sender": {"login": "someone"}}))
    out = _run({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "release", "GITHUB_EVENT_PATH": str(event)})
    assert out.returncode == 0, out.stderr
    assert f"=== DRY RUN -> {channel} ===" in out.stdout
    for platform in ("macOS", "Linux", "Windows"):
        assert platform in out.stdout


def test_failed_stable_release_build_goes_to_prod(tmp_path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": {
        "conclusion": "failure", "head_branch": "v1.2.0",
        "html_url": "https://github.com/x/run/1", "actor": {"login": "someone"}}}))
    out = _run({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "workflow_run", "GITHUB_EVENT_PATH": str(event)})
    assert "=== DRY RUN -> prod ===" in out.stdout and "failure" in out.stdout


def test_missing_webhook_warns_without_failing_the_push():
    out = _run({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "macos", "PUSH_AFTER": _head()})
    assert out.returncode == 0
    assert "SLACK_WEBHOOK_DEV is not set" in out.stdout

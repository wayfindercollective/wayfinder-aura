"""Slack routing mirrors Wayfinder OS: main -> prod, dev -> dev, all else -> feature."""

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
    ("dev", "dev", ""),
    ("macos", "feature", "macOS"),
    ("feature/styles-vocabulary", "feature", ""),
])
def test_push_routes_main_to_prod_dev_to_dev_and_other_branches_to_feature(branch, channel, tag):
    out = _run({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_NAME": branch, "PUSH_AFTER": _head()})
    assert out.returncode == 0, out.stderr
    assert f"=== DRY RUN -> {channel} ===" in out.stdout
    assert f"*Wayfinder Aura* · " in out.stdout and f"pushed to `{branch}`" in out.stdout
    if tag:
        assert tag in out.stdout


def _release(prerelease, name="v1.2.0"):
    base = f"https://github.com/wayfindercollective/wayfinder-aura/releases/download/{name}"
    return {
        "tag_name": name, "name": name, "prerelease": prerelease,
        "html_url": f"https://github.com/wayfindercollective/wayfinder-aura/releases/tag/{name}",
        "assets": [
            {"name": "Wayfinder_Aura-1.2.0-macOS-arm64.dmg", "browser_download_url": f"{base}/a.dmg"},
            {"name": "Wayfinder_Aura-1.2.0-x86_64.AppImage", "browser_download_url": f"{base}/a.AppImage"},
            {"name": "WayfinderAura-Setup.exe", "browser_download_url": f"{base}/a.exe"},
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
    out = _run({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "dev", "PUSH_AFTER": _head()})
    assert out.returncode == 0
    assert "SLACK_WEBHOOK_DEV is not set" in out.stdout
    feat = _run({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "macos", "PUSH_AFTER": _head()})
    assert feat.returncode == 0
    assert "SLACK_WEBHOOK_FEATURE is not set" in feat.stdout


def test_escapes_slack_mentions_links_and_blocks_untrusted_release_urls(tmp_path):
    # 1. Untrusted commit subject with broadcast tokens, user mention, and spoofed link
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Attacker <!channel>",
        "GIT_AUTHOR_EMAIL": "a@example.com",
        "GIT_COMMITTER_NAME": "Attacker <!channel>",
        "GIT_COMMITTER_EMAIL": "a@example.com",
    }
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, env=git_env, check=True, capture_output=True)
    (repo_dir / "f.txt").write_text("1\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, env=git_env, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: ping <!channel> <!here> <!subteam^S12345678> <@U12345678> <https://evil.example|click>"],
        cwd=repo_dir, env=git_env, check=True, capture_output=True,
    )
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()

    out = _run({
        "DRY_RUN": "1",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF_NAME": "feat/sec",
        "PUSH_AFTER": sha,
        "PUSH_SHAS": sha,
        "NOTIFY_GIT_DIR": str(repo_dir),
    })
    assert out.returncode == 0, out.stderr
    for raw_token in ("<!channel>", "<!here>", "<!subteam^S12345678>", "<@U12345678>", "<https://evil.example|click>", "https://evil.example"):
        assert raw_token not in out.stdout
    assert "&lt;!channel&gt;" in out.stdout
    assert "&lt;https[:]//evil[.]example|click&gt;" in out.stdout

    # 2. Release with untrusted html_url and untrusted asset browser_download_url
    event = tmp_path / "evil_release.json"
    event.write_text(json.dumps({
        "action": "published",
        "sender": {"login": "actor <!here>"},
        "release": {
            "tag_name": "v9.9.9",
            "name": "v9.9.9 <https://evil.example|spoof>",
            "prerelease": False,
            "html_url": "https://github.com/wayfindercollective/wayfinder-aura/../other/releases/tag/v9.9.9",
            "assets": [
                {"name": "Evil.dmg", "browser_download_url": "https://evil.example/Evil.dmg"},
                {
                    "name": "Traversal.dmg",
                    "browser_download_url": "https://github.com/wayfindercollective/wayfinder-aura/../other/releases/download/v9.9.9/a.dmg",
                },
                {
                    "name": "EncodedTraversal.dmg",
                    "browser_download_url": "https://github.com/wayfindercollective/wayfinder-aura/%2e%2e/other/releases/download/v9.9.9/b.dmg",
                },
                {
                    "name": "Good|Name.dmg",
                    "browser_download_url": "https://github.com/wayfindercollective/wayfinder-aura/releases/download/v9.9.9/Good.dmg",
                },
            ],
        },
    }))
    rel_out = _run({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "release", "GITHUB_EVENT_PATH": str(event)})
    assert rel_out.returncode == 0, rel_out.stderr
    assert "evil.example/Evil.dmg" not in rel_out.stdout
    assert "<https://evil.example|spoof>" not in rel_out.stdout
    assert "https://evil.example" not in rel_out.stdout
    assert "&lt;https[:]//evil[.]example|spoof&gt;" in rel_out.stdout
    assert "/other/" not in rel_out.stdout
    assert "Traversal" not in rel_out.stdout
    assert "<!here>" not in rel_out.stdout
    assert "Good/Name.dmg>" in rel_out.stdout
    assert "<https://github.com/wayfindercollective/wayfinder-aura/releases|Release notes and downloads>" in rel_out.stdout


def test_payloads_disable_automatic_url_parsing_and_defang_llm_summary_urls(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    captured = tmp_path / "payloads.jsonl"
    curl_shim = bin_dir / "curl"
    curl_shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "url=''\n"
        "data=''\n"
        "want_code=0\n"
        "status_code=0\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -d) data=\"$2\"; shift 2 ;;\n"
        "    -w)\n"
        "      if [[ \"$2\" == *\"__HTTP__\"* ]]; then status_code=1; else want_code=1; fi\n"
        "      shift 2 ;;\n"
        "    -H|-X|--connect-timeout|--max-time) shift 2 ;;\n"
        "    http*) url=\"$1\"; shift ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "if [ \"$data\" = \"@-\" ]; then data=\"$(cat)\"; fi\n"
        "if [[ \"$url\" == *\"/omlx/status\" ]]; then\n"
        "  printf '{\"loaded_models\":[\"qwen3.8-flash-next-mlx\"]}\\n__HTTP__200'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$url\" == *\"/v1/models\" ]]; then\n"
        "  printf '{\"data\":[{\"id\":\"qwen3.8-flash-next\"}]}\\n__HTTP__200'\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$url\" == *\"/v1/chat/completions\" ]]; then\n"
        "  printf '{\"choices\":[{\"message\":{\"content\":\"<<<WAYFINDER_FINAL>>>\\\\n✨ Download https://evil.example/update or visit evil.com (keeps slack-notify.sh)\\\\n<<<END_WAYFINDER_FINAL>>>\"}}]}\\n__HTTP__200'\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$data\" >> \"" + str(captured) + "\"\n"
        "if [[ \"$url\" == *\"chat.postMessage\" ]]; then\n"
        "  printf '{\"ok\":true,\"ts\":\"123.456\"}'\n"
        "elif [ \"$want_code\" = \"1\" ]; then\n"
        "  printf 'ok\\n200'\n"
        "fi\n"
    )
    curl_shim.chmod(0o755)

    base_env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "GITHUB_EVENT_NAME": "push",
        "PUSH_AFTER": _head(),
        "LITELLM_BASE_URL": "http://127.0.0.1:18080",
    }

    # 1. Webhook transport (`main` -> SLACK_WEBHOOK_PROD)
    w_out = _run({
        **base_env,
        "GITHUB_REF_NAME": "main",
        "SLACK_WEBHOOK_PROD": "https://hooks.slack.com/services/T000/B000/fake-unit-test",
    })
    assert w_out.returncode == 0, w_out.stderr

    # 2. Bot token transport (`feat/x` -> chat.postMessage)
    b_out = _run({
        **base_env,
        "GITHUB_REF_NAME": "feat/x",
        "SLACK_BOT_TOKEN": "xoxb-test-token",
    })
    assert b_out.returncode == 0, b_out.stderr

    lines = [json.loads(line) for line in captured.read_text().splitlines() if line.strip()]
    assert len(lines) == 2
    for payload in lines:
        assert payload["parse"] == "none"
        assert payload["unfurl_links"] is False
        assert payload["unfurl_media"] is False
        assert "https://evil.example" not in payload["text"]
        assert "evil.com" not in payload["text"]
        assert "https[:]//evil[.]example/update" in payload["text"]
        assert "evil[.]com" in payload["text"]
        assert "slack-notify.sh" in payload["text"]


def test_blocks_node_option_injection_in_release_and_commit_metadata(tmp_path):
    import urllib.parse

    marker = tmp_path / "node_option_injected"
    esm_code = f'import fs from "node:fs";fs.writeFileSync({json.dumps(str(marker))},"pwned");'
    injected = f"--import=data:text/javascript,{urllib.parse.quote(esm_code)} v9.9.9"

    # 1. Release title / actor starting with --import=data:text/javascript,...
    event = tmp_path / "option_injection_release.json"
    event.write_text(json.dumps({
        "action": "published",
        "sender": {"login": injected},
        "release": {
            "tag_name": "v9.9.9",
            "name": injected,
            "prerelease": False,
            "html_url": "https://github.com/wayfindercollective/wayfinder-aura/releases/tag/v9.9.9",
            "assets": [],
        },
    }))
    rel_out = _run({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "release", "GITHUB_EVENT_PATH": str(event)})
    assert rel_out.returncode == 0, rel_out.stderr
    assert not marker.exists(), "Node option injection executed code from release metadata!"
    assert "--import=data:text/javascript" in rel_out.stdout

    # 2. Commit subject and author starting with --import=data:text/javascript,...
    repo_dir = tmp_path / "opt_repo"
    repo_dir.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": injected,
        "GIT_AUTHOR_EMAIL": "a@example.com",
        "GIT_COMMITTER_NAME": injected,
        "GIT_COMMITTER_EMAIL": "a@example.com",
    }
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, env=git_env, check=True, capture_output=True)
    (repo_dir / "f.txt").write_text("1\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, env=git_env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", injected], cwd=repo_dir, env=git_env, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, check=True).stdout.strip()

    push_out = _run({
        "DRY_RUN": "1",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF_NAME": injected,
        "PUSH_AFTER": sha,
        "PUSH_SHAS": sha,
        "NOTIFY_GIT_DIR": str(repo_dir),
    })
    assert push_out.returncode == 0, push_out.stderr
    assert not marker.exists(), "Node option injection executed code from commit/branch metadata!"
    assert "--import=data:text/javascript" in push_out.stdout



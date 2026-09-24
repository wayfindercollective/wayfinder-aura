"""Local Slack poller: what it posts, and that it retries only what failed."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("slack_local_poller", ROOT / "scripts" / "slack_local_poller.py")
poller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(poller)


def _rel(i, draft=False, tag=None):
    return {"id": i, "draft": draft, "tag_name": tag or f"v1.{i}.0"}


def test_first_run_records_everything_and_posts_nothing():
    actions, state = poller.plan({}, {"main": "a", "macos": "b"}, [_rel(2), _rel(1)])
    assert actions == []
    assert state == {"heads": {"main": "a", "macos": "b"}, "releases": [2, 1]}


def test_changed_and_new_branches_are_posted_unchanged_ones_are_not():
    state = {"heads": {"main": "a", "macos": "b"}, "releases": []}
    actions, _ = poller.plan(state, {"main": "a", "macos": "c", "feature/x": "d"}, [])
    assert actions == [
        {"kind": "push", "branch": "feature/x", "before": None, "after": "d"},
        {"kind": "push", "branch": "macos", "before": "b", "after": "c"},
    ]


def test_new_published_releases_post_oldest_first_and_drafts_never():
    state = {"heads": {}, "releases": [1]}
    actions, new_state = poller.plan(state, {}, [_rel(4), _rel(3, draft=True), _rel(2), _rel(1)])
    assert [a["release"]["id"] for a in actions] == [2, 4]
    assert new_state["releases"] == [1, 4, 2]


def test_a_failed_post_is_retried_next_run():
    state = {"heads": {"macos": "b"}, "releases": []}
    actions, _ = poller.plan(state, {"macos": "c"}, [])
    # Post failed: state not advanced, so the next plan repeats it.
    again, _ = poller.plan(state, {"macos": "c"}, [])
    assert again == actions
    done = poller.mark_done(state, actions[0])
    assert poller.plan(done, {"macos": "c"}, [])[0] == []


def test_mark_done_bounds_the_release_history():
    state = {"heads": {}, "releases": list(range(poller.KEEP_RELEASE_IDS))}
    done = poller.mark_done(state, {"kind": "release", "release": {"id": 10_000}})
    assert len(done["releases"]) == poller.KEEP_RELEASE_IDS and done["releases"][-1] == 10_000


def test_push_post_passes_the_mirror_and_commit_range(monkeypatch):
    seen = {}

    def fake_run(cmd, env=None, **kw):
        seen.update(env)
        return subprocess.CompletedProcess(cmd, 0, "Slack response: 200\n", "")

    monkeypatch.setattr(poller, "litellm_key", lambda: "k")
    ok = poller.post({"kind": "push", "branch": "macos", "before": "b", "after": "c"},
                     {"macos": "c"}, {"dev": "https://d", "prod": "https://p"}, run=fake_run)
    assert ok
    assert seen["NOTIFY_GIT_DIR"] == str(poller.MIRROR)
    assert (seen["GITHUB_REF_NAME"], seen["PUSH_BEFORE"], seen["PUSH_AFTER"]) == ("macos", "b", "c")
    assert seen["LITELLM_BASE_URL"].startswith("http://127.0.0.1")
    assert "PUSH_SHAS" not in seen


def test_non_200_is_a_failure(monkeypatch):
    monkeypatch.setattr(poller, "litellm_key", lambda: "")
    bad = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "Slack response: 404\n", "")
    assert not poller.post({"kind": "push", "branch": "macos", "before": "b", "after": "c"},
                           {}, {"dev": "d", "prod": "p"}, run=bad)


def test_release_post_uses_a_release_event(monkeypatch):
    seen = {}

    def fake_run(cmd, env=None, **kw):
        seen.update(env)
        seen["payload"] = Path(env["GITHUB_EVENT_PATH"]).read_text()
        return subprocess.CompletedProcess(cmd, 0, "Slack response: 200\n", "")

    monkeypatch.setattr(poller, "litellm_key", lambda: "")
    rel = {"id": 7, "tag_name": "v1.2.0", "author": {"login": "someone"}}
    assert poller.post({"kind": "release", "release": rel}, {}, {"dev": "d", "prod": "p"}, run=fake_run)
    assert seen["GITHUB_EVENT_NAME"] == "release" and '"published"' in seen["payload"]
    assert not Path(seen["GITHUB_EVENT_PATH"]).exists()  # temp file cleaned up


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="needs this repository's history")
def test_notify_script_reads_commits_from_another_git_dir(tmp_path):
    """NOTIFY_GIT_DIR + PUSH_SHAS: the poller's mirror path, dry run."""
    import os
    import shutil
    import sys
    if sys.platform == "win32" or not shutil.which("jq"):
        pytest.skip("needs bash and jq")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SLACK_", "LITELLM_", "OPENAI_", "GITHUB_"))}
    env.update({"DRY_RUN": "1", "GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": "feature/new",
                "PUSH_AFTER": head, "PUSH_SHAS": head, "NOTIFY_GIT_DIR": str(ROOT)})
    out = subprocess.run(["bash", str(ROOT / ".github/scripts/slack-notify.sh")], env=env,
                         cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "=== DRY RUN -> dev ===" in out.stdout and head[:8] in out.stdout


@pytest.mark.parametrize("value,ok", [
    ("https://hooks.slack.com/services/T000/B000/abcdefghijklmnop", True),
    ("abc123", False),                          # a password typed by mistake
    ("http://hooks.slack.com/services/x/y/z", False),
    ("https://example.com/hooks.slack.com/services/a/b/c", False),
    ("", False),
])
def test_only_slack_webhook_urls_are_ever_used(value, ok):
    assert poller.valid_webhook(value) is ok


def test_main_refuses_a_non_webhook_keychain_value(monkeypatch, capsys):
    values = {"SLACK_WEBHOOK_DEV": "abc123",
              "SLACK_WEBHOOK_PROD": "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"}
    monkeypatch.setattr(poller, "keychain", lambda service: values.get(service, ""))
    monkeypatch.setattr(poller, "refresh_mirror", lambda: pytest.fail("must not fetch or post"))
    assert poller.main() == 0
    out = capsys.readouterr().out
    assert "dev" in out and "not a Slack webhook" in out and "abc123" not in out

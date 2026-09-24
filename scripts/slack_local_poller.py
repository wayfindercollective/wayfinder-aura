#!/usr/bin/env python3
"""Post Wayfinder Aura pushes and releases to Slack, summarised by local Qwen.

The same posts as .github/workflows/slack-notify.yml, but written on this Mac
so the AI summary comes from the local model behind LiteLLM (like Wayfinder
OS's). The GitHub workflow stands down while the repository variable
SLACK_POSTER is "local". Installed and scheduled (every 2 minutes, launchd)
by scripts/install_slack_local_poller.sh.

Safety:
- It never runs code from the repository. The notify script and prompts are
  fixed copies made at install time; fetched commits are only read (messages,
  diff stats) from a private bare mirror of the public repo.
- Webhook URLs come from the login Keychain (account "wayfinder-aura-slack");
  the LiteLLM key from the Keychain or ~/litellm-proxy/config.yaml. Nothing
  secret is written to the log.
- The first run records the current branches and releases and posts nothing.
- A post that fails is retried on the next run; nothing is marked done until
  Slack accepted it.

Python 3.9 standard library only (it runs under /usr/bin/python3).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

REPO = "wayfindercollective/wayfinder-aura"
BASE = Path.home() / "Library" / "Application Support" / "wayfinder-aura-slack"
MIRROR = BASE / "aura.git"
STATE = BASE / "state.json"
NOTIFY = BASE / "bin" / "slack-notify.sh"
KEYCHAIN_ACCOUNT = "wayfinder-aura-slack"
LITELLM_BASE_URL = os.environ.get("AURA_SLACK_LITELLM_URL", "http://127.0.0.1:8080")
LITELLM_CONFIG = Path.home() / "litellm-proxy" / "config.yaml"
KEEP_RELEASE_IDS = 200


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


# --- decisions (pure; tested) --------------------------------------------------

def plan(
    state: Dict,
    heads: Dict[str, str],
    releases: List[Dict],
) -> Tuple[List[Dict], Dict]:
    """What to post, and the state once everything posted.

    ``state``: {"heads": {branch: sha}, "releases": [ids]}; empty = first run.
    ``releases``: the API's list (newest first). Drafts never post.
    """
    published = [r for r in releases if not r.get("draft")]
    if not state:
        return [], {"heads": dict(heads), "releases": [r["id"] for r in published]}

    actions: List[Dict] = []
    known = state.get("heads", {})
    for branch in sorted(heads):
        sha = heads[branch]
        before = known.get(branch)
        if before == sha:
            continue
        actions.append({"kind": "push", "branch": branch, "before": before, "after": sha})
    seen = set(state.get("releases", []))
    for release in reversed(published):  # oldest first
        if release["id"] not in seen:
            actions.append({"kind": "release", "release": release})

    new_state = {
        "heads": dict(heads),
        "releases": (list(state.get("releases", [])) + [r["id"] for r in published if r["id"] not in seen])[-KEEP_RELEASE_IDS:],
    }
    return actions, new_state


def mark_done(state: Dict, action: Dict) -> Dict:
    """State after one action posted (so a failure retries only what failed)."""
    state = {"heads": dict(state.get("heads", {})), "releases": list(state.get("releases", []))}
    if action["kind"] == "push":
        state["heads"][action["branch"]] = action["after"]
    else:
        state["releases"] = (state["releases"] + [action["release"]["id"]])[-KEEP_RELEASE_IDS:]
    return state


# --- git mirror ----------------------------------------------------------------

def git(*args: str, check: bool = True) -> str:
    out = subprocess.run(["/usr/bin/git", "-C", str(MIRROR), *args],
                         capture_output=True, text=True, timeout=300)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def refresh_mirror() -> None:
    if not (MIRROR / "HEAD").exists():
        MIRROR.mkdir(parents=True, exist_ok=True)
        git("init", "--bare", "--quiet")
        git("remote", "add", "origin", f"https://github.com/{REPO}.git")
        # Branches and tags only (a plain --mirror also pulls every PR ref).
        git("config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    git("fetch", "--prune", "--quiet", "--tags", "origin")


def branch_heads() -> Dict[str, str]:
    heads = {}
    for line in git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines():
        name, _, sha = line.partition(" ")
        if name and sha:
            heads[name] = sha
    return heads


def new_branch_commits(branch: str, after: str, heads: Dict[str, str]) -> List[str]:
    """Commits on a new branch that no other branch has (oldest first)."""
    others = [sha for name, sha in heads.items() if name != branch]
    shas = git("rev-list", "--reverse", after, "--not", *others).split() if others else [after]
    return shas[-250:]


def fetch_releases() -> List[Dict]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/releases?per_page=10",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "wayfinder-aura-slack-poller"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


# --- secrets -------------------------------------------------------------------

def keychain(service: str) -> str:
    out = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def litellm_key() -> str:
    key = keychain("LITELLM_API_KEY")
    if key:
        return key
    try:
        for line in LITELLM_CONFIG.read_text().splitlines():
            m = re.match(r"\s*master_key:\s*[\"']?([^\"'\s#]+)", line)
            if m and not m.group(1).startswith("os.environ/"):
                return m.group(1)
    except OSError:
        pass
    return ""


# --- posting -------------------------------------------------------------------

def notify_env(webhooks: Dict[str, str]) -> Dict[str, str]:
    return {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_SERVER_URL": "https://github.com",
        "NOTIFY_GIT_DIR": str(MIRROR),
        "SLACK_WEBHOOK_DEV": webhooks["dev"],
        "SLACK_WEBHOOK_PROD": webhooks["prod"],
        "LITELLM_BASE_URL": LITELLM_BASE_URL,
        "LITELLM_API_KEY": litellm_key(),
        "LITELLM_STATUS_PATH": "/omlx/status",
    }


def post(action: Dict, heads: Dict[str, str], webhooks: Dict[str, str],
         run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> bool:
    env = notify_env(webhooks)
    tmp = None
    if action["kind"] == "push":
        env.update({"GITHUB_EVENT_NAME": "push", "GITHUB_REF_NAME": action["branch"],
                    "PUSH_AFTER": action["after"], "PUSH_BEFORE": action["before"] or ""})
        if not action["before"]:
            shas = new_branch_commits(action["branch"], action["after"], heads)
            if not shas:
                log(f"new branch {action['branch']} has no commits of its own; nothing to post")
                return True
            env["PUSH_SHAS"] = "\n".join(shas)
    else:
        release = action["release"]
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"action": "published", "release": release,
                   "sender": {"login": (release.get("author") or {}).get("login", "")}}, tmp)
        tmp.close()
        env.update({"GITHUB_EVENT_NAME": "release", "GITHUB_EVENT_PATH": tmp.name})
    try:
        out = run(["/bin/bash", str(NOTIFY)], env=env, capture_output=True, text=True, timeout=1500)
    finally:
        if tmp:
            os.unlink(tmp.name)
    for line in (out.stdout + out.stderr).splitlines():
        if "Slack response" in line or "::error" in line or "::warning" in line or "summary" in line.lower():
            log("  " + line.strip())
    return out.returncode == 0 and "Slack response: 200" in out.stdout


def load_state() -> Dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: Dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


def main() -> int:
    webhooks = {"dev": keychain("SLACK_WEBHOOK_DEV"), "prod": keychain("SLACK_WEBHOOK_PROD")}
    if not webhooks["dev"] or not webhooks["prod"]:
        log("Slack webhooks are not in the Keychain yet (see install_slack_local_poller.sh); waiting")
        return 0
    try:
        refresh_mirror()
        heads = branch_heads()
        releases = fetch_releases()
    except Exception as exc:  # offline, GitHub hiccup: try again next run
        log(f"could not refresh ({exc}); will retry")
        return 0

    state = load_state()
    actions, final_state = plan(state, heads, releases)
    if not state:
        save_state(final_state)
        log(f"first run: recorded {len(heads)} branches and {len(final_state['releases'])} releases; nothing posted")
        return 0
    for action in actions:
        label = action["branch"] if action["kind"] == "push" else action["release"].get("tag_name")
        log(f"posting {action['kind']} {label}")
        if post(action, heads, webhooks):
            state = mark_done(state, action)
            save_state(state)
        else:
            log(f"  post failed for {label}; will retry next run")
    # Branches that disappeared leave the state; unchanged ones are carried.
    state["heads"] = {b: s for b, s in state.get("heads", {}).items() if b in heads}
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

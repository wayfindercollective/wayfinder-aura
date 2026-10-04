#!/usr/bin/env python3
"""Cut the next beta from main, if main moved and its required checks passed.

Run by .github/workflows/beta.yml (nightly, or by hand). It:

1. skips when main is the commit the newest beta was cut from (unless --force),
   or when main's required checks have not all passed;
2. commits "release: X.Y.Z-beta.N" on top of main (version stamped in every
   file, a development <release> in the metainfo) and tags it vX.Y.Z-beta.N.
   The commit is reachable only through the tag: main is never written;
3. pushes the tag and starts the Release workflow on it (a tag pushed with the
   workflow token cannot start workflows itself, but a dispatch can);
4. deletes betas it made (release and tag) that are both older than
   --keep-days (14: longer than promote.py's 5-day soak) and not among the
   newest --keep (5). Stable releases and hand-made tags (v1.1.8-beta.10 and
   earlier) are never touched.

Resumable: when the newest automated beta is unfinished (its tag exists but
its release does not, or lacks a Linux download: the dispatch failed, or the
build or an upload did), the next run waits on its build, or starts it again
(once) while main is unchanged. Once main has moved (a fix landed, perhaps for
the very failure: a tag rebuilds with the workflow it was cut with), the next
beta replaces it instead.

Needs git, and gh authenticated (GH_TOKEN) with contents+actions write.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import versioning  # noqa: E402

ROOT = versioning.ROOT
# Check names that must have passed on main's head (the same ones that
# protect main). Windows may be "skipped" while AURA_WINDOWS_CI is off.
REQUIRED_CHECKS = {
    "Quality": {"success"},
    "Platform smoke (macOS)": {"success"},
    "Windows tests": {"success", "skipped"},
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], cwd=ROOT, text=True).strip()


def release_for(repo: str, tag: str) -> dict | None:
    """The tag's release, or None when it has none (any other API error raises:
    the run fails and the next one tries again)."""
    proc = subprocess.run(["gh", "api", f"repos/{repo}/releases/tags/{tag}"],
                          cwd=ROOT, capture_output=True, text=True)
    if proc.returncode == 0:
        return json.loads(proc.stdout or "null")
    if "404" in proc.stderr:
        return None
    raise RuntimeError(f"could not read the {tag} release: {proc.stderr.strip()[:200]}")


def note(message: str) -> None:
    print(message, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")


def beta_tags(tags):
    return sorted(
        (t for t in tags if versioning.beta_number(t) is not None),
        key=lambda t: (versioning.parse(t)[0], versioning.beta_number(t)),
    )


def latest_verdicts(runs: list) -> dict[str, str]:
    """Each check's newest run decides: check-run IDs only grow, so a re-run
    outranks an older success even while it is queued with no start time yet
    (it then reads as pending: no beta)."""
    newest: dict[str, dict] = {}
    for run in runs:
        current = newest.get(run["name"])
        if current is None or (run.get("id") or 0) > (current.get("id") or 0):
            newest[run["name"]] = run
    return {
        name: (run["conclusion"] if run["status"] == "completed" else run["status"]) or "pending"
        for name, run in newest.items()
    }


def check_verdicts(repo: str, sha: str) -> dict[str, str]:
    raw = gh("api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
             "--jq", "[.check_runs[] | {id, name, status, conclusion}]")
    return latest_verdicts(json.loads(raw or "[]"))


def checks_passed(verdicts: dict[str, str]) -> list[str]:
    """Names of required checks that have not passed (empty = all good)."""
    return [
        f"{name}: {verdicts.get(name, 'missing')}"
        for name, ok in REQUIRED_CHECKS.items()
        if verdicts.get(name) not in ok
    ]


AUTOMATED_MARK = "(beta of main "


def automated_betas() -> set[str]:
    """Beta tags this script made (its annotation), never older hand-made ones."""
    out = git("for-each-ref", "refs/tags/v*", "--format=%(refname:short)\t%(contents:subject)")
    return {
        line.split("\t", 1)[0] for line in out.splitlines()
        if AUTOMATED_MARK in line.partition("\t")[2]
    }


def releases(repo: str) -> list[dict]:
    raw = gh("api", f"repos/{repo}/releases?per_page=100",
             "--jq", "[.[] | {tag_name, published_at, draft, prerelease}]")
    return json.loads(raw or "[]")


def to_prune(listing: list[dict], ours: set[str], keep: int, keep_days: int,
             now: datetime) -> list[str]:
    """Automated betas past both limits: older than keep_days AND not among the
    newest `keep`. Keeping two weeks covers promote.py's 5-day soak, so the
    candidate being soaked is never deleted under it."""
    published = {r["tag_name"]: r.get("published_at") or "" for r in listing
                 if r.get("tag_name") in ours and not r.get("draft")}
    ordered = beta_tags(published)
    newest = set(ordered[-keep:]) if keep else set()
    old = []
    for tag in ordered:
        try:
            when = datetime.fromisoformat(published[tag].replace("Z", "+00:00"))
        except ValueError:
            continue  # unknown age: keep
        if tag not in newest and now - when > timedelta(days=keep_days):
            old.append(tag)
    return old


def prune(repo: str, keep: int, keep_days: int, dry_run: bool) -> None:
    for tag in to_prune(releases(repo), automated_betas(), keep, keep_days,
                        datetime.now(timezone.utc)):
        note(f"Pruning old beta {tag}")
        if not dry_run:
            gh("release", "delete", tag, "--repo", repo, "--cleanup-tag", "--yes")


def resume_unfinished(repo: str, tag: str, dry_run: bool, head: str | None = None) -> bool:
    """Finish the newest beta when its release is missing, a draft, or lacks
    a Linux download (the Mac DMG comes from the Mac release watcher).

    True when this run handled the beta (a build is running, or one was
    started), so no new beta today. False when the beta is complete; when
    main (``head``) has moved since the beta was cut, so the next beta
    replaces it; or when two builds of it ended without completing it. An
    unfinished beta is left as it is (promote.py never picks one).
    """
    missing = versioning.missing_assets(release_for(repo, tag),
                                        versioning.release_assets(tag, mac=False))
    if not missing:
        return False
    raw = gh("api", f"repos/{repo}/actions/workflows/release.yml/runs?branch={tag}&per_page=10",
             "--jq", "[.workflow_runs[] | {status, conclusion}]")
    runs = json.loads(raw or "[]")
    if any(r["status"] != "completed" for r in runs):
        note(f"{tag}: its release build is still running.")
        return True
    if head and git("rev-parse", f"{tag}^{{commit}}^") != head:
        note(f"{tag} is missing {', '.join(missing)}, and main has moved since it was cut: "
             "the next beta replaces it.")
        return False
    if len(runs) >= 2:
        note(f"{tag}: still missing {', '.join(missing)} after {len(runs)} release builds; "
             "needs a look (Actions → Release).")
        return False
    note(f"{tag} is missing {', '.join(missing)}: starting its Release workflow.")
    if not dry_run:
        gh("workflow", "run", "release.yml", "--repo", repo, "--ref", tag)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY",
                                                         "wayfindercollective/wayfinder-aura"))
    parser.add_argument("--force", action="store_true", help="cut even if main is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="decide and print only")
    parser.add_argument("--keep", type=int, default=5, help="newest betas always kept")
    parser.add_argument("--keep-days", type=int, default=14,
                        help="betas this recent are kept too (covers the 5-day soak)")
    args = parser.parse_args()

    head = git("rev-parse", "HEAD")
    tags = git("tag", "--list", "v*").split()
    ours = beta_tags(automated_betas())
    if ours and resume_unfinished(args.repo, ours[-1], args.dry_run, head):
        return 0
    betas = beta_tags(tags)
    if betas and not args.force:
        last = betas[-1]
        if git("rev-parse", f"{last}^{{commit}}^") == head:
            note(f"main ({head[:9]}) is unchanged since {last}: no new beta.")
            return 0

    failing = checks_passed(check_verdicts(args.repo, head))
    if failing:
        note(f"main ({head[:9]}) has not passed its checks yet ({'; '.join(failing)}): "
             "no beta today; the next run tries again.")
        return 0

    version = versioning.next_beta(versioning.read_version(), tags)
    tag = f"v{version}"
    note(f"Cutting {tag} from main {head[:9]}.")
    if args.dry_run:
        return 0

    today = date.today()
    versioning.set_version(version)
    titles = versioning.bullet_titles(
        versioning.unreleased_body((ROOT / versioning.CHANGELOG).read_text(encoding="utf-8")))
    versioning.add_metainfo_release(
        version, today, f"Beta built from main on {today.isoformat()}. Changes since the last stable release:",
        titles, development=True)
    git("add", "-A", "pyproject.toml", "src/wayfinder/__init__.py", "scripts/build-appimage.sh",
        "packaging/windows/installer.iss", versioning.METAINFO)
    bot = ["-c", "user.name=github-actions[bot]",
           "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]
    git(*bot, "commit", "-q", "-m", f"release: {version}\n\nBeta of main {head}.")
    git(*bot, "tag", "-a", tag, "-m", f"Wayfinder Aura {version} {AUTOMATED_MARK}{head})")
    git("push", "origin", f"refs/tags/{tag}")
    gh("workflow", "run", "release.yml", "--repo", args.repo, "--ref", tag)
    note(f"Pushed {tag} and started the Release workflow on it.")

    prune(args.repo, args.keep, args.keep_days, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

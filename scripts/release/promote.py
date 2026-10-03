#!/usr/bin/env python3
"""Promote a beta that has held up to a stable release.

    GH_TOKEN=$(gh auth token --user wayfindercollective) \
        python scripts/release/promote.py            # newest beta, its version
    python scripts/release/promote.py --beta v1.2.0-beta.3 --version 1.2.0

Stable ships exactly the code the beta was built from (the main commit under
the beta's release commit), not whatever main holds today. The script:

1. picks the newest beta whose downloads (Mac DMG included) have been
   available for --min-days (the soak; --yes overrides);
2. commits "release: X.Y.Z" on that main commit: version stamped everywhere,
   the beta's Unreleased CHANGELOG notes moved under ## [X.Y.Z], and a stable
   <release> in the metainfo (Flathub/AppStream);
3. opens a pull request into main carrying the same CHANGELOG and metainfo
   notes (only the bullets the beta had, so newer notes stay Unreleased);
4. pushes tag vX.Y.Z, which runs the Release workflow: Linux packages, and the
   signed Mac DMG once the signing secrets are set (docs/RELEASING.md).

Re-running after a failure resumes: a pushed tag or an open notes PR is
kept, a tag that never left this machine is remade. --dry-run prints the
plan and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import versioning  # noqa: E402
from cut_beta import automated_betas, beta_tags  # noqa: E402

ROOT = versioning.ROOT
REPO = "wayfindercollective/wayfinder-aura"


def run(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.check_output(list(args), cwd=cwd, text=True).strip()


def stamp_release(tree: Path, version: str, released: str, today: date) -> None:
    """Version, CHANGELOG and metainfo for ``version`` in the checkout ``tree``."""
    changelog = tree / versioning.CHANGELOG
    changelog.write_text(
        versioning.promote_changelog(
            changelog.read_text(encoding="utf-8"), version, released, today),
        encoding="utf-8")
    lead = released.split("\n### ", 1)[0].strip()
    intro = lead if lead and not lead.startswith("- ") else f"Wayfinder Aura {version}:"
    versioning.add_metainfo_release(
        version, today, intro, versioning.bullet_titles(released),
        development=False, root=tree)


def availability(release: dict) -> datetime | None:
    """When a beta became fully available: its newest uploaded asset (the Mac
    DMG lands an hour or so after the Linux packages), else its publication."""
    stamps = [a.get("updated_at") for a in release.get("assets") or []
              if a.get("state") == "uploaded"] + [release.get("published_at")]
    times = []
    for stamp in stamps:
        try:
            times.append(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")))
        except ValueError:
            continue
    return max(times) if times else None


def pick_candidate(releases: list[dict], ours: set[str], min_days: int,
                   now: datetime) -> tuple[str, int] | None:
    """The newest automated beta that has been available for min_days."""
    by_tag = {r["tag_name"]: r for r in releases
              if r.get("tag_name") in ours and not r.get("draft")}
    for tag in reversed(beta_tags(by_tag)):
        when = availability(by_tag[tag])
        if when is not None and (now - when).days >= min_days:
            return tag, (now - when).days
    return None


def remote_has(ref: str) -> bool:
    return bool(run("git", "ls-remote", "origin", ref))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--beta", help="beta tag to promote (default: the newest one "
                                       "available for --min-days)")
    parser.add_argument("--version", help="stable version (default: the beta's X.Y.Z)")
    parser.add_argument("--min-days", type=int, default=5, help="minimum soak, in days")
    parser.add_argument("--yes", action="store_true", help="promote a younger beta anyway")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run("git", "fetch", "-q", "origin", "main", "--tags")
    now = datetime.now(timezone.utc)
    listing = json.loads(run("gh", "api", f"repos/{REPO}/releases?per_page=100") or "[]")
    ours = automated_betas()
    if args.beta:
        beta = args.beta
        release = next((r for r in listing if r.get("tag_name") == beta), None)
        when = availability(release) if release else None
        age = (now - when).days if when else -1
    else:
        found = pick_candidate(listing, ours, 0 if args.yes else args.min_days, now)
        if not found:
            raise SystemExit(f"no beta has been available for {args.min_days} days yet "
                             "(the Beta workflow makes them; --yes skips the soak)")
        beta, age = found
    if versioning.beta_number(beta) is None or beta not in ours:
        raise SystemExit(f"{beta} is not a beta the Beta workflow made")
    version = args.version or versioning.core_str(versioning.parse(beta)[0])
    if versioning.parse(version) is None or versioning.parse(version)[1] is not None:
        raise SystemExit(f"{version!r} is not a stable version (X.Y.Z)")
    tag = f"v{version}"
    branch = f"docs/release-notes-{version}"
    base = run("git", "rev-parse", f"{beta}^{{commit}}^")
    print(f"Promoting {beta} (main {base[:9]}, available {age} day(s)) to {tag}.")
    if age < args.min_days and not args.yes:
        raise SystemExit(f"{beta} has been available only {age} day(s) (soak is "
                         f"{args.min_days}); re-run with --yes to ship it anyway.")
    if args.dry_run:
        return 0

    # Resumable: whatever an earlier run finished stays finished.
    tag_pushed = remote_has(f"refs/tags/{tag}")
    if not tag_pushed and run("git", "tag", "--list", tag):
        run("git", "tag", "-d", tag)  # made locally by a run that stopped before pushing
    pr = run("gh", "pr", "list", "--repo", REPO, "--head", branch, "--state", "all",
             "--json", "url", "--jq", ".[0].url // empty")

    today = date.today()
    with tempfile.TemporaryDirectory(prefix="aura-promote-") as tmp:
        if not pr:
            notes_tree = Path(tmp) / "notes"
            start = f"origin/{branch}" if remote_has(f"refs/heads/{branch}") else "origin/main"
            run("git", "worktree", "add", "-q", "-B", branch, str(notes_tree), start)
            try:
                if start == "origin/main":
                    released = versioning.unreleased_body(
                        run("git", "show", f"{base}:{versioning.CHANGELOG}") + "\n")
                    stamp_release(notes_tree, version, released, today)
                    current = versioning.parse(versioning.read_version(notes_tree))[0]
                    if current < versioning.parse(version)[0]:
                        versioning.set_version(version, root=notes_tree)
                    run("git", "commit", "-qam", f"docs(release): {version} notes", cwd=notes_tree)
                run("git", "push", "-q", "-u", "origin", branch, cwd=notes_tree)
                pr = run("gh", "pr", "create", "--repo", REPO, "--base", "main", "--head", branch,
                         "--title", f"docs(release): {version} notes",
                         "--body", f"Moves the notes shipped in {tag} (from {beta}) under "
                                   f"## [{version}] and adds its AppStream release entry.",
                         cwd=notes_tree)
            finally:
                run("git", "worktree", "remove", "--force", str(notes_tree))

        if not tag_pushed:
            release_tree = Path(tmp) / "release"
            run("git", "worktree", "add", "-q", "--detach", str(release_tree), base)
            try:
                released = versioning.unreleased_body(
                    (release_tree / versioning.CHANGELOG).read_text(encoding="utf-8"))
                versioning.set_version(version, root=release_tree)
                stamp_release(release_tree, version, released, today)
                run("git", "commit", "-qam",
                    f"release: {version}\n\nStable release of {beta} (main {base}).",
                    cwd=release_tree)
                run("git", "tag", "-a", tag, "-m", f"Wayfinder Aura {version} (from {beta})",
                    cwd=release_tree)
                run("git", "push", "origin", f"refs/tags/{tag}")
            finally:
                run("git", "worktree", "remove", "--force", str(release_tree))

    print(f"{tag} is pushed: the Release workflow builds it, and the Mac release "
          "watcher attaches its DMG within the hour.")
    print(f"Notes pull request: {pr} (merge it once its checks pass).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Promote a beta that has held up to a stable release.

    GH_TOKEN=$(gh auth token --user wayfindercollective) \
        python scripts/release/promote.py            # newest beta, its version
    python scripts/release/promote.py --beta v1.2.0-beta.3 --version 1.2.0

Stable ships exactly the code the beta was built from (the main commit under
the beta's release commit), not whatever main holds today. The script:

1. checks the beta is at least --min-days old (the soak; --yes overrides);
2. commits "release: X.Y.Z" on that main commit: version stamped everywhere,
   the beta's Unreleased CHANGELOG notes moved under ## [X.Y.Z], and a stable
   <release> in the metainfo (Flathub/AppStream);
3. opens a pull request into main carrying the same CHANGELOG and metainfo
   notes (only the bullets the beta had, so newer notes stay Unreleased);
4. pushes tag vX.Y.Z, which runs the Release workflow: Linux packages, and the
   signed Mac DMG once the signing secrets are set (docs/RELEASING.md).

--dry-run prints the plan and writes nothing.
"""

from __future__ import annotations

import argparse
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--beta", help="beta tag to promote (default: the newest)")
    parser.add_argument("--version", help="stable version (default: the beta's X.Y.Z)")
    parser.add_argument("--min-days", type=int, default=5, help="minimum beta age")
    parser.add_argument("--yes", action="store_true", help="promote a younger beta anyway")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    run("git", "fetch", "-q", "origin", "main", "--tags")
    betas = beta_tags(automated_betas())
    beta = args.beta or (betas[-1] if betas else None)
    if not beta or versioning.beta_number(beta) is None:
        raise SystemExit("no beta to promote (the Beta workflow makes them)")
    version = args.version or versioning.core_str(versioning.parse(beta)[0])
    if versioning.parse(version) is None or versioning.parse(version)[1] is not None:
        raise SystemExit(f"{version!r} is not a stable version (X.Y.Z)")
    tag = f"v{version}"
    if run("git", "tag", "--list", tag):
        raise SystemExit(f"{tag} already exists")

    base = run("git", "rev-parse", f"{beta}^{{commit}}^")
    created = datetime.fromtimestamp(
        int(run("git", "log", "-1", "--format=%ct", f"{beta}^{{commit}}")), timezone.utc)
    age = (datetime.now(timezone.utc) - created).days
    print(f"Promoting {beta} (main {base[:9]}, {age} day(s) old) to {tag}.")
    if age < args.min_days and not args.yes:
        raise SystemExit(f"{beta} is only {age} day(s) old (soak is {args.min_days}); "
                         "re-run with --yes to ship it anyway.")
    if args.dry_run:
        return 0

    today = date.today()
    branch = f"docs/release-notes-{version}"
    with tempfile.TemporaryDirectory(prefix="aura-promote-") as tmp:
        release_tree, notes_tree = Path(tmp) / "release", Path(tmp) / "notes"
        run("git", "worktree", "add", "-q", "--detach", str(release_tree), base)
        run("git", "worktree", "add", "-q", "-b", branch, str(notes_tree), "origin/main")
        try:
            released = versioning.unreleased_body(
                (release_tree / versioning.CHANGELOG).read_text(encoding="utf-8"))

            versioning.set_version(version, root=release_tree)
            stamp_release(release_tree, version, released, today)
            run("git", "commit", "-qam", f"release: {version}\n\nStable release of {beta} (main {base}).",
                cwd=release_tree)
            run("git", "tag", "-a", tag, "-m", f"Wayfinder Aura {version} (from {beta})",
                cwd=release_tree)

            stamp_release(notes_tree, version, released, today)
            if versioning.parse(versioning.read_version(notes_tree))[0] < versioning.parse(version)[0]:
                versioning.set_version(version, root=notes_tree)
            run("git", "commit", "-qam", f"docs(release): {version} notes", cwd=notes_tree)
            run("git", "push", "-q", "-u", "origin", branch, cwd=notes_tree)
            pr = run("gh", "pr", "create", "--repo", REPO, "--base", "main", "--head", branch,
                     "--title", f"docs(release): {version} notes",
                     "--body", f"Moves the notes shipped in {tag} (from {beta}) under "
                               f"## [{version}] and adds its AppStream release entry.",
                     cwd=notes_tree)

            run("git", "push", "origin", f"refs/tags/{tag}")
        finally:
            run("git", "worktree", "remove", "--force", str(release_tree))
            run("git", "worktree", "remove", "--force", str(notes_tree))

    print(f"Pushed {tag}: the Release workflow is building it.")
    print(f"Notes pull request: {pr} (merge it once its checks pass).")
    print("Mac DMG: attached by the Release workflow when signing secrets are set; "
          f"otherwise run scripts/release/attach_mac_dmg.sh {tag} on the Mac.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

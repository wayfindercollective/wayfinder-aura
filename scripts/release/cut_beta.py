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
4. deletes all but the newest --keep betas it made (release and tag), so
   GitHub's release list stays short. Stable releases and hand-made tags
   (v1.1.8-beta.10 and earlier) are never touched.

Needs git, and gh authenticated (GH_TOKEN) with contents+actions write.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
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


def check_verdicts(repo: str, sha: str) -> dict[str, str]:
    raw = gh("api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
             "--jq", "[.check_runs[] | {name, status, conclusion}]")
    verdicts: dict[str, str] = {}
    for run in json.loads(raw or "[]"):
        state = run["conclusion"] if run["status"] == "completed" else run["status"]
        # A re-run leaves several runs per name; any success counts.
        if verdicts.get(run["name"]) != "success":
            verdicts[run["name"]] = state or "pending"
    return verdicts


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


def prune(repo: str, keep: int, dry_run: bool) -> None:
    raw = gh("api", f"repos/{repo}/releases?per_page=100", "--jq", "[.[].tag_name]")
    ours = automated_betas()
    released = [t for t in json.loads(raw or "[]") if t in ours]
    old = beta_tags(released)[:-keep] if keep else []
    for tag in old:
        note(f"Pruning old beta {tag}")
        if not dry_run:
            gh("release", "delete", tag, "--repo", repo, "--cleanup-tag", "--yes")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY",
                                                         "wayfindercollective/wayfinder-aura"))
    parser.add_argument("--force", action="store_true", help="cut even if main is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="decide and print only")
    parser.add_argument("--keep", type=int, default=5, help="beta releases to keep")
    args = parser.parse_args()

    head = git("rev-parse", "HEAD")
    tags = git("tag", "--list", "v*").split()
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

    prune(args.repo, args.keep, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

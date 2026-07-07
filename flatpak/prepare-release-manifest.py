#!/usr/bin/env python3
"""Generate the Flathub submission manifest from the local-build manifest.

The checked-in manifest intentionally uses `type: dir, path: ..` for local
verification. Flathub needs a remote source and a filename matching the app ID.
Run this after tagging the release to write `release/<app-id>.yml` with the app
module sourced from the tagged Git commit.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

APP_ID = "io.wayfindercollective.WayfinderAura"
REPO_URL = "https://github.com/wayfindercollective/wayfinder-aura.git"
DEV_LICENSE_API_URL = "https://valuable-stoat-578.convex.site/activate"
DEV_LICENSE_PUBLIC_KEY_HEX = "e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a"

LOCAL_SOURCE = "    sources:\n      - type: dir\n        path: ..\n"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git(args: list[str], repo: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _resolve_commit(tag: str, repo: Path) -> str:
    return _git(["rev-parse", f"{tag}^{{commit}}"], repo)


def _working_tree_dirty(repo: Path) -> bool:
    return bool(_git(["status", "--porcelain"], repo))


def _validate_commit(commit: str) -> str:
    commit = commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"commit must be a full 40-character sha1, got {commit!r}")
    return commit


def _project_version(repo: Path) -> str:
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


def _validate_tag(tag: str, repo: Path) -> str:
    expected = f"v{_project_version(repo)}"
    if tag != expected:
        raise ValueError(f"tag must match pyproject version: expected {expected}, got {tag!r}")
    return tag


def _env_get_default(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if not isinstance(node.func.value, ast.Attribute) or node.func.value.attr != "environ":
        return None
    if not isinstance(node.func.value.value, ast.Name) or node.func.value.value.id != "os":
        return None
    default = node.args[1]
    if isinstance(default, ast.Constant) and isinstance(default.value, str):
        return default.value
    return None


def _license_default_literals(repo: Path) -> dict[str, str]:
    """Return release-critical license defaults from license.py without importing it."""
    source = repo / "src" / "wayfinder" / "license.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    defaults: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in {"LICENSE_API_URL", "LICENSE_PUBLIC_KEY_HEX"}:
                default = _env_get_default(node.value)
                if default is not None:
                    defaults[target.id] = default
    return defaults


def _dev_license_defaults(repo: Path) -> list[str]:
    defaults = _license_default_literals(repo)
    offenders = []
    if defaults.get("LICENSE_API_URL") == DEV_LICENSE_API_URL:
        offenders.append("LICENSE_API_URL")
    if defaults.get("LICENSE_PUBLIC_KEY_HEX") == DEV_LICENSE_PUBLIC_KEY_HEX:
        offenders.append("LICENSE_PUBLIC_KEY_HEX")
    return offenders


def render_release_manifest(text: str, tag: str, commit: str) -> str:
    """Replace the app module's local source with a tagged git source."""
    idx = text.rfind(LOCAL_SOURCE)
    if idx == -1:
        raise ValueError("could not find local app source block in manifest")

    before = text[:idx]
    if "- name: wayfinder-aura" not in before.split("# Main application", 1)[-1]:
        raise ValueError("local source block is not under the wayfinder-aura module")

    release_source = (
        "    sources:\n"
        "      - type: git\n"
        f"        url: {REPO_URL}\n"
        f"        tag: {tag}\n"
        f"        commit: {commit}\n"
    )
    return text[:idx] + release_source + text[idx + len(LOCAL_SOURCE) :]


def main(argv: list[str] | None = None) -> int:
    repo = _repo_root()
    flatpak_dir = repo / "flatpak"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, for example v1.1.0")
    parser.add_argument(
        "--commit",
        help="full commit sha for the tag; defaults to resolving the tag in the local git repo",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=flatpak_dir / f"{APP_ID}.yml",
        help="input local-build manifest",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=flatpak_dir / "release" / f"{APP_ID}.yml",
        help="output release manifest",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow generation while the worktree has uncommitted changes",
    )
    parser.add_argument(
        "--allow-dev-license",
        action="store_true",
        help="allow the dev license defaults for local dry-runs only; do not use for submission",
    )
    args = parser.parse_args(argv)

    if args.output.resolve() == args.manifest.resolve():
        print("error: output must not overwrite the local-build manifest", file=sys.stderr)
        return 2

    if not args.allow_dirty and _working_tree_dirty(repo):
        print("error: worktree is dirty; commit release contents first or pass --allow-dirty", file=sys.stderr)
        return 2

    dev_defaults = _dev_license_defaults(repo)
    if dev_defaults and not args.allow_dev_license:
        joined = ", ".join(dev_defaults)
        print(
            "error: license defaults still point at the dev backend "
            f"({joined}); set the production activation URL and Ed25519 public key first, "
            "or pass --allow-dev-license for local dry-runs only",
            file=sys.stderr,
        )
        return 2

    try:
        tag = _validate_tag(args.tag, repo)
        commit = _validate_commit(args.commit or _resolve_commit(args.tag, repo))
        text = args.manifest.read_text(encoding="utf-8")
        rendered = render_release_manifest(text, tag, commit)
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    deps_source = flatpak_dir / "python-deps.json"
    deps_dest = args.output.parent / "python-deps.json"
    if deps_dest.resolve() != deps_source.resolve():
        shutil.copy2(deps_source, deps_dest)
    print(f"wrote {args.output}")
    print(f"source: {REPO_URL} tag={tag} commit={commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

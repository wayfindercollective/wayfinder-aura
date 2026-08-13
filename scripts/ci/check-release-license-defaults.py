#!/usr/bin/env python3
"""Fail release artifact builds while license defaults still target dev."""

from __future__ import annotations

import argparse
import ast
import sys
import posixpath
from urllib.parse import urlsplit
from pathlib import Path

# Known non-production backends (must not ship in release artifacts).
DEV_LICENSE_API_URLS = {
    "https://valuable-stoat-578.convex.site/activate",
}
# Production activate URL (same Ed25519 keypair as pilot is OK).
PROD_LICENSE_API_URL = "https://shiny-goshawk-432.convex.site/activate"
# Ed25519 pubkey owned by the production deployment above. Pinned, not merely
# required to exist: the activate URL and the embedded pubkey must belong to
# the SAME deployment, or the server accepts the key and the app then rejects
# the token it signed. Rotating the keypair must be a deliberate two-line
# change here, not something that drifts silently.
PROD_LICENSE_PUBLIC_KEY_HEX = (
    "e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a"
)
REQUIRED_DEFAULTS = {"LICENSE_API_URL", "LICENSE_PUBLIC_KEY_HEX"}


def _norm_path(path: str) -> str:
    """Collapse dot segments and the trailing slash.

    `requests` resolves `/x/../activate` and `/activate/.` to `/activate`
    before sending, so a raw string compare let those reach production while
    skipping the keypair pin.
    """
    return posixpath.normpath(path or "/").rstrip("/")


def _same_endpoint(url: str, reference: str) -> bool:
    """True when `url` addresses the same endpoint as `reference`.

    Compared on scheme, HOST, port and path, ignoring query and fragment and
    normalising the trailing slash. `.../activate?build=1`, an explicit `:443`,
    and `user:pass@host` all still reach production, so all must be held to
    production's keypair. Comparing raw netloc let the port and userinfo
    variants skip the pin, and plain string equality missed the rest.
    """
    try:
        a, b = urlsplit(url), urlsplit(reference)
    except ValueError:
        return False

    def _port(parts):
        # Treat an explicit default port as equivalent to omitting it.
        default = {"https": 443, "http": 80}.get(parts.scheme)
        try:
            return parts.port or default
        except ValueError:  # malformed port
            return None

    return (
        a.scheme == b.scheme
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and _port(a) == _port(b)
        and _norm_path(a.path) == _norm_path(b.path)
    )


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


def license_default_literals(license_file: Path) -> dict[str, str]:
    tree = ast.parse(license_file.read_text(encoding="utf-8"), filename=str(license_file))
    defaults: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id not in REQUIRED_DEFAULTS:
                continue
            default = _env_get_default(node.value)
            if default is not None:
                defaults[target.id] = default
    return defaults


def dev_license_defaults(license_file: Path) -> list[str]:
    """Return fields that still point at a non-production activation backend.

    Pubkey may match the former pilot key when production uses the same keypair;
    we only fail when the activate URL is still a known dev deployment.
    """
    defaults = license_default_literals(license_file)
    offenders = []
    api = defaults.get("LICENSE_API_URL", "")
    if api in DEV_LICENSE_API_URLS or (
        api and "valuable-stoat-578" in api
    ):
        offenders.append("LICENSE_API_URL")
    elif api and api != PROD_LICENSE_API_URL and "shiny-goshawk-432" not in api:
        # Unknown non-prod URL — still block release until explicitly production.
        if "convex.site/activate" in api and "shiny-goshawk-432" not in api:
            offenders.append("LICENSE_API_URL")
    pubkey = defaults.get("LICENSE_PUBLIC_KEY_HEX")
    if not pubkey:
        offenders.append("LICENSE_PUBLIC_KEY_HEX")
    elif _same_endpoint(api, PROD_LICENSE_API_URL) and pubkey != PROD_LICENSE_PUBLIC_KEY_HEX:
        # Only pin the pubkey when we are actually shipping against OUR
        # production deployment. A self-hosted or third-party licensing
        # backend owns its own keypair, and pinning there would block a
        # legitimate configuration.
        offenders.append("LICENSE_PUBLIC_KEY_HEX")
    return offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--license-file",
        type=Path,
        default=Path("src/wayfinder/license.py"),
        help="license.py file to inspect",
    )
    args = parser.parse_args(argv)

    try:
        defaults = license_default_literals(args.license_file)
    except OSError as exc:
        print(f"error: cannot read license defaults: {exc}", file=sys.stderr)
        return 1
    except SyntaxError as exc:
        print(f"error: cannot parse license defaults: {exc}", file=sys.stderr)
        return 1

    missing = sorted(REQUIRED_DEFAULTS - defaults.keys())
    if missing:
        print(f"error: missing license defaults: {', '.join(missing)}", file=sys.stderr)
        return 1

    offenders = dev_license_defaults(args.license_file)
    if offenders:
        joined = ", ".join(offenders)
        print(
            "error: release artifacts require production license defaults; "
            f"still using dev defaults for {joined}",
            file=sys.stderr,
        )
        return 2

    print("release license defaults are production-ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

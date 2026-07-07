#!/usr/bin/env python3
"""Fail release artifact builds while license defaults still target dev."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

DEV_LICENSE_API_URL = "https://valuable-stoat-578.convex.site/activate"
DEV_LICENSE_PUBLIC_KEY_HEX = "e45d352f85af09afd208ca55458964aae2c018f4a538e17a11fd47211190c60a"
REQUIRED_DEFAULTS = {"LICENSE_API_URL", "LICENSE_PUBLIC_KEY_HEX"}


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
    defaults = license_default_literals(license_file)
    offenders = []
    if defaults.get("LICENSE_API_URL") == DEV_LICENSE_API_URL:
        offenders.append("LICENSE_API_URL")
    if defaults.get("LICENSE_PUBLIC_KEY_HEX") == DEV_LICENSE_PUBLIC_KEY_HEX:
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

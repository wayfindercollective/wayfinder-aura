"""Version signature parity test.

`src/wayfinder/__init__.py` __version__ and pyproject.toml [project] version
are two hand-maintained mirrors of the same number. They drifted once
(__init__ said 1.0.0 while pyproject said 1.1.0) — this test would have caught
it. Source-parse pyproject with tomllib (host python is 3.11+); no Tk imports.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def test_version_matches_pyproject():
    import wayfinder

    assert wayfinder.__version__ == _pyproject_version(), (
        "version drift: wayfinder.__version__ "
        f"({wayfinder.__version__}) != pyproject [project] version "
        f"({_pyproject_version()}). Keep the two mirrors in sync."
    )

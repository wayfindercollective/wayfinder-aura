"""Version signature parity test.

`src/wayfinder/__init__.py` __version__ and pyproject.toml [project] version
are two hand-maintained mirrors of the same number. They drifted once
(__init__ said 1.0.0 while pyproject said 1.1.0) — this test would have caught
it. Source-parse pyproject with tomllib (host python is 3.11+); no Tk imports.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _metainfo_version() -> str:
    """The newest AppStream <release version=...> — what software centers display."""
    hits = list((REPO / "flatpak").glob("*.metainfo.xml"))
    assert hits, "no metainfo file under flatpak/"
    text = hits[0].read_text(encoding="utf-8")
    m = re.search(r'<release\s+version="([^"]+)"', text)
    assert m, "no <release version=...> in metainfo"
    return m.group(1)


def test_version_matches_pyproject():
    import wayfinder

    assert wayfinder.__version__ == _pyproject_version(), (
        "version drift: wayfinder.__version__ "
        f"({wayfinder.__version__}) != pyproject [project] version "
        f"({_pyproject_version()}). Keep the two mirrors in sync."
    )


def test_metainfo_release_matches_pyproject():
    """The metainfo <release> version is a THIRD hand-maintained mirror (and the
    user-facing one). It drifts silently — this guards it. Bump it at tag time."""
    assert _metainfo_version() == _pyproject_version(), (
        f"version drift: metainfo <release> ({_metainfo_version()}) != pyproject "
        f"[project] version ({_pyproject_version()}). Bump the metainfo release."
    )


def test_pyproject_urls_point_to_real_repo():
    """Package metadata should not ship with cookiecutter placeholder URLs."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    urls = data["project"]["urls"]
    expected = "https://github.com/wayfindercollective/wayfinder-aura"
    assert urls["Homepage"] == expected
    assert urls["Repository"] == expected
    assert urls["Issues"] == f"{expected}/issues"

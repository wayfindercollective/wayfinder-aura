"""
App update detection for Wayfinder Aura.

Checks the GitHub Releases API once a day for a version newer than the one
running, so users on both packages hear about releases at all: the AppImage's
embedded zsync info only helps people who run an updater tool themselves, and
the Flatpak has no update channel until it is on Flathub.

Results are cached for 24 hours. Every failure mode (no network, API change,
unparseable tag) resolves to "no update" — a wrong nag is worse than a late one.
"""

import json
import re
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ..config import CONFIG_DIR

# Cache file for update check results
APP_UPDATE_CACHE_FILE = CONFIG_DIR / "app_update_cache.json"

# Check interval: once per day (seconds)
CHECK_INTERVAL = 86400

# /releases/latest returns the newest release not marked draft or pre-release.
# Betas here are published as ordinary releases (v1.1.8-beta.9 is "latest"),
# so they flow through this endpoint too.
RELEASES_API = "https://api.github.com/repos/wayfindercollective/wayfinder-aura/releases/latest"
RELEASES_PAGE = "https://github.com/wayfindercollective/wayfinder-aura/releases/latest"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?")


def parse_version(text: str) -> Optional[Tuple[Tuple[int, int, int], Optional[Tuple[str, ...]]]]:
    """Parse "v1.1.8-beta.10" into ((1, 1, 8), ("beta", "10")).

    Returns None when the text does not start with a semver-shaped version —
    callers treat that as "cannot compare", never as "newer".
    """
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text.strip().lstrip("vV"))
    if not m:
        return None
    nums = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = m.group(4)
    return nums, (tuple(pre.split(".")) if pre else None)


def is_newer(candidate: str, current: str) -> bool:
    """True only when candidate is strictly newer than current (semver rules).

    Unparseable input on either side is False: the banner must never appear on
    the strength of a tag we could not read.
    """
    a = parse_version(candidate)
    b = parse_version(current)
    if a is None or b is None:
        return False
    if a[0] != b[0]:
        return a[0] > b[0]
    apre, bpre = a[1], b[1]
    if apre is None and bpre is None:
        return False
    if apre is None:
        return True   # a release outranks any prerelease of the same triple
    if bpre is None:
        return False
    # Semver prerelease precedence: identifier by identifier; numeric compare
    # when both are numeric, numeric < alphanumeric otherwise, else lexical.
    for x, y in zip(apre, bpre):
        if x == y:
            continue
        xd, yd = x.isdigit(), y.isdigit()
        if xd and yd:
            return int(x) > int(y)
        if xd != yd:
            return yd  # the numeric side is lower; candidate wins iff current's is numeric
        return x > y
    return len(apre) > len(bpre)


def check_for_app_update(current_version: str, force: bool = False) -> Dict[str, Any]:
    """
    Check GitHub for a release newer than current_version.

    Uses cached results unless force=True or the cache is older than
    CHECK_INTERVAL. The comparison against current_version is always
    recomputed, so a cached check stays correct across an app upgrade.

    Returns dict with:
        - update_available: bool
        - latest_version: str (tag as published, e.g. "v1.1.9")
        - release_url: str (page to send the user to)
        - last_checked: ISO timestamp
        - error: optional error message
    """
    if not force:
        cached = _load_cache()
        if cached and _is_cache_fresh(cached):
            return _with_comparison(cached, current_version)

    results: Dict[str, Any] = {
        "update_available": False,
        "latest_version": "",
        "release_url": RELEASES_PAGE,
        "last_checked": datetime.now().isoformat(),
        "error": None,
    }

    try:
        import requests
        response = requests.get(
            RELEASES_API, timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        data = response.json()
        results["latest_version"] = str(data.get("tag_name", "") or "")
        results["release_url"] = str(data.get("html_url", "") or RELEASES_PAGE)
    except Exception as e:
        results["error"] = str(e)

    results = _with_comparison(results, current_version)
    _save_cache(results)
    return results


def _with_comparison(results: Dict[str, Any], current_version: str) -> Dict[str, Any]:
    """Recompute update_available for the running version."""
    results = dict(results)
    results["update_available"] = bool(
        results.get("latest_version")
        and is_newer(results["latest_version"], current_version)
    )
    return results


def _load_cache() -> Optional[Dict[str, Any]]:
    try:
        if APP_UPDATE_CACHE_FILE.exists():
            with open(APP_UPDATE_CACHE_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, IOError):
        pass
    return None


def _save_cache(data: Dict[str, Any]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(APP_UPDATE_CACHE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except IOError:
        pass


def _is_cache_fresh(cache: Dict[str, Any]) -> bool:
    last_checked = cache.get("last_checked", "")
    if not last_checked:
        return False
    try:
        checked_time = datetime.fromisoformat(last_checked)
        age = (datetime.now() - checked_time).total_seconds()
        return 0 <= age < CHECK_INTERVAL
    except (ValueError, TypeError):
        return False

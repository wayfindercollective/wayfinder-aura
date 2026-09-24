"""
App update detection for Wayfinder Aura.

Checks the GitHub Releases API once a day for a version newer than the one
running, so users on both packages hear about releases at all: the AppImage's
embedded zsync info only helps people who run an updater tool themselves, and
the Flatpak has no automatic update channel until it is on Flathub.

Stable installs follow stable releases. Prerelease installs follow both newer
prereleases and stable releases, so beta testers are not stranded by GitHub's
``/releases/latest`` endpoint (which deliberately excludes prereleases).

Results are cached for 24 hours. Every failure mode (no network, API change,
unparseable tag) resolves to "no update" — a wrong nag is worse than a late one.

macOS only: a release counts as an update only when it carries a Mac DMG
(named as packaging/macos/build.py names it) for this machine, so a Linux-only
release never nags a Mac user towards a page with nothing to install. The
DMG's direct URL is returned as ``download_url`` for a one-click download.

Windows likewise: only a release carrying ``WayfinderAura-Setup-<version>.exe``
(packaging/windows/installer.iss) is an update, and that installer is the
``download_url``. Releases without one (Windows is internal until sign-off, so
tags currently carry no Setup exe) never nag a Windows user.
Linux results are unchanged.
"""

import json
import platform
import re
import sys
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from ..config import CONFIG_DIR as _CONFIG_DIR
from ..utils.platform import get_cache_dir

# Cache file for update check results
# macOS keeps update caches in ~/Library/Caches; Linux/Windows keep them in the
# config dir, as on main.
CONFIG_DIR = get_cache_dir() if sys.platform == "darwin" else _CONFIG_DIR
APP_UPDATE_CACHE_FILE = CONFIG_DIR / "app_update_cache.json"

# Check interval: once per day (seconds)
CHECK_INTERVAL = 86400

# Query the release list rather than /releases/latest. GitHub excludes releases
# marked as prereleases from /latest, which made beta.10 invisible to beta.9.
RELEASES_API = "https://api.github.com/repos/wayfindercollective/wayfinder-aura/releases?per_page=30"
RELEASES_PAGE = "https://github.com/wayfindercollective/wayfinder-aura/releases/latest"

# End-anchored, and the prerelease is a dot-list of NON-EMPTY identifiers:
# "v9.9.9 trailing text", "1.2.3-", and "1.2.3-beta..1" must all fail to
# parse, because a tag we cannot read in full must never produce a banner.
# Build metadata (+...) is accepted and ignored, per semver precedence rules.
# The numeric class is [0-9], NOT \d — \d matches every Unicode digit, so
# "\u0669.\u0669.\u0669" (Arabic-Indic nines) parsed as an update. Leading
# zeros are invalid semver and are rejected, and each number is capped at 9
# digits so int() can never blow up on an adversarial tag.
_NUM = r"(?:0|[1-9][0-9]{0,8})"
_VERSION_RE = re.compile(
    r"^(" + _NUM + r")\.(" + _NUM + r")\.(" + _NUM + r")"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


# Mac DMG naming — must match create_dmg() in packaging/macos/build.py:
#   Wayfinder_Aura-<CFBundleShortVersionString>-macOS-<platform.machine()>.dmg
# (tests/test_app_updates.py pins the two together).
_MAC_DMG_RE = re.compile(r"^Wayfinder_Aura-(.+)-macOS-(arm64|x86_64)\.dmg$")
# Windows installer naming — must match OutputBaseFilename in
# packaging/windows/installer.iss: WayfinderAura-Setup-<version>.exe (x64 only).
_WIN_SETUP_RE = re.compile(r"^WayfinderAura-Setup-(.+)\.exe$")
# Release assets are only ever served from GitHub; anything else is not a
# download this module will hand to the browser.
_DOWNLOAD_URL_PREFIX = "https://github.com/"


def parse_version(text: str) -> Optional[Tuple[Tuple[int, int, int], Optional[Tuple[str, ...]]]]:
    """Parse "v1.1.8-beta.10" into ((1, 1, 8), ("beta", "10")).

    Returns None unless the WHOLE text is a semver-shaped version (one
    optional leading v) — callers treat that as "cannot compare", never as
    "newer".
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    if t[:1] in ("v", "V"):
        t = t[1:]   # exactly one prefix: "vv1.2.3" is not a version
    m = _VERSION_RE.match(t)
    if not m:
        return None
    nums = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = m.group(4)
    if pre is None:
        return nums, None
    ids = tuple(pre.split("."))
    for ident in ids:
        if ident.isdigit():
            # Numeric identifiers: no leading zeros (invalid semver), and
            # bounded so the int() in is_newer can never blow up.
            if (len(ident) > 1 and ident[0] == "0") or len(ident) > 9:
                return None
    return nums, ids


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


def _release_channel(version: str) -> Optional[str]:
    parsed = parse_version(version)
    if parsed is None:
        return None
    return "stable" if parsed[1] is None else "prerelease"


def _is_macos() -> bool:
    # Read at call time, not import time, so tests can simulate a platform.
    return sys.platform == "darwin"


def _is_windows() -> bool:
    return sys.platform == "win32"


def _mac_architectures() -> Tuple[str, ...]:
    """DMG architectures this Mac accepts, most preferred first.

    The Mac release is Apple Silicon only, so arm64 is always accepted; the
    running machine's own architecture (e.g. x86_64 under Rosetta) is
    preferred when a release carries more than one DMG.
    """
    machine = (platform.machine() or "").lower()
    return tuple(dict.fromkeys(
        arch for arch in (machine, "arm64") if arch in ("arm64", "x86_64")
    ))


def _platform_cache_key() -> Optional[str]:
    """Which download filter produced a cached verdict (macOS/Windows only).

    None on Linux, whose cache format is unchanged. On macOS a cache written
    without the DMG filter (an older build) or for a different architecture
    set is refetched rather than trusted; Windows the same for its installer
    filter.
    """
    if _is_windows():
        return "win32-x64"
    if not _is_macos():
        return None
    return "darwin-" + "+".join(_mac_architectures())


def _mac_dmg_url(release: Dict[str, Any], tag: str) -> Optional[str]:
    """browser_download_url of the release's DMG for this Mac, or None.

    The version embedded in the DMG name must equal the tag: a stale DMG
    attached to a newer release would otherwise nag forever (the "update"
    reinstalls the old version). Incomplete uploads and non-GitHub URLs never
    count.
    """
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    tag_version = parse_version(tag)
    if tag_version is None:
        return None
    by_arch: Dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("state") != "uploaded":
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if not url.startswith(_DOWNLOAD_URL_PREFIX):
            continue
        match = _MAC_DMG_RE.match(name)
        if match is None or parse_version(match.group(1)) != tag_version:
            continue
        by_arch.setdefault(match.group(2), url)
    for arch in _mac_architectures():
        if arch in by_arch:
            return by_arch[arch]
    return None


def _windows_setup_url(release: Dict[str, Any], tag: str) -> Optional[str]:
    """browser_download_url of the release's Windows installer, or None.

    Same rules as the DMG: the installer's version must equal the tag, the
    upload must be complete, and the URL must be a GitHub download.
    """
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    tag_version = parse_version(tag)
    if tag_version is None:
        return None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("state") != "uploaded":
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if not url.startswith(_DOWNLOAD_URL_PREFIX):
            continue
        match = _WIN_SETUP_RE.match(name)
        if match is not None and parse_version(match.group(1)) == tag_version:
            return url
    return None


def _select_release(payload: Any, current_version: str) -> Dict[str, str]:
    """Select the newest release allowed by the running version's channel.

    On macOS, releases without a DMG for this Mac are skipped (the newest
    release that HAS one wins) and the DMG's URL is returned as download_url.
    Windows does the same with the Setup exe.
    """
    channel = _release_channel(current_version)
    if channel is None:
        return {}
    if not isinstance(payload, list):
        raise ValueError("GitHub releases response is not a list")

    mac = _is_macos()
    win = _is_windows()
    selected: Dict[str, str] = {}
    for release in payload:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        tag = release.get("tag_name")
        parsed = parse_version(tag)
        if parsed is None:
            continue

        # Stable users never receive beta/rc notices, even if a prerelease was
        # accidentally published with GitHub's prerelease flag turned off.
        tag_is_prerelease = parsed[1] is not None
        if channel == "stable" and (release.get("prerelease") is True or tag_is_prerelease):
            continue

        download_url = None
        if mac:
            download_url = _mac_dmg_url(release, tag)
            if download_url is None:
                continue  # nothing this Mac can install: not an update here
        elif win:
            download_url = _windows_setup_url(release, tag)
            if download_url is None:
                continue  # no Windows installer on this release

        if not selected or is_newer(tag, selected["tag_name"]):
            selected = {
                "tag_name": tag,
                "html_url": str(release.get("html_url", "") or RELEASES_PAGE),
            }
            if mac or win:
                selected["download_url"] = download_url
    return selected


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
        - download_url: str (macOS/Windows only: the DMG / Setup exe, "" if none)
        - last_checked: ISO timestamp
        - error: optional error message
    """
    channel = _release_channel(current_version)
    platform_key = _platform_cache_key()  # None on Linux
    if not force:
        cached = _load_cache()
        if (
            cached
            and cached.get("channel") == channel
            and (platform_key is None or cached.get("platform") == platform_key)
            and _is_cache_fresh(cached)
        ):
            return _with_comparison(cached, current_version)

    results: Dict[str, Any] = {
        "update_available": False,
        "latest_version": "",
        "release_url": RELEASES_PAGE,
        "last_checked": datetime.now().isoformat(),
        "channel": channel,
        "error": None,
    }
    if platform_key is not None:
        results["platform"] = platform_key
        results["download_url"] = ""

    try:
        import requests
        response = requests.get(
            RELEASES_API, timeout=10,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        release = _select_release(response.json(), current_version)
        results["latest_version"] = release.get("tag_name", "")
        results["release_url"] = release.get("html_url", RELEASES_PAGE)
        if platform_key is not None:
            results["download_url"] = release.get("download_url") or ""
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
        APP_UPDATE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
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

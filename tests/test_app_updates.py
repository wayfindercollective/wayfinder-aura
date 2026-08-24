"""App-update check: version comparison, caching, and the banner wiring.

The banner exists because neither package notifies users today: the AppImage's
zsync metadata only helps people who run an updater themselves, and the Flatpak
has no channel until Flathub. The one behaviour that must never regress is
"no false banner": every unparseable or failed path resolves to no update.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from wayfinder.core import app_updates
from wayfinder.core.app_updates import (
    check_for_app_update,
    is_newer,
    parse_version,
)

MAIN_SRC = Path(__file__).resolve().parent.parent / "wayfinder_main.py"


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a temp dir so tests never touch the real config."""
    monkeypatch.setattr(app_updates, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        app_updates, "APP_UPDATE_CACHE_FILE", tmp_path / "app_update_cache.json")


def _github_response(tag, url="https://github.com/wayfindercollective/wayfinder-aura/releases/tag/x"):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"tag_name": tag, "html_url": url}
    return resp


class TestVersionComparison:
    @pytest.mark.parametrize("candidate,current,expected", [
        # Plain upgrades
        ("v1.1.9", "1.1.8", True),
        ("v1.2.0", "1.1.9", True),
        ("v2.0.0", "1.9.9", True),
        # Release outranks its own prereleases; prereleases never outrank it
        ("v1.1.8", "1.1.8-beta.10", True),
        ("v1.1.8-beta.10", "1.1.8", False),
        # Numeric prerelease ordering (the real tags in this repo)
        ("v1.1.8-beta.10", "1.1.8-beta.9", True),
        ("v1.1.8-beta.9", "1.1.8-beta.10", False),
        # rc > beta (alphanumeric identifiers compare lexically)
        ("v1.1.8-rc.1", "1.1.8-beta.10", True),
        # Longer prerelease outranks its prefix
        ("v1.1.8-beta.10.1", "1.1.8-beta.10", True),
        # A newer triple wins even as a prerelease
        ("v1.2.0-beta.1", "1.1.8", True),
        # Equality is never an update
        ("v1.1.8", "1.1.8", False),
        ("v1.1.8-beta.9", "1.1.8-beta.9", False),
        # Downgrades are never an update
        ("v1.1.7", "1.1.8", False),
    ])
    def test_ordering(self, candidate, current, expected):
        assert is_newer(candidate, current) is expected

    @pytest.mark.parametrize("candidate,current", [
        ("garbage", "1.1.8"),
        ("v1.1.9", "garbage"),
        ("", "1.1.8"),
        ("v1.1.9", ""),
        (None, "1.1.8"),
        ("v1.1.9", None),
        # The parser is end-anchored and structural: a tag we can only read
        # PARTIALLY is a tag we cannot read. (The review caught the original
        # prefix-match accepting "v9.9.9 trailing text" as newer.)
        ("v9.9.9 trailing text", "1.1.8"),
        ("vv9.9.9", "1.1.8"),
        ("v9.9.9-", "1.1.8"),
        ("v9.9.9-beta..1", "1.1.8"),
        ("v9.9.9-beta.1!", "1.1.8"),
        ("9.9.9.9", "1.1.8"),
    ])
    def test_unparseable_never_claims_newer(self, candidate, current):
        # A tag we cannot read must never produce a banner.
        assert is_newer(candidate, current) is False

    def test_build_metadata_is_accepted_and_ignored(self):
        # Semver: +build carries no precedence. It must parse (GitHub tags may
        # carry it) and compare as if absent.
        assert parse_version("1.2.3+build.7") == ((1, 2, 3), None)
        assert is_newer("v1.2.3+build.7", "1.2.2") is True
        assert is_newer("v1.2.3+build.7", "1.2.3") is False

    def test_parse_strips_v_and_splits_prerelease(self):
        assert parse_version("v1.1.8-beta.10") == ((1, 1, 8), ("beta", "10"))
        assert parse_version("1.2.3") == ((1, 2, 3), None)
        assert parse_version("nonsense") is None


class TestCheckForAppUpdate:
    def test_newer_release_reports_update(self):
        with patch("requests.get", return_value=_github_response("v9.9.9")):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is True
        assert info["latest_version"] == "v9.9.9"
        assert info["error"] is None

    def test_current_or_older_release_is_quiet(self):
        # Today's real state: dev build ahead of the newest published release.
        with patch("requests.get", return_value=_github_response("v1.1.8-beta.9")):
            info = check_for_app_update("1.1.8-beta.10")
        assert info["update_available"] is False

    def test_network_failure_is_quiet_and_recorded(self):
        with patch("requests.get", side_effect=OSError("no network")):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False
        assert "no network" in info["error"]

    def test_fresh_cache_skips_the_network(self):
        with patch("requests.get", return_value=_github_response("v9.9.9")) as get:
            check_for_app_update("1.1.8")
            check_for_app_update("1.1.8")
        assert get.call_count == 1

    def test_force_bypasses_cache(self):
        with patch("requests.get", return_value=_github_response("v9.9.9")) as get:
            check_for_app_update("1.1.8")
            check_for_app_update("1.1.8", force=True)
        assert get.call_count == 2

    def test_stale_cache_rechecks(self):
        with patch("requests.get", return_value=_github_response("v9.9.9")) as get:
            check_for_app_update("1.1.8")
        cache = json.loads(app_updates.APP_UPDATE_CACHE_FILE.read_text())
        cache["last_checked"] = (
            datetime.now() - timedelta(seconds=app_updates.CHECK_INTERVAL + 1)
        ).isoformat()
        app_updates.APP_UPDATE_CACHE_FILE.write_text(json.dumps(cache))
        with patch("requests.get", return_value=_github_response("v9.9.9")) as get:
            check_for_app_update("1.1.8")
        assert get.call_count == 1

    def test_cached_result_recompares_after_app_upgrade(self):
        # The cache stores the tag, not the verdict for one particular version:
        # upgrading the app must silence a cached "update available" without
        # waiting out the interval.
        with patch("requests.get", return_value=_github_response("v1.1.9")):
            before = check_for_app_update("1.1.8")
        assert before["update_available"] is True
        with patch("requests.get", side_effect=AssertionError("should use cache")):
            after = check_for_app_update("1.1.9")
        assert after["update_available"] is False

    def test_missing_tag_in_response_is_quiet(self):
        with patch("requests.get", return_value=_github_response(None)):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False

    def test_corrupt_cache_is_ignored(self):
        app_updates.APP_UPDATE_CACHE_FILE.write_text("{not json")
        with patch("requests.get", return_value=_github_response("v9.9.9")):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is True


@pytest.fixture(scope="module")
def src():
    return MAIN_SRC.read_text()


class TestBannerWiring:
    """Structural checks against wayfinder_main.py — the UI can't be
    instantiated headless, but the wiring that makes the feature exist at all
    can be pinned down in source."""

    def test_banner_is_built_in_the_dictate_tab(self, src):
        assert "self.app_update_banner = ctk.CTkFrame(" in src
        # Built alongside the other on-demand banners, before first use.
        assert src.index("self.app_update_banner = ctk.CTkFrame(") \
            < src.index('banner = getattr(self, "app_update_banner", None)')

    def test_startup_schedules_the_check(self, src):
        assert "self.after(2600, self._check_app_update_background)" in src

    def test_check_respects_the_config_toggle(self, src):
        body = src.split("def _check_app_update_background", 1)[1]
        body = body.split("def _show_app_update_banner", 1)[0]
        assert 'self.config.get("check_for_app_updates", True)' in body

    def test_dismissal_is_per_version(self, src):
        body = src.split("def _check_app_update_background", 1)[1]
        assert '"app_update_dismissed_version"' in body

    def test_banner_show_is_marshalled_to_the_tk_thread(self, src):
        body = src.split("def _check_app_update_background", 1)[1]
        body = body.split("def _show_app_update_banner", 1)[0]
        assert "self.after(0, lambda: self._show_app_update_banner(info))" in body

    def test_config_defaults_exist(self):
        from wayfinder.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["check_for_app_updates"] is True
        assert DEFAULT_CONFIG["app_update_dismissed_version"] == ""

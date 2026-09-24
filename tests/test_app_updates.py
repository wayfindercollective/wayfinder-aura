"""App-update check: version comparison, caching, and the banner wiring.

The banner exists because neither package notifies users today: the AppImage's
zsync metadata only helps people who run an updater themselves, and the Flatpak
has no channel until Flathub. The one behaviour that must never regress is
"no false banner": every unparseable or failed path resolves to no update.
"""

import json
import queue
import threading
import types
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

REPO = Path(__file__).resolve().parent.parent
MAIN_SRC = REPO / "wayfinder_main.py"


def _simulate_platform(monkeypatch, name, machine="arm64"):
    """Fake sys.platform / platform.machine() inside app_updates only."""
    monkeypatch.setattr(app_updates, "sys", types.SimpleNamespace(platform=name))
    monkeypatch.setattr(
        app_updates, "platform", types.SimpleNamespace(machine=lambda: machine))


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the cache at a temp dir so tests never touch the real config.

    Also pin the platform to Linux: the Linux verdicts below must hold on
    every host, including the macOS CI runner. Mac tests opt in explicitly.
    """
    monkeypatch.setattr(app_updates, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(
        app_updates, "APP_UPDATE_CACHE_FILE", tmp_path / "app_update_cache.json")
    _simulate_platform(monkeypatch, "linux", machine="x86_64")


def _release(tag, url="https://github.com/wayfindercollective/wayfinder-aura/releases/tag/x", **flags):
    return {
        "tag_name": tag,
        "html_url": url,
        "draft": False,
        "prerelease": False,
        **flags,
    }


def _github_response(*releases):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = list(releases)
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
        # Semver structural rules the second review round caught: [0-9] not
        # \d (Unicode digits!), no leading zeros, bounded numerics.
        ("٩.٩.٩", "1.1.8"),
        ("09.9.9", "1.1.8"),
        ("9.09.9", "1.1.8"),
        ("9.9.09", "1.1.8"),
        ("9.9.9-01", "1.1.8"),
        ("9.9.9-" + "9" * 5000, "1.1.8"),
        ("9" * 5000 + ".0.0", "1.1.8"),
    ])
    def test_unparseable_never_claims_newer(self, candidate, current):
        # A tag we cannot read must never produce a banner.
        assert is_newer(candidate, current) is False

    def test_a_bare_zero_identifier_is_still_valid(self):
        # "1.2.3-0" is legal semver; only MULTI-digit leading zeros are not.
        assert parse_version("1.2.3-0") == ((1, 2, 3), ("0",))

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
        with patch("requests.get", return_value=_github_response(_release("v9.9.9"))):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is True
        assert info["latest_version"] == "v9.9.9"
        assert info["error"] is None

    def test_current_or_older_release_is_quiet(self):
        # Today's real state: dev build ahead of the newest published release.
        with patch(
            "requests.get",
            return_value=_github_response(_release("v1.1.8-beta.9")),
        ):
            info = check_for_app_update("1.1.8-beta.10")
        assert info["update_available"] is False

    def test_prerelease_install_sees_newer_github_prerelease(self):
        with patch(
            "requests.get",
            return_value=_github_response(
                _release("v1.1.8-beta.9"),
                _release("v1.1.8-beta.10", prerelease=True),
            ),
        ):
            info = check_for_app_update("1.1.8-beta.9")
        assert info["update_available"] is True
        assert info["latest_version"] == "v1.1.8-beta.10"

    def test_stable_install_ignores_prerelease_tags_regardless_of_github_flag(self):
        with patch(
            "requests.get",
            return_value=_github_response(
                _release("v1.2.0-beta.1"),  # beta.9 was published this way
                _release("v1.1.9", prerelease=False),
            ),
        ):
            info = check_for_app_update("1.1.8")
        assert info["latest_version"] == "v1.1.9"

    def test_drafts_and_malformed_releases_are_ignored(self):
        with patch(
            "requests.get",
            return_value=_github_response(
                _release("v9.9.9", draft=True),
                _release("not-a-version"),
                _release("v1.1.9"),
            ),
        ):
            info = check_for_app_update("1.1.8")
        assert info["latest_version"] == "v1.1.9"

    def test_network_failure_is_quiet_and_recorded(self):
        with patch("requests.get", side_effect=OSError("no network")):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False
        assert "no network" in info["error"]

    def test_fresh_cache_skips_the_network(self):
        with patch("requests.get", return_value=_github_response(_release("v9.9.9"))) as get:
            check_for_app_update("1.1.8")
            check_for_app_update("1.1.8")
        assert get.call_count == 1

    def test_force_bypasses_cache(self):
        with patch("requests.get", return_value=_github_response(_release("v9.9.9"))) as get:
            check_for_app_update("1.1.8")
            check_for_app_update("1.1.8", force=True)
        assert get.call_count == 2

    def test_stale_cache_rechecks(self):
        with patch("requests.get", return_value=_github_response(_release("v9.9.9"))) as get:
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
        with patch("requests.get", return_value=_github_response(_release("v1.1.9"))):
            before = check_for_app_update("1.1.8")
        assert before["update_available"] is True
        with patch("requests.get", side_effect=AssertionError("should use cache")):
            after = check_for_app_update("1.1.9")
        assert after["update_available"] is False

    def test_switching_from_stable_to_prerelease_channel_refetches(self):
        with patch("requests.get", return_value=_github_response(_release("v1.1.9"))):
            check_for_app_update("1.1.8")
        with patch(
            "requests.get",
            return_value=_github_response(_release("v1.2.0-beta.2", prerelease=True)),
        ) as get:
            info = check_for_app_update("1.2.0-beta.1")
        assert get.call_count == 1
        assert info["latest_version"] == "v1.2.0-beta.2"

    def test_missing_tag_in_response_is_quiet(self):
        with patch("requests.get", return_value=_github_response(_release(None))):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False

    def test_corrupt_cache_is_ignored(self):
        app_updates.APP_UPDATE_CACHE_FILE.write_text("{not json")
        with patch("requests.get", return_value=_github_response(_release("v9.9.9"))):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is True


def _asset(name, state="uploaded", url=None):
    return {
        "name": name,
        "state": state,
        "browser_download_url": url or (
            "https://github.com/wayfindercollective/wayfinder-aura/releases/download/x/" + name
        ),
    }


def _linux_assets(version):
    return [
        _asset(f"Wayfinder_Aura-{version}-x86_64.AppImage"),
        _asset(f"Wayfinder_Aura-{version}-x86_64.AppImage.zsync"),
        _asset("io.wayfindercollective.WayfinderAura.flatpak"),
    ]


def _dmg(version, arch="arm64", **kw):
    return _asset(f"Wayfinder_Aura-{version}-macOS-{arch}.dmg", **kw)


# v1.1.9 shipped Linux-only; v1.1.8 carries a Mac DMG.
MIXED_PAYLOAD = (
    _release("v1.1.9", url="https://example.invalid/v1.1.9",
             assets=_linux_assets("1.1.9")),
    _release("v1.1.8", url="https://example.invalid/v1.1.8",
             assets=_linux_assets("1.1.8") + [_dmg("1.1.8")]),
)

# The exact result shape Linux/Windows have always returned.
LINUX_RESULT_KEYS = {
    "update_available", "latest_version", "release_url",
    "last_checked", "channel", "error",
}


class TestMacDownloads:
    """macOS only counts releases that carry a DMG for this Mac."""

    @pytest.fixture(autouse=True)
    def _mac(self, monkeypatch):
        _simulate_platform(monkeypatch, "darwin", machine="arm64")

    def test_skips_linux_only_release_and_picks_older_release_with_a_dmg(self):
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            info = check_for_app_update("1.1.7")
        assert info["update_available"] is True
        assert info["latest_version"] == "v1.1.8"
        assert info["release_url"] == "https://example.invalid/v1.1.8"

    def test_returns_the_direct_dmg_download_url(self):
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            info = check_for_app_update("1.1.7")
        assert info["download_url"] == (
            "https://github.com/wayfindercollective/wayfinder-aura/releases/download/x/"
            "Wayfinder_Aura-1.1.8-macOS-arm64.dmg"
        )

    def test_linux_only_newer_release_is_not_an_update_on_mac(self):
        # The v1.1.8 situation: a newer tag exists, but has nothing for a Mac.
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False
        assert info["latest_version"] == "v1.1.8"

    def test_no_release_with_a_dmg_leaves_download_url_empty(self):
        with patch("requests.get", return_value=_github_response(MIXED_PAYLOAD[0])):
            info = check_for_app_update("1.1.7")
        assert info["update_available"] is False
        assert info["download_url"] == ""

    def test_prerelease_dmg_matches_its_prerelease_tag(self):
        with patch("requests.get", return_value=_github_response(
            _release("v1.2.0-beta.2", prerelease=True, assets=[_dmg("1.2.0-beta.2")]),
        )):
            info = check_for_app_update("1.2.0-beta.1")
        assert info["latest_version"] == "v1.2.0-beta.2"
        assert info["download_url"].endswith("Wayfinder_Aura-1.2.0-beta.2-macOS-arm64.dmg")

    @pytest.mark.parametrize("bad_asset", [
        # A stale DMG attached to a newer tag would "update" to the old app forever.
        _dmg("1.1.8"),
        # An upload still in progress is not a download.
        _dmg("1.1.9", state="open"),
        # Only GitHub-served assets are handed to the browser.
        _dmg("1.1.9", url="http://evil.invalid/Wayfinder_Aura-1.1.9-macOS-arm64.dmg"),
        # Not the build.py naming.
        _asset("Wayfinder Aura 1.1.9.dmg"),
        _asset("Wayfinder_Aura-1.1.9-macOS-arm64.dmg.sha256"),
        # Intel-only DMG on an Apple Silicon Mac.
        _dmg("1.1.9", arch="x86_64"),
    ])
    def test_unusable_dmg_assets_do_not_count(self, bad_asset):
        with patch("requests.get", return_value=_github_response(
            _release("v1.1.9", assets=[bad_asset]),
        )):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False

    def test_prefers_the_machines_own_architecture(self, monkeypatch):
        _simulate_platform(monkeypatch, "darwin", machine="x86_64")
        with patch("requests.get", return_value=_github_response(
            _release("v1.1.9", assets=[_dmg("1.1.9", "arm64"), _dmg("1.1.9", "x86_64")]),
        )):
            info = check_for_app_update("1.1.8")
        assert info["download_url"].endswith("-macOS-x86_64.dmg")

    def test_cache_without_the_mac_filter_is_refetched(self):
        # A verdict cached by a build that did not filter for DMGs (i.e. a
        # Linux-style verdict) must not be served to the Mac.
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            check_for_app_update("1.1.7")
        cache = json.loads(app_updates.APP_UPDATE_CACHE_FILE.read_text())
        assert cache["platform"] == "darwin-arm64"
        del cache["platform"]
        cache["latest_version"] = "v1.1.9"
        app_updates.APP_UPDATE_CACHE_FILE.write_text(json.dumps(cache))
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)) as get:
            info = check_for_app_update("1.1.7")
        assert get.call_count == 1
        assert info["latest_version"] == "v1.1.8"

    def test_fresh_mac_cache_is_reused_with_its_download_url(self):
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            check_for_app_update("1.1.7")
        with patch("requests.get", side_effect=AssertionError("should use cache")):
            info = check_for_app_update("1.1.7")
        assert info["download_url"].endswith("Wayfinder_Aura-1.1.8-macOS-arm64.dmg")

    def test_dmg_pattern_matches_the_name_build_py_produces(self):
        builder = (REPO / "packaging" / "macos" / "build.py").read_text(encoding="utf-8")
        assert 'f"Wayfinder_Aura-{version}-macOS-{architecture}.dmg"' in builder
        assert app_updates._MAC_DMG_RE.match("Wayfinder_Aura-1.1.9-macOS-arm64.dmg")


def _setup_exe(version, **kw):
    return _asset(f"WayfinderAura-Setup-{version}.exe", **kw)


class TestWindowsDownloads:
    """Windows only counts releases that carry its Setup exe."""

    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch):
        _simulate_platform(monkeypatch, "win32", machine="AMD64")

    def test_release_without_an_installer_is_not_an_update(self):
        # Today's situation: Windows is internal, tags carry no Setup exe.
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            info = check_for_app_update("1.1.7")
        assert info["update_available"] is False
        assert info["download_url"] == ""

    def test_skips_newer_release_without_installer(self):
        payload = (
            MIXED_PAYLOAD[0],
            _release("v1.1.8", url="https://example.invalid/v1.1.8",
                     assets=_linux_assets("1.1.8") + [_setup_exe("1.1.8")]),
        )
        with patch("requests.get", return_value=_github_response(*payload)):
            info = check_for_app_update("1.1.7")
        assert info["update_available"] is True
        assert info["latest_version"] == "v1.1.8"
        assert info["download_url"].endswith("WayfinderAura-Setup-1.1.8.exe")

    @pytest.mark.parametrize("bad_asset", [
        _setup_exe("1.1.8"),  # stale installer on a newer tag
        _setup_exe("1.1.9", state="open"),
        _setup_exe("1.1.9", url="http://evil.invalid/WayfinderAura-Setup-1.1.9.exe"),
        _asset("WayfinderAura-Setup-1.1.9.exe.sha256"),
    ])
    def test_unusable_installer_assets_do_not_count(self, bad_asset):
        with patch("requests.get", return_value=_github_response(
            _release("v1.1.9", assets=[bad_asset]),
        )):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is False

    def test_cache_is_tagged_with_the_windows_filter(self):
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            check_for_app_update("1.1.7")
        cache = json.loads(app_updates.APP_UPDATE_CACHE_FILE.read_text())
        assert cache["platform"] == "win32-x64"
        # A Linux-style verdict cached by an older Windows build is refetched.
        del cache["platform"]
        cache["latest_version"] = "v1.1.9"
        cache["update_available"] = True
        app_updates.APP_UPDATE_CACHE_FILE.write_text(json.dumps(cache))
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)) as get:
            info = check_for_app_update("1.1.7")
        assert get.call_count == 1
        assert info["update_available"] is False

    def test_pattern_matches_the_installer_iss_name(self):
        iss = (REPO / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")
        assert "OutputBaseFilename=WayfinderAura-Setup-{#MyAppVersion}" in iss
        assert app_updates._WIN_SETUP_RE.match("WayfinderAura-Setup-1.1.9.exe")


class TestLinuxUnchanged:
    """The same payload on Linux behaves exactly as before the Mac filter."""

    def test_linux_takes_the_newest_release_regardless_of_assets(self):
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            info = check_for_app_update("1.1.8")
        assert info["update_available"] is True
        assert info["latest_version"] == "v1.1.9"
        assert info["release_url"] == "https://example.invalid/v1.1.9"
        assert set(info) == LINUX_RESULT_KEYS

    def test_linux_cache_format_is_unchanged(self):
        with patch("requests.get", return_value=_github_response(*MIXED_PAYLOAD)):
            check_for_app_update("1.1.8")
        cache = json.loads(app_updates.APP_UPDATE_CACHE_FILE.read_text())
        assert set(cache) == LINUX_RESULT_KEYS


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

    def test_check_for_updates_menu_item_is_mac_only(self, src):
        body = src.split("update_items = (", 1)[1].split("menu = pystray.Menu(", 1)[0]
        assert '"Check for Updates…", self.check_for_updates_from_tray' in body
        assert 'if sys.platform == "darwin"' in body
        assert "else []" in body


class TestMacUpdateActions:
    """The macOS-only UI paths, exercised on the unbound methods."""

    @pytest.fixture
    def wm(self):
        import wayfinder_main
        return wayfinder_main

    @staticmethod
    def _opener(info):
        opened = []
        app = types.SimpleNamespace(_app_update_info=info, _open_url=opened.append)
        return app, opened

    def test_get_update_downloads_the_dmg_on_mac(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "IS_MACOS", True)
        app, opened = self._opener(
            {"release_url": "https://r.invalid", "download_url": "https://d.invalid/a.dmg"})
        wm.WayfinderApp._open_app_update_page(app)
        assert opened == ["https://d.invalid/a.dmg"]

    def test_get_update_falls_back_to_the_release_page_on_mac(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "IS_MACOS", True)
        app, opened = self._opener({"release_url": "https://r.invalid", "download_url": ""})
        wm.WayfinderApp._open_app_update_page(app)
        assert opened == ["https://r.invalid"]

    def test_get_update_off_mac_still_opens_the_release_page(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "IS_MACOS", False)
        monkeypatch.setattr(wm, "IS_WINDOWS", False)
        app, opened = self._opener(
            {"release_url": "https://r.invalid", "download_url": "https://d.invalid/a.dmg"})
        wm.WayfinderApp._open_app_update_page(app)
        assert opened == ["https://r.invalid"]

    def test_get_update_downloads_the_installer_on_windows(self, wm, monkeypatch):
        monkeypatch.setattr(wm, "IS_MACOS", False)
        monkeypatch.setattr(wm, "IS_WINDOWS", True)
        app, opened = self._opener(
            {"release_url": "https://r.invalid", "download_url": "https://d.invalid/a.exe"})
        wm.WayfinderApp._open_app_update_page(app)
        assert opened == ["https://d.invalid/a.exe"]

    def test_manual_check_forces_a_fresh_check_off_the_tk_thread(self, wm, monkeypatch):
        calls = []
        verdict = {"update_available": False, "latest_version": "v1.1.8", "error": None}

        def fake_check(version, force=False):
            calls.append((threading.current_thread(), force))
            return verdict

        monkeypatch.setattr(app_updates, "check_for_app_update", fake_check)
        app = types.SimpleNamespace(event_queue=queue.Queue())
        wm.WayfinderApp._check_app_update_now(app)
        event_type, callback = app.event_queue.get(timeout=5)
        assert event_type == wm.EventType.UI_CALLBACK
        assert calls and calls[0][1] is True
        assert calls[0][0] is not threading.main_thread()
        reported = []
        app._report_app_update_check = reported.append
        callback()
        assert reported == [verdict]

    def test_manual_check_reports_up_to_date_or_shows_the_banner(self, wm):
        from wayfinder import __version__

        shown = []
        app = types.SimpleNamespace(
            _show_window=lambda: None,
            _welcome_active=False,
            _switch_tab=lambda tab: shown.append(("tab", tab)),
            _show_app_update_banner=lambda info: shown.append(("banner", info)),
            _show_app_update_status=lambda text: shown.append(("status", text)),
            log=lambda message: None,
        )
        wm.WayfinderApp._report_app_update_check(
            app, {"update_available": False, "error": None})
        assert shown == [("tab", "dictate"), ("status", f"You're up to date (v{__version__}).")]

        shown.clear()
        info = {"update_available": True, "latest_version": "v9.9.9"}
        wm.WayfinderApp._report_app_update_check(app, info)
        assert shown == [("tab", "dictate"), ("banner", info)]

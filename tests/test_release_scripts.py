"""Release tooling: beta numbering, version stamping, CHANGELOG promotion.

These scripts publish to every user on the Beta and Stable channels, so their
pure logic is pinned here (scripts/release/, docs/RELEASING.md).
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "release"))

import cut_beta  # noqa: E402
import mac_release_watcher as watcher  # noqa: E402
import versioning  # noqa: E402


class TestNextBeta:
    def test_first_beta_of_the_planned_version(self):
        assert versioning.next_beta("1.2.0", ["v1.1.8", "v1.1.8-beta.10"]) == "1.2.0-beta.1"

    def test_numbers_continue(self):
        tags = ["v1.1.8", "v1.2.0-beta.1", "v1.2.0-beta.2", "v1.2.0-beta.10"]
        assert versioning.next_beta("1.2.0", tags) == "1.2.0-beta.11"

    def test_after_the_stable_ships_betas_move_to_the_next_patch(self):
        tags = ["v1.2.0-beta.4", "v1.2.0"]
        assert versioning.next_beta("1.2.0", tags) == "1.2.1-beta.1"

    def test_a_planned_minor_wins_over_the_next_patch(self):
        assert versioning.next_beta("1.3.0", ["v1.2.0"]) == "1.3.0-beta.1"

    def test_other_prerelease_kinds_do_not_count(self):
        tags = ["v1.2.0-rc.1", "v1.2.0-beta.x", "v1.2.0-beta.2.1"]
        assert versioning.next_beta("1.2.0", tags) == "1.2.0-beta.1"

    def test_unreadable_main_version_is_refused(self):
        with pytest.raises(ValueError):
            versioning.next_beta("dev", [])


@pytest.fixture
def tree(tmp_path):
    """A copy of every file the release scripts write."""
    for relative, _ in versioning.VERSION_FILES:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, tmp_path / relative)
    for relative in (versioning.METAINFO, versioning.CHANGELOG):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / relative, tmp_path / relative)
    return tmp_path


class TestStamping:
    def test_every_version_file_agrees_with_pyproject_today(self):
        version = versioning.read_version()
        for relative, pattern in versioning.VERSION_FILES:
            import re
            text = (REPO / relative).read_text(encoding="utf-8")
            assert re.findall(pattern, text, re.MULTILINE) == [version], relative

    def test_set_version_writes_every_file(self, tree):
        versioning.set_version("9.8.7-beta.6", root=tree)
        assert versioning.read_version(tree) == "9.8.7-beta.6"
        for relative, _ in versioning.VERSION_FILES:
            assert "9.8.7-beta.6" in (tree / relative).read_text(encoding="utf-8")

    def test_set_version_refuses_junk(self, tree):
        with pytest.raises(ValueError):
            versioning.set_version("1.2", root=tree)

    def test_development_release_entry_is_valid_and_first(self, tree):
        versioning.add_metainfo_release(
            "99.0.0-beta.1", date(2026, 10, 3), "Beta <of> main", ["A & B", "C"],
            development=True, root=tree)
        root = ET.parse(tree / versioning.METAINFO).getroot()
        first = root.find("releases/release")
        assert first.get("version") == "99.0.0-beta.1"
        assert first.get("type") == "development"
        assert [li.text for li in first.findall("description/ul/li")] == ["A & B", "C"]

    def test_release_entry_is_not_added_twice(self, tree):
        for _ in range(2):
            versioning.add_metainfo_release(
                "99.0.0", date(2026, 10, 3), "x", [], development=False, root=tree)
        text = (tree / versioning.METAINFO).read_text(encoding="utf-8")
        assert text.count('<release version="99.0.0"') == 1


CHANGELOG = """# Changelog

## [Unreleased]

Mac: here at last.

### Added

- **Mac app.** Signed and
  notarized.
- **Beta channel.** Opt in.

### Fixed

- **Idle CPU.** Lower.

## [1.1.8] - 2026-08-26

- Old.
"""


class TestChangelog:
    def test_titles_are_the_bold_leads(self):
        body = versioning.unreleased_body(CHANGELOG)
        assert versioning.bullet_titles(body) == ["Mac app.", "Beta channel.", "Idle CPU."]

    def test_promote_moves_everything_when_main_matches_the_beta(self):
        body = versioning.unreleased_body(CHANGELOG)
        out = versioning.promote_changelog(CHANGELOG, "1.2.0", body, date(2026, 10, 10))
        unreleased = versioning.unreleased_body(out)
        assert unreleased.strip() == ""
        released = out.split("## [1.2.0] — 2026-10-10", 1)[1].split("## [1.1.8]", 1)[0]
        assert "Mac: here at last." in released
        assert "- **Mac app.** Signed and\n  notarized." in released
        assert "### Fixed" in released
        assert out.index("## [Unreleased]") < out.index("## [1.2.0]") < out.index("## [1.1.8]")

    def test_notes_merged_after_the_beta_stay_unreleased(self):
        released = versioning.unreleased_body(CHANGELOG)
        main = CHANGELOG.replace(
            "- **Idle CPU.** Lower.\n",
            "- **Idle CPU.** Lower.\n- **Newer fix.** Not in the beta.\n")
        out = versioning.promote_changelog(main, "1.2.0", released, date(2026, 10, 10))
        unreleased = versioning.unreleased_body(out)
        assert "Newer fix." in unreleased and "### Fixed" in unreleased
        assert "Mac app." not in unreleased and "### Added" not in unreleased
        section = out.split("## [1.2.0]", 1)[1]
        assert "Newer fix." not in section.split("## [1.1.8]")[0]

    def test_the_real_changelog_has_an_unreleased_section(self):
        text = (REPO / versioning.CHANGELOG).read_text(encoding="utf-8")
        assert versioning.bullet_titles(versioning.unreleased_body(text))


class TestCutBeta:
    def test_all_required_checks_must_pass(self):
        ok = {"Quality": "success", "Platform smoke (macOS)": "success",
              "Windows tests": "skipped"}
        assert cut_beta.checks_passed(ok) == []
        assert cut_beta.checks_passed({**ok, "Quality": "in_progress"}) == ["Quality: in_progress"]
        assert cut_beta.checks_passed({**ok, "Platform smoke (macOS)": "failure"})
        missing = dict(ok)
        del missing["Quality"]
        assert cut_beta.checks_passed(missing) == ["Quality: missing"]

    def test_mac_smoke_may_not_be_skipped(self):
        assert cut_beta.checks_passed({
            "Quality": "success", "Platform smoke (macOS)": "skipped",
            "Windows tests": "success"})

    def test_required_checks_match_mains_protection(self):
        doc = (REPO / "docs" / "CI.md").read_text(encoding="utf-8")
        for name in cut_beta.REQUIRED_CHECKS:
            assert f"`{name}`" in doc

    def test_beta_ordering_is_numeric(self):
        tags = ["v1.2.0-beta.10", "v1.2.0-beta.9", "v1.1.9-beta.30", "v1.2.0", "junk"]
        assert cut_beta.beta_tags(tags) == ["v1.1.9-beta.30", "v1.2.0-beta.9", "v1.2.0-beta.10"]

    NOW = __import__("datetime").datetime(2026, 10, 30, tzinfo=__import__("datetime").timezone.utc)

    def _listing(self, ages):
        from datetime import timedelta
        return [{"tag_name": f"v1.2.0-beta.{n}", "draft": False,
                 "published_at": (self.NOW - timedelta(days=age)).isoformat()}
                for n, age in ages.items()]

    def test_daily_betas_inside_the_soak_window_are_all_kept(self):
        ages = {n: 7 - n for n in range(1, 8)}  # beta.1 is 6 days old, beta.7 today
        ours = {f"v1.2.0-beta.{n}" for n in ages}
        assert cut_beta.to_prune(self._listing(ages), ours, keep=5, keep_days=14, now=self.NOW) == []

    def test_only_betas_past_both_limits_go(self):
        ages = {1: 20, 2: 19, 3: 18, 4: 17, 5: 16, 6: 15, 7: 1}
        ours = {f"v1.2.0-beta.{n}" for n in ages}
        assert cut_beta.to_prune(self._listing(ages), ours, keep=5, keep_days=14,
                                 now=self.NOW) == ["v1.2.0-beta.1", "v1.2.0-beta.2"]

    def test_hand_made_tags_are_never_pruned(self):
        listing = self._listing({1: 40}) + [{"tag_name": "v1.1.8-beta.10", "draft": False,
                                             "published_at": "2026-08-17T00:00:00Z"}]
        assert cut_beta.to_prune(listing, {"v1.2.0-beta.1"}, keep=0, keep_days=14,
                                 now=self.NOW) == ["v1.2.0-beta.1"]

    def test_the_newest_run_of_each_check_wins(self):
        runs = [
            {"id": 1, "name": "Quality", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T10:00:00Z"},
            {"id": 2, "name": "Quality", "status": "completed", "conclusion": "failure",
             "started_at": "2026-10-03T11:00:00Z"},
            {"id": 3, "name": "Windows tests", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T10:00:00Z"},
            {"id": 4, "name": "Windows tests", "status": "in_progress", "conclusion": None,
             "started_at": "2026-10-03T12:00:00Z"},
        ]
        assert cut_beta.latest_verdicts(runs) == {"Quality": "failure", "Windows tests": "in_progress"}

    def test_a_newer_queued_run_outranks_an_older_success(self):
        # The re-check's case: a re-run queued with no start time yet.
        runs = [
            {"id": 7, "name": "Quality", "status": "completed", "conclusion": "success",
             "started_at": "2026-10-03T10:00:00Z"},
            {"id": 9, "name": "Quality", "status": "queued", "conclusion": None, "started_at": None},
        ]
        assert cut_beta.latest_verdicts(runs) == {"Quality": "queued"}
        assert cut_beta.checks_passed({"Quality": "queued"})

    DISPATCH = ("workflow", "run", "release.yml", "--repo", "o/r", "--ref", "v1.2.0-beta.3")

    def _resume(self, monkeypatch, release, runs):
        calls = []
        monkeypatch.setattr(cut_beta, "release_for", lambda repo, tag: release)
        monkeypatch.setattr(cut_beta, "note", lambda m: None)
        monkeypatch.setattr(cut_beta, "gh", lambda *a: calls.append(a) or json.dumps(runs))
        handled = cut_beta.resume_unfinished("o/r", "v1.2.0-beta.3", dry_run=False)
        return handled, self.DISPATCH in calls

    @staticmethod
    def _release(*names):
        return {"tag_name": "v1.2.0-beta.3", "draft": False,
                "assets": [{"name": n, "state": "uploaded"} for n in names]}

    LINUX = ("Wayfinder_Aura-1.2.0-beta.3-x86_64.AppImage",
             "Wayfinder_Aura-1.2.0-beta.3-x86_64.AppImage.zsync",
             "io.wayfindercollective.WayfinderAura.flatpak")
    DONE = {"status": "completed", "conclusion": "success"}
    FAILED = {"status": "completed", "conclusion": "failure"}

    def test_an_unreleased_beta_is_resumed_not_skipped(self, monkeypatch):
        assert self._resume(monkeypatch, None, []) == (True, True)

    def test_a_release_whose_uploads_failed_is_rebuilt(self, monkeypatch):
        # The re-check's case: the release object exists, an asset does not.
        release = self._release(*self.LINUX[:1])
        assert self._resume(monkeypatch, release, [self.FAILED]) == (True, True)

    def test_a_draft_release_is_unfinished(self, monkeypatch):
        release = {**self._release(*self.LINUX), "draft": True}
        assert self._resume(monkeypatch, release, []) == (True, True)

    def test_a_complete_linux_release_is_finished_without_the_dmg(self, monkeypatch):
        # The Mac DMG is the watcher's job, not the Release workflow's.
        assert self._resume(monkeypatch, self._release(*self.LINUX), []) == (False, False)

    def test_a_running_build_is_waited_on(self, monkeypatch):
        running = {"status": "queued", "conclusion": None}
        assert self._resume(monkeypatch, None, [self.FAILED, running]) == (True, False)

    def test_two_builds_that_left_it_unfinished_are_left_for_a_person(self, monkeypatch):
        # Failed or "successful": either way the beta still lacks a download.
        assert self._resume(monkeypatch, None, [self.FAILED, self.FAILED]) == (False, False)
        assert self._resume(monkeypatch, self._release(*self.LINUX[1:]),
                            [self.DONE, self.DONE]) == (False, False)

    def test_only_a_missing_release_reads_as_none(self, monkeypatch):
        def fake(code, err):
            return lambda *a, **k: subprocess.CompletedProcess(a, code, '{"tag_name": "v1"}', err)
        monkeypatch.setattr(cut_beta.subprocess, "run", fake(0, ""))
        assert cut_beta.release_for("o/r", "v1") == {"tag_name": "v1"}
        monkeypatch.setattr(cut_beta.subprocess, "run", fake(1, "gh: Not Found (HTTP 404)"))
        assert cut_beta.release_for("o/r", "v1") is None
        monkeypatch.setattr(cut_beta.subprocess, "run", fake(1, "gh: Server Error (HTTP 502)"))
        with pytest.raises(RuntimeError):
            cut_beta.release_for("o/r", "v1")


class TestPromote:
    NOW = __import__("datetime").datetime(2026, 10, 20, tzinfo=__import__("datetime").timezone.utc)

    @staticmethod
    def _release(tag, linux="2026-10-10T09:40:00Z", dmg="2026-10-10T11:00:00Z", **extra):
        version = tag[1:]
        assets = [{"name": n, "state": "uploaded", "updated_at": linux} for n in (
            f"Wayfinder_Aura-{version}-x86_64.AppImage",
            f"Wayfinder_Aura-{version}-x86_64.AppImage.zsync",
            "io.wayfindercollective.WayfinderAura.flatpak")]
        if dmg:
            assets.append({"name": f"Wayfinder_Aura-{version}-macOS-arm64.dmg",
                           "state": "uploaded", "updated_at": dmg})
        return {"tag_name": tag, "draft": False, "published_at": linux, "assets": assets, **extra}

    def test_soak_counts_from_the_last_required_download(self):
        import promote
        release = self._release("v1.2.0-beta.4")
        release["assets"].append({"name": "notes.txt", "state": "uploaded",
                                  "updated_at": "2026-10-19T00:00:00Z"})
        assert promote.availability(release).isoformat() == "2026-10-10T11:00:00+00:00"

    @pytest.mark.parametrize("change", ["no dmg", "dmg uploading", "no flatpak", "draft"])
    def test_an_incomplete_beta_is_never_available(self, change):
        import promote
        release = self._release("v1.2.0-beta.4", dmg=None if change == "no dmg" else "2026-10-10T11:00:00Z")
        if change == "dmg uploading":
            release["assets"][-1]["state"] = "starter"
        if change == "no flatpak":
            release["assets"] = [a for a in release["assets"] if not a["name"].endswith(".flatpak")]
        if change == "draft":
            release["draft"] = True
        assert promote.availability(release) is None

    def test_the_newest_complete_beta_that_has_soaked_is_the_candidate(self):
        import promote
        listing = [
            self._release("v1.2.0-beta.9", linux="2026-10-19T09:30:00Z", dmg="2026-10-19T10:30:00Z"),
            # The re-check's case: six days old but its Mac DMG never came.
            self._release("v1.2.0-beta.7", linux="2026-10-14T09:30:00Z", dmg=None),
            self._release("v1.2.0-beta.6", linux="2026-10-14T09:30:00Z", dmg="2026-10-14T11:00:00Z"),
            self._release("v1.2.0-beta.5", linux="2026-10-13T09:30:00Z", dmg="2026-10-13T11:00:00Z"),
        ]
        ours = {r["tag_name"] for r in listing}
        assert promote.pick_candidate(listing, ours, 5, self.NOW) == ("v1.2.0-beta.6", 5)
        assert promote.pick_candidate(listing, ours, 30, self.NOW) is None

    def test_a_closed_unmerged_notes_pr_does_not_count(self):
        import promote
        mine = "Moves the notes shipped in v1.2.0 (from v1.2.0-beta.6) under ## [1.2.0]"
        closed = [{"url": "u1", "state": "CLOSED", "body": mine}]
        # Not done, and not silently replaced: someone may have closed it on purpose.
        with pytest.raises(SystemExit, match="closed without merging"):
            promote.usable_pr(closed, "v1.2.0-beta.6")
        assert promote.usable_pr(closed, "v1.2.0-beta.6", new_after_closed=True) is None
        other = [{"url": "u0", "state": "CLOSED", "body": mine.replace("beta.6", "beta.5")}]
        assert promote.usable_pr(other, "v1.2.0-beta.6") is None
        assert promote.usable_pr([{"url": "u2", "state": "OPEN", "body": mine},
                                  {"url": "u1", "state": "CLOSED", "body": mine}],
                                 "v1.2.0-beta.6") == "u2"
        assert promote.usable_pr([{"url": "u3", "state": "MERGED", "body": mine}],
                                 "v1.2.0-beta.6") == "u3"

    def test_a_live_notes_pr_for_another_beta_stops_the_promotion(self):
        import promote
        other = "Moves the notes shipped in v1.2.0 (from v1.2.0-beta.5) under ## [1.2.0]"
        with pytest.raises(SystemExit):
            promote.usable_pr([{"url": "u", "state": "OPEN", "body": other}], "v1.2.0-beta.6")

    def test_nothing_is_ever_force_pushed(self):
        # A person's commits on the notes branch must survive a re-run.
        source = (REPO / "scripts" / "release" / "promote.py").read_text(encoding="utf-8")
        pushes = [line for line in source.splitlines() if '"push"' in line]
        assert pushes and not [line for line in pushes if "force" in line]

    def test_an_existing_tag_must_be_this_promotions(self, monkeypatch):
        import promote
        base = "a" * 40
        commits = {"v1.2.0^{commit}^": base,
                   "v1.2.0^{commit}": promote.release_message("1.2.0", "v1.2.0-beta.6", base)}
        monkeypatch.setattr(promote, "run", lambda *a, **k: commits[a[-1]])
        assert promote.tag_problem("v1.2.0", "v1.2.0-beta.6", base) is None
        assert promote.tag_problem("v1.2.0", "v1.2.0-beta.5", base)
        assert promote.tag_problem("v1.2.0", "v1.2.0-beta.6", "b" * 40)

    def test_release_assets_match_what_the_workflow_and_watcher_publish(self):
        workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for pattern in ("dist/Wayfinder_Aura-*.AppImage\n", "dist/Wayfinder_Aura-*.AppImage.zsync\n",
                        "dist/io.wayfindercollective.WayfinderAura.flatpak\n"):
            assert pattern in workflow
        names = versioning.release_assets("v1.2.0-beta.3")
        assert "Wayfinder_Aura-1.2.0-beta.3-x86_64.AppImage" in names
        assert "io.wayfindercollective.WayfinderAura.flatpak" in names
        missing = watcher.releases_missing_a_dmg(
            [{"tag_name": "v1.2.0-beta.3", "published_at": "2026-10-03T09:00:00Z",
              "assets": [{"name": n, "state": "uploaded"} for n in names]}],
            __import__("datetime").datetime(2026, 10, 3, 12, tzinfo=__import__("datetime").timezone.utc))
        assert missing == []  # the watcher's DMG name is the one promote requires


class TestWorkflows:
    def test_beta_workflow_runs_the_script_on_aura_hardware(self):
        text = (REPO / ".github" / "workflows" / "beta.yml").read_text(encoding="utf-8")
        assert "runs-on: [self-hosted, Linux, X64, aura-linux]" in text
        assert "python scripts/release/cut_beta.py" in text
        assert "contents: write" in text and "actions: write" in text
        assert "schedule:" in text and "workflow_dispatch:" in text

    def test_tag_builds_skip_the_mac_until_signing_is_configured(self):
        text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        job = text.split("  build-macos:", 1)[1].split("\n  release:", 1)[0]
        assert "needs.macos-signing.outputs.configured == 'true'" in job
        assert "scripts/release/attach_mac_dmg.sh" in text


class TestMacReleaseWatcher:
    NOW = __import__("datetime").datetime(2026, 10, 10, tzinfo=__import__("datetime").timezone.utc)

    def _release(self, tag, days_ago=1, assets=(), draft=False):
        when = (self.NOW - __import__("datetime").timedelta(days=days_ago)).isoformat()
        return {"tag_name": tag, "draft": draft, "published_at": when,
                "assets": [{"name": name, "state": "uploaded"} for name in assets]}

    def test_builds_recent_betas_and_stables_without_a_dmg_newest_first(self):
        releases = [
            self._release("v1.2.0-beta.3", days_ago=3),
            self._release("v1.2.0", days_ago=1),
            self._release("v1.2.0-beta.4", days_ago=2,
                          assets=["Wayfinder_Aura-1.2.0-beta.4-macOS-arm64.dmg"]),
        ]
        assert watcher.releases_missing_a_dmg(releases, self.NOW) == ["v1.2.0", "v1.2.0-beta.3"]

    def test_ignores_old_drafts_previews_and_partial_uploads_count_as_missing(self):
        partial = self._release("v1.2.1-beta.1")
        partial["assets"] = [{"name": "Wayfinder_Aura-1.2.1-beta.1-macOS-arm64.dmg", "state": "starter"}]
        releases = [
            self._release("v1.1.8", days_ago=40),
            self._release("macos-preview-3"),
            self._release("v1.2.0-beta.9", draft=True),
            partial,
        ]
        assert watcher.releases_missing_a_dmg(releases, self.NOW) == ["v1.2.1-beta.1"]

    def test_installer_runs_it_in_the_background_lane(self):
        text = (REPO / "scripts" / "release" / "install_mac_release_watcher.sh").read_text(encoding="utf-8")
        assert "<key>ProcessType</key><string>Background</string>" in text
        assert "<key>LowPriorityIO</key><true/>" in text
        assert "<key>ExitTimeOut</key><integer>60</integer>" in text  # > stop_group's 40 + 10 s
        source = (REPO / "scripts" / "release" / "mac_release_watcher.py").read_text(encoding="utf-8")
        assert '"/usr/sbin/taskpolicy", "-b"' in source


GIB = 1024 ** 3


def _reading(**overrides):
    # The Mac Studio as measured on 2026-10-03: model at 92 GiB, VM at 64 GiB.
    base = {"availableBytes": int(81.6 * GIB), "pressure": 1, "modelPids": [56016],
            "modelFootprintBytes": int(92.2 * GIB), "vmFootprintBytes": int(64.1 * GIB),
            "ciJobRunning": False}
    return {**base, **overrides}


class TestWatcherAdmission:
    def test_the_model_ceiling_refuses_until_fox_grid_grants_its_reserve(self):
        reading = _reading()
        assert "short of protected headroom" in watcher.admission_refusal(reading, watcher.MODEL_CEILING)
        assert watcher.admission_refusal(reading, 125 * GIB) is None

    def test_sampled_free_memory_is_not_enough(self):
        # The review's point: a host can look free yet owe the model its growth.
        reading = _reading(modelFootprintBytes=10 * GIB, availableBytes=120 * GIB)
        assert watcher.admission_refusal(reading, 125 * GIB) is not None

    @pytest.mark.parametrize("override,reason", [
        ({"pressure": 2}, "pressure"), ({"ciJobRunning": True}, "CI job")])
    def test_pressure_or_a_running_ci_job_refuses(self, override, reason):
        assert reason in watcher.admission_refusal(_reading(**override), 125 * GIB)

    # The watcher runs only on the Mac; grant ownership checks use POSIX uids.
    posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and mode checks")

    def _grant(self, tmp_path, pid=56016, mode=0o600, age=0, reserve=125 * GIB):
        path = tmp_path / "grant.json"
        path.write_text(json.dumps({"modelPid": pid, "modelReserveBytes": reserve}))
        path.chmod(mode)
        if age:
            stamp = time.time() - age
            os.utime(path, (stamp, stamp))
        return path

    @posix
    def test_a_fresh_grant_for_the_loaded_model_applies(self, tmp_path):
        assert watcher.model_reserve(_reading(), self._grant(tmp_path)) == 125 * GIB

    @posix
    @pytest.mark.parametrize("kwargs", [
        {"pid": 1}, {"mode": 0o666}, {"age": 2 * 86400}, {"age": -3600},
        {"reserve": 500 * GIB}, {"reserve": 0}, {"reserve": -1}])
    def test_any_doubtful_grant_falls_back_to_the_ceiling(self, tmp_path, kwargs):
        assert watcher.model_reserve(_reading(), self._grant(tmp_path, **kwargs)) == watcher.MODEL_CEILING

    @posix
    @pytest.mark.parametrize("text", [
        # The re-check's probes: each of these used to reserve 1 byte or crash.
        '{"modelPid": 56016, "modelReserveBytes": true}',
        '{"modelPid": 56016, "modelReserveBytes": 1.5}',
        '{"modelPid": 56016, "modelReserveBytes": "1"}',
        '{"modelPid": 56016, "modelReserveBytes": Infinity}',
        '{"modelPid": 56016, "modelReserveBytes": NaN}',
        '{"modelPid": 56016, "modelReserveBytes": 1e300}',
        '{"modelPid": 56016.0, "modelReserveBytes": 134217728000}',
        '{"modelPid": "56016", "modelReserveBytes": 134217728000}',
        '{"modelPid": true, "modelReserveBytes": 134217728000}',
        '{"modelReserveBytes": 134217728000}',
        '[56016, 134217728000]', '"grant"', 'null', '{"modelPid": 56016',
    ])
    def test_a_malformed_grant_falls_back_to_the_ceiling(self, tmp_path, text):
        path = tmp_path / "grant.json"
        path.write_text(text)
        path.chmod(0o600)
        assert watcher.model_reserve(_reading(), path) == watcher.MODEL_CEILING


@pytest.mark.skipif(sys.platform == "win32", reason="the watcher runs on the Mac (ps, process groups)")
class TestWatcherHold:
    @pytest.fixture(autouse=True)
    def _hold(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watcher, "HOLD", tmp_path / "aura-mac-ci.hold")
        monkeypatch.setattr(watcher, "log", lambda message: None)

    def test_acquired_atomically_and_released_only_by_its_owner(self):
        assert watcher.acquire_hold() is True
        assert watcher.acquire_hold() is False
        watcher.HOLD.write_text("aura-mac-release pid=1 pgid=0\n")  # someone else's
        watcher.release_hold()
        assert watcher.HOLD.exists()
        watcher.HOLD.write_text(f"aura-mac-release pid={os.getpid()} pgid=0\n")
        watcher.release_hold()
        assert not watcher.HOLD.exists()

    def test_a_stale_hold_from_a_killed_watcher_is_reclaimed(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        watcher.HOLD.write_text(f"aura-mac-release pid={dead.pid} pgid={dead.pid}\n")
        assert watcher.reclaim_stale_hold() is True and not watcher.HOLD.exists()

    def test_a_stop_during_a_held_block_lands_after_it(self, monkeypatch):
        monkeypatch.setattr(watcher, "_stopping", False)
        monkeypatch.setattr(watcher, "_held", 0)
        monkeypatch.setattr(watcher, "_deferred", None)
        previous = signal.signal(signal.SIGTERM, watcher._raise_stop)
        done = []
        try:
            with pytest.raises(watcher.Stop):
                with watcher.signals_held():
                    os.kill(os.getpid(), signal.SIGTERM)
                    time.sleep(0.05)
                    done.append("recorded")
            os.kill(os.getpid(), signal.SIGTERM)  # a second signal is ignored
            time.sleep(0.05)
        finally:
            signal.signal(signal.SIGTERM, previous)
        assert done == ["recorded"]

    def test_live_or_foreign_holds_are_never_reclaimed(self):
        watcher.HOLD.write_text(f"aura-mac-release pid={os.getpid()} pgid=0\n")
        assert watcher.reclaim_stale_hold() is False
        watcher.HOLD.write_text("aura measure 12345\n")  # Infra or a person
        assert watcher.reclaim_stale_hold() is False and watcher.HOLD.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS taskpolicy, time -l and libproc")
class TestWatcherBuild:
    def _fake_attach(self, tmp_path, body):
        (tmp_path / "attach_mac_dmg.sh").write_text(body)

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(watcher, "HERE", tmp_path)
        monkeypatch.setattr(watcher, "HOLD", tmp_path / "aura-mac-ci.hold")
        monkeypatch.setattr(watcher, "log", lambda message: None)
        monkeypatch.setattr(watcher, "host_reading", lambda: _reading())
        monkeypatch.setattr(watcher, "model_reserve", lambda reading: 125 * GIB)
        monkeypatch.setattr(watcher, "CHECK_EVERY_S", 0.5)

    def _alive(self, pids_file):
        return [pid for pid in pids_file.read_text().split()
                if subprocess.run(["kill", "-0", pid], capture_output=True).returncode == 0]

    def test_a_hung_build_and_a_term_ignoring_child_are_all_stopped(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        pids = tmp_path / "pids"
        # The review's repro: a child that ignores SIGTERM must still be ended.
        self._fake_attach(tmp_path, f"""
(trap '' TERM; echo $BASHPID >> "{pids}"; sleep 300) &
echo $$ >> "{pids}"
wait
""")
        monkeypatch.setattr(watcher, "BUILD_TIMEOUT_S", 2)
        original = watcher.stop_group
        monkeypatch.setattr(watcher, "stop_group",
                            lambda pgid, grace=40, proc=None: original(pgid, 1, proc))
        assert watcher.acquire_hold()
        build = {}
        try:
            assert watcher.run_build("v9.9.9-beta.1", {}, build) != 0
        finally:
            watcher.finish_hold(build)
        assert self._alive(pids) == []
        assert not watcher.HOLD.exists()

    def test_a_build_over_its_memory_cap_is_stopped(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._fake_attach(tmp_path, "sleep 300\n")
        monkeypatch.setattr(watcher, "group_footprint", lambda pgid: 5 * GIB)
        assert watcher.acquire_hold()
        build = {}
        try:
            assert watcher.run_build("v9.9.9-beta.1", {}, build) == -1
        finally:
            watcher.finish_hold(build)

    def test_a_grant_that_stops_applying_mid_build_stops_the_build(self, tmp_path, monkeypatch):
        # The re-check's case: the model restarts (new PID), so its grant no
        # longer applies; the next check must use the 160 GiB guard again.
        self._setup(tmp_path, monkeypatch)
        self._fake_attach(tmp_path, "sleep 300\n")
        reserves = iter([125 * GIB])
        monkeypatch.setattr(watcher, "model_reserve",
                            lambda reading: next(reserves, watcher.MODEL_CEILING))
        build = {}
        assert watcher.acquire_hold()
        try:
            assert watcher.run_build("v9.9.9-beta.1", {}, build) == -1
        finally:
            watcher.finish_hold(build)
        assert not watcher.group_members(build["pgid"])

    def test_a_stop_while_the_build_is_being_recorded_still_ends_it(self, tmp_path, monkeypatch):
        # The re-check's case: Stop lands right after the build was spawned.
        self._setup(tmp_path, monkeypatch)
        self._fake_attach(tmp_path, "sleep 300\n")
        original = watcher.note_build_in_hold

        def note_then_stop(pgid):
            original(pgid)
            raise watcher.Stop("SIGTERM")

        monkeypatch.setattr(watcher, "note_build_in_hold", note_then_stop)
        build = {}
        assert watcher.acquire_hold()
        try:
            with pytest.raises(watcher.Stop):
                watcher.run_build("v9.9.9-beta.1", {}, build)
        finally:
            watcher.finish_hold(build)
        assert build["pgid"] and not watcher.group_members(build["pgid"])
        assert not watcher.HOLD.exists()

    def test_the_build_is_not_started_with_stop_signals_blocked(self, tmp_path, monkeypatch):
        # Started inside signals_held(), which must not leave a blocked
        # signal mask to the build: it would ignore the SIGTERM that ends it.
        self._setup(tmp_path, monkeypatch)
        self._fake_attach(tmp_path, f'kill -TERM $$\necho ignored > "{tmp_path}/survived"\n')
        assert watcher.run_build("v9.9.9-beta.1", {}, {}) != 0
        assert not (tmp_path / "survived").exists()

    def test_a_build_that_will_not_stop_keeps_the_hold(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(watcher, "group_members", lambda pgid: [pgid])
        monkeypatch.setattr(watcher, "stop_group", lambda pgid, grace=40, proc=None: False)
        assert watcher.acquire_hold()
        watcher.note_build_in_hold(4242)
        watcher.finish_hold({"pgid": 4242})
        assert watcher.HOLD.exists()

    def test_sigterm_to_the_watcher_ends_the_build_and_lifts_the_hold(self, tmp_path):
        pids = tmp_path / "pids"
        (tmp_path / "attach_mac_dmg.sh").write_text(f'echo $$ >> "{pids}"\nsleep 300\n')
        script = tmp_path / "run.py"
        script.write_text(f"""
import signal, sys, time
sys.path.insert(0, {str(REPO / "scripts" / "release")!r})
import mac_release_watcher as w
from pathlib import Path
w.HERE = Path({str(tmp_path)!r}); w.HOLD = Path({str(tmp_path / "hold")!r})
w.host_reading = lambda: {_reading()!r}
w.model_reserve = lambda reading: 125 * 1024 ** 3
w.CHECK_EVERY_S = 0.5
for sig in (signal.SIGTERM,):
    signal.signal(sig, w._raise_stop)
w.acquire_hold()
build = {{}}
try:
    w.run_build("v9.9.9-beta.1", {{}}, build)
except w.Stop:
    pass
finally:
    w.finish_hold(build)
""")
        proc = subprocess.Popen([sys.executable, str(script)])
        for _ in range(100):
            if pids.exists() and pids.read_text().strip():
                break
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=90)
        assert self._alive(pids) == []
        assert not (tmp_path / "hold").exists()

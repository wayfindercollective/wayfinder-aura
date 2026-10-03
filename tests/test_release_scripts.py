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

    def test_an_unreleased_beta_is_resumed_not_skipped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cut_beta, "gh_ok", lambda *a: False)  # no release for the tag
        monkeypatch.setattr(cut_beta, "note", lambda m: None)
        monkeypatch.setattr(cut_beta, "gh", lambda *a: calls.append(a) or "[]")
        assert cut_beta.resume_unreleased("o/r", "v1.2.0-beta.3", dry_run=False) is True
        assert ("workflow", "run", "release.yml", "--repo", "o/r", "--ref", "v1.2.0-beta.3") in calls

    def test_a_running_or_twice_failed_build_is_not_redispatched(self, monkeypatch):
        monkeypatch.setattr(cut_beta, "gh_ok", lambda *a: False)
        monkeypatch.setattr(cut_beta, "note", lambda m: None)
        running = '[{"status": "in_progress", "conclusion": null}]'
        twice = '[{"status": "completed", "conclusion": "failure"}, {"status": "completed", "conclusion": "failure"}]'
        for listing, handled in ((running, True), (twice, False)):
            calls = []
            monkeypatch.setattr(cut_beta, "gh", lambda *a, out=listing, log=calls: log.append(a) or out)
            assert cut_beta.resume_unreleased("o/r", "v1.2.0-beta.3", dry_run=False) is handled
            assert not [c for c in calls if c[:2] == ("workflow", "run")]


class TestPromote:
    NOW = __import__("datetime").datetime(2026, 10, 20, tzinfo=__import__("datetime").timezone.utc)

    def test_soak_counts_from_the_last_download_not_the_commit(self):
        import promote
        release = {"tag_name": "v1.2.0-beta.4", "published_at": "2026-10-10T09:30:00Z",
                   "assets": [{"state": "uploaded", "updated_at": "2026-10-10T11:00:00Z"},
                              {"state": "starter", "updated_at": "2026-10-19T00:00:00Z"}]}
        assert promote.availability(release).isoformat() == "2026-10-10T11:00:00+00:00"

    def test_the_newest_beta_that_has_soaked_is_the_candidate(self):
        import promote
        listing = [
            {"tag_name": "v1.2.0-beta.9", "published_at": "2026-10-19T09:30:00Z", "assets": []},
            {"tag_name": "v1.2.0-beta.6", "published_at": "2026-10-14T09:30:00Z", "assets": []},
            {"tag_name": "v1.2.0-beta.5", "published_at": "2026-10-13T09:30:00Z", "assets": []},
        ]
        ours = {"v1.2.0-beta.9", "v1.2.0-beta.6", "v1.2.0-beta.5"}
        assert promote.pick_candidate(listing, ours, 5, self.NOW) == ("v1.2.0-beta.6", 5)
        assert promote.pick_candidate(listing, ours, 30, self.NOW) is None


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
        {"pid": 1}, {"mode": 0o666}, {"age": 2 * 86400}, {"reserve": 500 * GIB}])
    def test_any_doubtful_grant_falls_back_to_the_ceiling(self, tmp_path, kwargs):
        assert watcher.model_reserve(_reading(), self._grant(tmp_path, **kwargs)) == watcher.MODEL_CEILING


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
                            lambda pgid, grace=60, proc=None: original(pgid, 1, proc))
        assert watcher.acquire_hold()
        try:
            assert watcher.run_build("v9.9.9-beta.1", {}, 125 * GIB) != 0
        finally:
            watcher.release_hold()
        assert self._alive(pids) == []
        assert not watcher.HOLD.exists()

    def test_a_build_over_its_memory_cap_is_stopped(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._fake_attach(tmp_path, "sleep 300\n")
        monkeypatch.setattr(watcher, "group_footprint", lambda pgid: 5 * GIB)
        assert watcher.acquire_hold()
        try:
            assert watcher.run_build("v9.9.9-beta.1", {}, 125 * GIB) == -1
        finally:
            watcher.release_hold()

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
w.CHECK_EVERY_S = 0.5
for sig in (signal.SIGTERM,):
    signal.signal(sig, w._raise_stop)
w.acquire_hold()
try:
    w.run_build("v9.9.9-beta.1", {{}}, 125 * 1024 ** 3)
except w.Stop:
    pass
finally:
    w.release_hold()
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

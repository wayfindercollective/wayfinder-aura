"""Release tooling: beta numbering, version stamping, CHANGELOG promotion.

These scripts publish to every user on the Beta and Stable channels, so their
pure logic is pinned here (scripts/release/, docs/RELEASING.md).
"""

import shutil
import sys
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts" / "release"))

import cut_beta  # noqa: E402
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
            "1.2.0-beta.1", date(2026, 10, 3), "Beta <of> main", ["A & B", "C"],
            development=True, root=tree)
        root = ET.parse(tree / versioning.METAINFO).getroot()
        first = root.find("releases/release")
        assert first.get("version") == "1.2.0-beta.1"
        assert first.get("type") == "development"
        assert [li.text for li in first.findall("description/ul/li")] == ["A & B", "C"]

    def test_release_entry_is_not_added_twice(self, tree):
        for _ in range(2):
            versioning.add_metainfo_release(
                "1.2.0", date(2026, 10, 3), "x", [], development=False, root=tree)
        text = (tree / versioning.METAINFO).read_text(encoding="utf-8")
        assert text.count('<release version="1.2.0"') == 1


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
        released = out.split("## [1.2.0] - 2026-10-10", 1)[1].split("## [1.1.8]", 1)[0]
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

    def test_pruning_only_ever_touches_automated_betas(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cut_beta, "automated_betas", lambda: {
            f"v1.2.0-beta.{n}" for n in range(1, 8)})
        monkeypatch.setattr(cut_beta, "gh", lambda *args: (
            calls.append(args) or (
                '["v1.2.0-beta.7","v1.2.0-beta.6","v1.2.0-beta.5","v1.2.0-beta.4",'
                '"v1.2.0-beta.3","v1.2.0-beta.2","v1.2.0-beta.1","v1.1.8","v1.1.8-beta.10"]'
                if args[0] == "api" else "")))
        monkeypatch.setattr(cut_beta, "note", lambda message: None)
        cut_beta.prune("o/r", keep=5, dry_run=False)
        deleted = [args[2] for args in calls if args[:2] == ("release", "delete")]
        assert deleted == ["v1.2.0-beta.1", "v1.2.0-beta.2"]
        assert all("--cleanup-tag" in args for args in calls if args[:2] == ("release", "delete"))


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

"""scripts/export_game_catalog.py: the server's game catalog and release lines.

Wayfinder OS commits the script's output as convex/auraDesktop/gameCatalog.ts
and accepts game reports only for those games and version lines (design
docs/plans/game-reports-design-2026-10-15.md, section 5.5). No network: the
script runs with --no-fetch and reads this checkout's tags.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from wayfinder.core import linux_game_chat, macos_game_chat, windows_game_chat
from wayfinder.core.game_reports import game_key

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "export_game_catalog.py"


def _load():
    spec = importlib.util.spec_from_file_location("export_game_catalog", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def export():
    return _load()


# --- release lines from tags ----------------------------------------------------------

FIXTURE_TAGS = [
    "v1.1.0", "v1.1.8-beta.1", "v1.1.8-beta.10",
    "v1.2.0-beta.1", "v1.2.0", "v1.2.1-beta.4",
    "v1.9.3", "v1.10.0-beta.1",
    "v2.0.0-rc.1",
    # ignored: not a vX.Y.Z or vX.Y.Z-pre release tag
    "macos-preview-1", "macos-preview-2", "archive/develop", "archive/ci/self-hosted-runners",
    "v3.0", "3.1.0", "v4.0.0.1", "vnext", "release/1.5.0", "v5.0.0-", "",
]


def test_release_lines_come_from_release_tags_only(export):
    assert export.release_lines(FIXTURE_TAGS) == ["1.1", "1.2", "1.9", "1.10", "2.0"]


def test_release_lines_are_sorted_numerically_and_unique(export):
    lines = export.release_lines(["v1.10.0", "v1.9.0", "v1.9.1", "v1.2.0-beta.3", "v1.2.0"])
    assert lines == ["1.2", "1.9", "1.10"]
    assert export.release_lines(["macos-preview-1", "archive/macos"]) == []


def test_release_lines_version_is_a_short_hash_of_the_list(export):
    a = export.release_lines_version(["1.1", "1.2"])
    assert re.fullmatch(r"[0-9a-f]{12}", a)
    assert a == export.release_lines_version(["1.1", "1.2"])
    assert a != export.release_lines_version(["1.1", "1.2", "1.3"])


# --- the catalog ------------------------------------------------------------------------

def _union_names() -> set[str]:
    return ({e.name for e in macos_game_chat.game_list()}
            | {e.name for e in windows_game_chat.game_list()}
            | {e.name for e in linux_game_chat.game_list()})


def test_catalog_covers_every_listed_game_once(export):
    catalog = export.build_catalog(export.game_name_lists())
    assert {name for _key, name in catalog} == _union_names()
    assert all(key == game_key(name) for key, name in catalog)
    assert [key for key, _ in catalog] == sorted({key for key, _ in catalog})


def test_the_same_name_on_several_systems_is_one_entry(export):
    lists = {"macos": ["World of Warcraft"], "windows": ["World of Warcraft"],
             "linux": ["World of Warcraft", "Dark Age of Camelot"]}
    assert export.build_catalog(lists) == [
        ("dark-age-of-camelot", "Dark Age of Camelot"),
        ("world-of-warcraft", "World of Warcraft"),
    ]


def test_a_duplicate_key_in_one_list_fails(export):
    with pytest.raises(export.CatalogError, match="duplicate key 'foo-bar'"):
        export.build_catalog({"macos": ["Foo Bar", "Foo Bar"]})


def test_a_key_with_two_names_fails(export):
    with pytest.raises(export.CatalogError, match="'foo-bar' has two names"):
        export.build_catalog({"macos": ["Foo: Bar"], "windows": ["Foo Bar"]})


def test_a_name_without_a_usable_key_fails(export):
    with pytest.raises(export.CatalogError, match="no usable key"):
        export.build_catalog({"linux": ["!!!"]})


def test_main_exits_nonzero_on_a_conflict(export, monkeypatch, capsys):
    monkeypatch.setattr(export, "game_name_lists",
                        lambda: {"macos": ["Garry's Mod"], "linux": ["Garrys Mod"]})
    assert export.main(["--no-fetch"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "'garrys-mod' has two names" in captured.err


# --- the generated file ------------------------------------------------------------------

@pytest.fixture(scope="module")
def tags_file(tmp_path_factory):
    # CI checks out one commit without tags, so the run reads a fixture list.
    path = tmp_path_factory.mktemp("tags") / "tags.txt"
    path.write_text("\n".join(FIXTURE_TAGS) + "\n", encoding="utf-8")
    return path


def _run(tags_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--no-fetch", "--tags-file", str(tags_file)],
                          cwd=REPO, capture_output=True, encoding="utf-8", timeout=120)


def test_export_is_deterministic_and_names_the_aura_sha(tags_file):
    first, second = _run(tags_file), _run(tags_file)
    assert first.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                         text=True, check=True).stdout.strip()
    header = first.stdout.splitlines()[0]
    assert header.startswith("// Generated by wayfinder-aura `scripts/export_game_catalog.py` at ")
    assert sha in header and header.rstrip().endswith("do not edit.")


def test_export_holds_every_entry_and_the_three_constants(export, tags_file):
    out = _run(tags_file).stdout
    pairs = re.findall(r'\{ key: "([a-z0-9-]+)", name: (".*?") \}', out)
    assert {(k, json.loads(n)) for k, n in pairs} == {
        (game_key(n), n) for n in _union_names()}
    assert "export const AURA_GAME_CATALOG: ReadonlyArray<{ key: string; name: string }> = [" in out
    assert "export const AURA_RELEASE_LINES: ReadonlyArray<string> = [" in out
    block = out.split("AURA_RELEASE_LINES", 1)[1].split("];", 1)[0]
    lines = re.findall(r'"(\d+\.\d+)"', block)
    assert lines == ["1.1", "1.2", "1.9", "1.10", "2.0"]
    version = re.search(r'export const RELEASE_LINES_VERSION = "([0-9a-f]{12})";', out)
    assert version and version[1] == export.release_lines_version(lines)


def test_export_refuses_an_empty_release_list(tmp_path):
    empty = tmp_path / "tags.txt"
    empty.write_text("macos-preview-1\narchive/develop\n", encoding="utf-8")
    result = _run(empty)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no vX.Y.Z release tags" in result.stderr


def test_render_escapes_names_as_string_literals(export):
    text = export.render([("say-hi", 'Say "Hi" \\ Now')], ["1.2"], "abc123")
    assert '{ key: "say-hi", name: "Say \\"Hi\\" \\\\ Now" },' in text

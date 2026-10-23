"""The English word tiers load from core/wordlist.txt, and every package ships it.

The tiers used to be a generated 6,500-line Python module. They are data, so
they live in a data file now; voice_profile imports them on the dictation path,
so a bundle without the file would break dictation, not just the profile.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from wayfinder.core import wordlist

REPO = Path(__file__).resolve().parent.parent
GENERATOR = REPO / "scripts" / "gen_wordlists.py"


def _generator():
    spec = importlib.util.spec_from_file_location("gen_wordlists", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tiers_in_file_order(text: str) -> dict[str, list[str]]:
    tiers, words = {}, None
    for line in text.splitlines():
        if line.startswith("["):
            words = tiers.setdefault(line.strip("[]"), [])
        elif words is not None and not line.startswith("#"):
            words.extend(line.split())
    return tiers


def test_the_tiers_are_disjoint_lowercase_words():
    tiers = (wordlist.STOP_WORDS, wordlist.MID_WORDS, wordlist.COMMON_WORDS)
    assert [len(t) > 1000 for t in tiers] == [True, True, True]
    assert not (tiers[0] & tiers[1] or tiers[0] & tiers[2] or tiers[1] & tiers[2])
    assert all(w.isalpha() and w.islower() and len(w) >= 3 for t in tiers for w in t)
    # Known ranks from the generator's notes: "fix" ~2464, "niece" 23k-59k.
    assert "the" in wordlist.STOP_WORDS and "fix" in wordlist.STOP_WORDS
    assert "niece" in wordlist.COMMON_WORDS


def test_the_committed_file_is_exactly_what_the_generator_writes():
    text = wordlist.DATA_FILE.read_text(encoding="utf-8")
    tiers = _tiers_in_file_order(text)
    assert _generator().render(tiers["stop"], tiers["mid"], tiers["common"]) == text


def test_the_generator_splits_the_corpus_at_its_cutoffs(tmp_path):
    gen = _generator()

    def word(rank):  # a distinct alphabetic word per rank: 12 -> "bcxyz"
        return "".join("abcdefghij"[int(d)] for d in str(rank)) + "xyz"

    # Rank 1 is too short for the tokenizer, so it is skipped.
    lines = ["ab\t999"] + [f"{word(rank)}\t1" for rank in range(2, gen.COMMON_CUTOFF + 50)]
    stop, mid, common = gen.tiers("\n".join(lines))
    assert "ab" not in stop
    assert len(stop) == gen.STOP_CUTOFF - 1
    assert len(mid) == gen.MID_CUTOFF - gen.STOP_CUTOFF
    assert len(common) == gen.COMMON_CUTOFF - gen.MID_CUTOFF
    path = tmp_path / "wordlist.txt"
    path.write_text(gen.render(stop, mid, common), encoding="utf-8")
    loaded = wordlist._load_tiers(path)
    assert loaded == {"stop": frozenset(stop), "mid": frozenset(mid), "common": frozenset(common)}


def test_words_before_a_tier_header_are_rejected(tmp_path):
    path = tmp_path / "wordlist.txt"
    path.write_text("# header\nstray words\n[stop]\nthe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="before the first"):
        wordlist._load_tiers(path)


# ---- packaging -----------------------------------------------------------

def test_a_wheel_install_includes_the_data_file():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["wayfinder"]
    assert "core/wordlist.txt" in patterns


@pytest.mark.parametrize("spec", [
    "wayfinder-aura.spec", "wayfinder-aura-macos.spec", "packaging/windows/wayfinder-aura-windows.spec",
])
def test_each_pyinstaller_build_ships_the_package_tree(spec):
    # The specs copy src/wayfinder whole as data, which carries wordlist.txt.
    text = (REPO / spec).read_text(encoding="utf-8")
    assert re.search(r"""\(\s*(?:'src/wayfinder'|str\(SRC_DIR / "wayfinder"\))\s*,\s*["']wayfinder["']\s*\)""", text)


def test_the_flatpak_copies_the_package_tree():
    manifest = (REPO / "flatpak" / "io.wayfindercollective.WayfinderAura.yml").read_text(encoding="utf-8")
    assert "cp -r src/wayfinder ${FLATPAK_DEST}/lib/wayfinder-aura/src/" in manifest


def test_the_flatpak_smoke_runs_the_app_import_self_test():
    # The AppImage, Windows and macOS release smokes already run it.
    script = (REPO / "scripts" / "ci" / "build-flatpak-candidate.sh").read_text(encoding="utf-8")
    assert "--app-import-self-test" in script and "APP_IMPORT_SELF_TEST_OK" in script


def test_a_bundle_without_the_data_file_fails_loudly(tmp_path):
    # A copy of the loader with no wordlist.txt beside it, as in a broken bundle.
    copy = tmp_path / "wordlist.py"
    shutil.copy(REPO / "src" / "wayfinder" / "core" / "wordlist.py", copy)
    spec = importlib.util.spec_from_file_location("bundled_wordlist", copy)
    with pytest.raises(FileNotFoundError):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_the_app_import_self_test_loads_the_word_lists(tmp_path):
    main = (REPO / "main.py").read_text(encoding="utf-8")
    block = main[main.index('if "--app-import-self-test" in sys.argv:'):]
    block = block[:block.index("sys.exit(1)")]
    assert "from wayfinder.core.wordlist import" in block
    env = os.environ.copy()
    env.update(
        HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_DATA_HOME=str(tmp_path / "data"), XDG_RUNTIME_DIR=str(tmp_path),
        QT_QPA_PLATFORM="offscreen", PYTHONPATH=str(REPO / "src"),
    )
    run = subprocess.run(
        [sys.executable, str(REPO / "main.py"), "--app-import-self-test"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0 and "APP_IMPORT_SELF_TEST_OK" in run.stdout, run.stderr[-2000:]

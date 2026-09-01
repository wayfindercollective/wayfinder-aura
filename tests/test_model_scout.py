"""Behavioural coverage for `scripts/model_scout.py` and its cron workflow.

The scout runs unattended on `ubuntu-latest` every fortnight, so the two ways
it can fail are both silent: it can stop being headless (importing
`wayfinder_main` drags in tkinter, which has no display on a runner), or its
`--json` stdout contract can drift so the workflow's `jq`/`json.load` step
stops branching. Both are covered here.

Everything is offline: `_http_json` is the single network seam and every test
that reaches `main()` monkeypatches it. Every `main()` call also patches
`sys.argv` (the script's `parse_args()` takes no argv parameter, so it would
otherwise read pytest's own) and passes an explicit `-o` under `tmp_path` —
the default output path is under `$HOME` and `write_digest()` mkdir+writes it.
`--notify` is never passed: that branch can write `SCOUT_NEEDS_REVIEW` under
`$HOME` too. See `tests/conftest.py::temp_config_dir` for the scar this
follows.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import urllib.error
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "model_scout.py"
WORKFLOW = REPO / ".github" / "workflows" / "model-scout.yml"

# Filenames that are already shipped, so `known` filtering must drop them.
SHIPPED_QWEN3_4B = "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
# Same repo, a *different* file that still matches the pin-watch regex: this is
# what a stale pin looks like upstream.
REPINNED_QWEN3_4B = "Qwen_Qwen3-4B-Instruct-2507-v2-Q4_K_M.gguf"
NEW_DISCOVERY_FILE = "microsoft_Phi-4-mini-instruct-Q4_K_M.gguf"

WHISPER_API = "https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main"
QWEN3_4B_API = (
    "https://huggingface.co/api/models/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/tree/main"
)
PHI4_API = (
    "https://huggingface.co/api/models/bartowski/microsoft_Phi-4-mini-instruct-GGUF/tree/main"
)

# The other two PIN_WATCH sources. Stubbing all three is what makes a
# "zero unverifiable pins" assertion possible: `_fetcher({})` returns [] for any
# URL it does not know, and an empty tree is *unverifiable*, not "matching".
GEMMA3_1B_API = (
    "https://huggingface.co/api/models/bartowski/google_gemma-3-1b-it-GGUF/tree/main"
)
QWEN35_2B_API = "https://huggingface.co/api/models/unsloth/Qwen3.5-2B-GGUF/tree/main"

SHIPPED_GEMMA3_1B = "google_gemma-3-1b-it-Q4_K_M.gguf"
SHIPPED_QWEN35_2B = "Qwen3.5-2B-Q4_K_M.gguf"

PIN_WATCH_API_FOR_FILE = {
    SHIPPED_QWEN3_4B: QWEN3_4B_API,
    SHIPPED_GEMMA3_1B: GEMMA3_1B_API,
    SHIPPED_QWEN35_2B: QWEN35_2B_API,
}


def _all_pins_matching(scout, omit=(), drop_lfs=()):
    """Stub every PIN_WATCH repo with its shipped file at the pinned oid.

    `omit` leaves a repo's tree empty; `drop_lfs` returns the file with no LFS
    block. Both are unverifiable states, not matches.
    """
    pins = scout.load_pinned_digests()
    mapping = {}
    for filename, api in PIN_WATCH_API_FOR_FILE.items():
        if filename in omit:
            mapping[api] = []
            continue
        entry = {"path": filename, "size": 1_000_000_000}
        if filename not in drop_lfs:
            entry["lfs"] = {"oid": pins[filename]}
        mapping[api] = [entry]
    return mapping


def _load_scout():
    """Exec the script as a module without registering it in `sys.modules`.

    Same pattern as `tests/test_flatpak_release_manifest.py::_load_script`.
    """
    spec = importlib.util.spec_from_file_location("model_scout_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scout():
    return _load_scout()


@pytest.fixture(scope="module")
def yml():
    assert WORKFLOW.exists(), f"missing {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _home_is_disposable(tmp_path, monkeypatch):
    """Belt and braces: even a bug in a test cannot reach the real `$HOME`.

    Every test here also passes an explicit `-o`, but the script's default
    output is `Path.home()/.local/share/wayfinder-aura/model-scout-latest.md`,
    and `Path.home()` honours `$HOME` on POSIX.
    """
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


def _run_main(scout, monkeypatch, tmp_path, fetcher, extra_args=()):
    """Call `main()` offline, headless, and confined to `tmp_path`."""
    out = tmp_path / "digest.md"
    argv = ["model_scout.py", "--json", "-o", str(out), *extra_args]
    assert "--notify" not in argv, "--notify can write under $HOME; never use it in tests"
    monkeypatch.setattr(sys, "argv", argv)
    # `_http_json` is the single seam behind BOTH scout_whisper() and
    # scout_llm(); main() always hits the whisper tree plus all of
    # SCOUT_LLM_SOURCES, so patching anything narrower leaks real requests.
    monkeypatch.setattr(scout, "_http_json", fetcher)
    rc = scout.main()
    return rc, out


def _fetcher(mapping):
    """Build a fake `_http_json` that returns [] for any URL not in `mapping`."""

    def fake(url, timeout=20.0):
        return mapping.get(url, [])

    return fake


class TestHeadless:
    """The scout must run on a CI runner with no display."""

    def test_source_imports_no_ui_stack(self):
        """Primary check: static, order-independent, cannot be faked green."""
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("wayfinder_main", "tkinter", "customtkinter"):
            assert not re.search(
                rf"^\s*(?:import|from)\s+{re.escape(forbidden)}\b", source, re.M
            ), (
                f"scripts/model_scout.py imports {forbidden!r}. It must stay headless — "
                "read the catalogs as AST literals via read_app_catalogs() instead."
            )

    def test_importing_the_script_pulls_in_no_ui_modules(self):
        """Secondary, behavioural: measure the `sys.modules` DELTA.

        A process-wide `"wayfinder_main" not in sys.modules` assertion would be
        guaranteed red — ten gating test files import it at module scope, so it
        is already loaded at collection time. The delta is the honest question.
        """
        before = set(sys.modules)
        _load_scout()
        added = set(sys.modules) - before
        for forbidden in ("wayfinder_main", "tkinter", "customtkinter"):
            assert not any(
                m == forbidden or m.startswith(forbidden + ".") for m in added
            ), f"importing the scout pulled in {forbidden!r}: {sorted(added)}"


class TestCatalogReading:
    def test_read_app_catalogs_returns_both_sections(self, scout):
        catalogs = scout.read_app_catalogs()
        assert set(catalogs) == {"WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"}
        assert catalogs["WHISPER_CPP_MODELS"], "whisper catalog came back empty"

    def test_llm_catalog_is_exactly_the_three_shipped_models(self, scout):
        llm = scout.read_app_catalogs()["LLM_GGUF_MODELS"]
        assert set(llm) == {"gemma3-1b", "qwen3.5-2b", "qwen3-4b-2507"}

    def test_known_filenames_cover_every_shipped_gguf(self, scout):
        known = scout.load_known_filenames()
        llm = scout.read_app_catalogs()["LLM_GGUF_MODELS"]
        for key, info in llm.items():
            assert info["filename"] in known, f"{key} is shipped but not in load_known_filenames()"

    def test_known_filenames_cover_the_published_catalog(self, scout):
        """A remote-only model must not be re-reported as 'new' every fortnight."""
        known = scout.load_known_filenames()
        doc = json.loads((REPO / "catalog" / "v1.json").read_text(encoding="utf-8"))
        published = {
            info["filename"]
            for section in ("whisper", "llm")
            for info in (doc.get(section) or {}).values()
            if isinstance(info, dict) and info.get("filename")
        }
        assert published, "catalog/v1.json exposed no filenames — fixture is wrong"
        assert published <= known


class TestSourceLists:
    def test_pin_watch_and_discovery_are_disjoint(self, scout):
        pinned = {src["api"] for src in scout.PIN_WATCH}
        discovered = {src["api"] for src in scout.DISCOVERY}
        assert not (pinned & discovered), (
            "a repo in both PIN_WATCH and DISCOVERY would be fetched and counted twice"
        )

    def test_scout_llm_sources_is_the_union(self, scout):
        assert scout.SCOUT_LLM_SOURCES == scout.PIN_WATCH + scout.DISCOVERY


class TestJsonContract:
    """`--json` stdout is what the workflow's Summarise step branches on."""

    def test_stdout_is_pure_parseable_json(self, scout, monkeypatch, tmp_path, capsys):
        rc, out = _run_main(scout, monkeypatch, tmp_path, _fetcher({}))
        captured = capsys.readouterr()
        assert rc == 0
        payload = json.loads(captured.out)  # markdown leaking here breaks CI silently
        assert isinstance(payload["candidates"], int)
        assert "# Wayfinder model scout" in captured.err, "digest should go to stderr under --json"
        assert out.exists(), "digest file was not written to the explicit -o path"

    def test_digest_is_written_only_where_asked(self, scout, monkeypatch, tmp_path,
                                                _home_is_disposable):
        _run_main(scout, monkeypatch, tmp_path, _fetcher({}))
        stray = _home_is_disposable / ".local" / "share" / "wayfinder-aura"
        assert not stray.exists(), f"scout wrote under $HOME: {stray}"


class TestKnownFiltering:
    def test_shipped_file_is_filtered_out_and_new_candidate_counted(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """Pin-watch returns only the file we already ship -> not a candidate.
        One discovery repo returns one genuinely new file -> exactly 1.
        """
        rc, _ = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(
                {
                    QWEN3_4B_API: [
                        {
                            "path": SHIPPED_QWEN3_4B,
                            "size": 2_500_000_000,
                            # oid == our pin, so the bytes have not moved.
                            "lfs": {"oid": scout.load_pinned_digests()[SHIPPED_QWEN3_4B]},
                        }
                    ],
                    PHI4_API: [{"path": NEW_DISCOVERY_FILE, "size": 2_400_000_000}],
                }
            ),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["candidates"] == 1
        assert payload["pin_watch_hits"] == 0
        assert payload["discovery_hits"] == 1
        assert payload["llm_new"][0]["filename"] == NEW_DISCOVERY_FILE
        assert payload["errors"] == []

    def test_pin_watch_rerelease_is_counted_as_a_candidate(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """`candidates` deliberately INCLUDES pin-watch hits.

        A re-released file in a repo we ship from means our revision pin may be
        stale. If it did not raise `candidates`, stale-pin detection would go
        silent — the workflow only comments when `candidates != 0`.
        """
        rc, _ = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher({QWEN3_4B_API: [{"path": REPINNED_QWEN3_4B, "size": 2_500_000_000}]}),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["pin_watch_hits"] == 1
        assert payload["candidates"] == 1
        assert payload["discovery_hits"] == 0

    def test_whisper_hits_are_reported_but_not_counted_as_candidates(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """whisper.cpp ships ~22 quantised variants we choose not to ship;
        counting them would make every fortnightly run notify."""
        rc, _ = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(
                {
                    WHISPER_API: [
                        {"path": "ggml-large-v9-brand-new.bin", "size": 1_000_000},
                        {"path": "ggml-tiny.en.bin", "size": 77_000_000},  # shipped -> filtered
                        {"path": "ggml-tiny-for-tests.bin", "size": 100},  # test fixture -> skipped
                        {"path": "README.md", "size": 10},  # not a weight -> skipped
                    ]
                }
            ),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["whisper_new_count"] == 1
        assert payload["whisper_new"][0]["filename"] == "ggml-large-v9-brand-new.bin"
        assert payload["candidates"] == 0


class TestNetworkFailureIsSurvivable:
    def test_unreachable_sources_still_produce_parseable_json_and_rc_zero(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """The workflow marks the scout step `continue-on-error`; this proves
        the script cooperates rather than emitting a half-written report."""

        def boom(url, timeout=20.0):
            raise urllib.error.URLError("no route to host")

        rc, out = _run_main(scout, monkeypatch, tmp_path, boom)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["candidates"] == 0
        # 1 whisper tree + every configured LLM source failed, each reported.
        assert len(payload["errors"]) == 1 + len(scout.SCOUT_LLM_SOURCES)
        assert out.exists()
        assert "Errors (source unreachable" in out.read_text(encoding="utf-8")


class TestModelScoutWorkflow:
    """Raw-text/regex assertions on the cron workflow.

    Same approach `tests/test_release_metadata.py` uses for ci.yml /
    release.yml / flatpak-build.yml — no YAML parser dependency for one file.
    """

    def test_runs_biweekly_and_on_demand(self, yml):
        assert re.search(r'-\s*cron:\s*"0 8 1,15 \* \* *"', yml), "biweekly cron schedule changed"
        assert re.search(r"^\s*workflow_dispatch:", yml, re.M), "no manual trigger"

    def test_permissions_allow_issue_writes(self, yml):
        block = re.search(r"^permissions:\n((?:[ \t]+.*\n)+)", yml, re.M)
        assert block, "no top-level permissions block"
        assert re.search(r"^\s*issues:\s*write\s*$", block.group(1), re.M), (
            "the notifier comments on an issue; it needs issues: write"
        )

    def test_no_secret_is_referenced(self, yml):
        """It uses `${{ github.token }}` only. A `secrets.` reference would mean
        someone handed this reports-only cron a real credential."""
        assert "secrets." not in yml

    def test_scout_is_invoked_with_the_json_contract(self, yml):
        # Anchor on the actual invocation; the file's header comment also names
        # the script and would otherwise match.
        step = re.search(
            r"^\s*python3?\s+scripts/model_scout\.py\b((?:[^\n]*\\\n)*[^\n]*)", yml, re.M
        )
        assert step, "workflow no longer runs scripts/model_scout.py"
        assert "--json" in step.group(1), "the Summarise step parses stdout; --json is required"

    def test_scout_step_tolerates_a_network_hiccup(self, yml):
        run_step = re.search(r"-\s*name:\s*Run scout\n((?:[ \t]+.*\n|\n)+?)(?=^\s*-\s*name:)", yml, re.M)
        assert run_step, "could not locate the 'Run scout' step"
        assert re.search(r"^\s*continue-on-error:\s*true\s*$", run_step.group(1), re.M), (
            "a HuggingFace outage must not turn the cron badge red"
        )

    def test_existing_issue_is_looked_up_before_creating_one(self, yml):
        listed = yml.find("gh issue list")
        created = yml.find("gh issue create")
        assert listed != -1 and created != -1
        assert listed < created, (
            "creating before listing would open a fresh issue every fortnight"
        )

    def test_workflow_never_writes_production(self, yml):
        """Reports only: no R2/publish/PR path may appear."""
        for forbidden in ("wrangler", "r2 object", "gh pr create", "aws s3"):
            assert forbidden not in yml.lower(), f"scout workflow references {forbidden!r}"


class TestStalePinDetection:
    """A same-filename re-upload is the common way a pin goes stale.

    Nothing else we run catches it: `model-pin-drift.yml` reads our *pinned
    revision*, where our bytes are still sitting untouched, so it passes (and is
    right to). The question only this scout asks is "has upstream `main` moved
    past the bytes we pin?".
    """

    def test_load_pinned_digests_maps_shipped_files_to_their_sha256(self, scout):
        pins = scout.load_pinned_digests()
        llm = scout.read_app_catalogs()["LLM_GGUF_MODELS"]
        for key, info in llm.items():
            assert pins.get(info["filename"]) == info["sha256"], f"{key} pin not exposed"
        # An LFS oid is a sha256: 64 lowercase hex chars, directly comparable.
        for filename, digest in pins.items():
            assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{filename} pin is not a sha256"

    def test_changed_oid_on_a_shipped_filename_raises_a_stale_pin_alert(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """THE regression this class exists for. Same filename, different bytes."""
        rc, out = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(
                {
                    QWEN3_4B_API: [
                        {
                            "path": SHIPPED_QWEN3_4B,
                            "size": 2_500_000_000,
                            "lfs": {"oid": "f" * 64},  # != our pinned sha256
                        }
                    ]
                }
            ),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["stale_pin_hits"] == 1, "a same-filename re-upload went unreported"
        assert payload["pin_watch_hits"] == 1
        assert payload["candidates"] == 1, "a stale pin must reach the CI branch condition"
        hit = payload["llm_new"][0]
        assert hit["filename"] == SHIPPED_QWEN3_4B
        assert hit["stale_pin"] is True
        assert hit["upstream_oid"] == "f" * 64
        assert hit["pinned_sha256"] == scout.load_pinned_digests()[SHIPPED_QWEN3_4B]
        # The human digest must explain that this is freshness, not an outage.
        assert "Same filename, different bytes" in out.read_text(encoding="utf-8")

    def test_unchanged_oid_on_a_shipped_filename_stays_silent(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """The other half of the pair: no alert while the bytes still match."""
        rc, _ = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(
                {
                    QWEN3_4B_API: [
                        {
                            "path": SHIPPED_QWEN3_4B,
                            "size": 2_500_000_000,
                            "lfs": {"oid": scout.load_pinned_digests()[SHIPPED_QWEN3_4B]},
                        }
                    ]
                }
            ),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["stale_pin_hits"] == 0
        assert payload["candidates"] == 0, "an unchanged pin must not notify every fortnight"

    def test_missing_lfs_metadata_does_not_alert(self, scout, monkeypatch, tmp_path, capsys):
        """Nothing to compare -> stay quiet. An unactionable fortnightly alert
        trains the reader to ignore the issue, which is the failure mode the
        whole notify-threshold design exists to avoid."""
        rc, _ = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher({QWEN3_4B_API: [{"path": SHIPPED_QWEN3_4B, "size": 2_500_000_000}]}),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["candidates"] == 0
        assert payload["stale_pin_hits"] == 0

    def test_a_discovery_repo_never_raises_a_stale_pin_alert(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """We do not pin DISCOVERY repos, so their bytes moving is not our news.
        Only PIN_WATCH entries carry a pin worth comparing against."""
        rc, _ = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(
                {
                    PHI4_API: [
                        {
                            "path": SHIPPED_QWEN3_4B,  # a filename we ship...
                            "size": 2_500_000_000,
                            "lfs": {"oid": "e" * 64},  # ...with different bytes
                        }
                    ]
                }
            ),
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["candidates"] == 0
        assert payload["stale_pin_hits"] == 0


class TestPinVerificationIsHonest:
    """A watcher that reports success when it verified nothing is worse than none.

    `scout_llm` reaches "no alert" through four different routes: the oids
    matched, the tree was empty, the file carried no LFS metadata, or the source
    was unreachable. Only the first is evidence that our pin is still good. The
    digest used to print "Every shipped pin still matches the file we ship."
    for all four — including directly underneath an Errors section listing the
    sources it had just failed to reach.
    """

    CLAIM = "still matches the file we ship"

    def test_the_stub_set_covers_every_pin_watch_source(self, scout):
        """If a fourth PIN_WATCH source is added, fail here rather than let the
        all-verified test below quietly stop being all-verified."""
        watched = {src["api"] for src in scout.PIN_WATCH}
        assert watched == set(PIN_WATCH_API_FOR_FILE.values()), (
            "PIN_WATCH has changed; update PIN_WATCH_API_FOR_FILE so the "
            f"all-verified fixture still covers every source. Missing: "
            f"{sorted(watched - set(PIN_WATCH_API_FOR_FILE.values()))}"
        )

    def test_all_three_pins_verified_and_matching_reports_zero_unverifiable(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """The only state in which the 'every pin matches' claim is earned."""
        rc, out = _run_main(
            scout, monkeypatch, tmp_path, _fetcher(_all_pins_matching(scout))
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["pins_unverifiable"] == 0
        assert payload["pins_verified"] == 3, (
            "expected all three shipped pins compared against upstream"
        )
        assert payload["stale_pin_hits"] == 0
        assert payload["candidates"] == 0
        assert self.CLAIM in out.read_text(encoding="utf-8")

    def test_empty_tree_on_a_pin_watch_source_is_unverifiable_not_success(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        rc, out = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(_all_pins_matching(scout, omit=(SHIPPED_QWEN35_2B,))),
        )
        payload = json.loads(capsys.readouterr().out)
        digest = out.read_text(encoding="utf-8")
        assert rc == 0
        assert payload["pins_unverifiable"] == 1
        assert payload["pins_verified"] == 2
        assert self.CLAIM not in digest, "claimed every pin matches after checking two of three"
        assert SHIPPED_QWEN35_2B in digest
        # An unverifiable pin is a coverage gap, not actionable news.
        assert payload["candidates"] == 0

    def test_missing_lfs_metadata_is_unverifiable_not_success(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """Distinct from an empty tree: the file came back, just without an oid."""
        rc, out = _run_main(
            scout,
            monkeypatch,
            tmp_path,
            _fetcher(_all_pins_matching(scout, drop_lfs=(SHIPPED_GEMMA3_1B,))),
        )
        payload = json.loads(capsys.readouterr().out)
        digest = out.read_text(encoding="utf-8")
        assert rc == 0
        assert payload["pins_unverifiable"] == 1
        assert payload["pins_verified"] == 2
        assert self.CLAIM not in digest
        assert "no upstream LFS oid" in digest
        assert payload["candidates"] == 0

    def test_an_unreachable_pin_watch_source_is_unverifiable_not_success(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """The sharpest case: the Errors section and the success line used to
        print together, one directly above the other."""
        matching = _all_pins_matching(scout)

        def fetcher(url, timeout=20.0):
            if url == QWEN3_4B_API:
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)
            return matching.get(url, [])

        rc, out = _run_main(scout, monkeypatch, tmp_path, fetcher)
        payload = json.loads(capsys.readouterr().out)
        digest = out.read_text(encoding="utf-8")
        assert rc == 0
        assert payload["pins_unverifiable"] == 1
        assert payload["pins_verified"] == 2
        assert payload["errors"], "an unreachable source must still be reported"
        assert self.CLAIM not in digest, (
            "digest claimed every pin matches directly below its own Errors section"
        )
        assert "source unreachable" in digest
        assert payload["candidates"] == 0

    def test_every_unverifiable_path_keeps_candidates_at_zero(
        self, scout, monkeypatch, tmp_path, capsys
    ):
        """All three gaps at once: still a coverage report, still no issue."""
        matching = _all_pins_matching(
            scout, omit=(SHIPPED_QWEN35_2B,), drop_lfs=(SHIPPED_GEMMA3_1B,)
        )

        def fetcher(url, timeout=20.0):
            if url == QWEN3_4B_API:
                raise urllib.error.URLError("dns went away")
            return matching.get(url, [])

        rc, out = _run_main(scout, monkeypatch, tmp_path, fetcher)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["pins_verified"] == 0
        assert payload["pins_unverifiable"] == 3
        assert payload["candidates"] == 0, "a blind run must not open a GitHub issue"
        assert self.CLAIM not in out.read_text(encoding="utf-8")

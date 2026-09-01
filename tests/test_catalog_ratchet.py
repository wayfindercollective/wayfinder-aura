"""Ratchets on the shipped model catalogs.

The same models are described in two places — `wayfinder_main.WHISPER_CPP_MODELS`
/ `LLM_GGUF_MODELS` (the in-app catalog) and `wayfinder.core.setup.WHISPER_MODELS`
/ `LLM_MODELS` (first-run Setup). Since the 2026-08-17 audit both copies carry
pinned digests, exact byte counts and revision-pinned URLs, and downloads fail
closed when they disagree with the server. That makes silent drift between the
two copies a customer-visible outage, so it is pinned here rather than trusted
to review.

If one of these fails, fix the data — do not relax the test. A digest that no
longer matches the object on the server means the object changed, and the fix is
a new pin (or a new filename), never a weaker check.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Models we have published and then retired. Committed rather than derived from
# a git ref: these are historical facts, and a `git show main:...` lookup both
# rots (once `main` moves past the retirement it compares tombstones against
# themselves) and skips in a shallow/detached checkout. Append when a model is
# retired; never remove an entry — the tombstone is what retires the model on
# clients that already have it installed.
RETIRED_LLM_IDS = (
    "phi-3-mini",
    "qwen2.5-1.5b",
    "smollm2-360m",
    "llama3.2-1b",
    "lfm2.5-1.2b",
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _literal_catalogs(path: Path, names) -> dict:
    """Read module-level dict literals without importing (no tkinter needed)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    found[target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id in names and node.value is not None:
                found[node.target.id] = ast.literal_eval(node.value)
    missing = set(names) - set(found)
    assert not missing, f"catalog(s) not found in {path.name}: {sorted(missing)}"
    return found


@pytest.fixture(scope="module")
def app_catalogs():
    return _literal_catalogs(
        REPO_ROOT / "wayfinder_main.py", {"WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"}
    )


@pytest.fixture(scope="module")
def setup_catalogs():
    return _literal_catalogs(
        REPO_ROOT / "src/wayfinder/core/setup.py", {"WHISPER_MODELS", "LLM_MODELS"}
    )


def _app_entries(app_catalogs):
    for section in ("WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"):
        for model_id, info in app_catalogs[section].items():
            yield f"{section}:{model_id}", info


class TestShippedPins:
    def test_every_model_pins_a_digest(self, app_catalogs):
        for label, info in _app_entries(app_catalogs):
            digest = info.get("sha256")
            assert digest and SHA256_RE.match(digest), f"{label} has no valid sha256"

    def test_every_model_pins_an_exact_size(self, app_catalogs):
        """size_bytes is the pre-hash download bound, so it must be exact.

        A rounded value would either reject the real object or loosen the bound.
        """
        for label, info in _app_entries(app_catalogs):
            size = info.get("size_bytes")
            assert isinstance(size, int) and size > 0, f"{label} has no size_bytes"
            # Round numbers are the signature of a hand-typed estimate.
            assert size % 1_000_000 != 0, (
                f"{label} size_bytes={size} looks rounded, not measured"
            )

    def test_no_url_points_at_a_mutable_ref(self, app_catalogs, setup_catalogs):
        """`main` moves under us; every Hugging Face URL must pin a revision."""
        urls = [info.get("url", "") for _, info in _app_entries(app_catalogs)]
        for section in ("WHISPER_MODELS", "LLM_MODELS"):
            urls += [i.get("url", "") for i in setup_catalogs[section].values()]
        urls.append(
            (REPO_ROOT / "src/wayfinder/core/setup.py").read_text(encoding="utf-8")
        )
        for url in urls:
            assert "/resolve/main/" not in url, f"mutable HF ref in {url[:80]}"

    def test_display_label_matches_the_exact_size(self, app_catalogs):
        """Labels are decimal units derived from the real byte count."""
        for label, info in _app_entries(app_catalogs):
            size = info["size_bytes"]
            expected = (
                f"{size / 1_000_000_000:.1f} GB"
                if size >= 1_000_000_000
                else f"{size / 1_000_000:.0f} MB"
            )
            assert info.get("size") == expected, (
                f"{label}: label {info.get('size')!r} != {expected!r} for {size} bytes"
            )


class TestCatalogCopiesAgree:
    """Setup's catalog and the in-app catalog describe the same files."""

    def test_whisper_digests_and_sizes_match(self, app_catalogs, setup_catalogs):
        app_by_filename = {
            info["filename"]: info
            for info in app_catalogs["WHISPER_CPP_MODELS"].values()
        }
        for model_id, setup_info in setup_catalogs["WHISPER_MODELS"].items():
            filename = f"ggml-{model_id}.bin"
            app_info = app_by_filename.get(filename)
            # Setup offers a subset, but every model it offers must exist in the
            # app catalog — silently skipping a missing counterpart would let the
            # two copies drift apart unnoticed, which is what this ratchet is for.
            assert app_info is not None, (
                f"setup.py offers {filename} but the app catalog does not ship it"
            )
            assert setup_info.get("sha256") == app_info.get("sha256"), (
                f"{filename}: setup.py and wayfinder_main.py disagree on the digest"
            )
            assert setup_info.get("bytes") == app_info.get("size_bytes"), (
                f"{filename}: setup.py and wayfinder_main.py disagree on the size"
            )

    def test_llm_digests_and_sizes_match(self, app_catalogs, setup_catalogs):
        app_by_filename = {
            info["filename"]: info for info in app_catalogs["LLM_GGUF_MODELS"].values()
        }
        for _key, setup_info in setup_catalogs["LLM_MODELS"].items():
            app_info = app_by_filename.get(setup_info.get("filename"))
            assert app_info is not None, (
                f"setup.py offers {setup_info.get('filename')} "
                "but the app catalog does not ship it"
            )
            assert setup_info.get("sha256") == app_info.get("sha256"), (
                f"{setup_info['filename']}: digest disagrees between catalogs"
            )
            assert setup_info.get("bytes") == app_info.get("size_bytes"), (
                f"{setup_info['filename']}: size disagrees between catalogs"
            )

    def test_llm_catalogs_are_an_exact_set_in_both_directions(self, app_catalogs, setup_catalogs):
        """Reverse of `test_llm_digests_and_sizes_match` — Setup must not lose a model.

        That test iterates Setup -> app only, so deleting an entry from
        `setup.LLM_MODELS` just runs fewer iterations and passes vacuously: an
        active LLM could silently vanish from the installer. This closes the
        loop. LLM only — `WHISPER_MODELS` is deliberately asymmetric (Setup
        offers a subset), so the same check there would fail on correct data.
        """
        app_files = {info["filename"] for info in app_catalogs["LLM_GGUF_MODELS"].values()}
        setup_files = {
            info["filename"] for info in setup_catalogs["LLM_MODELS"].values() if info.get("filename")
        }
        assert app_files == setup_files, (
            "setup.LLM_MODELS and LLM_GGUF_MODELS have drifted apart: "
            f"only in the app catalog={sorted(app_files - setup_files)}, "
            f"only in setup.py={sorted(setup_files - app_files)}"
        )

    def test_setup_entries_carry_digest_and_size(self, setup_catalogs):
        for section in ("WHISPER_MODELS", "LLM_MODELS"):
            for key, info in setup_catalogs[section].items():
                digest = info.get("sha256")
                assert digest and SHA256_RE.match(digest), f"{section}:{key} unpinned"
                assert isinstance(info.get("bytes"), int), f"{section}:{key} has no bytes"

    def test_setup_labels_match_their_byte_counts(self, setup_catalogs):
        """Setup's labels are derived from bytes too, not typed by hand."""
        for section in ("WHISPER_MODELS", "LLM_MODELS"):
            for key, info in setup_catalogs[section].items():
                size = info["bytes"]
                expected = (
                    f"{size / 1_000_000_000:.1f} GB"
                    if size >= 1_000_000_000
                    else f"{size / 1_000_000:.0f} MB"
                )
                assert info.get("size") == expected, (
                    f"{section}:{key}: label {info.get('size')!r} != {expected!r}"
                )


class TestCrossSectionFilenames:
    def test_no_filename_is_shared_between_the_two_catalogs(self, app_catalogs):
        """Digest trust is keyed by filename app-wide, so it must be unique app-wide.

        A filename appearing in both catalogs would let an entry in one section
        claim the other section's shipped pin.
        """
        whisper = {i["filename"] for i in app_catalogs["WHISPER_CPP_MODELS"].values()}
        llm = {i["filename"] for i in app_catalogs["LLM_GGUF_MODELS"].values()}
        assert not (whisper & llm), f"filenames shared across catalogs: {whisper & llm}"


class TestFilenamesAreUnambiguous:
    def test_no_two_models_share_a_filename(self, app_catalogs):
        """The digest pin is keyed by filename, so filenames must identify one model."""
        for section in ("WHISPER_CPP_MODELS", "LLM_GGUF_MODELS"):
            seen: dict[str, str] = {}
            for model_id, info in app_catalogs[section].items():
                filename = info["filename"]
                assert filename not in seen, (
                    f"{section}: {model_id} and {seen[filename]} both use {filename}"
                )
                seen[filename] = model_id


def _scout_pin_watch() -> list[dict]:
    """Read `PIN_WATCH` out of scripts/model_scout.py without importing it.

    Read by AST rather than import for the same reason the catalogs are: this
    file must stay headless and offline. `name_re` is a `re.compile(...)` call,
    not a literal, so the pattern string is pulled out of the call node and
    compiled here.
    """
    source = (REPO_ROOT / "scripts/model_scout.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PIN_WATCH" for t in stmt.targets
        ):
            node = stmt.value
    assert node is not None, "PIN_WATCH not found in scripts/model_scout.py"

    entries = []
    for element in node.elts:
        entry = {}
        for key_node, value_node in zip(element.keys, element.values):
            key = key_node.value
            if key == "name_re":
                # re.compile(r"...", re.I) -> take the pattern argument
                entry[key] = re.compile(value_node.args[0].value, re.I)
            else:
                entry[key] = ast.literal_eval(value_node)
        entries.append(entry)
    return entries


def _repo_from_pinned_url(url: str) -> str:
    """`https://huggingface.co/<owner>/<repo>/resolve/<rev>/<file>` -> `<owner>/<repo>`.

    The download URL is the one place the upstream repository is stated as a
    fact rather than retyped, so it is the right thing to ratchet `repo_id`
    against — see `_monitored()` callers below.
    """
    match = re.match(r"https://huggingface\.co/([^/]+/[^/]+)/resolve/", url or "")
    assert match, f"not a pinned HuggingFace download URL: {url!r}"
    return match.group(1)


def _published_catalog() -> dict:
    import json

    return json.loads((REPO_ROOT / "catalog/v1.json").read_text(encoding="utf-8"))


class TestMonitoringMirrorsTheCatalog:
    """Everything that watches the lineup must watch exactly the shipped lineup.

    Ten hand-maintained mirrors of the same three models drifted apart in
    2026-07/08 because nothing enforced agreement: `catalog/v1.json` shipped a
    `qwen3.5-2b` row with `size_bytes: 1390000000` against a real object of
    1280835840 bytes, which `merge_section()` silently rejected as "weakens its
    size" — a published row that no client ever applied. These ratchets exist so
    that class of silent failure cannot come back.

    If one fails, fix the data — never relax the test.
    """

    # ---- model_updates.MONITORED_MODELS <-> LLM_GGUF_MODELS -------------

    def test_every_shipped_llm_is_monitored(self, app_catalogs):
        monitored = _literal_catalogs(
            REPO_ROOT / "src/wayfinder/core/model_updates.py", {"MONITORED_MODELS"}
        )["MONITORED_MODELS"]
        watched = {
            info.get("current_filename")
            for info in monitored.values()
            if info.get("category") == "llm"
        }
        for model_id, info in app_catalogs["LLM_GGUF_MODELS"].items():
            assert info["filename"] in watched, (
                f"{model_id} ships {info['filename']} but MONITORED_MODELS does not "
                "watch it — a new upstream revision would go unnoticed"
            )

    def test_no_monitored_llm_is_unshipped(self, app_catalogs):
        """A retired model must leave MONITORED_MODELS too.

        Watching a model we no longer offer spends a HuggingFace API call per
        startup check to surface an update nobody can act on.
        """
        monitored = _literal_catalogs(
            REPO_ROOT / "src/wayfinder/core/model_updates.py", {"MONITORED_MODELS"}
        )["MONITORED_MODELS"]
        shipped = {i["filename"] for i in app_catalogs["LLM_GGUF_MODELS"].values()}
        for key, info in monitored.items():
            if info.get("category") != "llm":
                continue
            assert info.get("current_filename") in shipped, (
                f"MONITORED_MODELS[{key!r}] watches {info.get('current_filename')!r}, "
                "which the app catalog no longer ships"
            )

    def test_every_monitored_repo_id_matches_the_pinned_download_url(self, app_catalogs):
        """`repo_id` must name the repo we actually download from.

        The playbook says site 6 is ratcheted on "`current_filename` / `repo_id`",
        but only `current_filename` ever was: a typo'd or stale `repo_id` left
        every suite green while `check_for_updates()` silently polled the wrong
        repository forever — the update checker's single job, failing open.

        The pinned URL in the app catalog is the source of truth: it is the repo
        we really fetch bytes from, so it cannot drift from reality the way a
        hand-retyped `repo_id` can.
        """
        monitored = _literal_catalogs(
            REPO_ROOT / "src/wayfinder/core/model_updates.py", {"MONITORED_MODELS"}
        )["MONITORED_MODELS"]
        by_filename = {i["filename"]: i for i in app_catalogs["LLM_GGUF_MODELS"].values()}

        checked = 0
        for key, info in monitored.items():
            if info.get("category") == "llm":
                shipped = by_filename.get(info.get("current_filename"))
                assert shipped is not None, (
                    f"MONITORED_MODELS[{key!r}] watches "
                    f"{info.get('current_filename')!r}, which is not shipped"
                )
                expected = _repo_from_pinned_url(shipped["url"])
            elif info.get("category") == "whisper":
                # One shared repo for the whole whisper section; assert it is
                # genuinely uniform rather than trusting the first row.
                repos = {
                    _repo_from_pinned_url(w["url"])
                    for w in app_catalogs["WHISPER_CPP_MODELS"].values()
                }
                assert len(repos) == 1, (
                    f"whisper models now span several repos ({sorted(repos)}); "
                    "MONITORED_MODELS needs one entry per repo"
                )
                expected = repos.pop()
            else:
                continue
            checked += 1
            assert info.get("repo_id") == expected, (
                f"MONITORED_MODELS[{key!r}].repo_id={info.get('repo_id')!r} but the "
                f"pinned download URL says {expected!r} — the update checker would "
                "poll the wrong repository"
            )
        assert checked == len(monitored), (
            f"only {checked} of {len(monitored)} MONITORED_MODELS entries were "
            "checked; an unknown `category` would slip through unratcheted"
        )

    # ---- model_scout.PIN_WATCH <-> LLM_GGUF_MODELS ----------------------

    def test_every_shipped_llm_has_exactly_one_pin_watch(self, app_catalogs):
        pin_watch = _scout_pin_watch()
        for model_id, info in app_catalogs["LLM_GGUF_MODELS"].items():
            matches = [s for s in pin_watch if s["name_re"].search(info["filename"])]
            assert len(matches) == 1, (
                f"{model_id} ({info['filename']}) is matched by {len(matches)} "
                "PIN_WATCH entries in scripts/model_scout.py; expected exactly 1"
            )

    def test_no_pin_watch_entry_is_orphaned(self, app_catalogs):
        """DISCOVERY entries are excluded — they are candidates, not shipped."""
        pin_watch = _scout_pin_watch()
        filenames = [i["filename"] for i in app_catalogs["LLM_GGUF_MODELS"].values()]
        for src in pin_watch:
            assert src.get("group") == "pin_watch", (
                f"{src['label']!r} is in PIN_WATCH but not tagged group='pin_watch'"
            )
            assert any(src["name_re"].search(f) for f in filenames), (
                f"PIN_WATCH entry {src['label']!r} matches no shipped model — "
                "move it to DISCOVERY or delete it"
            )

    # ---- catalog/v1.json <-> LLM_GGUF_MODELS ---------------------------

    def test_published_llm_rows_match_the_app_catalog(self, app_catalogs):
        shipped = app_catalogs["LLM_GGUF_MODELS"]
        for model_id, row in _published_catalog()["llm"].items():
            if row.get("disabled") is True:
                continue  # retired: intentionally absent from the app catalog
            assert model_id in shipped, (
                f"catalog/v1.json publishes {model_id!r}, which the app does not ship. "
                "Retire it with {'disabled': true} instead of leaving a live row."
            )
            app = shipped[model_id]
            for field in ("filename", "sha256", "size_bytes"):
                assert row.get(field) == app.get(field), (
                    f"catalog/v1.json {model_id}.{field}={row.get(field)!r} != "
                    f"app catalog {app.get(field)!r}. merge_section() rejects rows "
                    "that weaken a shipped digest/size, so this row would be a "
                    "silent no-op on every client."
                )
            assert row.get("requires_feature") == app.get("requires_feature"), (
                f"catalog/v1.json {model_id} disagrees on requires_feature — "
                "an entitlement mismatch is rejected by merge_section()"
            )

    def test_live_published_llm_ids_are_exactly_the_shipped_ids(self, app_catalogs):
        """Reverse of `test_published_llm_rows_match_the_app_catalog`.

        That test walks published -> app, so *omitting* a live row just runs
        fewer iterations and passes vacuously. Remote data overlays a copy of
        the built-in catalog, so an omitted live row remains available with its
        built-in metadata; the defect is silent drift and a missing remote
        overlay, not model invisibility. This closes the loop, so the two id
        sets must match exactly in both directions.

        Retired rows are excluded by design — they stay as `disabled: true`
        tombstones and are covered by `test_retired_rows_are_disabled_not_deleted`.
        """
        live_published = {
            model_id
            for model_id, row in _published_catalog()["llm"].items()
            if row.get("disabled") is not True
        }
        shipped = set(app_catalogs["LLM_GGUF_MODELS"])
        assert live_published == shipped, (
            "catalog/v1.json and LLM_GGUF_MODELS disagree about what ships: "
            f"shipped but not published live={sorted(shipped - live_published)}, "
            f"published live but not shipped={sorted(live_published - shipped)}"
        )

    def test_no_published_url_points_at_a_mutable_ref(self):
        """`main` moves; a published URL must pin a revision, like the app's."""
        doc = _published_catalog()
        for section in ("whisper", "llm"):
            for model_id, row in (doc.get(section) or {}).items():
                url = row.get("url", "")
                assert "/resolve/main/" not in url, (
                    f"catalog/v1.json {section}:{model_id} publishes a mutable ref: {url}"
                )

    def test_retired_rows_are_disabled_not_deleted(self, app_catalogs):
        """Every model we ever published must still be addressable.

        `merge_section()` only *removes* an id from a client's catalog when the
        published document says `disabled: true`. Dropping the key instead
        leaves old installs offering the retired model forever.

        The expected ids are committed here on purpose. An earlier version of
        this test read them from `git show main:catalog/v1.json` and skipped
        when that failed — but `main` is a mutable local ref, so once it moved
        past this change the test would compare the tombstones against
        themselves (vacuously green), and in a shallow or detached checkout it
        skipped outright. A frozen list cannot rot that way: deleting a
        tombstone fails, and adding a retirement forces an explicit edit here.
        """
        published = _published_catalog()["llm"]
        for model_id in RETIRED_LLM_IDS:
            assert model_id in published, (
                f"{model_id!r} was published and has been deleted from "
                "catalog/v1.json. Set {'disabled': true} instead — a deleted key "
                "leaves already-installed clients still offering it."
            )
            assert published[model_id].get("disabled") is True, (
                f"{model_id!r} is retired but its row is not marked "
                "`disabled: true`, so merge_section() will never retire it on "
                "an installed client."
            )
            assert model_id not in app_catalogs["LLM_GGUF_MODELS"], (
                f"{model_id!r} is tombstoned in catalog/v1.json but still live "
                "in LLM_GGUF_MODELS — the two disagree about whether it ships."
            )

    def test_every_live_published_llm_is_not_tombstoned(self):
        """The inverse guard: a shipped model must never carry `disabled`."""
        published = _published_catalog()["llm"]
        for model_id, row in published.items():
            if model_id in RETIRED_LLM_IDS:
                continue
            assert not row.get("disabled"), (
                f"{model_id!r} is not in RETIRED_LLM_IDS but is marked disabled; "
                "add it to RETIRED_LLM_IDS or un-retire the row."
            )


# Ollama serves the same models under its own tags, so the recommendation API
# cannot be compared to the catalog without this bridge. Kept explicit (and
# completeness-checked below) rather than derived: the tags are upstream naming
# choices, not something we can compute from a filename.
OLLAMA_TAG_FOR_MODEL_ID = {
    "gemma3-1b": "gemma3:1b",
    "qwen3.5-2b": "qwen3.5:2b",
    "qwen3-4b-2507": "qwen3:4b",
}

# Ollama tags for models we retired in the 2026-09 refresh. They may still be
# *mentioned* (they keep working via Ollama, and old installs still run them),
# but none of them may carry a ⭐ — a star is a current recommendation.
RETIRED_OLLAMA_TAGS = frozenset(
    {"phi3:mini", "qwen2.5:1.5b", "smollm2:360m", "llama3.2:1b", "lfm2.5:1.2b"}
)

# Tags a recommendation surface may name that are NOT rows in our own catalog —
# Ollama-only or cloud-only models we never ship as a GGUF. Deliberately empty:
# every tag we currently recommend is a model we ship, and adding one here must
# be a conscious edit rather than a silent pass.
OLLAMA_ONLY_RECOMMENDATION_ALLOWLIST: frozenset[str] = frozenset()

# The five tones `get_model_recommendation_for_style` branches on. `personal`
# is also the `else` fallback, so this list covers every branch.
POST_PROCESSING_STYLES = ("minimal", "professional", "casual", "dev", "personal")

# Every branch of `get_upgrade_suggestion_for_intensity`; "light" is its `else`.
POST_PROCESSING_INTENSITIES = ("caricature", "strong", "standard", "light")


def _recommended_model_id(app_catalogs: dict) -> str:
    """The single catalog row flagged `recommended`."""
    flagged = [
        model_id
        for model_id, info in app_catalogs["LLM_GGUF_MODELS"].items()
        if info.get("recommended")
    ]
    assert len(flagged) == 1, f"expected exactly one recommended LLM, found {flagged}"
    return flagged[0]


def _model_recommendations_text() -> str:
    """Read components.MODEL_RECOMMENDATIONS as source text.

    Read, not imported: `wayfinder.ui.components` pulls in customtkinter, and
    this suite must stay runnable headless.
    """
    source = (REPO_ROOT / "src" / "wayfinder" / "ui" / "components.py").read_text(encoding="utf-8")
    match = re.search(r'MODEL_RECOMMENDATIONS\s*=\s*"""(.*?)"""', source, re.S)
    assert match, "MODEL_RECOMMENDATIONS block not found in components.py"
    return match.group(1)


class TestRecommendationsMatchTheLineup:
    """Sync points 3 and 5 — the two the ratchet used to miss.

    `AGENTS.md` claims every sync point a model swap touches is ratcheted here.
    Before this class that was false for `config._LLM_PREFERENCE` and for the
    recommendation surfaces, so a refresh could retire a model and still leave
    the app recommending it.
    """

    # ---- sync point 5: the recommendation API + the UI string --------------

    def test_tag_map_covers_every_shipped_model(self, app_catalogs):
        assert set(OLLAMA_TAG_FOR_MODEL_ID) == set(app_catalogs["LLM_GGUF_MODELS"]), (
            "OLLAMA_TAG_FOR_MODEL_ID has drifted from LLM_GGUF_MODELS; add the "
            "new model's Ollama tag so the recommendation ratchet can see it"
        )

    def test_recommended_tier_leads_with_the_catalogs_recommended_model(self, app_catalogs):
        from wayfinder.core.postprocessor import get_recommended_models

        expected = OLLAMA_TAG_FOR_MODEL_ID[_recommended_model_id(app_catalogs)]
        tiers = {t["tier"]: t for t in get_recommended_models()}
        lead = tiers["recommended"]["models"][0]["name"]
        assert lead == expected, (
            f"get_recommended_models() leads with {lead!r} but the catalog flags "
            f"{expected!r} as `recommended`. Two surfaces disagree about the default."
        )

    def test_no_starred_recommendation_names_a_retired_model(self):
        from wayfinder.core.postprocessor import get_recommended_models

        for tier in get_recommended_models():
            for model in tier["models"]:
                if "⭐" in model["description"]:
                    assert model["name"] not in RETIRED_OLLAMA_TAGS, (
                        f"{model['name']!r} is retired but still starred in tier "
                        f"{tier['tier']!r}"
                    )

    def test_ui_string_and_api_agree_on_best_overall(self, app_catalogs):
        from wayfinder.core.postprocessor import get_recommended_models

        text = _model_recommendations_text()
        starred = [ln for ln in text.splitlines() if "⭐ Best overall" in ln]
        assert len(starred) == 1, f"expected one '⭐ Best overall' line, got {starred}"
        ui_tag = starred[0].split("•", 1)[1].split("—")[0].strip()
        api_tag = {t["tier"]: t for t in get_recommended_models()}["recommended"]["models"][0]["name"]
        expected = OLLAMA_TAG_FOR_MODEL_ID[_recommended_model_id(app_catalogs)]
        assert ui_tag == api_tag == expected, (
            f"'best overall' disagrees across surfaces: UI={ui_tag!r}, "
            f"get_recommended_models()={api_tag!r}, catalog={expected!r}"
        )

    def test_ui_string_stars_no_retired_model(self):
        for line in _model_recommendations_text().splitlines():
            if "⭐" not in line or "•" not in line:
                continue
            tag = line.split("•", 1)[1].split("—")[0].strip()
            assert tag not in RETIRED_OLLAMA_TAGS, (
                f"components.MODEL_RECOMMENDATIONS still stars retired {tag!r}"
            )

    def test_upgrade_suggestions_only_name_shipped_gguf_files(self, app_catalogs):
        """`get_upgrade_suggestion_for_intensity` must never offer a retired file.

        This surface was previously unratcheted: it hands the user a concrete
        `.gguf` filename to go and get, so naming a retired model sends them
        after bytes we no longer publish.
        """
        from wayfinder.core.postprocessor import get_upgrade_suggestion_for_intensity

        shipped = {info["filename"] for info in app_catalogs["LLM_GGUF_MODELS"].values()}
        seen_gguf = 0
        for intensity in POST_PROCESSING_INTENSITIES:
            result = get_upgrade_suggestion_for_intensity(intensity)
            for entry in result["recommended_models"]:
                if entry.get("type") != "gguf":
                    continue
                seen_gguf += 1
                # The key is `name` — there is no `filename` key on these
                # entries. Do not add one to production to make this read nicer.
                assert entry["name"] in shipped, (
                    f"intensity {intensity!r} suggests {entry['name']!r}, which is "
                    "not in LLM_GGUF_MODELS — a retired or unshipped model"
                )
        assert seen_gguf, "no GGUF suggestions inspected; the ratchet would be vacuous"

    def test_style_recommendations_only_name_live_models(self):
        """Every ⭐ `recommended` tag from the style API must be a model we ship."""
        from wayfinder.core.postprocessor import get_model_recommendation_for_style

        live_tags = set(OLLAMA_TAG_FOR_MODEL_ID.values()) | OLLAMA_ONLY_RECOMMENDATION_ALLOWLIST
        for style in POST_PROCESSING_STYLES:
            for strong_mode in (False, True):
                result = get_model_recommendation_for_style(style, strong_mode=strong_mode)
                recommended = result["recommended"]
                assert recommended, (
                    f"style={style!r} strong={strong_mode} recommends nothing"
                )
                for tag in recommended:
                    assert tag in live_tags, (
                        f"style={style!r} strong={strong_mode} recommends {tag!r}, "
                        "which maps to no shipped catalog model"
                    )

    def test_style_also_works_never_names_a_retired_model(self):
        """`also_works` is still a recommendation — it may not list a retired model.

        Checked by exact `RETIRED_OLLAMA_TAGS` membership, deliberately NOT via
        `OLLAMA_TAG_FOR_MODEL_ID` (retired tags are absent from that map, so the
        check would be vacuous) and NOT by substring or family prefix
        (`llama3.2:3b` and `qwen2.5:3b` are legitimate Ollama-only 3B
        alternatives, not the retired 1B/1.5B GGUF entries).

        `avoid` is deliberately NOT checked: listing `phi3:mini` there is a
        warning *against* a retired model and must survive.
        """
        from wayfinder.core.postprocessor import get_model_recommendation_for_style

        for style in POST_PROCESSING_STYLES:
            for strong_mode in (False, True):
                result = get_model_recommendation_for_style(style, strong_mode=strong_mode)
                for tag in result["also_works"]:
                    assert tag not in RETIRED_OLLAMA_TAGS, (
                        f"style={style!r} strong={strong_mode} still offers retired "
                        f"{tag!r} under `also_works`"
                    )

    # ---- sync point 5: coverage, not just correctness ----------------------
    #
    # The checks above are one-directional, but NOT uniformly so — the style
    # surface has to be stated per field, or the claim overstates one of them.
    # `recommended` may not be empty and every tag in it must map to a live
    # shipped model, so it rejects retired *and* unshipped names alike.
    # `also_works` is checked against `RETIRED_OLLAMA_TAGS` only, so an
    # unshipped Ollama-only tag passes there deliberately — `qwen3.5:4b`,
    # `qwen2.5:3b` and `llama3.2:3b` are live entries that ship no GGUF.
    # What no check notices is a shipped model dropping out of the *union*:
    # replacing `qwen3:4b` in the strong branch with the live `qwen3.5:2b`
    # leaves the whole suite green. Also verified by
    # deleting Qwen3 4B from `get_recommended_models()`, from both heavy
    # branches of `get_upgrade_suggestion_for_intensity()`, and from the
    # `MODEL_RECOMMENDATIONS` string: the full suite stayed green all three
    # times, while the docs claimed "removing a shipped model from any of the
    # seven fails CI". These three close that gap for the surfaces where
    # coverage is actually the right invariant.
    #
    # `get_model_recommendation_for_style()` deliberately gets no such guard:
    # it picks one model per tone, and `gemma3-1b` is intentionally named by no
    # style. Requiring coverage there would force a fake recommendation.

    def test_recommended_models_lists_every_shipped_model(self, app_catalogs):
        """Every model we ship must appear somewhere in the tier list.

        Deliberately a subset check, not set equality: the `budget` and `cloud`
        tiers legitimately name models we do not ship as a GGUF (`smollm2:360m`
        through Ollama, `gpt-4o-mini`), and
        `test_no_starred_recommendation_names_a_retired_model` already stops a
        retired one from carrying a star.
        """
        from wayfinder.core.postprocessor import get_recommended_models

        listed = {
            model["name"]
            for tier in get_recommended_models()
            for model in tier["models"]
        }
        for model_id in app_catalogs["LLM_GGUF_MODELS"]:
            tag = OLLAMA_TAG_FOR_MODEL_ID[model_id]
            assert tag in listed, (
                f"{model_id!r} ships but get_recommended_models() never lists "
                f"{tag!r} — we would ship a model the API never recommends"
            )

    def test_ui_string_lists_every_shipped_model(self, app_catalogs):
        """`components.MODEL_RECOMMENDATIONS` must name every shipped model.

        Same gap as above on the surface the user actually reads: the string is
        the in-app guidance, so a shipped model missing from it is invisible
        guidance-wise even though the downloader offers it.
        """
        listed = set()
        for line in _model_recommendations_text().splitlines():
            if "•" not in line:
                continue
            listed.add(line.split("•", 1)[1].split("—")[0].strip())
        for model_id in app_catalogs["LLM_GGUF_MODELS"]:
            tag = OLLAMA_TAG_FOR_MODEL_ID[model_id]
            assert tag in listed, (
                f"{model_id!r} ships but components.MODEL_RECOMMENDATIONS never "
                f"names {tag!r}; the in-app guidance omits a shipped model"
            )

    def test_upgrade_suggestions_cover_every_shipped_gguf(self, app_catalogs):
        """Every shipped GGUF must be suggested by at least one intensity.

        Checked as a union across intensities rather than per intensity on
        purpose: `light` should not offer the 4B and `caricature` should not
        offer the 1B. What must not happen is a shipped model that *no*
        intensity points at. The non-empty assert is separate — an intensity
        that suggests nothing renders an empty upgrade prompt.
        """
        from wayfinder.core.postprocessor import get_upgrade_suggestion_for_intensity

        suggested: set[str] = set()
        for intensity in POST_PROCESSING_INTENSITIES:
            entries = get_upgrade_suggestion_for_intensity(intensity)["recommended_models"]
            assert entries, (
                f"intensity {intensity!r} suggests no model at all; the upgrade "
                "prompt would render an empty list"
            )
            suggested.update(e["name"] for e in entries if e.get("type") == "gguf")
        for model_id, info in app_catalogs["LLM_GGUF_MODELS"].items():
            assert info["filename"] in suggested, (
                f"{model_id!r} ships but no intensity suggests "
                f"{info['filename']!r} — downloadable, yet recommended nowhere"
            )

    # ---- sync point 3: config._LLM_PREFERENCE ------------------------------

    def test_llm_preference_leads_with_the_recommended_model(self, app_catalogs):
        from wayfinder.config import _LLM_PREFERENCE

        expected = app_catalogs["LLM_GGUF_MODELS"][_recommended_model_id(app_catalogs)]["filename"]
        assert _LLM_PREFERENCE[0] == expected, (
            f"_pick_llm() defaults to {_LLM_PREFERENCE[0]!r} but the catalog "
            f"recommends {expected!r}"
        )

    def test_llm_preference_lists_every_shipped_model(self, app_catalogs):
        from wayfinder.config import _LLM_PREFERENCE

        for model_id, info in app_catalogs["LLM_GGUF_MODELS"].items():
            assert info["filename"] in _LLM_PREFERENCE, (
                f"{model_id!r} ships but _pick_llm() would never select it"
            )

    def test_llm_preference_keeps_its_legacy_tail(self):
        """Trimming the tail orphans an already-downloaded retired model: the
        file is still on disk and still loadable, but _pick_llm stops finding it.
        """
        from wayfinder.config import _LLM_PREFERENCE

        assert "qwen2.5-1.5b-instruct-q4_k_m.gguf" in _LLM_PREFERENCE, (
            "the legacy tail of _LLM_PREFERENCE no longer contains the exact "
            "filename 'qwen2.5-1.5b-instruct-q4_k_m.gguf'; retired models must "
            "stay selectable from disk, and only the byte-exact on-disk name is "
            "loadable — a near-miss like 'qwen2.5-1.5b-something-else.gguf' "
            "would orphan the downloaded file"
        )


class TestDocsMatchTheRatchet:
    """The sync-point docs are a checklist people follow during a refresh.

    A wrong count or a citation of a test class that no longer exists makes the
    checklist worse than none, because it reads as authoritative. Review found
    `AGENTS.md` claiming "five data sites plus two watchers ... every one
    ratcheted" while the table listed four and two, and two of them had no
    ratchet at all. These tests keep the claim honest.
    """

    @staticmethod
    def _playbook() -> str:
        return (REPO_ROOT / "docs" / "MODEL-REFRESH-PLAYBOOK.md").read_text(encoding="utf-8")

    def test_every_ratchet_class_cited_by_the_playbook_exists(self):
        source = (REPO_ROOT / "tests" / "test_catalog_ratchet.py").read_text(encoding="utf-8")
        cited = set(re.findall(r"`(Test[A-Za-z0-9_]+)`", self._playbook()))
        assert cited, "playbook no longer attributes sync points to ratchet classes"
        for name in sorted(cited):
            assert re.search(rf"^class {name}\b", source, re.M), (
                f"docs/MODEL-REFRESH-PLAYBOOK.md cites {name!r}, which does not "
                "exist in tests/test_catalog_ratchet.py"
            )

    def test_playbook_enumerates_five_data_sites_and_two_watchers(self):
        doc = self._playbook()
        data = re.search(r"\*\*Five data sites:\*\*(.+?)\*\*Two watchers:\*\*", doc, re.S)
        assert data, "playbook no longer has the 'Five data sites' table"
        watchers = doc[data.end() - len("**Two watchers:**"):]
        # Table body rows look like: | 3 | `site` | what changes | `TestX` |
        data_rows = re.findall(r"^\| \d+ \|", data.group(1), re.M)
        watcher_rows = re.findall(r"^\| \d+ \|", watchers.split("\n\n\n")[0], re.M)
        assert len(data_rows) == 5, f"'five data sites' but {len(data_rows)} rows listed"
        assert len(watcher_rows) == 2, f"'two watchers' but {len(watcher_rows)} rows listed"

    def test_site_five_names_every_ratcheted_recommendation_surface(self):
        """The claim that broke last review: site 5 said "the recommendation
        surfaces" while naming only two of the four, and the other two were
        genuinely unratcheted. Now that all four are covered, the row has to
        name all four — otherwise the next person reads a half-truth as a
        checklist and skips the surfaces it forgot to mention.
        """
        row = [
            ln
            for ln in self._playbook().splitlines()
            if ln.startswith("| 5 |")
        ]
        assert len(row) == 1, f"expected exactly one site-5 row, got {len(row)}"
        for surface in (
            "get_recommended_models",
            "MODEL_RECOMMENDATIONS",
            "get_upgrade_suggestion_for_intensity",
            "get_model_recommendation_for_style",
        ):
            assert surface in row[0], (
                f"playbook site 5 does not name {surface!r}, but "
                "TestRecommendationsMatchTheLineup ratchets it"
            )

    def test_rows_four_and_six_state_their_actual_coverage(self):
        """Rows 4 and 6 each claimed more than the tests delivered.

        Row 6 said "swap the `current_filename` / `repo_id`" while `repo_id` was
        never validated; row 4 read as full agreement while only published -> app
        was checked. Both are real ratchets now, so the rows must say what is
        actually enforced — this test is what stops the claim drifting back
        ahead of the coverage a fourth time.
        """
        rows = {
            line.split("|")[1].strip(): line
            for line in self._playbook().splitlines()
            if line.startswith("| ") and line.split("|")[1].strip().isdigit()
        }
        assert "4" in rows and "6" in rows, f"missing rows 4/6; found {sorted(rows)}"
        assert "both ways" in rows["4"], (
            "playbook row 4 no longer states that catalog/v1.json live ids are "
            "checked both ways, but "
            "test_live_published_llm_ids_are_exactly_the_shipped_ids enforces it"
        )
        assert "repo_id" in rows["6"] and "pinned download URL" in rows["6"], (
            "playbook row 6 no longer states how `repo_id` is ratcheted, but "
            "test_every_monitored_repo_id_matches_the_pinned_download_url enforces it"
        )

    def test_agents_md_matches_the_playbook_count(self):
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        assert "five data sites plus two watchers" in agents, (
            "AGENTS.md sync-point count changed; keep it in step with the "
            "playbook table (five data sites + two watchers)"
        )

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

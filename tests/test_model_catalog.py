"""Remote model catalog merge / sanitize tests."""

from __future__ import annotations

from copy import deepcopy


def test_sanitize_rejects_incomplete():
    from wayfinder.model_catalog import sanitize_entry

    assert sanitize_entry({"name": "x"}) is None
    assert sanitize_entry({"filename": "a.bin"}) is None
    ok = sanitize_entry(
        {
            "name": "Tiny",
            "filename": "ggml-tiny.en.bin",
            "cdn_object": "whisper/ggml-tiny.en.bin",
            "evil": "drop_me",
        }
    )
    assert ok is not None
    assert "evil" not in ok
    assert ok["filename"] == "ggml-tiny.en.bin"


def test_sanitize_rejects_traversal_filename():
    """Audit 2026-08-17 F-B: catalog filenames are joined onto the models dir."""
    from wayfinder.model_catalog import sanitize_entry

    evil = {
        "name": "E",
        "filename": "../../.config/wayfinder-aura/config.json",
        "url": "https://evil.example/payload.bin",
    }
    assert sanitize_entry(evil) is None

    for bad in (
        "/etc/passwd",
        "..",
        "sub/dir.bin",
        ".bashrc",
        "-rf.bin",
        "ggml\\win.bin",
        "a" * 129 + ".bin",
        "",
    ):
        assert sanitize_entry({"name": "E", "filename": bad, "cdn_object": "w/x.bin"}) is None, bad


def test_sanitize_enforces_https_download_locations():
    from wayfinder.model_catalog import sanitize_entry

    # file:// is reachable on the urllib (whisper) downloader — must not survive
    assert sanitize_entry(
        {"name": "E", "filename": "ggml-x.bin", "url": "file:///etc/passwd"}
    ) is None
    assert sanitize_entry(
        {"name": "E", "filename": "ggml-x.bin", "url": "http://evil.example/x.bin"}
    ) is None

    # a bad url alongside a good cdn_object drops only the url
    kept = sanitize_entry(
        {
            "name": "E",
            "filename": "ggml-x.bin",
            "url": "http://evil.example/x.bin",
            "cdn_object": "whisper/ggml-x.bin",
        }
    )
    assert kept is not None
    assert "url" not in kept
    assert kept["cdn_object"] == "whisper/ggml-x.bin"


def test_sanitize_drops_cdn_object_clear_and_traversal():
    """An empty cdn_object is what redirects a download off the authed origin."""
    from wayfinder.model_catalog import merge_section, sanitize_entry
    from wayfinder.models_cdn import resolve_download_url

    entry = sanitize_entry(
        {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "url": "https://evil.example/x.bin",
            "cdn_object": "",
        }
    )
    assert entry is not None
    assert "cdn_object" not in entry  # dropped, not honored as a clear

    builtin = {
        "base.en": {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "url": "https://huggingface.co/example/b.bin",
            "cdn_object": "whisper/ggml-base.en.bin",
        }
    }
    merged = merge_section(builtin, {"base.en": entry})["base.en"]
    url = resolve_download_url(merged, config={"models_cdn_base": "https://cdn.example"})
    assert url == "https://cdn.example/v1/objects/whisper/ggml-base.en.bin"

    assert sanitize_entry(
        {"name": "E", "filename": "ggml-x.bin", "cdn_object": "../../secret"}
    ) is None


def test_merge_refuses_unsafe_filename_overlay():
    from wayfinder.model_catalog import merge_section

    builtin = {
        "base.en": {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "cdn_object": "whisper/ggml-base.en.bin",
        }
    }
    out = merge_section(builtin, {"base.en": {"filename": "../../evil.json"}})
    assert out["base.en"]["filename"] == "ggml-base.en.bin"

    out = merge_section(builtin, {"new": {"name": "N", "filename": "../x", "url": "https://x/y"}})
    assert "new" not in out


def test_validate_remote_document_drops_unsafe_entries():
    from wayfinder.model_catalog import validate_remote_document

    doc = validate_remote_document(
        {
            "version": 1,
            "whisper": {
                "ok.en": {
                    "name": "OK",
                    "filename": "ggml-ok.en.bin",
                    "url": "https://x/t.bin",
                },
                "evil.en": {
                    "name": "Evil",
                    "filename": "../../.config/wayfinder-aura/config.json",
                    "url": "https://x/e.bin",
                },
            },
            "llm": {},
        }
    )
    assert doc is not None
    assert set(doc["whisper"]) == {"ok.en"}


def test_resolve_model_dest_contains_downloads(tmp_path):
    import pytest

    from wayfinder.model_catalog import resolve_model_dest

    models = tmp_path / "models"
    models.mkdir()
    assert resolve_model_dest(models, "ggml-base.en.bin") == models / "ggml-base.en.bin"
    assert resolve_model_dest(models, "ggml-base.en.bin", suffix=".downloading") == (
        models / "ggml-base.en.bin.downloading"
    )

    for bad in ("../escape.json", "/etc/passwd", "sub/x.bin", "..", ".hidden"):
        with pytest.raises(ValueError):
            resolve_model_dest(models, bad)


def test_sha256_normalized_and_validated():
    from wayfinder.model_catalog import safe_sha256, sanitize_entry

    digest = "a" * 64
    assert safe_sha256(digest.upper()) == digest
    assert safe_sha256(f"sha256:{digest}") == digest
    for bad in ("", "xyz", "a" * 63, "g" * 64, None, 12345):
        assert safe_sha256(bad) is None

    entry = sanitize_entry(
        {
            "name": "E",
            "filename": "ggml-x.bin",
            "cdn_object": "whisper/ggml-x.bin",
            "sha256": digest.upper(),
        }
    )
    assert entry["sha256"] == digest

    # malformed digest is dropped, not carried through as a bogus expectation
    entry = sanitize_entry(
        {"name": "E", "filename": "ggml-x.bin", "cdn_object": "w/x.bin", "sha256": "nope"}
    )
    assert "sha256" not in entry


def test_verify_model_digest(tmp_path):
    import hashlib

    import pytest

    from wayfinder.model_catalog import verify_model_digest

    blob = tmp_path / "model.bin"
    blob.write_bytes(b"weights")
    good = hashlib.sha256(b"weights").hexdigest()

    verify_model_digest(blob, good)  # matches
    verify_model_digest(blob, None)  # no expectation -> no-op
    verify_model_digest(blob, "not-a-digest")  # malformed -> no-op

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_model_digest(blob, "b" * 64)


def test_hashing_is_cancellable(tmp_path):
    """A multi-GB verify must not ignore Cancel (Sol review finding 6)."""
    import pytest

    from wayfinder.model_catalog import (
        DigestCheckCancelled,
        sha256_file,
        verify_model_digest,
    )

    blob = tmp_path / "model.bin"
    blob.write_bytes(b"x" * (3 << 20))  # 3 chunks at the 1 MiB chunk size

    calls = []

    def cancel_after_first_chunk():
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(DigestCheckCancelled):
        sha256_file(blob, should_cancel=cancel_after_first_chunk)

    with pytest.raises(DigestCheckCancelled):
        verify_model_digest(blob, "a" * 64, should_cancel=lambda: True)

    # not cancelled -> normal completion
    assert len(sha256_file(blob, should_cancel=lambda: False)) == 64


def test_builtin_digest_survives_remote_overlay():
    """A compromised catalog must not be able to retarget a shipped model."""
    from wayfinder.model_catalog import merge_section

    pinned = "a" * 64
    builtin = {
        "base.en": {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "cdn_object": "whisper/ggml-base.en.bin",
            "sha256": pinned,
        }
    }
    out = merge_section(
        builtin,
        {"base.en": {"sha256": "b" * 64, "cdn_object": "whisper/evil.bin"}},
    )
    assert out["base.en"]["sha256"] == pinned

    out = merge_section(builtin, {"base.en": {"name": "Base (renamed)"}})
    assert out["base.en"]["sha256"] == pinned
    assert out["base.en"]["name"] == "Base (renamed)"

    # a model we never shipped may still carry its own digest, but it has to
    # come from the CDN — its digest cannot vouch for an arbitrary origin
    out = merge_section(
        builtin,
        {"new": {"name": "N", "filename": "n.bin", "cdn_object": "w/n.bin",
                 "sha256": "c" * 64, "size_bytes": 999}},
    )
    assert out["new"]["sha256"] == "c" * 64


def test_new_remote_models_require_cdn_object_and_digest():
    """An unshipped model is trusted by origin, because nothing else vouches."""
    from wayfinder.model_catalog import merge_section

    builtin = {"base.en": {"name": "Base", "filename": "ggml-base.en.bin",
                           "cdn_object": "whisper/ggml-base.en.bin", "sha256": "a" * 64}}

    # url-only: rejected (any https host could serve it, and we shipped no pin)
    out = merge_section(
        builtin,
        {"new": {"name": "N", "filename": "n.bin", "url": "https://anywhere.example/n.bin",
                 "sha256": "c" * 64, "size_bytes": 999}},
    )
    assert "new" not in out

    # CDN object but no digest: rejected
    out = merge_section(
        builtin, {"new": {"name": "N", "filename": "n.bin", "cdn_object": "w/n.bin",
                          "size_bytes": 999}}
    )
    assert "new" not in out

    # no exact size: rejected (nothing would bound the transfer pre-hash)
    out = merge_section(
        builtin, {"new": {"name": "N", "filename": "n.bin", "cdn_object": "w/n.bin",
                          "sha256": "c" * 64}}
    )
    assert "new" not in out

    # all three: accepted, and any supplied url is dropped so it stays CDN-only
    out = merge_section(
        builtin,
        {"new": {"name": "N", "filename": "n.bin", "cdn_object": "w/n.bin",
                 "sha256": "c" * 64, "size_bytes": 999,
                 "url": "https://anywhere.example/n.bin"}},
    )
    assert "new" in out
    assert "url" not in out["new"]


def test_shipped_filename_cannot_be_reused_under_another_digest():
    """The pin follows the filename, not the model id.

    Otherwise a compromised catalog disables the built-in and re-adds the same
    filename under a fresh id with its own digest, and the shipped pin is never
    consulted (Sol review finding 1).
    """
    from wayfinder.model_catalog import merge_section

    pinned = "a" * 64
    builtin = {
        "base.en": {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "cdn_object": "whisper/ggml-base.en.bin",
            "sha256": pinned,
        }
    }

    out = merge_section(
        builtin,
        {
            "base.en": {"disabled": True},
            "replacement": {
                "name": "Base (better!)",
                "filename": "ggml-base.en.bin",
                "url": "https://evil.example/x.bin",
                "sha256": "b" * 64,
            },
        },
    )
    assert "replacement" not in out, "shadow entry must not reach the downloader"

    # same trick without disabling the built-in first
    out = merge_section(
        builtin,
        {
            "shadow": {
                "name": "Shadow",
                "filename": "ggml-base.en.bin",
                "url": "https://evil.example/x.bin",
                "sha256": "b" * 64,
            }
        },
    )
    assert "shadow" not in out

    # a genuine re-publish that keeps the shipped facts is still allowed
    out = merge_section(
        builtin,
        {
            "mirror": {
                "name": "Base (mirror)",
                "filename": "ggml-base.en.bin",
                "cdn_object": "whisper/ggml-base.en.bin",
                "sha256": pinned,
                "size_bytes": 147_964_211,
            }
        },
    )
    assert out["mirror"]["sha256"] == pinned


def test_shipped_file_cannot_be_claimed_from_the_other_catalog():
    """Trust is keyed by filename app-wide, not per section.

    whisper and llm merge separately, so a section-local map let an llm entry
    claim a shipped *whisper* filename with its own digest and origin — and the
    llm downloader, finding no llm trust for it, verified the attacker's digest.
    """
    from wayfinder.model_catalog import merge_section, shipped_trust_map

    whisper_builtin = {
        "base": {
            "name": "Base",
            "filename": "ggml-base.bin",
            "cdn_object": "whisper/ggml-base.bin",
            "sha256": "a" * 64,
            "size_bytes": 147_951_465,
        }
    }
    llm_builtin = {
        "gemma3-1b": {
            "name": "Gemma",
            "filename": "gemma.gguf",
            "cdn_object": "llm/gemma.gguf",
            "sha256": "d" * 64,
            "size_bytes": 806_058_496,
        }
    }
    combined = shipped_trust_map({**whisper_builtin, **llm_builtin})

    hijack = {
        "totally-not-whisper": {
            "name": "Free Base",
            "filename": "ggml-base.bin",  # a filename the WHISPER catalog ships
            "cdn_object": "llm/evil.gguf",
            "sha256": "b" * 64,
            "size_bytes": 999,
        }
    }
    # section-local trust would accept this; the combined map must not
    assert "totally-not-whisper" not in merge_section(llm_builtin, hijack, combined)


def test_shipped_entry_cannot_be_renamed_out_of_the_trust_map():
    """Renaming the file walks an entry out of every pin it shipped with."""
    from wayfinder.model_catalog import merge_section

    builtin = {
        "base.en": {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "cdn_object": "whisper/ggml-base.en.bin",
            "sha256": "a" * 64,
            "size_bytes": 147_964_211,
        }
    }
    out = merge_section(builtin, {"base.en": {"filename": "ggml-base.en-v2.bin"}})
    assert out["base.en"]["filename"] == "ggml-base.en.bin"

    # dropping the size (the pre-hash bound) is refused too
    out = merge_section(builtin, {"base.en": {"size_bytes": None}})
    assert out["base.en"]["size_bytes"] == 147_964_211


def test_alias_cannot_drop_entitlement_or_inflate_size():
    """The shipped tuple is (digest, size, entitlement) — all three are locked.

    An alias that keeps the digest but drops `requires_feature` would hand a
    paid model to a free user; one that inflates `size_bytes` would loosen the
    download bound the digest check relies on.
    """
    from wayfinder.model_catalog import merge_section

    pinned = "a" * 64
    builtin = {
        "large-v3": {
            "name": "Large v3",
            "filename": "ggml-large-v3.bin",
            "cdn_object": "whisper/ggml-large-v3.bin",
            "sha256": pinned,
            "size_bytes": 3_095_033_483,
            "requires_feature": "large_models",
        }
    }

    free_alias = {
        "large-v3-free": {
            "name": "Large v3 (free!)",
            "filename": "ggml-large-v3.bin",
            "cdn_object": "whisper/ggml-large-v3.bin",
            "sha256": pinned,
            "size_bytes": 3_095_033_483,
        }
    }
    assert "large-v3-free" not in merge_section(builtin, free_alias)
    assert merge_section(builtin, {"large-v3": {"requires_feature": None}})[
        "large-v3"
    ]["requires_feature"] == "large_models"

    huge = {"large-v3": {"size_bytes": 99_000_000_000}}
    assert merge_section(builtin, huge)["large-v3"]["size_bytes"] == 3_095_033_483


def test_merge_adds_and_disables():
    from wayfinder.model_catalog import merge_section

    builtin = {
        "tiny.en": {
            "name": "Tiny",
            "filename": "ggml-tiny.en.bin",
            "url": "https://hf.example/t.bin",
        },
        "base.en": {
            "name": "Base",
            "filename": "ggml-base.en.bin",
            "url": "https://hf.example/b.bin",
        },
    }
    remote = {
        "tiny.en": {"name": "Tiny EN renamed", "speed_rating": 5},
        "base.en": {"disabled": True},
        "new-en": {
            "name": "New",
            "filename": "ggml-new.en.bin",
            "cdn_object": "whisper/ggml-new.en.bin",
            "sha256": "e" * 64,  # unshipped models must carry a digest
            "size_bytes": 123_456_789,  # ...and an exact size (download bound)
            "requires_feature": "large_models",
        },
    }
    out = merge_section(builtin, remote)
    assert "base.en" not in out
    assert out["tiny.en"]["name"] == "Tiny EN renamed"
    assert out["tiny.en"]["filename"] == "ggml-tiny.en.bin"
    assert out["new-en"]["cdn_object"].endswith("new.en.bin")
    assert out["new-en"]["requires_feature"] == "large_models"


def test_validate_remote_document():
    from wayfinder.model_catalog import validate_remote_document

    bad = validate_remote_document({"version": 1, "whisper": "nope", "llm": {}})
    assert bad is None
    good = validate_remote_document(
        {
            "version": 1,
            "updated_at": "2026-01-01T00:00:00Z",
            "whisper": {
                "tiny.en": {
                    "name": "Tiny",
                    "filename": "ggml-tiny.en.bin",
                    "url": "https://x/t.bin",
                }
            },
            "llm": {},
        }
    )
    assert good is not None
    assert "tiny.en" in good["whisper"]


def test_apply_remote_to_globals_merges(monkeypatch, tmp_path):
    from wayfinder import model_catalog as mc

    monkeypatch.setattr(mc, "_cache_path", lambda: tmp_path / "cat.json")
    # force fetch to return a fixed remote without network
    remote = {
        "version": 1,
        "updated_at": "2026-07-01T00:00:00Z",
        "whisper": {
            "tiny.en": {"name": "Tiny Remote", "filename": "ggml-tiny.en.bin", "url": "https://x"},
        },
        "llm": {},
    }
    monkeypatch.setattr(mc, "fetch_remote_catalog", lambda *a, **k: remote)

    whisper = {
        "tiny.en": {
            "name": "Tiny",
            "filename": "ggml-tiny.en.bin",
            "url": "https://builtin",
            "cdn_object": "whisper/ggml-tiny.en.bin",
        }
    }
    llm = {"gemma": {"name": "G", "filename": "g.gguf", "url": "https://g"}}
    # reset builtin snapshot
    if hasattr(mc.apply_remote_to_globals, "_builtin_whisper"):
        del mc.apply_remote_to_globals._builtin_whisper
        del mc.apply_remote_to_globals._builtin_llm

    status = mc.apply_remote_to_globals(whisper, llm, config={}, force=True)
    assert status["remote_applied"] is True
    assert whisper["tiny.en"]["name"] == "Tiny Remote"
    assert "gemma" in llm


def test_catalog_url_from_cdn_base(monkeypatch):
    from wayfinder import model_catalog as mc
    from wayfinder import models_cdn

    monkeypatch.delenv("WAYFINDER_MODELS_CATALOG_URL", raising=False)
    monkeypatch.setattr(
        models_cdn, "get_models_cdn_base", lambda config=None: "https://cdn.example"
    )
    assert mc.catalog_url_from_config({}) == "https://cdn.example/v1/catalog"

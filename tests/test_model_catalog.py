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

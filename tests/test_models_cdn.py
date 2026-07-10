"""Models CDN URL resolution and download gating."""

from __future__ import annotations


def test_resolve_falls_back_to_hf_without_cdn_base(monkeypatch):
    from wayfinder import models_cdn

    monkeypatch.setattr(models_cdn, "DEFAULT_MODELS_CDN_BASE", "")
    monkeypatch.delenv("WAYFINDER_MODELS_CDN_BASE", raising=False)
    info = {
        "url": "https://example.com/a.bin",
        "cdn_object": "whisper/a.bin",
        "requires_feature": "large_models",
    }
    # Explicit empty models_cdn_base disables CDN even when pilot default exists.
    assert (
        models_cdn.resolve_download_url(info, config={"models_cdn_base": ""})
        == "https://example.com/a.bin"
    )


def test_resolve_prefers_cdn_when_configured():
    from wayfinder.models_cdn import resolve_download_url

    info = {
        "url": "https://example.com/a.bin",
        "cdn_object": "whisper/ggml-large-v3-turbo-q5_0.bin",
    }
    url = resolve_download_url(
        info, config={"models_cdn_base": "https://cdn.example"}
    )
    assert url == "https://cdn.example/v1/objects/whisper/ggml-large-v3-turbo-q5_0.bin"


def test_default_pilot_cdn_used_without_config_key(monkeypatch):
    """Production-shaped path: pilot Worker is the default base."""
    from wayfinder import models_cdn

    monkeypatch.delenv("WAYFINDER_MODELS_CDN_BASE", raising=False)
    monkeypatch.setattr(
        models_cdn,
        "DEFAULT_MODELS_CDN_BASE",
        "https://wayfinder-models-cdn.peter-7b5.workers.dev",
    )
    info = {
        "url": "https://example.com/a.bin",
        "cdn_object": "whisper/ggml-tiny.en.bin",
    }
    url = models_cdn.resolve_download_url(info, config=None)
    assert url.startswith(
        "https://wayfinder-models-cdn.peter-7b5.workers.dev/v1/objects/"
    )


def test_env_overrides_config_cdn_base(monkeypatch):
    from wayfinder import models_cdn

    monkeypatch.setenv("WAYFINDER_MODELS_CDN_BASE", "https://env.cdn.example")
    info = {"url": "https://hf.example/a.bin", "cdn_object": "whisper/x.bin"}
    url = models_cdn.resolve_download_url(
        info, config={"models_cdn_base": "https://config.cdn.example"}
    )
    assert url.startswith("https://env.cdn.example/v1/objects/")


def test_assert_may_download_blocks_free_user():
    from wayfinder.models_cdn import assert_may_download

    info = {"requires_feature": "large_models"}
    err = assert_may_download(info, lambda f: False)
    assert err and "Ultra" in err
    assert assert_may_download(info, lambda f: f == "large_models") is None


def test_auth_headers_include_bearer_when_gated():
    from wayfinder.models_cdn import download_auth_headers

    h = download_auth_headers(
        {"requires_feature": "large_cleanup_models"},
        bearer_token="TOK.EN",
    )
    assert h["Authorization"] == "Bearer TOK.EN"
    h2 = download_auth_headers({"requires_feature": None}, bearer_token="TOK.EN")
    assert "Authorization" not in h2


def test_pilot_catalog_entries_are_gated():
    """Pilot Ultra models must declare CDN + feature requirements."""
    import wayfinder_main as main

    turbo = main.WHISPER_CPP_MODELS["large-v3-turbo-q5_0"]
    assert turbo.get("cdn_object", "").endswith("turbo-q5_0.bin")
    assert turbo.get("requires_feature") == "large_models"

    qwen = main.LLM_GGUF_MODELS["qwen3-4b-2507"]
    assert "Qwen3-4B" in qwen.get("cdn_object", "")
    assert qwen.get("requires_feature") == "large_cleanup_models"

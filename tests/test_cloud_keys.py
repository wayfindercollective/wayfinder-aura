"""Cloud key help: verification outcomes, format hints, retired-model migration."""
import io
import json
import ssl
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.core import cloud_keys as ck  # noqa: E402


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _Resp(json.dumps({"data": [{}, {}, {}]}).encode())


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "x", {}, io.BytesIO(b""))


@pytest.mark.parametrize("provider,host", [
    ("groq", "api.groq.com"), ("openai", "api.openai.com"), ("anthropic", "api.anthropic.com"),
])
def test_valid_key(provider, host):
    opener = _Opener(None)
    ok, message = ck.verify_key(provider, " key\n", opener=opener)
    assert ok and "3 models" in message
    (req,) = opener.requests
    assert req.full_url.startswith(f"https://{host}/")
    if provider == "anthropic":
        assert req.get_header("X-api-key") == "key"
    else:
        assert req.get_header("Authorization") == "Bearer key"  # whitespace stripped


@pytest.mark.parametrize("outcome,ok,phrase", [
    (_http_error(401), False, "rejected"),
    (_http_error(403), False, "rejected"),
    (_http_error(429), True, "rate-limiting"),
    (_http_error(503), False, "having problems"),
    (urllib.error.URLError(OSError("no route")), False, "internet"),
    (urllib.error.URLError(ssl.SSLError("bad cert")), False, "secure connection"),
    (TimeoutError(), False, "internet"),
])
def test_failures_become_plain_advice(outcome, ok, phrase):
    got_ok, message = ck.verify_key("groq", "gsk_x", opener=_Opener(outcome))
    assert got_ok is ok and phrase in message
    assert "gsk_x" not in message


def test_empty_key_makes_no_request():
    opener = _Opener(None)
    assert ck.verify_key("openai", "   ", opener=opener) == (False, "Paste a key first.")
    assert opener.requests == []


def test_redirects_are_never_followed():
    handler = ck._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example") is None


@pytest.mark.parametrize("provider,key,expected", [
    ("groq", "gsk_abc", None),
    ("groq", "sk-abc", "an OpenAI key, not a Groq key"),
    ("openai", "sk-ant-abc", "Anthropic key"),
    ("openai", "gsk_abc", "Groq key"),
    ("anthropic", "sk-abc", "an OpenAI key"),
    ("anthropic", "abc", "start with"),
    ("anthropic", "sk-ant-abc", None),
])
def test_format_hints(provider, key, expected):
    hint = ck.format_warning(provider, key)
    assert (hint is None) if expected is None else (expected in hint)


def test_model_lists_contain_no_retired_ids():
    retired = {old for table in ck.RETIRED_MODEL_REPLACEMENTS.values() for old in table}
    assert not retired & set(ck.OPENAI_CLEANUP_MODELS + ck.ANTHROPIC_CLEANUP_MODELS)


def test_macos_config_moves_retired_models(tmp_path, monkeypatch):
    import wayfinder.config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    if not hasattr(config_module.os, "fchmod"):  # Windows simulating macOS
        monkeypatch.setattr(config_module.os, "fchmod", lambda _fd, _mode: None, raising=False)
    (tmp_path / "config.json").write_text(json.dumps({
        "anthropic_model": "claude-3-haiku-20240307", "openai_model": "gpt-3.5-turbo",
    }))
    config = config_module.load_config()
    assert config["anthropic_model"] == "claude-haiku-4-5-20251001"
    assert config["openai_model"] == "gpt-4o-mini"


def test_linux_config_moves_retired_models_and_saves_only_them(tmp_path, monkeypatch):
    import wayfinder.config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    (tmp_path / "config.json").write_text(json.dumps({
        "anthropic_model": "claude-3-haiku-20240307", "openai_model": "gpt-4-turbo",
    }))
    config = config_module.load_config()
    assert config["anthropic_model"] == "claude-haiku-4-5-20251001"
    assert config["openai_model"] == "gpt-4o-mini"
    # Only the rewritten keys are persisted: unsaved defaults stay unsaved.
    assert json.loads((tmp_path / "config.json").read_text()) == {
        "anthropic_model": "claude-haiku-4-5-20251001", "openai_model": "gpt-4o-mini",
    }


def test_linux_config_keeps_current_models(tmp_path, monkeypatch):
    import wayfinder.config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    saved = {"anthropic_model": "claude-sonnet-4-6", "openai_model": "gpt-4o"}
    (tmp_path / "config.json").write_text(json.dumps(saved))
    config = config_module.load_config()
    assert {k: config[k] for k in saved} == saved
    assert json.loads((tmp_path / "config.json").read_text()) == saved


def test_default_cloud_models_are_current():
    from wayfinder.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["anthropic_model"] in ck.ANTHROPIC_CLEANUP_MODELS
    assert DEFAULT_CONFIG["openai_model"] in ck.OPENAI_CLEANUP_MODELS


def test_a_key_that_could_not_be_read_is_never_deleted(tmp_path, monkeypatch):
    """A locked Keychain or a denied access prompt made get() fail; the next
    save treated the empty value as "removed" and deleted every stored key."""
    import wayfinder.config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config_module.sys, "platform", "darwin")
    if not hasattr(config_module.os, "fchmod"):  # Windows simulating macOS
        monkeypatch.setattr(config_module.os, "fchmod", lambda _fd, _mode: None, raising=False)
    calls = []

    class Locked:
        def get(self, name):
            calls.append(("get", name))
            return None              # the store failed: the key may still be there

        def set(self, name, value):
            calls.append(("set", name))
            return True

        def delete(self, name):
            calls.append(("delete", name))
            return True

    monkeypatch.setattr(config_module, "_macos_keychain", lambda: Locked())
    monkeypatch.setattr(config_module, "_KEYCHAIN_SYNCED", {})
    monkeypatch.setattr(config_module, "_KEYCHAIN_UNREADABLE", set())
    config = config_module.load_config()
    config_module.save_config(config)
    assert not [c for c in calls if c[0] == "delete"]
    # A key the user enters afterwards is still stored.
    config["groq_api_key"] = "gsk_new"
    config_module.save_config(config)
    assert ("set", "groq_api_key") in calls

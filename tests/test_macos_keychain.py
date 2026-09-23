"""API keys in the macOS Keychain: real Keychain round trip + config integration.

Uses a throwaway Keychain service per test run and deletes its items, so the
developer's real "Wayfinder Aura" items are never touched.
"""
import json
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")

SECRETS = ("groq_api_key", "openai_api_key", "anthropic_api_key")


@pytest.fixture
def keychain(monkeypatch):
    from wayfinder.utils import macos_keychain
    monkeypatch.delenv("WAYFINDER_DISABLE_KEYCHAIN", raising=False)
    monkeypatch.setattr(macos_keychain, "SERVICE", f"Wayfinder Aura test {uuid.uuid4().hex[:8]}")
    import wayfinder.config as config_module
    config_module._KEYCHAIN_SYNCED.clear()
    yield macos_keychain
    for name in SECRETS:
        macos_keychain.delete(name)
    config_module._KEYCHAIN_SYNCED.clear()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    import wayfinder.config as config_module
    monkeypatch.setattr(config_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_module, "CONFIG_FILE", tmp_path / "config.json")
    return config_module


def test_round_trip(keychain):
    assert keychain.get("groq_api_key") == ""
    assert keychain.set("groq_api_key", "gsk_one")
    assert keychain.set("groq_api_key", "gsk_two")  # update, not duplicate
    assert keychain.get("groq_api_key") == "gsk_two"
    assert keychain.delete("groq_api_key")
    assert keychain.get("groq_api_key") == ""


def test_saved_keys_never_reach_config_json(keychain, cfg):
    config = cfg.load_config()
    config["groq_api_key"] = "gsk_secret_value"
    config["openai_api_key"] = " sk-secret \n"
    cfg.save_config(config)

    on_disk = json.loads(cfg.CONFIG_FILE.read_text())
    assert on_disk["groq_api_key"] == "" and on_disk["openai_api_key"] == ""
    assert "secret" not in cfg.CONFIG_FILE.read_text()
    assert keychain.get("groq_api_key") == "gsk_secret_value"
    assert keychain.get("openai_api_key") == "sk-secret"  # trimmed

    cfg._KEYCHAIN_SYNCED.clear()
    reloaded = cfg.load_config()
    assert reloaded["groq_api_key"] == "gsk_secret_value"


def test_plain_text_keys_migrate_and_backups_are_scrubbed(keychain, cfg):
    cfg.CONFIG_FILE.write_text(json.dumps({"groq_api_key": "gsk_legacy"}))
    backup = cfg.CONFIG_DIR / "config.json.bak-20260101"
    backup.write_text(json.dumps({"groq_api_key": "gsk_legacy", "hotkey_key": 57}))

    config = cfg.load_config()
    assert config["groq_api_key"] == "gsk_legacy"
    assert keychain.get("groq_api_key") == "gsk_legacy"
    assert json.loads(cfg.CONFIG_FILE.read_text())["groq_api_key"] == ""
    scrubbed = json.loads(backup.read_text())
    assert scrubbed["groq_api_key"] == "" and scrubbed["hotkey_key"] == 57


def test_clearing_a_key_deletes_it(keychain, cfg):
    config = cfg.load_config()
    config["openai_api_key"] = "sk-to-remove"
    cfg.save_config(config)
    config["openai_api_key"] = ""
    cfg.save_config(config)
    assert keychain.get("openai_api_key") == ""
    cfg._KEYCHAIN_SYNCED.clear()
    assert cfg.load_config()["openai_api_key"] == ""


def test_config_file_and_dir_are_owner_only(keychain, cfg):
    cfg.save_config(cfg.load_config())
    assert stat.S_IMODE(os.stat(cfg.CONFIG_FILE).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cfg.CONFIG_DIR).st_mode) == 0o700


def test_keychain_failure_keeps_the_key_in_the_owner_only_file(cfg, monkeypatch):
    import wayfinder.config as config_module

    class Broken:
        def set(self, *_):
            return False

        def delete(self, *_):
            return False

        def get(self, *_):
            return None

    monkeypatch.setattr(config_module, "_macos_keychain", lambda: Broken())
    config_module._KEYCHAIN_SYNCED.clear()
    config = cfg.load_config()
    config["groq_api_key"] = "gsk_fallback"
    cfg.save_config(config)
    assert json.loads(cfg.CONFIG_FILE.read_text())["groq_api_key"] == "gsk_fallback"


def test_disabled_keychain_behaves_like_before(cfg, monkeypatch):
    monkeypatch.setenv("WAYFINDER_DISABLE_KEYCHAIN", "1")
    config = cfg.load_config()
    config["groq_api_key"] = "gsk_plain"
    cfg.save_config(config)
    assert json.loads(cfg.CONFIG_FILE.read_text())["groq_api_key"] == "gsk_plain"


def test_native_helpers_do_not_inherit_api_keys(monkeypatch):
    from wayfinder.utils import hostexec
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    env = hostexec.bundle_binary_env({"GGML_METAL_NO_RESIDENCY": "1"})
    assert "GROQ_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["GGML_METAL_NO_RESIDENCY"] == "1"


def test_linux_child_environment_is_unchanged(monkeypatch):
    from wayfinder.utils import hostexec
    monkeypatch.setattr(hostexec.sys, "platform", "linux")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert hostexec.bundle_binary_env()["GROQ_API_KEY"] == "gsk_x"

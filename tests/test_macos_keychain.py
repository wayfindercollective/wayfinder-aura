"""API keys in the macOS Keychain: real Keychain round trip + config integration.

Uses a private temporary Keychain and a throwaway service per test. The
developer's credentials, default Keychain and search list are never changed.
"""
import json
import os
import stat
import sys
import uuid
from pathlib import Path

import pytest

from tests.macos_keychain_fixture import private_keychain

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")

SECRETS = ("groq_api_key", "openai_api_key", "anthropic_api_key")


@pytest.fixture
def keychain(monkeypatch, tmp_path):
    from wayfinder.utils import macos_keychain
    monkeypatch.delenv("WAYFINDER_DISABLE_KEYCHAIN", raising=False)
    monkeypatch.setattr(macos_keychain, "SERVICE", f"Wayfinder Aura test {uuid.uuid4().hex[:8]}")
    import wayfinder.config as config_module
    config_module._KEYCHAIN_SYNCED.clear()
    with private_keychain(macos_keychain, tmp_path) as libs, monkeypatch.context() as patch:
        patch.setattr(macos_keychain, "_libs", libs)
        try:
            yield macos_keychain
        finally:
            try:
                for name in SECRETS:
                    assert macos_keychain.delete(name)
                    assert macos_keychain.get(name) == ""
            finally:
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


def test_private_keychains_isolate_identical_service_and_account(monkeypatch, tmp_path):
    from wayfinder.utils import macos_keychain

    monkeypatch.setattr(macos_keychain, "SERVICE", f"Wayfinder Aura test {uuid.uuid4().hex}")
    with private_keychain(macos_keychain, tmp_path) as first, \
            private_keychain(macos_keychain, tmp_path) as second, monkeypatch.context() as patch:
        patch.setattr(macos_keychain, "_libs", first)
        assert macos_keychain.set("groq_api_key", "first-fake")
        patch.setattr(macos_keychain, "_libs", second)
        assert macos_keychain.get("groq_api_key") == ""
        assert macos_keychain.set("groq_api_key", "second-fake")
        assert macos_keychain.delete("groq_api_key")
        patch.setattr(macos_keychain, "_libs", first)
        assert macos_keychain.get("groq_api_key") == "first-fake"


def test_private_keychain_is_deleted_after_test_failure(monkeypatch, tmp_path):
    from wayfinder.utils import macos_keychain

    monkeypatch.setattr(macos_keychain, "SERVICE", f"Wayfinder Aura test {uuid.uuid4().hex}")
    with pytest.raises(RuntimeError, match="simulated test failure"):
        with private_keychain(macos_keychain, tmp_path) as libs, monkeypatch.context() as patch:
            patch.setattr(macos_keychain, "_libs", libs)
            assert macos_keychain.set("groq_api_key", "fake-before-failure")
            raise RuntimeError("simulated test failure")
    assert not list(tmp_path.glob("aura-keychain-*/*.keychain*"))


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

"""DEV-UNLOCK (remove before GA): tests for the developer premium-unlock override.

Covers FeatureGate.is_premium honoring the WAYFINDER_DEV_UNLOCK env var and the
"dev_unlock_all" config flag, and that has_feature() then unlocks every premium
feature. Delete this file when the dev unlock is removed (grep: DEV-UNLOCK).
"""
import json

import pytest

from wayfinder.license import FeatureGate, LicenseInfo, PREMIUM_FEATURES


@pytest.fixture(autouse=True)
def _isolate_override(monkeypatch):
    """Pin the real license to non-premium and clear the env flag so each test
    isolates the dev override (independent of this machine's actual license)."""
    monkeypatch.setattr(
        "wayfinder.license.load_stored_license",
        lambda: LicenseInfo(is_valid=False, is_premium=False),
    )
    monkeypatch.delenv("WAYFINDER_DEV_UNLOCK", raising=False)


def test_baseline_not_premium(monkeypatch, tmp_path):
    monkeypatch.setattr("wayfinder.config.CONFIG_FILE", tmp_path / "config.json")
    gate = FeatureGate()
    assert gate.is_premium is False
    assert gate.has_feature("large_models") is False
    assert gate.has_feature("faster_whisper") is False
    # Free features are always available.
    assert gate.has_feature("small_models") is True


@pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
def test_env_var_unlocks_everything(monkeypatch, val):
    monkeypatch.setenv("WAYFINDER_DEV_UNLOCK", val)
    gate = FeatureGate()
    assert gate.is_premium is True
    for fid in PREMIUM_FEATURES:
        assert gate.has_feature(fid) is True, fid


def test_config_flag_unlocks_everything(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dev_unlock_all": True}))
    monkeypatch.setattr("wayfinder.config.CONFIG_FILE", cfg)
    gate = FeatureGate()
    assert gate.is_premium is True
    assert gate.has_feature("large_models") is True


def test_config_flag_false_stays_locked(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"dev_unlock_all": False}))
    monkeypatch.setattr("wayfinder.config.CONFIG_FILE", cfg)
    gate = FeatureGate()
    assert gate.is_premium is False
    assert gate.has_feature("large_models") is False


def test_env_var_off_value_stays_locked(monkeypatch, tmp_path):
    monkeypatch.setenv("WAYFINDER_DEV_UNLOCK", "0")
    monkeypatch.setattr("wayfinder.config.CONFIG_FILE", tmp_path / "config.json")
    gate = FeatureGate()
    assert gate.is_premium is False

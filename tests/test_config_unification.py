"""Config single-source unification tests (Phase 1b of the premium polish pass).

wayfinder_main.py used to carry its own DEFAULT_CONFIG + load_config +
save_config — the duplicate that CLAUDE.md rule 3 warns about, and the one the
running app actually loaded (bypassing config.py's protective migrations).
Phase 1b deletes that duplicate; wayfinder_main.py now imports everything from
wayfinder.config, so config.py is the single source of truth.

Two kinds of test here:
  1. Structural (source-parse only, no Tk import): assert the duplicate is
     really gone from wayfinder_main.py.
  2. Behavioral: exercise wayfinder.config.load_config against a tmp config dir
     (via the temp_config_dir fixture, which monkeypatches CONFIG_DIR/FILE) to
     lock in the reconciled defaults and the migrations the app now inherits.

Existing hotkey-migration and stale-path cases live in test_config.py; this
file adds the coverage that was missing (superset defaults, API-key round-trip,
overlay + whisper-server field presence, fresh-install file creation).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MAIN_SRC = (REPO / "wayfinder_main.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Structural — the duplicate must be gone from wayfinder_main.py
# ---------------------------------------------------------------------------

def test_main_has_no_default_config_literal():
    assert re.search(r"(?m)^DEFAULT_CONFIG\s*=\s*\{", MAIN_SRC) is None, (
        "wayfinder_main.py still defines its own DEFAULT_CONFIG dict — it must "
        "import it from wayfinder.config (single source, CLAUDE.md rule 3)."
    )


def test_main_has_no_load_config_def():
    assert re.search(r"(?m)^def load_config\b", MAIN_SRC) is None, (
        "wayfinder_main.py still defines load_config — import it from wayfinder.config."
    )


def test_main_has_no_save_config_def():
    assert re.search(r"(?m)^def save_config\b", MAIN_SRC) is None, (
        "wayfinder_main.py still defines save_config — import it from wayfinder.config."
    )


def test_main_imports_config_symbols_from_package():
    # The names must still be available in wayfinder_main's namespace — via import.
    assert "from wayfinder.config import" in MAIN_SRC
    for sym in ("DEFAULT_CONFIG", "load_config", "save_config"):
        assert re.search(rf"\b{sym}\b", MAIN_SRC), f"{sym} no longer referenced/imported"


# ---------------------------------------------------------------------------
# 2. Behavioral — reconciled defaults + inherited migrations (config.py)
# ---------------------------------------------------------------------------

def test_reconciled_keys_present_in_defaults():
    """Every key that the old wayfinder_main.py default carried must survive in
    the unified config.py default (superset guarantee). Includes the one key
    that was ONLY in main before Phase 1b."""
    from wayfinder.config import DEFAULT_CONFIG

    # The lone key that lived only in wayfinder_main.py pre-unification.
    assert DEFAULT_CONFIG["openai_whisper_model"] == "whisper-1"

    # Spot-check a representative superset spanning both former dicts.
    for key in (
        "whisper_binary", "model_path", "hotkey_key", "style_toggle_key",
        "output_tone", "strong_mode", "post_processing_enabled",
        "groq_api_key", "openai_api_key", "anthropic_api_key",
        "benchmark_results", "api_benchmark_results", "setup_completed",
    ):
        assert key in DEFAULT_CONFIG, f"reconciled default lost key: {key}"


def test_overlay_keys_in_defaults():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["overlay_scale"] == 1.0  # live wayfinder_main value kept
    assert DEFAULT_CONFIG["overlay_anchor"] == "bottom-center"
    assert DEFAULT_CONFIG["overlay_vertical_offset"] == 0


def test_whisper_server_fields_in_defaults():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["whisper_server_mode"] is True
    assert DEFAULT_CONFIG["whisper_server_port"] == 8178
    assert DEFAULT_CONFIG["whisper_server_timeout"] == 30


def test_fresh_install_creates_file_and_returns_defaults(temp_config_dir: Path):
    from wayfinder.config import load_config, save_config, DEFAULT_CONFIG, CONFIG_FILE

    assert not CONFIG_FILE.exists()
    config = load_config()
    # First run persists a config file...
    assert CONFIG_FILE.exists(), "fresh install should write the default config file"
    # ...and returns the defaults for every default key.
    for key, val in DEFAULT_CONFIG.items():
        assert config[key] == val


def test_api_key_fields_round_trip(temp_config_dir: Path):
    from wayfinder.config import load_config, save_config

    keys = {
        "groq_api_key": "gsk_test_groq",
        "openai_api_key": "sk-test-openai",
        "anthropic_api_key": "sk-ant-test",
    }
    base = load_config()
    base.update(keys)
    save_config(base)

    reloaded = load_config()
    for k, v in keys.items():
        assert reloaded[k] == v, f"API key {k} did not round-trip"


def test_partial_legacy_config_preserves_legacy_hotkeys(temp_config_dir: Path):
    """The migration wayfinder_main.py never ran: an existing install whose
    config predates the Super+F2 default must keep bare-F3 recording rather than
    silently gaining ['super']. This is the protective behavior the app now
    inherits from config.py."""
    from wayfinder.config import load_config, CONFIG_FILE

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"typing_speed": "instant"}))

    config = load_config()
    assert config["hotkey_key"] == 61          # legacy F3, not the new F2
    assert config["hotkey_modifiers"] == []    # bare key, no injected super
    assert config["style_toggle_key"] == 68    # legacy F10


def test_stale_whisper_binary_falls_back_to_existing_default(
    temp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A saved whisper_binary path that no longer exists on this machine is
    replaced by the auto-detected default when that default exists — the
    stale-path repair the naive main.load_config lacked."""
    from wayfinder import config as cfg

    good = tmp_path / "whisper-cli"
    good.write_text("")
    monkeypatch.setitem(cfg.DEFAULT_CONFIG, "whisper_binary", str(good))

    cfg.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg.CONFIG_FILE.write_text(json.dumps({"whisper_binary": "/nonexistent/whisper.cpp/bin/whisper-cli"}))

    config = cfg.load_config()
    assert config["whisper_binary"] == str(good)

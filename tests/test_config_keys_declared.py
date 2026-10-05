"""Every config key the app reads by name is declared in DEFAULT_CONFIG.

CLAUDE.md: config keys live only in src/wayfinder/config.py DEFAULT_CONFIG.
A key read with an inline fallback but never declared has no single default,
and two readers can (and did) disagree about it.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from wayfinder.config import DEFAULT_CONFIG  # noqa: E402


def _is_config(node) -> bool:
    """``config`` or ``<anything>.config`` (self.config, app.config)."""
    return ((isinstance(node, ast.Name) and node.id == "config")
            or (isinstance(node, ast.Attribute) and node.attr == "config"))


def _text(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def config_keys_used(source: str):
    """(key, line) for config.get/setdefault/pop("key"), config["key"] and
    "key" in config, however the call is wrapped across lines."""
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("get", "setdefault", "pop")
                and _is_config(node.func.value) and node.args and _text(node.args[0])):
            yield _text(node.args[0]), node.lineno
        elif isinstance(node, ast.Subscript) and _is_config(node.value) and _text(node.slice):
            yield _text(node.slice), node.lineno
        elif (isinstance(node, ast.Compare) and _text(node.left) and len(node.ops) == 1
              and isinstance(node.ops[0], (ast.In, ast.NotIn))
              and _is_config(node.comparators[0])):
            yield _text(node.left), node.lineno


def _sources():
    yield REPO / "wayfinder_main.py"
    yield REPO / "main.py"
    yield from sorted((REPO / "src" / "wayfinder").rglob("*.py"))


def test_the_scan_sees_every_way_a_key_is_read():
    source = '''
self.config.get(
    "wrapped_get", 1)
config["item"] = 2
if "member" not in app.config:
    pass
config.setdefault("defaulted", 3)
user_config.get("raw_file_key")
'''
    assert sorted(key for key, _line in config_keys_used(source)) == [
        "defaulted", "item", "member", "wrapped_get"]


def test_every_config_key_read_by_name_has_a_default():
    undeclared = {}
    for path in _sources():
        for key, lineno in config_keys_used(path.read_text(encoding="utf-8")):
            if key not in DEFAULT_CONFIG:
                undeclared.setdefault(key, f"{path.relative_to(REPO)}:{lineno}")
    assert not undeclared, (
        "add these keys to DEFAULT_CONFIG in src/wayfinder/config.py: "
        + ", ".join(f"{key} ({where})" for key, where in sorted(undeclared.items())))


def test_scale_and_mic_defaults_mean_detect_and_platform_default():
    # None (not 1.0 / 30) so first-run scale detection and the per-platform
    # warm-mic time still apply when the user never chose a value.
    assert DEFAULT_CONFIG["ui_scale"] is None
    assert DEFAULT_CONFIG["mic_warm_idle_secs"] is None
    assert DEFAULT_CONFIG["window_geometry"] is None

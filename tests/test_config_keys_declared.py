"""Every config key the app reads by name is declared in DEFAULT_CONFIG.

CLAUDE.md: config keys live only in src/wayfinder/config.py DEFAULT_CONFIG.
A key read with an inline fallback but never declared has no single default,
and two readers can (and did) disagree about it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from wayfinder.config import DEFAULT_CONFIG  # noqa: E402

# config.get("key"), config["key"], config.setdefault("key"), config.pop("key"),
# on any name ending in "config" (self.config, app.config, user_config...).
_READ = re.compile(r"""config(?:\.get|\.setdefault|\.pop)?\s*[\(\[]\s*["']([a-z][a-z0-9_]*)["']""")


def _sources():
    yield REPO / "wayfinder_main.py"
    yield REPO / "main.py"
    yield from sorted((REPO / "src" / "wayfinder").rglob("*.py"))


def test_every_config_key_read_by_name_has_a_default():
    undeclared = {}
    for path in _sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for key in _READ.findall(line):
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

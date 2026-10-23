"""Shared widgets, GPU detection and the Games-tab search have one copy each.

wayfinder_main.py used to carry its own ToolTip, ModeSelector,
CompatibilityBanner, SmoothScrollableFrame, SETTING_TOOLTIPS,
get_dynamic_tooltip and GPU detection next to the package versions. The two
copies drifted: the package ToolTip lost the <Unmap> fix for tooltips stranded
over another tab, the package GPUInfo had no Apple vendor, and app.py and the
tests exercised the stale ones.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "wayfinder"


def _top_level_defs(path: Path) -> dict[str, str]:
    """Top-level class/function names -> their source (docstrings dropped)."""
    src = path.read_text(encoding="utf-8")
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            body = node.body[1:] if ast.get_docstring(node) else node.body
            out[node.name] = "\n".join(ast.unparse(stmt) for stmt in body)
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id.isupper() and node.targets[0].id.endswith("TOOLTIPS"):
                out[node.targets[0].id] = ast.unparse(node.value)
    return out


def test_wayfinder_main_keeps_no_copy_of_shared_code():
    main = _top_level_defs(REPO / "wayfinder_main.py")
    for shared in (PACKAGE / "ui" / "components.py", PACKAGE / "utils" / "gpu.py"):
        copies = sorted(set(main) & set(_top_level_defs(shared)))
        assert not copies, f"wayfinder_main.py redefines {copies} from {shared.name}; import it instead"


def test_game_chat_modules_share_no_identical_function():
    # A one-line ``return shared_helper(...)`` reads each module's own PROFILES
    # and INFO_ONLY, so it is not a copy; anything longer must live in one place.
    modules = ["macos_game_chat.py", "windows_game_chat.py", "linux_game_chat.py"]
    seen: dict[str, str] = {}
    for name in modules:
        for fn, body in _top_level_defs(PACKAGE / "core" / name).items():
            if body.startswith("return ") and "\n" not in body:
                continue
            if body in seen and seen[body] != name:
                pytest.fail(f"{fn} in {name} is a copy of the one in {seen[body]}")
            seen.setdefault(body, name)


def test_app_and_wayfinder_main_use_the_same_objects():
    import wayfinder_main as wm
    from wayfinder.ui import components
    from wayfinder.utils import gpu

    for name in ("ToolTip", "ModeSelector", "CompatibilityBanner", "SmoothScrollableFrame",
                 "SETTING_TOOLTIPS", "get_dynamic_tooltip", "hide_all_tooltips"):
        assert getattr(wm, name) is getattr(components, name), name
    for name in ("GPUInfo", "get_gpu_info", "get_optimal_thread_count"):
        assert getattr(wm, name) is getattr(gpu, name), name


def test_the_package_tooltip_has_the_stranded_tooltip_fix():
    # app.py and ui/__init__ import this one; it used to lack <Unmap>/<Destroy>.
    from wayfinder.ui.components import ToolTip

    bound = []
    widget = SimpleNamespace(bind=lambda event, fn, add=None: bound.append(event))
    ToolTip(widget, "hint")
    assert "<Unmap>" in bound and "<Destroy>" in bound


# ---- GPU ----------------------------------------------------------------

@pytest.mark.parametrize(
    "brand, vendor",
    [("Apple M2 Pro", "apple"), ("Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz", "intel")],
)
def test_mac_gpu_is_reported_on_metal(monkeypatch, brand, vendor):
    from wayfinder.utils import gpu

    monkeypatch.setattr(gpu.sys, "platform", "darwin")
    monkeypatch.setattr(
        gpu.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=brand + "\n", stderr=""),
    )
    info = gpu.detect_gpu()
    assert (info.vendor, info.driver) == (vendor, "metal")
    assert info.has_gpu and info.is_known and info.is_apple is (vendor == "apple")
    assert info.name == f"{brand} (Metal)"


def test_mac_gpu_probe_failure_still_reports_metal(monkeypatch):
    from wayfinder.utils import gpu

    def boom(*_a, **_k):
        raise OSError("no sysctl")

    monkeypatch.setattr(gpu.sys, "platform", "darwin")
    monkeypatch.setattr(gpu.subprocess, "run", boom)
    assert gpu.detect_gpu().vendor == "apple"


def test_unknown_gpu_has_no_gpu():
    from wayfinder.utils.gpu import GPUInfo

    info = GPUInfo("unknown", "Unknown GPU")
    assert not info.has_gpu and not info.is_known and not info.is_apple


def test_gpu_is_probed_once_for_the_app_and_basic_settings(monkeypatch):
    import wayfinder_main as wm
    from wayfinder.utils import gpu

    calls = []
    monkeypatch.setattr(gpu, "_cached_gpu_info", None)
    monkeypatch.setattr(gpu, "detect_gpu", lambda: calls.append(1) or gpu.GPUInfo("amd", "AMD GPU", "amdgpu"))
    assert wm.get_gpu_info() is gpu.get_gpu_info()
    assert calls == [1]


# ---- Games tab ----------------------------------------------------------

def test_merge_keeps_profiles_over_researched_rows_and_sorts():
    from wayfinder.core.macos_game_chat import GameEntry, merge_game_entries

    ours = [GameEntry("World of Warcraft", "verified", "ours"), GameEntry("albion", "untested", "x")]
    researched = [GameEntry("world of warcraft", "untested", "theirs"), GameEntry("EVE Online", "untested", "r")]
    merged = merge_game_entries(ours, researched)
    assert [e.name for e in merged] == ["albion", "EVE Online", "World of Warcraft"]
    assert next(e for e in merged if e.name == "World of Warcraft").note == "ours"


@pytest.mark.parametrize("query, expected", [
    ("", ["Dark Age of Camelot", "RuneScape"]),
    ("   ", ["Dark Age of Camelot", "RuneScape"]),
    ("  DARK   age ", ["Dark Age of Camelot"]),
    ("daoc", ["Dark Age of Camelot"]),
    ("osrs", ["RuneScape"]),
    ("zzz", []),
])
def test_filter_matches_names_and_aliases(query, expected):
    from wayfinder.core.macos_game_chat import GameEntry, filter_games

    entries = [GameEntry("Dark Age of Camelot", "verified", "", ("daoc",)),
               GameEntry("RuneScape", "untested", "", ("osrs", "old school"))]
    assert [e.name for e in filter_games(entries, query)] == expected


@pytest.mark.parametrize("module", ["macos_game_chat", "windows_game_chat", "linux_game_chat"])
def test_each_platform_search_is_its_own_list_filtered(module):
    import importlib

    gc = importlib.import_module(f"wayfinder.core.{module}")
    everything = gc.game_list()
    assert gc.search_games("") == everything
    first = everything[0]
    hits = gc.search_games(f"  {first.name.upper()} ")
    assert first in hits and all(e in everything for e in hits)

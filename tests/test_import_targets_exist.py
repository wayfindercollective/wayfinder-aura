"""Every ``wayfinder`` import names a module and a name that exist.

SOURCE-PARSING ONLY, like test_design_ratchet: nothing here imports Tk or the
app. Function-local imports matter most: they only run when a pane opens or a
feature fires, often inside ``except Exception``, so a wrong path fails
silently. Basic Settings imported a ``wayfinder.core.benchmark`` module that
never existed, and its hardware readout said "Detecting…" forever.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO / "src"
LEGACY_ROOT_FILES = ("main.py", "wayfinder_main.py", "status_overlay.py")


def _app_sources() -> list[Path]:
    files = sorted((PACKAGE_ROOT / "wayfinder").rglob("*.py"))
    files += [REPO / name for name in LEGACY_ROOT_FILES]
    return files


def _module_file(dotted: str) -> Path | None:
    base = PACKAGE_ROOT.joinpath(*dotted.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _package_of(path: Path) -> list[str] | None:
    try:
        rel = path.relative_to(PACKAGE_ROOT)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    return parts if path.name == "__init__.py" else parts[:-1]


def _absolute_module(node: ast.ImportFrom, path: Path) -> str | None:
    if node.level == 0:
        return node.module
    package = _package_of(path)
    if package is None:
        return None
    if path.name == "__init__.py":
        package = package[:-1] if package[-1] == "__init__" else package
    keep = len(package) - (node.level - 1)
    if keep <= 0:
        return None
    parts = package[:keep] + (node.module.split(".") if node.module else [])
    return ".".join(parts)


def _top_level_names(tree: ast.Module) -> tuple[set[str], bool]:
    """Names a module defines at top level, and whether it has ``__getattr__``."""
    names: set[str] = set()
    lazy = False

    def visit(body: list[ast.stmt]) -> None:
        nonlocal lazy
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(stmt.name)
                lazy = lazy or stmt.name == "__getattr__"
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    for node in ast.walk(target):
                        if isinstance(node, ast.Name):
                            names.add(node.id)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    names.add(alias.asname or alias.name)
            elif isinstance(stmt, ast.If):
                visit(stmt.body)
                visit(stmt.orelse)
            elif isinstance(stmt, ast.Try):
                visit(stmt.body)
                visit(stmt.orelse)
                visit(stmt.finalbody)
                for handler in stmt.handlers:
                    visit(handler.body)
            elif isinstance(stmt, (ast.With, ast.For, ast.While)):
                visit(stmt.body)

    visit(tree.body)
    return names, lazy


_DEFINITIONS: dict[Path, tuple[set[str], bool]] = {}


def _definitions(module_file: Path) -> tuple[set[str], bool]:
    if module_file not in _DEFINITIONS:
        tree = ast.parse(module_file.read_text(encoding="utf-8"))
        _DEFINITIONS[module_file] = _top_level_names(tree)
    return _DEFINITIONS[module_file]


def broken_imports(path: Path, source: str | None = None) -> list[str]:
    """``file:line message`` for each wayfinder import that cannot resolve."""
    text = source if source is not None else path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "wayfinder" and not _module_file(alias.name):
                    problems.append(f"{rel}:{node.lineno} no module {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_module(node, path)
            if not module or module.split(".")[0] != "wayfinder":
                continue
            module_file = _module_file(module)
            if module_file is None:
                problems.append(f"{rel}:{node.lineno} no module {module}")
                continue
            defined, lazy = _definitions(module_file)
            for alias in node.names:
                name = alias.name
                if name == "*" or lazy or name in defined:
                    continue
                if _module_file(f"{module}.{name}"):
                    continue
                problems.append(f"{rel}:{node.lineno} {module} has no {name}")
    return problems


def test_every_wayfinder_import_resolves():
    problems = [p for path in _app_sources() for p in broken_imports(path)]
    assert not problems, "Imports that would fail at runtime:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("from wayfinder.core.benchmark import BenchmarkRunner", "no module wayfinder.core.benchmark"),
        ("from wayfinder.utils.system_info import nope", "wayfinder.utils.system_info has no nope"),
        ("import wayfinder.core.nothing_here", "no module wayfinder.core.nothing_here"),
    ],
)
def test_checker_catches_a_missing_target(line, expected):
    source = f"def probe():\n    try:\n        {line}\n    except Exception:\n        return\n"
    fake = PACKAGE_ROOT / "wayfinder" / "ui" / "_probe.py"
    problems = broken_imports(fake, source)
    assert len(problems) == 1 and problems[0].endswith(expected)


def test_checker_resolves_relative_imports_and_submodules():
    source = "from ..utils import system_info\nfrom . import basic_settings\n"
    fake = PACKAGE_ROOT / "wayfinder" / "ui" / "_probe.py"
    assert broken_imports(fake, source) == []

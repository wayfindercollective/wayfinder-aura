"""Every build ships the same CustomTkinter.

The Flatpak vendored 5.2.2 while the AppImage, macOS and Windows builds got
6.x, so the UI's CTk patches (slider wheel, Aqua button release, scrollbar
redraw) were live in some packages and inert in others. Each install path now
pins one exact version; this test fails the moment one of them drifts.
"""

from __future__ import annotations

import json
import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _pins(text: str) -> list[str]:
    return re.findall(r"customtkinter\s*([<>=!~][^\s\"',;#]*)", text, flags=re.IGNORECASE)


def _flatpak_module() -> dict:
    deps = json.loads((REPO / "flatpak" / "python-deps.json").read_text(encoding="utf-8"))
    return next(m for m in deps["modules"] if "customtkinter" in m["name"])


def _expected() -> str:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    (spec,) = [d for d in deps if d.lower().startswith("customtkinter")]
    match = re.fullmatch(r"customtkinter==(\d+\.\d+\.\d+)", spec.replace(" ", ""))
    assert match, f"pyproject must pin customtkinter exactly, not {spec!r}"
    return match.group(1)


@pytest.mark.parametrize("path", [
    "requirements.txt",
    "flatpak/flatpak-requirements.txt",
    "packaging/macos/constraints.txt",
])
def test_each_requirements_file_pins_the_same_version(path):
    assert _pins((REPO / path).read_text(encoding="utf-8")) == [f"=={_expected()}"], path


def test_windows_inherits_the_shared_pin():
    text = (REPO / "requirements-windows.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^-r requirements\.txt$", text)
    assert not _pins(text)


def test_the_flatpak_installs_the_wheel_it_pins():
    module = _flatpak_module()
    (command,) = module["build-commands"]
    assert _pins(command) == [f"=={_expected()}"]
    wheels = [Path(s["url"]).name for s in module["sources"]]
    assert f"customtkinter-{_expected()}-py3-none-any.whl" in wheels
    # CTk 6 needs darkdetect and packaging; the build is --no-index, so
    # darkdetect must be vendored here. packaging ships in org.kde.Platform.
    assert any(w.startswith("darkdetect-") for w in wheels)


def test_the_dev_environment_runs_the_pinned_version():
    try:
        installed = metadata.version("customtkinter")
    except metadata.PackageNotFoundError:
        pytest.skip("customtkinter is not installed")
    assert installed == _expected(), "UI tests must run on the CTk the packages ship"

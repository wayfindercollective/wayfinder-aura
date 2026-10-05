"""Setup's install commands per package manager: what a user is told to run.

The manual-install lines are printed when pkexec is missing, refused, or the
package manager fails; they must be runnable as printed on that distro.
"""
from __future__ import annotations

import pytest

from wayfinder.core import setup


@pytest.mark.parametrize("pkg_mgr, expected", [
    ("apt", "sudo apt update && sudo apt install -y git cmake"),  # sudo on both steps
    ("dnf", "sudo dnf install -y git cmake"),
    ("pacman", "sudo pacman -S --needed git cmake"),               # never apt on Arch
    ("zypper", "sudo zypper install git cmake"),
    ("ostree", "rpm-ostree install git cmake"),
    ("brew", "brew install git cmake"),
])
def test_manual_install_command_is_runnable_for_each_manager(pkg_mgr, expected):
    assert setup._manual_install_command(pkg_mgr, ["git", "cmake"]) == expected


def test_zypper_is_detected(monkeypatch):
    monkeypatch.setattr(setup.sys, "platform", "linux")
    monkeypatch.setattr(setup, "_is_atomic_host", lambda: False)
    monkeypatch.setattr(setup.shutil, "which", lambda name: "/usr/bin/zypper" if name == "zypper" else None)
    assert setup._detect_package_manager() == "zypper"
    assert setup._resolve_packages(["build-essential"], "zypper") == ["gcc-c++", "make"]


def test_atomic_fedora_layers_fedora_package_names():
    # rpm-ostree installs Fedora packages: "build-essential" is a Debian name.
    assert setup._resolve_packages(["build-essential", "libfuse2"], "ostree") == [
        "gcc-c++", "make", "fuse-libs"]

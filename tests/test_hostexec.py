"""host_env(): the environment used to spawn HOST binaries from the bundle.

Field bug this guards: the AppImage runs with LD_LIBRARY_PATH pointing at the
PyInstaller extraction dir; host /usr/bin/qdbus loaded the bundled jammy
libstdc++ and died with "CXXABI_1.3.15 not found", silently killing overlay
placement. LD_LIBRARY_PATH_ORIG is equally polluted (AppRun sets it first),
so the only safe construction is stripping bundle-owned path entries.
"""

from __future__ import annotations

import os
import sys

import pytest

from wayfinder.utils import hostexec
from wayfinder.utils.hostexec import bundle_binary_env, host_env


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """Simulate a frozen AppImage run: _MEIPASS + APPDIR both set."""
    mei = tmp_path / "_MEIxyz"
    appdir = tmp_path / "mount" / "AppDir"
    (mei / "lib").mkdir(parents=True)
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "lib").mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(mei), raising=False)
    monkeypatch.setenv("APPDIR", str(appdir))
    return {"mei": mei, "appdir": appdir}


def test_source_run_is_passthrough(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/custom/lib")
    env = host_env({"EXTRA": "1"})
    assert env["LD_LIBRARY_PATH"] == "/opt/custom/lib"
    assert env["EXTRA"] == "1"


def test_bundle_entries_stripped_from_path_lists(bundle, monkeypatch):
    monkeypatch.setenv(
        "PATH", f"{bundle['appdir']}/usr/bin{os.pathsep}/usr/bin{os.pathsep}/bin"
    )
    monkeypatch.setenv(
        "LD_LIBRARY_PATH",
        f"{bundle['mei']}{os.pathsep}{bundle['appdir']}/usr/lib",
    )
    env = host_env()
    assert env["PATH"] == f"/usr/bin{os.pathsep}/bin"
    # every entry was bundle-owned -> variable must be gone, not empty
    assert "LD_LIBRARY_PATH" not in env


def test_bundle_only_vars_dropped(bundle, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", f"{bundle['appdir']}/usr/lib")
    monkeypatch.setenv("TCL_LIBRARY", f"{bundle['mei']}/tcl")
    monkeypatch.setenv("TK_LIBRARY", f"{bundle['mei']}/tk")
    monkeypatch.setenv("PYTHONHOME", str(bundle["mei"]))
    monkeypatch.setenv("FONTCONFIG_FILE", "/run/user/1000/wf-fc/fonts.conf")
    env = host_env()
    for var in ("LD_LIBRARY_PATH_ORIG", "TCL_LIBRARY", "TK_LIBRARY",
                "PYTHONHOME", "FONTCONFIG_FILE"):
        assert var not in env


def test_symlinked_bundle_path_still_stripped(bundle, tmp_path, monkeypatch):
    alias = tmp_path / "alias"
    alias.symlink_to(bundle["mei"])
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{alias}/lib{os.pathsep}/usr/lib64")
    env = host_env()
    assert env["LD_LIBRARY_PATH"] == "/usr/lib64"


def test_overrides_win(bundle, monkeypatch):
    monkeypatch.setenv("YDOTOOL_SOCKET", "/stale")
    env = host_env({"YDOTOOL_SOCKET": "/run/user/1000/.ydotool_socket"})
    assert env["YDOTOOL_SOCKET"] == "/run/user/1000/.ydotool_socket"


def test_path_never_ends_up_missing(bundle, monkeypatch):
    monkeypatch.setenv("PATH", f"{bundle['appdir']}/usr/bin")
    env = host_env()
    assert env["PATH"] == hostexec._FALLBACK_PATH


def test_bundle_binary_env_restores_pre_pyinstaller_library_path(bundle, monkeypatch):
    original = f"{bundle['appdir']}/usr/lib:/opt/vendor/lib"
    monkeypatch.setenv(
        "LD_LIBRARY_PATH",
        f"{bundle['mei']}:{original}",
    )
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)

    env = bundle_binary_env()

    assert env["LD_LIBRARY_PATH"] == original
    assert env["LD_LIBRARY_PATH_ORIG"] == original


@pytest.mark.linux_only
def test_bundle_binary_env_without_orig_drops_only_meipass(bundle, monkeypatch):
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    monkeypatch.setenv(
        "LD_LIBRARY_PATH",
        f"{bundle['mei']}:{bundle['appdir']}/usr/lib:/usr/lib64",
    )

    env = bundle_binary_env()

    assert env["LD_LIBRARY_PATH"] == (
        f"{bundle['appdir']}/usr/lib:/usr/lib64"
    )


def test_bundle_binary_env_source_run_is_passthrough(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/custom/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/original/lib")

    env = bundle_binary_env({"EXTRA": "1"})

    assert env["LD_LIBRARY_PATH"] == "/opt/custom/lib"
    assert env["EXTRA"] == "1"

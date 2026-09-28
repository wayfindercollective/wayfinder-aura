"""One Dock icon on macOS: helpers start hidden, the app asks for its icon."""

from __future__ import annotations

import re
from pathlib import Path

from wayfinder.utils import macos_dock

REPO = Path(__file__).resolve().parent.parent


class _FakeLib:
    def __init__(self, result=0):
        self.calls = []
        self.result = result

        def transform(psn, kind):
            self.calls.append((psn._obj.highLongOfPSN, psn._obj.lowLongOfPSN, kind))
            return self.result

        self.TransformProcessType = transform


def test_off_macos_nothing_is_loaded():
    def loader(_path):
        raise AssertionError("must not load ApplicationServices off macOS")

    assert macos_dock.show_in_dock("linux", loader) is False
    assert macos_dock.show_in_dock("win32", loader) is False


def test_the_current_process_becomes_a_foreground_app():
    lib = _FakeLib()
    loaded = []

    def loader(path):
        loaded.append(path)
        return lib

    assert macos_dock.show_in_dock("darwin", loader) is True
    assert loaded == [macos_dock._APPLICATION_SERVICES]
    assert lib.calls == [(0, 2, 1)]  # {0, kCurrentProcess}, ToForegroundApplication


def test_an_os_error_or_missing_api_is_not_fatal(capsys):
    assert macos_dock.show_in_dock("darwin", lambda _p: _FakeLib(result=-50)) is False

    def broken(_path):
        raise OSError("no such framework")

    assert macos_dock.show_in_dock("darwin", broken) is False
    assert "Tk will" in capsys.readouterr().out


def test_bundle_starts_every_process_without_a_dock_icon():
    spec = (REPO / "wayfinder-aura-macos.spec").read_text(encoding="utf-8")
    assert "'LSUIElement': True" in spec
    assert "'LSBackgroundOnly': False" in spec  # the app still gets windows


def test_main_asks_for_the_dock_icon_only_as_the_running_instance():
    main = (REPO / "main.py").read_text(encoding="utf-8")
    body = main[main.index("def main():"):]
    lock = body.index("if _signal_existing_instance():")
    dock = body.index("show_in_dock()")
    tk = body.index("from wayfinder_main import WayfinderApp")
    # A second launch exits before asking (no flash); the icon comes before Tk.
    assert lock < dock < tk
    # Helper modes return before main() runs, so they never ask.
    for flag in ("--overlay-subprocess", "--child-supervisor"):
        assert main.index(f'if "{flag}" in sys.argv') < main.index("def main():")
    assert not re.search(r"show_in_dock\(\)", main[: main.index("def main():")])

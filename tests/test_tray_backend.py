"""Matrix test for should_host_qt_tray — the tray-backend decision.

The desktop app showed a pystray (libappindicator) tray whose menu rendered on
KDE Plasma 6 but whose clicks did nothing: the DBusMenu activations never
dispatched, so "Quit" (and every other item) was a no-op. The fix routes Linux
through the native Qt StatusNotifierItem tray hosted in the overlay subprocess —
the same tray the Steam Deck Flatpak already uses — and retires pystray on Linux.

should_host_qt_tray is the pure mirror of that choice (used at OverlayController
construction AND in setup_tray, so the two can't drift). Pinning the matrix here
verifies the desktop-vs-Flatpak-vs-macOS behavior headlessly — the tray itself
can't be click-tested in CI.

Importing wayfinder_main pulls in customtkinter/numpy, so the file is skipped
where those deps are absent (matching tests/test_hotkey_backend.py).
"""

import pytest

pytest.importorskip("customtkinter")

from wayfinder_main import should_host_qt_tray  # noqa: E402


# ── Linux: always the Qt tray, pystray retired (desktop and Flatpak alike) ────

@pytest.mark.parametrize("has_pystray", [True, False])
def test_linux_always_uses_qt_tray(has_pystray):
    # Desktop (pystray importable) AND Flatpak (no pystray) both host the Qt tray.
    assert should_host_qt_tray("linux", has_pystray=has_pystray,
                               enable_tray_icon=True) is True


# ── macOS: keep pystray (it works there); Qt tray only if pystray is missing ──

def test_macos_prefers_pystray():
    assert should_host_qt_tray("darwin", has_pystray=True,
                               enable_tray_icon=True) is False


def test_macos_falls_back_to_qt_tray_without_pystray():
    assert should_host_qt_tray("darwin", has_pystray=False,
                               enable_tray_icon=True) is True


# ── The user's "show tray icon" setting wins everywhere ───────────────────────

@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("has_pystray", [True, False])
def test_disabled_setting_hosts_no_tray(platform, has_pystray):
    assert should_host_qt_tray(platform, has_pystray=has_pystray,
                               enable_tray_icon=False) is False

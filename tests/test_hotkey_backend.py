"""Matrix test for resolve_hotkey_backend — the environment→backend decision.

This is the "does the hotkey system work on GNOME / X11 / Flatpak / macOS?" proof
that can't be gathered by running on each machine. resolve_hotkey_backend is a
pure mirror of WayfinderApp.start_hotkey_listener's dispatch, so pinning the whole
matrix here verifies the cross-desktop behavior headlessly:

  - native (non-Flatpak) Linux → evdev, IDENTICALLY on X11 and Wayland, on any DE
    (GNOME, KDE, XFCE…); the compositor/DE never changes the top-level choice.
  - Flatpak → the XDG GlobalShortcuts portal when dbus is present (KDE + GNOME),
    else the pynput XRecord fallback on X11, else socket-only on Wayland.
  - macOS → pynput.

Importing wayfinder_main pulls in customtkinter/numpy, so the file is skipped
where those deps are absent (matching tests/test_dropdown_geometry.py).
"""

import pytest

pytest.importorskip("customtkinter")

from wayfinder_main import resolve_hotkey_backend  # noqa: E402


# ── native (non-Flatpak) Linux: evdev everywhere, X11 == Wayland ──────────────

@pytest.mark.parametrize("session", ["wayland", "x11", "", "tty", "Wayland", "X11"])
def test_native_linux_always_evdev(session):
    # GNOME, KDE, XFCE, i3 — the DE is irrelevant to the top-level pick; the app
    # reads /dev/input directly the same way on X11 and Wayland.
    assert resolve_hotkey_backend("linux", is_flatpak=False,
                                  dbus_available=False, session_type=session) == "evdev"
    # dbus presence doesn't matter for the native path either.
    assert resolve_hotkey_backend("linux", is_flatpak=False,
                                  dbus_available=True, session_type=session) == "evdev"


# ── Flatpak (how the Steam Deck runs it) ─────────────────────────────────────

@pytest.mark.parametrize("session", ["wayland", "x11", ""])
def test_flatpak_with_dbus_uses_portal(session):
    # The cross-desktop standard — works on KDE and GNOME under Wayland, and on X11.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  dbus_available=True, session_type=session) == "portal"


def test_flatpak_x11_without_dbus_falls_back_to_pynput():
    # Deck desktop-mode X11 Flatpak without dbus-python: XRecord global listener.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  dbus_available=False, session_type="x11") == "pynput"
    # Empty/unknown session is treated as not-Wayland → pynput (the shipped default).
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  dbus_available=False, session_type="") == "pynput"


def test_flatpak_wayland_without_dbus_is_unavailable():
    # No portal, no uinput — only the socket trigger works. Must not silently claim
    # a working hotkey.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  dbus_available=False, session_type="wayland") == "unavailable"


# ── macOS ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flatpak", [True, False])
@pytest.mark.parametrize("dbus", [True, False])
def test_macos_always_pynput(flatpak, dbus):
    assert resolve_hotkey_backend("darwin", is_flatpak=flatpak,
                                  dbus_available=dbus, session_type="") == "pynput"


def test_session_type_is_case_insensitive():
    # $XDG_SESSION_TYPE casing must not flip Flatpak-Wayland into the X11 fallback.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  dbus_available=False, session_type="WAYLAND") == "unavailable"

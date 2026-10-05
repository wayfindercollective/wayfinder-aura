"""Matrix test for resolve_hotkey_backend — the environment→backend decision.

This is the "does the hotkey system work on GNOME / X11 / Flatpak / macOS?" proof
that can't be gathered by running on each machine. resolve_hotkey_backend is a
pure mirror of WayfinderApp.start_hotkey_listener's dispatch, so pinning the whole
matrix here verifies the cross-desktop behavior headlessly:

  - native (non-Flatpak) Linux → evdev, IDENTICALLY on X11 and Wayland, on any DE
    (GNOME, KDE, XFCE…); the compositor/DE never changes the top-level choice.
  - Flatpak → the XDG GlobalShortcuts portal when PyGObject is present (KDE +
    GNOME), else the pynput XRecord fallback on X11, else socket-only on Wayland.
  - macOS → pynput.

`portal_available` is the Gio probe (wayfinder.hotkeys.dbus), not dbus-python —
the portal listener speaks GDBus and needs only PyGObject.

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
                                  portal_available=False, session_type=session) == "evdev"
    # Portal availability doesn't matter for the native path either.
    assert resolve_hotkey_backend("linux", is_flatpak=False,
                                  portal_available=True, session_type=session) == "evdev"


# ── Flatpak (how the Steam Deck runs it) ─────────────────────────────────────

@pytest.mark.parametrize("session", ["wayland", "x11", ""])
def test_flatpak_with_gi_uses_portal(session):
    # The cross-desktop standard — works on KDE and GNOME under Wayland, and on X11.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  portal_available=True, session_type=session) == "portal"


def test_flatpak_x11_without_gi_falls_back_to_pynput():
    # Deck desktop-mode X11 Flatpak without PyGObject: XRecord global listener.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  portal_available=False, session_type="x11") == "pynput"
    # Empty/unknown session is treated as not-Wayland → pynput (the shipped default).
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  portal_available=False, session_type="") == "pynput"


def test_flatpak_wayland_without_gi_is_unavailable():
    # No portal, no uinput — only the socket trigger works. Must not silently claim
    # a working hotkey.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  portal_available=False, session_type="wayland") == "unavailable"


# ── macOS ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flatpak", [True, False])
@pytest.mark.parametrize("portal", [True, False])
def test_macos_always_pynput(flatpak, portal):
    assert resolve_hotkey_backend("darwin", is_flatpak=flatpak,
                                  portal_available=portal, session_type="") == "pynput"


def test_session_type_is_case_insensitive():
    # $XDG_SESSION_TYPE casing must not flip Flatpak-Wayland into the X11 fallback.
    assert resolve_hotkey_backend("linux", is_flatpak=True,
                                  portal_available=False, session_type="WAYLAND") == "unavailable"


# ── portal listener retry backoff ────────────────────────────────────────────

def test_portal_retry_delay_doubles_then_caps():
    from wayfinder_main import portal_retry_delay

    assert portal_retry_delay(1) == 20.0
    assert portal_retry_delay(2) == 40.0
    assert portal_retry_delay(5) == 320.0
    assert portal_retry_delay(6) == 600.0


def test_portal_retry_delay_saturates_without_overflow():
    # 10.0 * 2**1024 raises OverflowError on float conversion; a week-long
    # portal outage must not permanently disable retries (Codex review).
    from wayfinder_main import portal_retry_delay

    assert portal_retry_delay(1024) == 600.0
    assert portal_retry_delay(10_000_000) == 600.0


# ── AppImage / source on X11 without the 'input' group ──────────────────────

def _x11(monkeypatch, readable):
    import glob

    import wayfinder_main as wm
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(glob, "glob", lambda pattern: ["/dev/input/event0", "/dev/input/event1"])
    monkeypatch.setattr(wm.os, "access", lambda path, mode: readable)
    return wm


def test_x11_without_readable_devices_is_detected(monkeypatch):
    wm = _x11(monkeypatch, readable=False)
    assert wm.WayfinderApp._x11_without_input_devices() is True
    monkeypatch.setattr(wm.os, "access", lambda path, mode: True)
    assert wm.WayfinderApp._x11_without_input_devices() is False
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")   # no XRecord alternative there
    monkeypatch.setattr(wm.os, "access", lambda path, mode: False)
    assert wm.WayfinderApp._x11_without_input_devices() is False


def test_x11_without_the_input_group_uses_the_x11_listener(monkeypatch):
    from types import SimpleNamespace
    wm = _x11(monkeypatch, readable=False)
    monkeypatch.setattr(wm, "HAS_EVDEV", True)
    started, logs = [], []
    app = SimpleNamespace(log=logs.append, _hotkey_backend="evdev",
                          _x11_without_input_devices=wm.WayfinderApp._x11_without_input_devices,
                          _compositor_owns_hotkeys=lambda: False)
    app._start_pynput_listener = lambda: started.append(True) or setattr(
        app, "_pynput_listener_started", True)
    wm.WayfinderApp._start_evdev_listener(app)
    wm.WayfinderApp._start_evdev_listener(app)   # a config-change restart: no second listener
    assert started == [True] and app._hotkey_backend == "pynput"
    assert "'input' group" in logs[0]



def test_a_kde_owned_shortcut_gets_no_second_x11_listener(monkeypatch):
    """KDE delivers its binding through the socket; an extra X11 listener
    would see the same press and toggle the recording twice."""
    from types import SimpleNamespace
    wm = _x11(monkeypatch, readable=False)
    monkeypatch.setattr(wm, "HAS_EVDEV", True)
    logs = []
    app = SimpleNamespace(log=logs.append, _hotkey_backend="evdev", _hotkey_thread=None,
                          _x11_without_input_devices=wm.WayfinderApp._x11_without_input_devices,
                          _compositor_owns_hotkeys=lambda: True,
                          _start_pynput_listener=lambda: pytest.fail("second listener"))
    wm.WayfinderApp._start_evdev_listener(app)
    assert app._hotkey_backend == "evdev" and "KDE" in logs[0]

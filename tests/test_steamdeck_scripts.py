"""Guards for the host-side Steam Deck trigger scripts.

These are release-facing scripts, not importable package modules. The tests are
mostly static by design: they ensure the documented Deck path stays on the
host-evdev -> Unix-socket daemon and does not regress to the old xdotool/F3
bridge.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
STEAMDECK = REPO / "scripts" / "steamdeck"


def _read(relative: str) -> str:
    return (STEAMDECK / relative).read_text(encoding="utf-8")


def test_trigger_daemon_sends_socket_commands_not_xdotool_f3():
    daemon = _read("wayfinder-trigger-daemon.py")

    assert "socket.AF_UNIX" in daemon
    assert '"wayfinder-aura.sock"' in daemon
    assert "sock.connect(SOCKET_PATH)" in daemon
    assert "sock.send(cmd)" in daemon
    assert 'ecodes.BTN_THUMBR: b"toggle"' in daemon
    assert 'ecodes.KEY_F3: b"toggle"' in daemon
    assert 'ecodes.KEY_F2: b"style"' in daemon

    assert '["xdotool", "key", "F3"]' not in daemon
    assert "/usr/bin/xdotool" not in daemon


def test_trigger_daemon_has_a_no_package_stdlib_evdev_backend(tmp_path):
    """Stock SteamOS cannot compile python-evdev against its stripped headers."""
    daemon = _read("wayfinder-trigger-daemon.py")
    assert "except ImportError:" in daemon
    assert 'INPUT_BACKEND = "stdlib-evdev"' in daemon
    assert 'glob.glob("/dev/input/event*")' in daemon
    assert "fcntl.ioctl" in daemon

    # Shadow a possibly installed evdev with a module that behaves like the
    # dependency being absent, then exercise the script's diagnostic fast path.
    (tmp_path / "evdev.py").write_text("raise ImportError('simulated missing evdev')\n")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(STEAMDECK / "wayfinder-trigger-daemon.py"),
            "--print-input-backend",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stdlib-evdev"


def test_steamdeck_installer_installs_current_daemon_and_disables_legacy_bridge():
    installer = _read("install-steamdeck.sh")

    assert 'install -Dm755 "${SCRIPT_DIR}/wayfinder-trigger-daemon.py"' in installer
    assert 'install -Dm755 "${SCRIPT_DIR}/wayfinder-aura-show-or-start.py"' in installer
    assert '"${APPLICATION_DIR}/wayfinder-aura.desktop"' in installer
    assert 'install -Dm644 "${SYSTEMD_SRC}/wayfinder-trigger.service"' in installer
    assert 'install -Dm755 "${SCRIPT_DIR}/r4-f3-bridge.py"' not in installer
    assert "systemctl --user disable --now r4-f3-bridge.service" in installer
    assert "releases/latest/download/${APP_ID}.flatpak" in installer
    assert "flatpak install --user --noninteractive -y --or-update" in installer
    assert "flatpak info --user" in installer
    assert 'MIN_FLATPAK_VERSION="1.1.7"' in installer
    assert "is older than the SteamOS-safe" in installer
    assert "pacman -S" not in installer
    assert "steamos-readonly disable" not in installer


def test_steamdeck_installer_reads_app_version_with_a_supported_flatpak_flag():
    """`flatpak info` has no --show-version (checked against 1.16).

    The version gate runs under `set -e` before any host file is installed, so
    an unsupported flag aborts the whole install with "Unknown option" and the
    trigger daemon never lands — the back-button trigger then looks broken in
    the app with nothing listening on the socket.
    """
    installer = _read("install-steamdeck.sh")

    assert "--show-version" not in installer
    assert "installed_version=" in installer
    # Parse the human-readable `Version:` field instead.
    assert "Version:" in installer


_SAMPLE_FLATPAK_INFO = """\
Wayfinder Aura - Local voice dictation for Linux

          ID: io.wayfindercollective.WayfinderAura
         Ref: app/io.wayfindercollective.WayfinderAura/x86_64/master
        Arch: x86_64
      Branch: master
     Version: 1.1.7
     License: Elastic-2.0
      Origin: wayfinder
"""


def _installed_version_awk_program() -> str:
    """The awk program the installer uses, read out of the script itself."""
    installer = _read("install-steamdeck.sh")
    line = next(
        (ln for ln in installer.splitlines() if ln.strip().startswith("installed_version=")),
        None,
    )
    assert line, "installed_version assignment not found"
    match = re.search(r"awk '([^']+)'", line)
    assert match, f"version parse no longer uses a single-quoted awk program: {line}"
    return match.group(1)


def test_installer_version_parse_actually_extracts_the_version():
    """Exercise the parse, not just its source text.

    Asserting the string "Version:" appears somewhere in the script is
    satisfied by a comment, so it cannot catch a broken expression.
    """
    result = subprocess.run(
        ["awk", _installed_version_awk_program()],
        input=_SAMPLE_FLATPAK_INFO,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout.strip() == "1.1.7"


def test_installer_pins_the_c_locale_and_refuses_an_unparsable_version():
    """`flatpak info` field labels go through gettext, so a translated
    "Version:" would yield an empty parse — and an empty version makes the
    `sort -V` gate reject a perfectly good install with a nonsense message."""
    installer = _read("install-steamdeck.sh")

    assert "LC_ALL=C flatpak info" in installer, "version read must pin the C locale"
    assert "could not read the installed Wayfinder Aura version" in installer, (
        "an unparsable version must fail loudly, not fall through to the sort -V gate"
    )


def test_steamdeck_services_are_home_portable_and_launch_flatpak_directly():
    app_unit = _read("systemd/wayfinder-aura.service")
    trigger_unit = _read("systemd/wayfinder-trigger.service")
    supervisor_unit = _read("systemd/wayfinder-mode-supervisor.service")

    assert "ExecStart=/usr/bin/flatpak run io.wayfindercollective.WayfinderAura" in app_unit
    assert "ydotool.service" not in app_unit
    assert "ExecStart=%h/.local/bin/wayfinder-trigger-daemon.py" in trigger_unit
    assert "ExecStart=%h/.local/bin/wayfinder-mode-supervisor.py" in supervisor_unit
    assert "/home/deck" not in app_unit + trigger_unit + supervisor_unit


def test_taskbar_launcher_accepts_current_ack_and_legacy_eof_before_starting_service():
    launcher = _read("wayfinder-aura-show-or-start.py")
    desktop = _read("wayfinder-aura.desktop")

    assert 'client.sendall(command.encode("ascii"))' in launcher
    assert 'response in (b"ok", b"")' in launcher
    assert 'os.execvp("flatpak"' not in launcher
    assert '"systemctl", "--user", "start", SERVICE' in launcher
    assert 'COMMANDS = {"show", "toggle", "style", "hide"}' in launcher
    assert "Exec=@WAYFINDER_BIN_DIR@/wayfinder-aura-show-or-start" in desktop
    assert "Exec=@WAYFINDER_BIN_DIR@/wayfinder-aura-show-or-start toggle" in desktop
    assert "/home/deck/dev/wayfinder-aura" not in desktop


def test_steamdeck_uninstaller_removes_every_installed_host_file():
    uninstaller = _read("uninstall-steamdeck.sh")

    assert '"${BIN_DIR}/wayfinder-aura-show-or-start"' in uninstaller
    assert '"${APPLICATION_DIR}/wayfinder-aura.desktop"' in uninstaller
    assert '"${ICON_DIR}/io.wayfindercollective.WayfinderAura.png"' in uninstaller


def test_legacy_bridge_is_disabled_by_default_and_conflicts_with_current_daemon():
    bridge = _read("r4-f3-bridge.py")
    legacy_unit = _read("systemd/r4-f3-bridge.service")
    current_unit = _read("systemd/wayfinder-trigger.service")

    assert 'LEGACY_ENABLE_ENV = "WAYFINDER_ALLOW_LEGACY_R4_F3"' in bridge
    assert "if not legacy_enabled():" in bridge
    assert "sys.exit(1)" in bridge
    assert "Conflicts=wayfinder-trigger.service" in legacy_unit
    assert "Conflicts=r4-f3-bridge.service" in current_unit


def test_legacy_bridge_exits_before_requiring_evdev_unless_explicitly_enabled():
    env = os.environ.copy()
    env.pop("WAYFINDER_ALLOW_LEGACY_R4_F3", None)
    result = subprocess.run(
        [sys.executable, str(STEAMDECK / "r4-f3-bridge.py")],
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 1
    assert "superseded and disabled" in result.stdout
    assert "No module named" not in result.stderr


def test_steamdeck_readme_documents_socket_daemon_as_primary_trigger_path():
    readme = _read("README.md")

    assert "Fresh app installs use **Super+F2**" in readme
    assert "commands directly to the app's Unix socket" in readme
    assert "wayfinder-trigger-daemon.py` — the primary" in readme
    assert "WAYFINDER_ALLOW_LEGACY_R4_F3=1" in readme
    assert "The app itself listens for **F3** (its default record hotkey)" not in readme

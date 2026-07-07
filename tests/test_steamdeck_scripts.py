"""Guards for the host-side Steam Deck trigger scripts.

These are release-facing scripts, not importable package modules. The tests are
mostly static by design: they ensure the documented Deck path stays on the
host-evdev -> Unix-socket daemon and does not regress to the old xdotool/F3
bridge.
"""

from __future__ import annotations

import os
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


def test_steamdeck_installer_installs_current_daemon_and_disables_legacy_bridge():
    installer = _read("install-steamdeck.sh")

    assert 'install -Dm755 "${SCRIPT_DIR}/wayfinder-trigger-daemon.py"' in installer
    assert 'install -Dm644 "${SYSTEMD_SRC}/wayfinder-trigger.service"' in installer
    assert 'install -Dm755 "${SCRIPT_DIR}/r4-f3-bridge.py"' not in installer
    assert "systemctl --user disable --now r4-f3-bridge.service" in installer


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

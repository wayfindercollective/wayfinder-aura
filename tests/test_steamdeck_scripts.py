"""Guards for the host-side Steam Deck trigger scripts.

These are release-facing scripts, not importable package modules. The tests are
mostly static by design: they ensure the documented Deck path stays on the
host-evdev -> Unix-socket daemon and does not regress to the old xdotool/F3
bridge.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


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


@pytest.mark.linux_only
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
    if shutil.which("awk") is None:
        pytest.skip("awk is not installed (the installer only runs on SteamOS)")
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


def test_steamdeck_units_log_to_the_journal_not_a_runtime_dir_file():
    """systemd opens StandardOutput=append: before it creates RuntimeDirectory=.

    %t is a tmpfs, so on a fresh boot the directory is missing, the open fails
    and the unit dies with 209/STDOUT before RuntimeDirectory= is ever reached.
    Nothing then creates the directory, so Restart= cannot recover either.
    """
    for name in (
        "wayfinder-aura.service",
        "wayfinder-mode-supervisor.service",
        "wayfinder-trigger.service",
        "r4-f3-bridge.service",
    ):
        unit = _read(f"systemd/{name}")
        assert "StandardOutput=journal" in unit, name
        assert "StandardError=journal" in unit, name
        assert not re.search(
            r"^Standard(Output|Error)=(append|file|truncate):", unit, re.MULTILINE
        ), name
        assert "RuntimeDirectory" not in unit, name


def test_nothing_points_at_the_retired_runtime_log_files():
    support = (REPO / "SUPPORT.md").read_text(encoding="utf-8")
    readme = _read("README.md")
    for text in (
        support,
        readme,
        _read("install-steamdeck.sh"),
        _read("systemd/wayfinder-aura-failed.service"),
        (REPO / "scripts" / "wayfinder-aura-launcher-steamdeck.sh").read_text(encoding="utf-8"),
    ):
        assert "wayfinder-aura-logs" not in text
        assert "/tmp/wfa-stderr" not in text

    for service in ("wayfinder-trigger.service", "wayfinder-mode-supervisor.service"):
        assert f"journalctl --user -u {service}" in support
        assert f"journalctl --user -u {service}" in readme
    assert "journalctl --user -u wayfinder-aura.service" in _read(
        "systemd/wayfinder-aura-failed.service"
    )


WAIT_HELPER = STEAMDECK / "wayfinder-aura-wait-desktop.sh"


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _run_wait_helper(tmp_path, *, game=False, kde_after=0, portal="ok"):
    """Run the helper against stubs and return (exit code, calls it made).

    systemctl, busctl and sleep all resolve to stubs, so nothing real is
    touched and no time passes. kde_after is how many environment reads still
    see gamescope's stale value. portal is "ok", "after-restart" or "never".
    """
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    log.touch()
    _stub(stubs, "systemctl", """\
echo "systemctl $*" >> "$STUB_LOG"
case "$*" in
  *is-active*) [ "$STUB_GAME" = 1 ]; exit $? ;;
  *show-environment*)
    seen=$(grep -c show-environment "$STUB_LOG")
    if [ "$seen" -gt "$STUB_KDE_AFTER" ]; then
      echo XDG_CURRENT_DESKTOP=KDE
    else
      echo XDG_CURRENT_DESKTOP=gamescope
    fi ;;
esac
exit 0
""")
    _stub(stubs, "busctl", """\
echo "busctl $*" >> "$STUB_LOG"
case "$STUB_PORTAL" in
  ok) exit 0 ;;
  after-restart) grep -q 'restart xdg-desktop-portal' "$STUB_LOG" ;;
  *) exit 1 ;;
esac
""")
    _stub(stubs, "sleep", 'echo "sleep $*" >> "$STUB_LOG"\n')
    env = {
        **os.environ,
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "STUB_GAME": "1" if game else "0",
        "STUB_KDE_AFTER": str(kde_after),
        "STUB_PORTAL": portal,
    }
    result = subprocess.run(
        ["sh", str(WAIT_HELPER)],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    return result.returncode, log.read_text(encoding="utf-8").splitlines()


@pytest.mark.linux_only
def test_wait_helper_does_not_wait_in_game_mode(tmp_path):
    code, calls = _run_wait_helper(tmp_path, game=True)

    assert code == 0
    assert calls == ["systemctl --user -q is-active gamescope-session.service"]


@pytest.mark.linux_only
def test_wait_helper_holds_the_portal_back_until_plasma_publishes_its_environment(tmp_path):
    """Waking the portal on gamescope's stale environment is the whole bug: it
    then serves gamescope's backends for the rest of the session."""
    code, calls = _run_wait_helper(tmp_path, kde_after=3)

    assert code == 0
    environment_reads = [i for i, call in enumerate(calls) if "show-environment" in call]
    first_portal_call = next(i for i, call in enumerate(calls) if call.startswith("busctl"))
    assert len(environment_reads) == 4
    assert first_portal_call > environment_reads[-1]
    assert calls.count("sleep 1") == 3
    assert not any("restart" in call for call in calls)


@pytest.mark.linux_only
def test_wait_helper_restarts_a_portal_that_came_up_without_global_shortcuts(tmp_path):
    code, calls = _run_wait_helper(tmp_path, portal="after-restart")

    assert code == 0
    assert [call for call in calls if "restart" in call] == [
        "systemctl --user --no-block restart xdg-desktop-portal.service"
    ]
    assert calls[-1].startswith("busctl")


@pytest.mark.linux_only
def test_aura_start_timeout_exceeds_the_wait_helpers_worst_case(tmp_path):
    """A leading "-" forgives a failed ExecStartPre but not an expired
    TimeoutStartSec, so the wait has to be capped below the start timeout."""
    code, calls = _run_wait_helper(tmp_path, kde_after=100000, portal="never")
    assert code == 0, "a helper that gives up must still let Aura start"

    sleeps = [call for call in calls if call.startswith("sleep")]
    assert set(sleeps) == {"sleep 1"}
    portal_calls = [call for call in calls if call.startswith("busctl")]
    per_call = int(re.search(r"--timeout=(\d+)", portal_calls[0]).group(1))
    worst_case = len(sleeps) + len(portal_calls) * per_call

    unit = _read("systemd/wayfinder-aura.service")
    capped = re.search(
        r"^ExecStartPre=-/usr/bin/timeout --kill-after=(\d+) (\d+) "
        r"%h/\.local/bin/wayfinder-aura-wait-desktop$",
        unit,
        re.MULTILINE,
    )
    assert capped, "the wait must be failure-tolerant (-) and capped"
    kill_after, limit = int(capped.group(1)), int(capped.group(2))
    start_timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", unit, re.MULTILINE).group(1))

    assert worst_case <= limit
    assert limit + kill_after < start_timeout


def _run_deck_script(tmp_path, script: str, *args: str) -> Path:
    """Run an installer script with HOME sandboxed and systemctl/flatpak stubbed."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    _stub(stubs, "systemctl", "exit 0\n")
    _stub(stubs, "flatpak", 'echo "     Version: 9.9.9"\n')
    result = subprocess.run(
        ["bash", str(STEAMDECK / script), *args],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        },
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return home


@pytest.mark.linux_only
def test_uninstall_removes_every_file_install_adds(tmp_path):
    home = _run_deck_script(tmp_path, "install-steamdeck.sh", "--skip-flatpak")
    helper = home / ".local" / "bin" / "wayfinder-aura-wait-desktop"
    assert os.access(helper, os.X_OK)
    assert helper.read_text(encoding="utf-8") == WAIT_HELPER.read_text(encoding="utf-8")

    _run_deck_script(tmp_path, "uninstall-steamdeck.sh")

    assert [path for path in home.rglob("*") if not path.is_dir()] == []


@pytest.mark.linux_only
def test_uninstall_clears_hand_made_drop_ins_on_a_deck_that_never_reinstalled(tmp_path):
    """Otherwise wait-desktop.conf is left naming a helper uninstall just removed."""
    units = tmp_path / "home" / ".config" / "systemd" / "user"
    aura_drop_ins = units / "wayfinder-aura.service.d"
    aura_drop_ins.mkdir(parents=True)
    (aura_drop_ins / "wait-desktop.conf").write_text("[Service]\n")
    (aura_drop_ins / "journal-logs.conf").write_text("[Service]\n")

    _run_deck_script(tmp_path, "uninstall-steamdeck.sh")

    assert not aura_drop_ins.exists()


@pytest.mark.linux_only
def test_reinstall_over_the_hand_made_drop_ins_leaves_one_failure_tolerant_wait(tmp_path):
    """The hand-made wait-desktop.conf has no "-" prefix. Left in place it would
    run the wait a second time, and that run could fail the start."""
    units = tmp_path / "home" / ".config" / "systemd" / "user"
    for service in ("wayfinder-aura", "wayfinder-mode-supervisor", "wayfinder-trigger"):
        drop_ins = units / f"{service}.service.d"
        drop_ins.mkdir(parents=True)
        (drop_ins / "journal-logs.conf").write_text("[Service]\nStandardOutput=journal\n")
    aura_drop_ins = units / "wayfinder-aura.service.d"
    (aura_drop_ins / "wait-desktop.conf").write_text(
        "[Service]\nExecStartPre=%h/.local/bin/wayfinder-aura-wait-desktop\n"
    )
    (aura_drop_ins / "local.conf").write_text("[Service]\nEnvironment=KEEP=1\n")

    _run_deck_script(tmp_path, "install-steamdeck.sh", "--skip-flatpak")

    assert [path.name for path in aura_drop_ins.iterdir()] == ["local.conf"]
    assert not (units / "wayfinder-trigger.service.d").exists()
    assert not (units / "wayfinder-mode-supervisor.service.d").exists()
    effective = (units / "wayfinder-aura.service").read_text() + (
        aura_drop_ins / "local.conf"
    ).read_text()
    assert effective.count("ExecStartPre=") == 1
    assert "ExecStartPre=-/usr/bin/timeout" in effective

    _run_deck_script(tmp_path, "uninstall-steamdeck.sh")

    assert [path.name for path in aura_drop_ins.iterdir()] == ["local.conf"]

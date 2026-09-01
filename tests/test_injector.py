"""
Tests for wayfinder.core.injector module.

Tests text injection via ydotool: typing speeds, binary resolution,
socket discovery, error handling, and whitespace behavior.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from wayfinder.core.injector import (
    InjectionError,
    TYPING_SPEEDS,
    inject_text,
    press_enter,
    prime_wayland_injection,
    _get_ydotool_binary,
    _get_ydotool_env,
    _inject_text_type_linux,
    _inject_text_xdotool,
    _open_modifier_probe,
    _send_ctrl_v_linux,
    _wait_for_modifier_release,
    _x11_held_modifiers,
    _X_HELD_MODIFIER_BITS,
)


# =============================================================================
# prime_wayland_injection — surface KDE's "allow input" approval before dictation
# =============================================================================
class TestPrimeWaylandInjection:
    """The startup primer is a no-op except on the wtype/Wayland path, where it
    fires a benign Shift no-op to trigger KDE's one-time approval early."""

    def test_noop_when_injector_is_not_wtype(self):
        with patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
             patch("subprocess.run") as mock_run:
            ran, msg = prime_wayland_injection()
        assert ran is False
        assert msg == ""
        mock_run.assert_not_called()  # never touch the desktop's ydotool path

    def test_fires_benign_shift_noop_on_wtype(self):
        result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.utils.platform.get_text_injector", return_value="wtype"), \
             patch("wayfinder.core.injector.shutil.which", return_value="/usr/bin/wtype"), \
             patch("subprocess.run", return_value=result) as mock_run:
            ran, msg = prime_wayland_injection()
        assert ran is True
        cmd = mock_run.call_args[0][0]
        # Press+release Shift = no character typed, but it exercises the protocol.
        assert cmd == ["wtype", "-M", "shift", "-m", "shift"]

    def test_reports_failure_without_raising_when_wtype_missing(self):
        with patch("wayfinder.utils.platform.get_text_injector", return_value="wtype"), \
             patch("wayfinder.core.injector.shutil.which", return_value=None):
            ran, msg = prime_wayland_injection()
        assert ran is False
        assert "wtype not found" in msg

    def test_nonzero_exit_is_reported_not_raised(self):
        result = MagicMock(returncode=1, stdout="", stderr="boom")
        with patch("wayfinder.utils.platform.get_text_injector", return_value="wtype"), \
             patch("wayfinder.core.injector.shutil.which", return_value="/usr/bin/wtype"), \
             patch("subprocess.run", return_value=result):
            ran, msg = prime_wayland_injection()
        assert ran is False
        assert "boom" in msg


# =============================================================================
# TYPING_SPEEDS constant
# =============================================================================


class TestTypingSpeeds:
    """Tests for the TYPING_SPEEDS dictionary."""

    def test_instant_speed(self):
        # 2ms is the proven floor: 1ms can lose the first XWayland key press
        # and can race ydotool's synthetic Shift around punctuation.
        assert TYPING_SPEEDS["instant"] == (2, 2)

    def test_fast_speed(self):
        assert TYPING_SPEEDS["fast"] == (2, 2)

    def test_normal_speed(self):
        assert TYPING_SPEEDS["normal"] == (12, 12)

    def test_slow_speed(self):
        assert TYPING_SPEEDS["slow"] == (50, 20)

    def test_very_slow_speed(self):
        assert TYPING_SPEEDS["very_slow"] == (100, 50)

    def test_has_exactly_five_presets(self):
        assert len(TYPING_SPEEDS) == 5


# =============================================================================
# inject_text — early returns
# =============================================================================


class TestInjectTextEarlyReturn:
    """Tests for inject_text returning early without calling subprocess."""

    def test_empty_string_returns_early(self, mock_ydotool_success):
        inject_text("", "instant")
        mock_ydotool_success.assert_not_called()

    def test_none_returns_early(self, mock_ydotool_success):
        inject_text(None, "instant")
        mock_ydotool_success.assert_not_called()

    def test_unimplemented_platform_fails_closed_before_linux_tool_selection(self):
        with patch("wayfinder.core.injector.sys.platform", "win32"), \
             patch("wayfinder.utils.platform.get_text_injector") as mock_selector:
            with pytest.raises(InjectionError, match="not implemented"):
                inject_text("hello", "instant")

        mock_selector.assert_not_called()

    def test_whitespace_only_returns_early(self, mock_ydotool_success):
        inject_text("   \t\n  ", "instant")
        mock_ydotool_success.assert_not_called()

    def test_newlines_only_returns_early(self, mock_ydotool_success):
        inject_text("\n\n\n", "instant")
        mock_ydotool_success.assert_not_called()


# =============================================================================
# inject_text — correct subprocess calls
# =============================================================================


class TestInjectTextSubprocess:
    """Tests for inject_text building and executing the correct ydotool command."""

    def test_calls_ydotool_with_correct_structure(self, mock_ydotool_success):
        """inject_text should call ydotool type --key-delay D --key-hold H -- TEXT."""
        inject_text("hello world", "instant")
        mock_ydotool_success.assert_called_once()
        cmd = mock_ydotool_success.call_args[0][0]
        # Verify command structure
        assert cmd[1] == "type"
        assert "--key-delay" in cmd
        assert "--key-hold" in cmd
        assert "--" in cmd
        assert cmd[-1] == "hello world"

    def test_strips_leading_trailing_whitespace(self, mock_ydotool_success):
        inject_text("  trimmed text  ", "instant")
        cmd = mock_ydotool_success.call_args[0][0]
        assert cmd[-1] == "trimmed text"

    @pytest.mark.parametrize(
        "speed,expected_delay,expected_hold",
        [
            ("instant", "2", "2"),
            ("fast", "2", "2"),
            ("normal", "12", "12"),
            ("slow", "50", "20"),
            ("very_slow", "100", "50"),
        ],
    )
    def test_typing_speed_delay_values(
        self, mock_ydotool_success, speed, expected_delay, expected_hold
    ):
        inject_text("test", speed)
        cmd = mock_ydotool_success.call_args[0][0]
        delay_idx = cmd.index("--key-delay") + 1
        hold_idx = cmd.index("--key-hold") + 1
        assert cmd[delay_idx] == expected_delay
        assert cmd[hold_idx] == expected_hold

    def test_unknown_speed_uses_safe_default(self, mock_ydotool_success):
        # Unknown speeds fall back to inject_text's (2, 2) "safe default", not instant.
        inject_text("test", "warp_speed")
        cmd = mock_ydotool_success.call_args[0][0]
        delay_idx = cmd.index("--key-delay") + 1
        hold_idx = cmd.index("--key-hold") + 1
        assert cmd[delay_idx] == "2"
        assert cmd[hold_idx] == "2"

    def test_passes_timeout_120(self, mock_ydotool_success):
        inject_text("test", "instant")
        kwargs = mock_ydotool_success.call_args[1]
        assert kwargs["timeout"] == 120

    def test_passes_capture_output_and_text(self, mock_ydotool_success):
        inject_text("test", "instant")
        kwargs = mock_ydotool_success.call_args[1]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True

    def test_passes_env_dict(self, mock_ydotool_success):
        inject_text("test", "instant")
        kwargs = mock_ydotool_success.call_args[1]
        assert isinstance(kwargs["env"], dict)


# =============================================================================
# inject_text — error handling
# =============================================================================


class TestInjectTextErrors:
    """Tests for InjectionError being raised on various failures."""

    def test_nonzero_returncode_raises_injection_error(self, mock_ydotool_failure):
        with pytest.raises(InjectionError, match="ydotool type failed"):
            inject_text("hello", "instant")

    def test_error_message_includes_stderr(self, mock_ydotool_failure):
        with pytest.raises(InjectionError, match="ydotool daemon not running"):
            inject_text("hello", "instant")

    def test_timeout_raises_injection_error(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "mocked")), \
             patch("wayfinder.core.injector._get_ydotool_binary", return_value="/usr/bin/ydotool"), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ydotool", timeout=120)):
            mock_sys.platform = "linux"
            with pytest.raises(InjectionError, match="timed out"):
                inject_text("hello", "instant")

    def test_file_not_found_raises_injection_error(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "mocked")), \
             patch("wayfinder.core.injector._get_ydotool_binary", return_value="/usr/bin/ydotool"), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=FileNotFoundError("ydotool")):
            mock_sys.platform = "linux"
            with pytest.raises(InjectionError, match="ydotool not found"):
                inject_text("hello", "instant")


# =============================================================================
# _get_ydotool_binary
# =============================================================================


class TestGetYdotoolBinary:
    """Tests for finding the ydotool binary.

    Contract (2026-07): prefer the HOST's ydotool client — it protocol-matches
    the host ydotoold; a bundled client against a foreign daemon silently
    typed nothing ("backend unavailable" + direct-uinput fallback). The
    bundled client is only a fallback for hosts without ydotool installed.
    """

    def _host_client(self, temp_dir):
        hostdir = temp_dir / "hostbin"
        hostdir.mkdir()
        host = hostdir / "ydotool"
        host.write_text("#!/bin/sh\n")
        host.chmod(0o755)
        return hostdir, host

    def test_prefers_host_client_over_bundle(
        self, monkeypatch, temp_dir, appimage_env
    ):
        """Host ydotool on PATH wins even when a bundled one exists."""
        bundled = appimage_env / "usr" / "bin" / "ydotool"
        bundled.touch()
        bundled.chmod(0o755)
        hostdir, host = self._host_client(temp_dir)
        # AppRun-style PATH: bundle dir first, host dir after
        monkeypatch.setenv("PATH", f"{appimage_env / 'usr' / 'bin'}:{hostdir}")

        assert _get_ydotool_binary() == str(host)

    def test_host_client_when_no_appdir(self, monkeypatch, temp_dir):
        monkeypatch.delenv("APPDIR", raising=False)
        hostdir, host = self._host_client(temp_dir)
        monkeypatch.setenv("PATH", str(hostdir))
        assert _get_ydotool_binary() == str(host)

    def test_no_bundle_fallback_ever(self, monkeypatch, temp_dir, appimage_env):
        """A bundled client is NEVER used, even with no host client on PATH.

        Jammy's bundled 0.1.8 didn't speak the app's CLI, and any bundled
        client can protocol-mismatch the host daemon (silent no-op typing).
        """
        bundled = appimage_env / "usr" / "bin" / "ydotool"
        bundled.touch()
        bundled.chmod(0o755)
        emptydir = temp_dir / "emptybin"
        emptydir.mkdir()
        monkeypatch.setenv("PATH", f"{appimage_env / 'usr' / 'bin'}:{emptydir}")

        assert _get_ydotool_binary() is None

    def test_none_when_no_client_anywhere(self, monkeypatch, temp_dir):
        """APPDIR set, nothing bundled, nothing on host → None."""
        appdir = temp_dir / "EmptyAppDir"
        appdir.mkdir()
        monkeypatch.setenv("APPDIR", str(appdir))
        emptydir = temp_dir / "emptybin"
        emptydir.mkdir()
        monkeypatch.setenv("PATH", str(emptydir))

        assert _get_ydotool_binary() is None

    def test_appdir_exclusion_is_realpath_based(self, monkeypatch, temp_dir):
        """A sibling dir sharing the APPDIR string prefix is NOT excluded."""
        appdir = temp_dir / "AppDir"
        (appdir / "usr" / "bin").mkdir(parents=True)
        lookalike = temp_dir / "AppDir2"  # startswith('.../AppDir') would wrongly skip this
        lookalike.mkdir()
        host = lookalike / "ydotool"
        host.write_text("#!/bin/sh\n")
        host.chmod(0o755)
        monkeypatch.setenv("APPDIR", str(appdir))
        monkeypatch.setenv("PATH", str(lookalike))

        assert _get_ydotool_binary() == str(host)


# =============================================================================
# _get_ydotool_env
# =============================================================================


class TestGetYdotoolEnv:
    """Tests for finding the ydotool socket path."""

    def test_finds_socket_at_run_ydotool(self):
        """First priority: /run/ydotool/ydotool.sock."""
        def fake_exists(path_self):
            return str(path_self) == "/run/ydotool/ydotool.sock"

        with patch.object(Path, "exists", fake_exists):
            env = _get_ydotool_env()
            assert env.get("YDOTOOL_SOCKET") == "/run/ydotool/ydotool.sock"

    def test_finds_socket_at_user_runtime(self):
        """Second priority: /run/user/{uid}/.ydotool_socket."""
        uid = os.getuid()
        socket_path = f"/run/user/{uid}/.ydotool_socket"

        def fake_exists(path_self):
            return str(path_self) == socket_path

        with patch.object(Path, "exists", fake_exists):
            env = _get_ydotool_env()
            assert env.get("YDOTOOL_SOCKET") == socket_path

    def test_finds_socket_at_tmp(self):
        """Third priority: /tmp/.ydotool_socket."""
        def fake_exists(path_self):
            return str(path_self) == "/tmp/.ydotool_socket"

        with patch.object(Path, "exists", fake_exists):
            env = _get_ydotool_env()
            assert env.get("YDOTOOL_SOCKET") == "/tmp/.ydotool_socket"

    def test_no_socket_found_omits_env_var(self):
        """When no socket exists, YDOTOOL_SOCKET should not be set."""
        with patch.object(Path, "exists", return_value=False):
            env = _get_ydotool_env()
            assert "YDOTOOL_SOCKET" not in env

    def test_env_includes_inherited_environment(self):
        """Returned env should contain the process's existing variables."""
        with patch.object(Path, "exists", return_value=False):
            env = _get_ydotool_env()
            # Should contain at least PATH (present in virtually all envs)
            assert "PATH" in env


# =============================================================================
# InjectionError
# =============================================================================


class TestInjectionError:
    """Basic tests for the InjectionError exception class."""

    def test_is_exception_subclass(self):
        assert issubclass(InjectionError, Exception)

    def test_message_preserved(self):
        err = InjectionError("custom message")
        assert str(err) == "custom message"


# =============================================================================
# _inject_text_xdotool — the PREFERRED X11 path + drift-refocus logic
# =============================================================================


def _xdotool_dispatcher(active_window="win-1", type_result=None):
    """Build a subprocess.run side_effect that routes by xdotool subcommand.

    getactivewindow -> returns `active_window` on stdout; windowactivate/type ->
    return `type_result` (default success). Any windowactivate/type call is also
    recorded so tests can assert whether the drift-refocus fired.
    """
    if type_result is None:
        type_result = MagicMock(returncode=0, stdout="", stderr="")

    def side_effect(cmd, *args, **kwargs):
        subcmd = cmd[1] if len(cmd) > 1 else ""
        if subcmd == "getactivewindow":
            return MagicMock(returncode=0, stdout=f"{active_window}\n", stderr="")
        return type_result

    return side_effect


class TestInjectTextXdotool:
    """Tests for _inject_text_xdotool: the reliable X11 XTEST path and its
    getactivewindow -> windowactivate --sync -> 60ms-settle drift-refocus guard
    (a redundant activate before typing previously dropped whole injections)."""

    def test_already_focused_skips_windowactivate(self):
        # getactivewindow == target_window -> type straight away, NO windowactivate.
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher(active_window="win-42")) as mock_run, \
             patch("time.sleep") as mock_sleep:
            _inject_text_xdotool("hello", "instant", target_window="win-42")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert not any("windowactivate" in cmd for cmd in calls)
        # No settle sleep when we didn't activate.
        mock_sleep.assert_not_called()
        # getactivewindow then type — two calls.
        assert calls[0] == ["xdotool", "getactivewindow"]
        assert calls[-1][1] == "type"

    def test_drift_activates_target_before_typing(self):
        # getactivewindow != target_window -> windowactivate --sync <target> then settle then type.
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher(active_window="other-win")) as mock_run, \
             patch("time.sleep") as mock_sleep:
            _inject_text_xdotool("hello", "instant", target_window="win-42")

        calls = [c.args[0] for c in mock_run.call_args_list]
        activate = ["xdotool", "windowactivate", "--sync", "win-42"]
        assert activate in calls
        # windowactivate must precede the type call.
        activate_idx = calls.index(activate)
        type_idx = next(i for i, cmd in enumerate(calls) if cmd[1] == "type")
        assert activate_idx < type_idx
        # Settle so the target toolkit completes FocusIn before the first key.
        mock_sleep.assert_called_once_with(0.10)

    def test_no_target_window_skips_focus_check(self):
        # Without a target_window there's no getactivewindow/windowactivate — just type.
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher()) as mock_run, \
             patch("time.sleep") as mock_sleep:
            _inject_text_xdotool("hello", "instant")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert len(calls) == 1
        assert calls[0][1] == "type"
        mock_sleep.assert_not_called()

    def test_type_command_passes_text_as_trailing_arg(self):
        # Text is a trailing argv element after `--` — no shell, no injection surface.
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher()) as mock_run, \
             patch("time.sleep"):
            _inject_text_xdotool("rm -rf ~; echo pwned", "instant")

        type_cmd = next(c.args[0] for c in mock_run.call_args_list if c.args[0][1] == "type")
        assert type_cmd[0] == "xdotool"
        assert "--" in type_cmd
        assert type_cmd[-1] == "rm -rf ~; echo pwned"
        # The text sits after the `--` guard so it can never be read as a flag.
        assert type_cmd.index("--") == len(type_cmd) - 2

    def test_typing_speed_maps_to_delay(self):
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher()) as mock_run, \
             patch("time.sleep"):
            _inject_text_xdotool("hi", "normal")

        type_cmd = next(c.args[0] for c in mock_run.call_args_list if c.args[0][1] == "type")
        delay_idx = type_cmd.index("--delay") + 1
        assert type_cmd[delay_idx] == "12"

    def test_unknown_speed_uses_safe_default_delay(self):
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher()) as mock_run, \
             patch("time.sleep"):
            _inject_text_xdotool("hi", "warp_speed")

        type_cmd = next(c.args[0] for c in mock_run.call_args_list if c.args[0][1] == "type")
        delay_idx = type_cmd.index("--delay") + 1
        assert type_cmd[delay_idx] == "2"

    def test_nonzero_exit_raises_injection_error(self):
        failure = MagicMock(returncode=1, stdout="", stderr="X connection refused")
        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=_xdotool_dispatcher(type_result=failure)), \
             patch("time.sleep"):
            with pytest.raises(InjectionError, match="xdotool failed"):
                _inject_text_xdotool("hello", "instant")

    def test_missing_binary_raises_injection_error(self):
        # xdotool absent from PATH -> FileNotFoundError -> graceful InjectionError.
        def side_effect(cmd, *args, **kwargs):
            raise FileNotFoundError("xdotool")

        with patch("wayfinder.core.injector.subprocess.run", side_effect=side_effect), \
             patch("time.sleep"):
            with pytest.raises(InjectionError, match="xdotool not found"):
                _inject_text_xdotool("hello", "instant")

    def test_timeout_raises_injection_error(self):
        def side_effect(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="xdotool", timeout=120)

        with patch("wayfinder.core.injector.subprocess.run", side_effect=side_effect), \
             patch("time.sleep"):
            with pytest.raises(InjectionError, match="timed out"):
                _inject_text_xdotool("hello", "instant")

    def test_focus_check_failure_falls_through_to_type(self):
        # If getactivewindow itself raises, already_focused is False but the activate
        # is best-effort (swallowed) — typing must still happen (no crash).
        def side_effect(cmd, *args, **kwargs):
            if cmd[1] == "getactivewindow":
                raise OSError("display gone")
            if cmd[1] == "windowactivate":
                raise OSError("display gone")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("wayfinder.core.injector.subprocess.run",
                   side_effect=side_effect) as mock_run, \
             patch("time.sleep"):
            _inject_text_xdotool("hello", "instant", target_window="win-42")

        type_cmd = next(c.args[0] for c in mock_run.call_args_list if c.args[0][1] == "type")
        assert type_cmd[-1] == "hello"


class TestUncertainDelivery:
    """Exit-0 'backend unavailable' means delivery is UNCERTAIN, never success."""

    def _unavailable_result(self):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = "ydotool: notice: ydotoold backend unavailable (may have latency+delay issues)"
        return m

    def test_backend_unavailable_raises_uncertain(self):
        from wayfinder.core.injector import InjectionError, _check_ydotool_result

        with pytest.raises(InjectionError) as exc:
            _check_ydotool_result(self._unavailable_result(), "type")
        assert getattr(exc.value, "uncertain_delivery", False) is True
        assert "uncertain" in str(exc.value)

    def test_clean_success_passes(self):
        from wayfinder.core.injector import _check_ydotool_result

        m = MagicMock(returncode=0, stdout="", stderr="")
        _check_ydotool_result(m, "type")  # must not raise

    def test_game_mode_paste_retry_suppressed_for_uncertain(self, monkeypatch):
        """Uncertain delivery must NOT trigger the clipboard retry (double-type risk)."""
        from wayfinder.core import injector as inj

        err = inj.InjectionError("uncertain")
        err.uncertain_delivery = True

        def raise_uncertain(*a, **k):
            raise err

        pasted = []
        monkeypatch.setattr(inj, "_inject_text_type_linux", raise_uncertain)
        monkeypatch.setattr(inj, "inject_text_clipboard_paste", lambda t: pasted.append(t))
        monkeypatch.setattr(inj.sys, "platform", "linux")

        with pytest.raises(inj.InjectionError):
            inj.inject_text("hello", "instant", game_mode=True, paste_fallback=True)
        assert pasted == []

    def test_game_mode_paste_retry_still_works_for_certain_failures(self, monkeypatch):
        from wayfinder.core import injector as inj

        def raise_plain(*a, **k):
            raise inj.InjectionError("wtype refused")

        pasted = []
        monkeypatch.setattr(inj, "_inject_text_type_linux", raise_plain)
        monkeypatch.setattr(inj, "inject_text_clipboard_paste", lambda t: pasted.append(t))
        monkeypatch.setattr(inj.sys, "platform", "linux")

        inj.inject_text("hello", "instant", game_mode=True, paste_fallback=True)
        assert pasted == ["hello"]


class TestSocketSelectionPrefersConnectable:
    """A stale system socket FILE must not mask a live user socket (Codex P1)."""

    def test_stale_system_socket_does_not_mask_live_user_socket(
        self, monkeypatch, temp_dir
    ):
        import socket as socketlib
        from wayfinder.core import injector as inj

        stale_system = temp_dir / "system.sock"
        stale_system.touch()  # exists, nothing listening
        live_user = temp_dir / "user.sock"
        server = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_STREAM)
        try:
            server.bind(str(live_user))
            server.listen(1)
            monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
            monkeypatch.setattr(
                inj, "_ydotool_socket_candidates",
                lambda: [str(stale_system), str(live_user)],
            )
            env = inj._get_ydotool_env()
        finally:
            server.close()
        assert env["YDOTOOL_SOCKET"] == str(live_user)

    def test_falls_back_to_first_existing_when_none_connectable(
        self, monkeypatch, temp_dir
    ):
        from wayfinder.core import injector as inj

        stale_a = temp_dir / "a.sock"
        stale_a.touch()
        stale_b = temp_dir / "b.sock"
        stale_b.touch()
        monkeypatch.delenv("YDOTOOL_SOCKET", raising=False)
        monkeypatch.setattr(
            inj, "_ydotool_socket_candidates",
            lambda: [str(stale_a), str(stale_b)],
        )
        env = inj._get_ydotool_env()
        # Named for error messages, and check_ydotool_ready's probe then fails it.
        assert env["YDOTOOL_SOCKET"] == str(stale_a)


class TestProbeDgramSocket:
    """ydotoold binds a DGRAM socket — the probe must detect it (field bug:
    a STREAM-only probe got EPROTOTYPE against every LIVE daemon)."""

    def test_live_dgram_socket_probes_true(self, temp_dir):
        import socket as socketlib
        from wayfinder.core.injector import _probe_unix_socket

        path = temp_dir / "dgram.sock"
        server = socketlib.socket(socketlib.AF_UNIX, socketlib.SOCK_DGRAM)
        try:
            server.bind(str(path))
            assert _probe_unix_socket(str(path)) is True
        finally:
            server.close()

    def test_plain_file_probes_false(self, temp_dir):
        from wayfinder.core.injector import _probe_unix_socket

        path = temp_dir / "stale"
        path.touch()
        assert _probe_unix_socket(str(path)) is False


class TestYdotoolUserServiceRecovery:
    """Recover the Bazzite login race after /dev/uinput gains its uaccess ACL."""

    def test_resets_failed_loaded_user_unit_and_waits_for_socket(self, monkeypatch):
        from wayfinder.core import injector as inj

        monkeypatch.setattr(inj, "_YDOTOOL_USER_SERVICE_RESTART_ATTEMPTED", False)
        monkeypatch.setattr("wayfinder.utils.platform.is_wayland", lambda: True)
        monkeypatch.setattr(inj.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(inj.os.path, "exists", lambda path: path == "/dev/uinput")
        monkeypatch.setattr(inj.os, "access", lambda path, mode: True)
        monkeypatch.setattr("wayfinder.utils.hostexec.host_env", lambda: {"PATH": "/usr/bin"})
        monkeypatch.setattr(
            inj, "_get_ydotool_env", lambda: {"YDOTOOL_SOCKET": "/run/user/1000/.ydotool_socket"}
        )
        monkeypatch.setattr(inj, "_probe_unix_socket", lambda path, timeout=1.0: True)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "show" in cmd:
                loaded = cmd[-1] == "ydotoold.service"
                return MagicMock(returncode=0, stdout="loaded\n" if loaded else "not-found\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(inj.subprocess, "run", fake_run)

        ready, detail = inj._restart_ydotool_user_service_once()

        assert ready is True
        assert detail == "restarted ydotoold.service"
        assert ["systemctl", "--user", "reset-failed", "ydotoold.service"] in calls
        assert ["systemctl", "--user", "start", "ydotoold.service"] in calls

    def test_does_not_restart_without_uinput_access(self, monkeypatch):
        from wayfinder.core import injector as inj

        monkeypatch.setattr(inj, "_YDOTOOL_USER_SERVICE_RESTART_ATTEMPTED", False)
        monkeypatch.setattr("wayfinder.utils.platform.is_wayland", lambda: True)
        monkeypatch.setattr(inj.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(inj.os.path, "exists", lambda path: path == "/dev/uinput")
        monkeypatch.setattr(inj.os, "access", lambda path, mode: False)
        run = MagicMock()
        monkeypatch.setattr(inj.subprocess, "run", run)

        ready, detail = inj._restart_ydotool_user_service_once()

        assert ready is False
        assert "/dev/uinput is not writable" in detail
        run.assert_not_called()


# =============================================================================
# Modifier-release gate — injection must wait for the user's hands
# =============================================================================
class _FakeProbe:
    """Stands in for _ModifierProbe: yields queued masks, then 0 (clear)."""

    def __init__(self, masks):
        self.masks = list(masks)
        self.closed = False

    def query(self):
        return self.masks.pop(0) if self.masks else 0

    def close(self):
        self.closed = True


class TestModifierReleaseGate:
    """xdotool's --clearmodifiers is unreliable under XWayland (the compositor
    re-asserts physically-held keys), so every synthetic-key path must first
    wait for the live X modifier mask to clear. Field bug: a 267-char dictation
    typed ~1s after hotkey release, with a hand back on Shift, landed fully
    shifted (`I"M ... NOW<`)."""

    def test_returns_true_immediately_when_mask_clear(self):
        probe = _FakeProbe([0])
        with patch("wayfinder.core.injector._open_modifier_probe", return_value=probe), \
             patch("wayfinder.core.injector.time.sleep") as slept:
            assert _wait_for_modifier_release() is True
        slept.assert_not_called()
        assert probe.closed  # one connection per wait, always released

    def test_unavailable_probe_never_blocks_injection(self):
        # None = no DISPLAY / no libxcb / connect failure — behave exactly
        # like the pre-gate code (no wait, no error).
        with patch("wayfinder.core.injector._open_modifier_probe", return_value=None), \
             patch("wayfinder.core.injector.time.sleep") as slept:
            assert _wait_for_modifier_release() is True
        slept.assert_not_called()

    def test_waits_until_shift_released(self):
        probe = _FakeProbe([0x01, 0x01, 0])
        with patch("wayfinder.core.injector._open_modifier_probe", return_value=probe), \
             patch("wayfinder.core.injector.time.sleep") as slept, \
             patch("wayfinder.core.injector.time.monotonic", return_value=0.0):
            assert _wait_for_modifier_release() is True
        assert slept.call_count == 2
        assert probe.closed

    def test_times_out_and_reports_still_held(self):
        # Clock: deadline computed at 0.0, then checks march past timeout.
        probe = _FakeProbe([0x01] * 10)
        clock = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        with patch("wayfinder.core.injector._open_modifier_probe", return_value=probe), \
             patch("wayfinder.core.injector.time.sleep"), \
             patch("wayfinder.core.injector.time.monotonic", side_effect=clock):
            assert _wait_for_modifier_release(timeout=2.0) is False
        assert probe.closed  # released even on the timeout path

    def test_xdotool_aborts_instead_of_typing_shift_corrupted_text(self):
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.core.injector._wait_for_modifier_release",
                   return_value=False), \
             patch("wayfinder.core.injector.subprocess.run",
                   return_value=ok) as run:
            with pytest.raises(InjectionError, match="still held"):
                _inject_text_xdotool("I'm ready.")
        run.assert_not_called()

    def test_wtype_aborts_instead_of_typing_shift_corrupted_text(self):
        with patch("wayfinder.core.injector._wait_for_modifier_release",
                   return_value=False), \
             patch("wayfinder.core.injector.subprocess.run") as run:
            from wayfinder.core.injector import _inject_text_wtype
            with pytest.raises(InjectionError, match="still held"):
                _inject_text_wtype("I'm ready.")
        run.assert_not_called()

    def test_poll_interval_honors_no_sub_100ms_rule(self):
        probe = _FakeProbe([0x01, 0])
        with patch("wayfinder.core.injector._open_modifier_probe", return_value=probe), \
             patch("wayfinder.core.injector.time.sleep") as slept, \
             patch("wayfinder.core.injector.time.monotonic", return_value=0.0):
            _wait_for_modifier_release()
        assert slept.call_args[0][0] >= 0.1

    def test_latched_lock_bits_never_gate(self):
        # CapsLock (Lock, 0x02) and NumLock (Mod2, 0x10) are latched states,
        # not held keys — a caps-lock user must not stall 2s per dictation.
        assert _X_HELD_MODIFIER_BITS & 0x02 == 0
        assert _X_HELD_MODIFIER_BITS & 0x10 == 0

    def test_corrupting_modifiers_all_gate(self):
        # Shift, Ctrl, Alt (Mod1), Super (Mod4), AltGr (Mod5) held → corruption.
        for bit in (0x01, 0x04, 0x08, 0x40, 0x80):
            assert _X_HELD_MODIFIER_BITS & bit == bit

    def test_one_shot_query_closes_probe(self):
        probe = _FakeProbe([0x05])
        with patch("wayfinder.core.injector._open_modifier_probe", return_value=probe):
            assert _x11_held_modifiers() == 0x05
        assert probe.closed

    def test_probe_unavailable_without_display(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _open_modifier_probe() is None
            assert _x11_held_modifiers() is None

    def test_xdotool_type_waits_after_refocus_before_keys(self):
        order = []
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.core.injector._wait_for_modifier_release",
                   side_effect=lambda *a, **k: order.append("wait") or True), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=lambda *a, **k: order.append(a[0][:2]) or ok):
            _inject_text_xdotool("hello")
        assert order == ["wait", ["xdotool", "type"]]

    def test_wtype_waits_before_keys(self):
        order = []
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.core.injector._wait_for_modifier_release",
                   side_effect=lambda *a, **k: order.append("wait") or True), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=lambda *a, **k: order.append("keys") or ok), \
             patch("wayfinder.core.injector.time.sleep"):
            from wayfinder.core.injector import _inject_text_wtype
            _inject_text_wtype("hello")
        assert order == ["wait", "keys"]

    def test_ydotool_type_waits_before_keys(self):
        order = []
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "")), \
             patch("wayfinder.core.injector._get_ydotool_binary", return_value="/usr/bin/ydotool"), \
             patch("wayfinder.core.injector._get_ydotool_env", return_value={}), \
             patch("wayfinder.core.injector._wait_for_modifier_release",
                   side_effect=lambda *a, **k: order.append("wait") or True), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=lambda *a, **k: order.append("keys") or ok):
            _inject_text_type_linux("hello")
        assert order == ["wait", "keys"]

    def test_ctrl_v_waits_before_keypress(self):
        order = []
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.core.injector._wait_for_modifier_release",
                   side_effect=lambda *a, **k: order.append("wait") or True), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=lambda *a, **k: order.append("key") or ok):
            _send_ctrl_v_linux("xdotool")
        assert order == ["wait", "key"]

    def test_press_enter_waits_before_return_key(self):
        order = []
        ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch("wayfinder.utils.platform.get_text_injector", return_value="xdotool"), \
             patch("wayfinder.core.injector._wait_for_modifier_release",
                   side_effect=lambda *a, **k: order.append("wait") or True), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=lambda *a, **k: order.append("key") or ok):
            press_enter()
        assert order == ["wait", "key"]

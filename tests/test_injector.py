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
    prime_wayland_injection,
    _get_ydotool_binary,
    _get_ydotool_env,
    _inject_text_xdotool,
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
        # "instant" uses 1ms (not 0) delays to prevent ydotool Shift bleed (commit 8bcedd4).
        assert TYPING_SPEEDS["instant"] == (1, 1)

    def test_fast_speed(self):
        assert TYPING_SPEEDS["fast"] == (1, 1)

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
            ("instant", "1", "1"),
            ("fast", "1", "1"),
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
        with pytest.raises(InjectionError, match="ydotool failed"):
            inject_text("hello", "instant")

    def test_error_message_includes_stderr(self, mock_ydotool_failure):
        with pytest.raises(InjectionError, match="ydotool daemon not running"):
            inject_text("hello", "instant")

    def test_timeout_raises_injection_error(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "mocked")), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="ydotool", timeout=120)):
            mock_sys.platform = "linux"
            with pytest.raises(InjectionError, match="timed out"):
                inject_text("hello", "instant")

    def test_file_not_found_raises_injection_error(self):
        with patch("wayfinder.core.injector.sys") as mock_sys, \
             patch("wayfinder.utils.platform.get_text_injector", return_value="ydotool"), \
             patch("wayfinder.core.injector.check_ydotool_ready", return_value=(True, "mocked")), \
             patch("wayfinder.core.injector.subprocess.run",
                   side_effect=FileNotFoundError("ydotool")):
            mock_sys.platform = "linux"
            with pytest.raises(InjectionError, match="ydotool not found"):
                inject_text("hello", "instant")


# =============================================================================
# _get_ydotool_binary
# =============================================================================


class TestGetYdotoolBinary:
    """Tests for finding the ydotool binary."""

    def test_returns_ydotool_when_no_appdir(self, monkeypatch):
        monkeypatch.delenv("APPDIR", raising=False)
        assert _get_ydotool_binary() == "ydotool"

    def test_returns_appimage_path_when_appdir_set_and_binary_exists(
        self, appimage_env
    ):
        """When APPDIR is set and the bundled binary exists, return full path."""
        bundled = appimage_env / "usr" / "bin" / "ydotool"
        bundled.touch()
        bundled.chmod(0o755)

        result = _get_ydotool_binary()
        assert result == str(bundled)

    def test_returns_ydotool_when_appdir_set_but_binary_missing(
        self, monkeypatch, temp_dir
    ):
        """When APPDIR is set but bundled binary doesn't exist, fall back."""
        appdir = temp_dir / "EmptyAppDir"
        appdir.mkdir()
        monkeypatch.setenv("APPDIR", str(appdir))

        assert _get_ydotool_binary() == "ydotool"

    def test_ignores_foreign_appdir_with_ydotool(self, monkeypatch, temp_dir):
        """Do not use another AppImage app's bundled ydotool."""
        appdir = temp_dir / "ForeignAppDir"
        bundled = appdir / "usr" / "bin" / "ydotool"
        bundled.parent.mkdir(parents=True)
        bundled.write_text("#!/bin/sh\n")
        monkeypatch.setenv("APPDIR", str(appdir))

        assert _get_ydotool_binary() == "ydotool"


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
        # Settle so the focus-in completes before synthetic keys.
        mock_sleep.assert_called_once_with(0.06)

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

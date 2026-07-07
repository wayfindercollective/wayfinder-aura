"""Tests for the host-side Steam Deck mode supervisor.

The supervisor is a standalone script (hyphenated filename, lives outside the
`wayfinder` package), so it is imported by file path via importlib rather than
a normal `import`. It has no third-party deps, so this runs headless.

Coverage:
* decide() — full truth table.
* read_toggle() — "1" true; everything else false; strip() handling.
* write_mode_marker() — atomic write, read-back, tolerates a missing dir.
* the actuator loop, with _run_systemctl mocked, for: start when inactive,
  stop when active, restart on mode change, no-restart on the seeding first
  poll, the never-stop-unless-game+off guard, and the min-interval guard.
"""

import importlib.util
import threading
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import the script-by-path.
# ---------------------------------------------------------------------------
_SUPERVISOR_PATH = (
    Path(__file__).parent.parent
    / "scripts" / "steamdeck" / "wayfinder-mode-supervisor.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wayfinder_mode_supervisor", _SUPERVISOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sup = _load_module()


# ---------------------------------------------------------------------------
# decide() — pure truth table.
# ---------------------------------------------------------------------------
class TestDecide:
    def test_game_toggle_on_runs(self):
        assert sup.decide("game", True) == "RUNNING"

    def test_game_toggle_off_stops(self):
        assert sup.decide("game", False) == "STOPPED"

    def test_desktop_toggle_on_runs(self):
        assert sup.decide("desktop", True) == "RUNNING"

    def test_desktop_toggle_off_runs(self):
        assert sup.decide("desktop", False) == "RUNNING"

    def test_none_toggle_on_runs(self):
        assert sup.decide(None, True) == "RUNNING"

    def test_none_toggle_off_runs(self):
        # Indeterminate is treated like desktop: never stop.
        assert sup.decide(None, False) == "RUNNING"


# ---------------------------------------------------------------------------
# read_toggle()
# ---------------------------------------------------------------------------
class TestReadToggle:
    def _point_toggle_at(self, monkeypatch, tmp_path, content):
        """Write `content` (or leave absent if None) and repoint the module."""
        toggle = tmp_path / "game-mode-dictation"
        if content is not None:
            toggle.write_text(content)
        monkeypatch.setattr(sup, "TOGGLE_PATH", str(toggle))
        return toggle

    def test_one_is_true(self, monkeypatch, tmp_path):
        self._point_toggle_at(monkeypatch, tmp_path, "1")
        assert sup.read_toggle() is True

    def test_one_with_whitespace_is_true(self, monkeypatch, tmp_path):
        self._point_toggle_at(monkeypatch, tmp_path, " 1 \n")
        assert sup.read_toggle() is True

    def test_zero_is_false(self, monkeypatch, tmp_path):
        self._point_toggle_at(monkeypatch, tmp_path, "0")
        assert sup.read_toggle() is False

    def test_empty_is_false(self, monkeypatch, tmp_path):
        self._point_toggle_at(monkeypatch, tmp_path, "")
        assert sup.read_toggle() is False

    def test_absent_is_false(self, monkeypatch, tmp_path):
        self._point_toggle_at(monkeypatch, tmp_path, None)
        assert sup.read_toggle() is False

    def test_garbage_is_false(self, monkeypatch, tmp_path):
        self._point_toggle_at(monkeypatch, tmp_path, "yes please")
        assert sup.read_toggle() is False

    def test_eleven_is_false(self, monkeypatch, tmp_path):
        # Only an exact "1" after strip — not "anything truthy".
        self._point_toggle_at(monkeypatch, tmp_path, "11")
        assert sup.read_toggle() is False

    def test_default_path_falls_back_to_legacy_toggle(self, monkeypatch, tmp_path):
        flatpak_toggle = tmp_path / "flatpak" / "game-mode-dictation"
        legacy_toggle = tmp_path / "legacy" / "game-mode-dictation"
        legacy_toggle.parent.mkdir(parents=True)
        legacy_toggle.write_text("1")

        monkeypatch.setattr(sup, "_DEFAULT_TOGGLE_PATH", str(flatpak_toggle))
        monkeypatch.setattr(sup, "TOGGLE_PATH", str(flatpak_toggle))
        monkeypatch.setattr(sup, "LEGACY_TOGGLE_PATH", str(legacy_toggle))

        assert sup.read_toggle() is True

    def test_default_path_prefers_flatpak_toggle_over_legacy(self, monkeypatch, tmp_path):
        flatpak_toggle = tmp_path / "flatpak" / "game-mode-dictation"
        legacy_toggle = tmp_path / "legacy" / "game-mode-dictation"
        flatpak_toggle.parent.mkdir(parents=True)
        legacy_toggle.parent.mkdir(parents=True)
        flatpak_toggle.write_text("0")
        legacy_toggle.write_text("1")

        monkeypatch.setattr(sup, "_DEFAULT_TOGGLE_PATH", str(flatpak_toggle))
        monkeypatch.setattr(sup, "TOGGLE_PATH", str(flatpak_toggle))
        monkeypatch.setattr(sup, "LEGACY_TOGGLE_PATH", str(legacy_toggle))

        assert sup.read_toggle() is False


# ---------------------------------------------------------------------------
# write_mode_marker()
# ---------------------------------------------------------------------------
class TestWriteModeMarker:
    def test_writes_game(self, monkeypatch, tmp_path):
        marker = tmp_path / "wayfinder-aura" / "mode"
        monkeypatch.setattr(sup, "MODE_MARKER_PATH", str(marker))
        sup.write_mode_marker("game")
        assert marker.read_text() == "game"

    def test_writes_desktop(self, monkeypatch, tmp_path):
        marker = tmp_path / "wayfinder-aura" / "mode"
        monkeypatch.setattr(sup, "MODE_MARKER_PATH", str(marker))
        sup.write_mode_marker("desktop")
        assert marker.read_text() == "desktop"

    def test_none_writes_desktop_failsafe(self, monkeypatch, tmp_path):
        marker = tmp_path / "wayfinder-aura" / "mode"
        monkeypatch.setattr(sup, "MODE_MARKER_PATH", str(marker))
        sup.write_mode_marker(None)
        assert marker.read_text() == "desktop"

    def test_creates_missing_parent_dirs(self, monkeypatch, tmp_path):
        marker = tmp_path / "a" / "b" / "c" / "mode"
        monkeypatch.setattr(sup, "MODE_MARKER_PATH", str(marker))
        sup.write_mode_marker("game")
        assert marker.read_text() == "game"

    def test_overwrites_atomically(self, monkeypatch, tmp_path):
        marker = tmp_path / "wayfinder-aura" / "mode"
        monkeypatch.setattr(sup, "MODE_MARKER_PATH", str(marker))
        sup.write_mode_marker("game")
        sup.write_mode_marker("desktop")
        assert marker.read_text() == "desktop"
        # No leftover temp files in the directory.
        leftovers = list(marker.parent.glob(".mode.*"))
        assert leftovers == []

    def test_never_raises_on_bad_path(self, monkeypatch, tmp_path):
        # Point the marker at a path whose "directory" is actually a file, so
        # makedirs would fail — write_mode_marker must swallow it.
        afile = tmp_path / "iam_a_file"
        afile.write_text("x")
        monkeypatch.setattr(sup, "MODE_MARKER_PATH", str(afile / "mode"))
        sup.write_mode_marker("game")  # must not raise


# ---------------------------------------------------------------------------
# Actuator loop.
#
# We drive main() but make it run exactly N polls by raising out of a fake
# time.sleep after a counted number of iterations, and feed mode/toggle/active
# through monkeypatched seams. _run_systemctl is replaced with a recorder.
# ---------------------------------------------------------------------------
class _StopLoop(Exception):
    pass


class Harness:
    """Drives sup.main() deterministically and records systemctl verbs."""

    def __init__(self, monkeypatch, modes, toggle=True, max_polls=None):
        self.monkeypatch = monkeypatch
        self.modes = list(modes)
        self.toggle = toggle
        self.calls = []           # list of (verb, unit) for non-query calls
        self.active = False       # current service_active() answer
        self.poll = 0
        self.max_polls = max_polls if max_polls is not None else len(modes)
        self.fake_clock = [1000.0]

    def _systemctl(self, args):
        # is-active queries are answered from self.active / mode; everything
        # else (start/stop/restart) is recorded and mutates self.active.
        if args[:1] == ["is-active"]:
            unit = args[1] if len(args) > 1 else ""
            if unit == sup.SERVICE:
                return (0, "active") if self.active else (3, "inactive")
            return (3, "inactive")
        verb, unit = args[0], (args[1] if len(args) > 1 else "")
        self.calls.append((verb, unit))
        if verb in ("start", "restart"):
            self.active = True
        elif verb == "stop":
            self.active = False
        return 0, ""

    def _detect_mode(self):
        idx = min(self.poll, len(self.modes) - 1)
        return self.modes[idx]

    def _sleep(self, _secs):
        # Advance our fake monotonic clock well past the min-interval guard so
        # throttling never trips unless a test deliberately freezes the clock.
        self.fake_clock[0] += 100.0
        self.poll += 1
        if self.poll >= self.max_polls:
            raise _StopLoop()

    def _time(self):
        return self.fake_clock[0]

    def install(self, *, initial_active=False, freeze_clock=False):
        self.active = initial_active
        self.monkeypatch.setattr(sup, "_run_systemctl", self._systemctl)
        self.monkeypatch.setattr(sup, "detect_mode", self._detect_mode)
        self.monkeypatch.setattr(sup, "read_toggle", lambda: self.toggle)
        self.monkeypatch.setattr(sup, "write_mode_marker", lambda _m: None)
        self.monkeypatch.setattr(sup.time, "sleep", self._sleep)
        if freeze_clock:
            # Clock never advances -> every actuation after the first is inside
            # the min-interval window.
            self.monkeypatch.setattr(sup.time, "time", lambda: 1000.0)
        else:
            self.monkeypatch.setattr(sup.time, "time", self._time)

    def run(self):
        with pytest.raises(_StopLoop):
            sup.main()


class TestActuator:
    def test_running_and_inactive_starts_once(self, monkeypatch):
        h = Harness(monkeypatch, modes=["desktop"], toggle=False)
        h.install(initial_active=False)
        h.run()
        assert h.calls == [("start", sup.SERVICE)]

    def test_stopped_and_active_stops_once(self, monkeypatch):
        h = Harness(monkeypatch, modes=["game"], toggle=False)
        h.install(initial_active=True)
        h.run()
        assert h.calls == [("stop", sup.SERVICE)]

    def test_seeding_poll_never_restarts(self, monkeypatch):
        # First poll, service already correctly RUNNING in desktop mode: the
        # supervisor must NOT restart it just because prev_mode was the sentinel.
        h = Harness(monkeypatch, modes=["desktop"], toggle=True)
        h.install(initial_active=True)
        h.run()
        assert h.calls == []

    def test_mode_change_restarts(self, monkeypatch):
        # Poll 0 seeds desktop (active, no-op); poll 1 flips to game with the
        # toggle ON -> still RUNNING but mode changed -> exactly one restart.
        h = Harness(monkeypatch, modes=["desktop", "game"], toggle=True)
        h.install(initial_active=True)
        h.run()
        assert h.calls == [("restart", sup.SERVICE)]

    def test_no_restart_without_mode_change(self, monkeypatch):
        # Two desktop polls, active, toggle on -> seeded then steady: no action.
        h = Harness(monkeypatch, modes=["desktop", "desktop"], toggle=True)
        h.install(initial_active=True)
        h.run()
        assert h.calls == []

    def test_guard_blocks_stop_when_toggle_on(self, monkeypatch):
        # decide() would say RUNNING here, but make sure that even in game mode
        # with the toggle ON we never emit a stop.
        h = Harness(monkeypatch, modes=["game"], toggle=True)
        h.install(initial_active=True)
        h.run()
        assert all(verb != "stop" for verb, _ in h.calls)

    def test_guard_blocks_stop_outside_game(self, monkeypatch):
        # Desktop + toggle off: RUNNING, never a stop even though service active.
        h = Harness(monkeypatch, modes=["desktop"], toggle=False)
        h.install(initial_active=True)
        h.run()
        assert h.calls == []

    def test_min_interval_guard_suppresses_rapid_actuation(self, monkeypatch):
        # Freeze the clock so the first actuation consumes the budget and the
        # second (a stop on poll 1) is throttled away.
        # Poll 0: game, toggle off, inactive -> RUNNING? No: game+off => STOPPED
        #         but service inactive so nothing to stop. Use a sequence that
        #         forces two actuations: start then stop.
        # Simpler: desktop (inactive) -> start on poll 0; then game+off (now
        # active because start flipped it) -> wants stop on poll 1, throttled.
        h = Harness(
            monkeypatch, modes=["desktop", "game"], toggle=False
        )
        h.install(initial_active=False, freeze_clock=True)
        h.run()
        # Only the very first actuation (start) goes through; the stop is
        # suppressed by the min-interval guard.
        assert h.calls == [("start", sup.SERVICE)]
        # Service is therefore left active (the throttled stop never ran).
        assert h.active is True

    def test_starts_when_running_desired_and_inactive_after_stop(self, monkeypatch):
        # game+off keeps it stopped; flipping to desktop should start it again.
        h = Harness(monkeypatch, modes=["game", "desktop"], toggle=False)
        h.install(initial_active=True)
        h.run()
        # Poll 0: game+off, active -> stop. Poll 1: desktop -> RUNNING, inactive
        # -> start.
        assert h.calls == [("stop", sup.SERVICE), ("start", sup.SERVICE)]


# ---------------------------------------------------------------------------
# detect_mode() — exercise the seam directly (no real systemctl).
# ---------------------------------------------------------------------------
class TestDetectMode:
    def test_active_is_game(self, monkeypatch):
        monkeypatch.setattr(sup, "_run_systemctl", lambda args: (0, "active"))
        assert sup.detect_mode() == "game"

    def test_inactive_is_desktop(self, monkeypatch):
        monkeypatch.setattr(sup, "_run_systemctl", lambda args: (3, "inactive"))
        assert sup.detect_mode() == "desktop"

    def test_failed_is_desktop(self, monkeypatch):
        monkeypatch.setattr(sup, "_run_systemctl", lambda args: (3, "failed"))
        assert sup.detect_mode() == "desktop"

    def test_indeterminate_falls_back_to_ps(self, monkeypatch):
        # Unexpected systemctl output -> fall back to `ps -eo comm`.
        monkeypatch.setattr(sup, "_run_systemctl", lambda args: (-1, "???"))

        class _Proc:
            returncode = 0
            stdout = "systemd\ngamescope\nbash\n"

        monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: _Proc())
        assert sup.detect_mode() == "game"

    def test_fallback_exact_match_only(self, monkeypatch):
        # A process named "gamescope-wl" must NOT count as gamescope.
        monkeypatch.setattr(sup, "_run_systemctl", lambda args: (-1, "???"))

        class _Proc:
            returncode = 0
            stdout = "systemd\ngamescope-wl\nbash\n"

        monkeypatch.setattr(sup.subprocess, "run", lambda *a, **k: _Proc())
        assert sup.detect_mode() == "desktop"

    def test_fallback_failure_returns_none(self, monkeypatch):
        monkeypatch.setattr(sup, "_run_systemctl", lambda args: (-1, "???"))

        def _boom(*a, **k):
            raise OSError("no ps")

        monkeypatch.setattr(sup.subprocess, "run", _boom)
        assert sup.detect_mode() is None

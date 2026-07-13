"""Single-instance guard: a second launch must raise the live window and exit.

Regression: the PID-lock's cmdline heuristic ("wayfinder" substring) missed
relative-path launches like `./venv-gpu/bin/python main.py`, letting a second
instance start and fight the first over the hotkeys and the microphone.
"""

import socket as socket_mod
import threading

import pytest

wayfinder_main = pytest.importorskip("wayfinder_main")


class TestSingleInstanceGuard:
    def _serve_once(self, sock_path: str, received: list) -> threading.Thread:
        """One-shot AF_UNIX server standing in for a live app instance."""
        server = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        server.bind(sock_path)
        server.listen(1)

        def run():
            conn, _ = server.accept()
            received.append(conn.recv(64))
            conn.close()
            server.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def test_live_instance_gets_show_and_blocks_second_launch(self, tmp_path, monkeypatch):
        sock_path = str(tmp_path / "wayfinder-aura.sock")
        received = []
        t = self._serve_once(sock_path, received)
        monkeypatch.setattr(wayfinder_main, "SOCKET_PATH", sock_path)

        assert wayfinder_main._raise_existing_instance() is True
        t.join(timeout=3)
        assert received and received[0].strip() == b"show"

    def test_no_socket_means_no_instance(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "SOCKET_PATH",
                            str(tmp_path / "missing.sock"))
        assert wayfinder_main._raise_existing_instance() is False

    def test_stale_socket_file_reads_as_not_running(self, tmp_path, monkeypatch):
        # A socket file whose owner died refuses connections — must NOT count
        # as a running instance (would otherwise brick startup forever).
        sock_path = str(tmp_path / "wayfinder-aura.sock")
        s = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        s.bind(sock_path)
        s.close()
        monkeypatch.setattr(wayfinder_main, "SOCKET_PATH", sock_path)

        assert wayfinder_main._raise_existing_instance() is False

    def test_check_single_instance_defers_to_live_socket(self, tmp_path, monkeypatch):
        """The socket probe must win regardless of lock-file/cmdline state."""
        sock_path = str(tmp_path / "wayfinder-aura.sock")
        received = []
        t = self._serve_once(sock_path, received)
        monkeypatch.setattr(wayfinder_main, "SOCKET_PATH", sock_path)

        assert wayfinder_main._check_single_instance() is False
        t.join(timeout=3)
        assert received and received[0].strip() == b"show"


class TestInputDeviceFilter:
    """Remapper virtual keyboards must be monitored; only our injector is excluded.

    Regression: a mouse button remapped to F3 re-emits through the remapper's
    virtual keyboard (gsr-ui / keyd / input-remapper grab the physical device),
    so the blanket '"virtual" in name' exclusion made the hotkey invisible.
    """

    def _fake_device(self, name):
        from unittest.mock import MagicMock
        dev = MagicMock()
        dev.name = name
        dev.path = "/dev/input/event99"
        # EV_KEY capabilities including F1..F12
        dev.capabilities.return_value = {1: list(range(59, 89))}
        return dev

    def _devices_for(self, monkeypatch, names):
        from unittest.mock import MagicMock
        fake_evdev = MagicMock()
        devices = [self._fake_device(n) for n in names]
        fake_evdev.list_devices.return_value = [d.path for d in devices]
        fake_evdev.InputDevice.side_effect = devices
        monkeypatch.setattr(wayfinder_main, "evdev", fake_evdev)
        monkeypatch.setattr(wayfinder_main, "HAS_EVDEV", True)
        return [d["name"] for d in wayfinder_main.get_all_input_devices()]

    def test_remapper_virtual_keyboards_included(self, monkeypatch):
        names = self._devices_for(monkeypatch, [
            "gsr-ui virtual keyboard",
            "keyd virtual keyboard",
            "input-remapper Corsair SCIMITAR forwarded",
            "Keychron Link Keyboard",
        ])
        assert "gsr-ui virtual keyboard" in names
        assert "keyd virtual keyboard" in names
        assert "Keychron Link Keyboard" in names

    def test_own_injection_device_excluded(self, monkeypatch):
        names = self._devices_for(monkeypatch, [
            "ydotoold virtual device",
            "Keychron Link Keyboard",
        ])
        assert "ydotoold virtual device" not in names
        assert "Keychron Link Keyboard" in names


class TestExclusiveGrab:
    """grabbed_input_devices: exclusive-grab named devices, release during games.

    Use case: an MMO mouse's side-grid interface emits hardware F3 — without a
    grab the same press also opens the browser's find bar. During a GameMode
    game the grab is released so the grid returns to the game's keybinds.
    """

    def _fake_device(self, name, fd):
        from unittest.mock import MagicMock
        dev = MagicMock()
        dev.name = name
        dev.fd = fd
        dev.grab = MagicMock()
        dev.ungrab = MagicMock()
        return dev

    def _run_listener(self, monkeypatch, devices, grabbed_patterns, body,
                      gamemode_flag=None):
        """Run hotkey_listener in a thread against fake devices; body(stop) drives it."""
        import os
        import queue
        import threading

        pipes = []
        for dev in devices:
            r, w = os.pipe()
            dev.fd = r
            pipes.append((r, w))

        monkeypatch.setattr(wayfinder_main, "find_keyboard_devices",
                            lambda enabled: devices)
        if gamemode_flag is not None:
            monkeypatch.setattr(wayfinder_main, "_gamemode_hotkeys_paused",
                                lambda: gamemode_flag["paused"])

        stop = threading.Event()
        t = threading.Thread(
            target=wayfinder_main.hotkey_listener,
            args=(queue.Queue(), 61, [], stop, None, None, 68, [], grabbed_patterns),
            daemon=True,
        )
        t.start()
        try:
            body(stop)
        finally:
            stop.set()
            t.join(timeout=3)
            for r, w in pipes:
                os.close(r)
                os.close(w)
        assert not t.is_alive()

    def test_matching_device_grabbed_then_released_on_stop(self, monkeypatch):
        import time
        grid = self._fake_device("Corsair SCIMITAR Gaming Mouse Keyboard", -1)
        keychron = self._fake_device("Keychron Link Keyboard", -1)

        def body(stop):
            time.sleep(0.7)
            grid.grab.assert_called_once()
            keychron.grab.assert_not_called()

        self._run_listener(monkeypatch, [grid, keychron],
                           ["Gaming Mouse Keyboard"], body)
        grid.ungrab.assert_called()

    def test_gamemode_releases_and_reacquires_grab(self, monkeypatch):
        import time
        grid = self._fake_device("Corsair SCIMITAR Gaming Mouse Keyboard", -1)
        flag = {"paused": False}

        def body(stop):
            time.sleep(0.7)
            grid.grab.assert_called_once()        # grabbed on desktop
            flag["paused"] = True                  # game starts
            time.sleep(1.2)
            grid.ungrab.assert_called()            # released for the game
            flag["paused"] = False                 # game ends
            time.sleep(1.2)
            assert grid.grab.call_count == 2       # re-acquired

        self._run_listener(monkeypatch, [grid], ["Gaming Mouse Keyboard"], body,
                           gamemode_flag=flag)

    def test_grab_failure_is_tolerated(self, monkeypatch):
        import time
        grid = self._fake_device("Corsair SCIMITAR Gaming Mouse Keyboard", -1)
        grid.grab.side_effect = OSError(16, "Device or resource busy")

        def body(stop):
            time.sleep(0.7)
            grid.grab.assert_called()  # attempted, failed, listener kept running

        self._run_listener(monkeypatch, [grid], ["Gaming Mouse Keyboard"], body)

    def test_no_patterns_means_no_grabs(self, monkeypatch):
        import time
        grid = self._fake_device("Corsair SCIMITAR Gaming Mouse Keyboard", -1)

        def body(stop):
            time.sleep(0.5)
            grid.grab.assert_not_called()

        self._run_listener(monkeypatch, [grid], [], body)


class TestHotkeyDetect:
    """Settings 'Detect' button: the listener reports the next press to the UI."""

    @pytest.fixture(autouse=True)
    def _clean_capture(self):
        wayfinder_main._HOTKEY_CAPTURE["armed"] = False
        wayfinder_main._HOTKEY_CAPTURE["suppress_until"] = 0.0
        yield
        wayfinder_main._HOTKEY_CAPTURE["armed"] = False
        wayfinder_main._HOTKEY_CAPTURE["suppress_until"] = 0.0

    def test_keycode_display_known_and_unknown(self):
        assert wayfinder_main._keycode_display(61) == "F3"
        assert wayfinder_main._keycode_display(275) == "Mouse Side"
        assert wayfinder_main._keycode_display(99999) == "Key 99999"

    def _make_detect_app(self, monkeypatch, config=None):
        """Minimal stand-in for WayfinderApp Detect-apply without constructing Tk."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        saved = []

        def fake_save(cfg):
            saved.append(dict(cfg))

        monkeypatch.setattr(wayfinder_main, "save_config", fake_save)

        logs = []
        app = SimpleNamespace(
            config=config or {"hotkey_key": 61, "hotkey_modifiers": [],
                              "style_toggle_key": 68, "style_toggle_modifiers": []},
            _hotkey_capture_target="record",
            _detect_cancel_after_id=None,
            _detect_rearm_after=0.0,
            _DETECT_REARM_COOLDOWN_S=wayfinder_main.WayfinderApp._DETECT_REARM_COOLDOWN_S,
            _hotkey_key_codes={},
            _hotkey_code_to_name={},
            _hotkey_key_var=SimpleNamespace(set=MagicMock()),
            _hotkey_mod_vars={},
            _style_hotkey_key_var=SimpleNamespace(set=MagicMock()),
            _style_hotkey_mod_vars={},
            _detect_btn_record=MagicMock(),
            _detect_btn_style=MagicMock(),
            _hotkey_thread=None,
            _pynput_listener_started=False,
            _capture_thread=None,
            _capture_stop_event=None,
            log=lambda msg: logs.append(msg),
            after=MagicMock(return_value="after-id"),
            after_cancel=MagicMock(),
            restart_evdev_listener=MagicMock(),
        )
        # Bind real methods onto the stand-in
        for name in (
            "_apply_captured_hotkey",
            "_ensure_hotkey_label",
            "_pure_hotkey_label",
            "_reset_detect_button",
            "_stop_capture_listener",
            "_cancel_detect_timeout",
            "_start_hotkey_detect",
            "_cancel_hotkey_detect",
        ):
            setattr(app, name, getattr(wayfinder_main.WayfinderApp, name).__get__(app))
        return app, saved, logs

    def test_apply_persists_binding_before_listener_restart(self, monkeypatch):
        """Regression 2026-07-13: crash mid-Detect lost the capture because config
        was only written after UI work. Save must happen even if restart raises."""
        from unittest.mock import MagicMock

        app, saved, logs = self._make_detect_app(monkeypatch)
        order = []

        def save_side(cfg):
            order.append("save")
            saved.append(dict(cfg))

        def restart_side(reason=""):
            order.append("restart")
            raise RuntimeError("boom")

        monkeypatch.setattr(wayfinder_main, "save_config", save_side)
        app.restart_evdev_listener = MagicMock(side_effect=restart_side)
        app._hotkey_capture_gen = 1
        app._apply_captured_hotkey(
            {"code": 24, "modifiers": [], "device": "Corsair SCIMITAR", "gen": 1}
        )

        assert saved, "config must be saved even when listener restart fails"
        assert saved[0]["hotkey_key"] == 24
        assert app.config["hotkey_key"] == 24
        assert order.index("save") < order.index("restart"), "save must precede listener restart"
        assert any("set to O" in m or "set to Key" in m or "Record hotkey set" in m
                   for m in logs)
        assert any("restart after Detect failed" in m for m in logs)
        assert app._hotkey_capture_target is None
        assert wayfinder_main._HOTKEY_CAPTURE["armed"] is False

    def test_apply_rolls_back_config_when_save_fails(self, monkeypatch):
        app, saved, logs = self._make_detect_app(monkeypatch)
        monkeypatch.setattr(
            wayfinder_main, "save_config",
            lambda cfg: (_ for _ in ()).throw(OSError("disk full")),
        )
        app._hotkey_capture_gen = 1
        app._apply_captured_hotkey(
            {"code": 24, "modifiers": [], "device": "Corsair SCIMITAR", "gen": 1}
        )
        assert app.config["hotkey_key"] == 61  # rolled back to F3
        assert not saved
        assert any("Could not save" in m for m in logs)
        app.restart_evdev_listener.assert_not_called()

    def test_apply_rejects_stale_generation(self, monkeypatch):
        app, saved, _logs = self._make_detect_app(monkeypatch)
        app._hotkey_capture_gen = 5
        app._hotkey_capture_target = "record"
        app._apply_captured_hotkey(
            {"code": 24, "modifiers": [], "device": "x", "gen": 4}
        )
        assert not saved
        assert app.config["hotkey_key"] == 61
        assert app._hotkey_capture_target == "record"  # untouched stale event

    def test_detect_rearm_cooldown_blocks_immediate_rearm(self, monkeypatch):
        """Scimitar side buttons can re-click Detect right after bind and overwrite
        the new hotkey with F3. Cooldown must reject that re-arm."""
        import time
        app, _saved, logs = self._make_detect_app(monkeypatch)
        app._detect_rearm_after = time.monotonic() + 5.0

        app._start_hotkey_detect("record")

        assert wayfinder_main._HOTKEY_CAPTURE["armed"] is False
        assert any("Detect ready in" in m for m in logs)

    def test_successful_apply_sets_rearm_cooldown(self, monkeypatch):
        import time
        app, saved, _logs = self._make_detect_app(monkeypatch)
        app._hotkey_capture_gen = 1
        before = time.monotonic()
        app._apply_captured_hotkey(
            {"code": 24, "modifiers": [], "device": "Corsair", "gen": 1}
        )
        assert saved
        assert app._detect_rearm_after >= before + app._DETECT_REARM_COOLDOWN_S - 0.05

    def test_capture_listener_stamps_immutable_session_gen(self, monkeypatch):
        """Sol R2: a join-timed-out capture reader must not emit the *newer*
        session gen from the shared global — stamp the gen it was started with.
        """
        import os
        import queue
        import threading
        import time
        from itertools import chain, repeat
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        r, w = os.pipe()
        fake_event = SimpleNamespace(type=1, code=24, value=1)
        dev = MagicMock()
        dev.name = "Scimitar"
        dev.fd = r
        dev.read = MagicMock(side_effect=chain([[fake_event]], repeat([])))

        monkeypatch.setattr(wayfinder_main, "find_keyboard_devices", lambda e: [dev])
        monkeypatch.setattr(wayfinder_main, "ecodes", SimpleNamespace(EV_KEY=1))
        monkeypatch.setattr(
            wayfinder_main, "categorize",
            lambda ev: SimpleNamespace(scancode=ev.code, keystate=ev.value),
        )

        q = queue.Queue()
        stop = threading.Event()
        # Session 1 starts, then a newer session overwrites the global gen.
        wayfinder_main._HOTKEY_CAPTURE["armed"] = True
        wayfinder_main._HOTKEY_CAPTURE["gen"] = 1
        t = threading.Thread(
            target=wayfinder_main.capture_hotkey_listener,
            args=(q, stop, None, None, 1),  # immutable capture_gen=1
            daemon=True,
        )
        t.start()
        try:
            time.sleep(0.15)
            # Simulate a newer Detect session taking over the globals.
            wayfinder_main._HOTKEY_CAPTURE["armed"] = True
            wayfinder_main._HOTKEY_CAPTURE["gen"] = 2
            os.write(w, b"x")
            time.sleep(0.3)
            # Old reader must refuse (session no longer ours) — no event for gen 2.
            assert q.empty(), "stale reader must not emit under a newer session gen"
        finally:
            stop.set()
            t.join(timeout=3)
            os.close(r)
            os.close(w)

    def test_armed_capture_reports_key_instead_of_firing_hotkey(self, monkeypatch):
        import os
        import queue
        import threading
        import time
        from itertools import chain, repeat
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        r, w = os.pipe()
        fake_event = SimpleNamespace(type=1, code=183, value=1)  # F13 press
        dev = MagicMock()
        dev.name = "Corsair SCIMITAR Gaming Mouse Keyboard"
        dev.fd = r
        dev.read = MagicMock(side_effect=chain([[fake_event]], repeat([])))

        monkeypatch.setattr(wayfinder_main, "find_keyboard_devices", lambda e: [dev])
        monkeypatch.setattr(wayfinder_main, "ecodes", SimpleNamespace(EV_KEY=1))
        monkeypatch.setattr(
            wayfinder_main, "categorize",
            lambda ev: SimpleNamespace(scancode=ev.code, keystate=ev.value))

        q = queue.Queue()
        stop = threading.Event()
        t = threading.Thread(
            target=wayfinder_main.hotkey_listener,
            # hotkey_key=183 ON PURPOSE: armed capture must win over hotkey firing
            args=(q, 183, [], stop, None, None, 68, [], []),
            daemon=True,
        )
        wayfinder_main._HOTKEY_CAPTURE["armed"] = True
        t.start()
        try:
            time.sleep(0.3)
            os.write(w, b"x")  # wake select; device.read() yields the F13 press
            deadline = time.time() + 3
            captured = None
            while time.time() < deadline:
                try:
                    captured = q.get(timeout=0.2)
                    break
                except queue.Empty:
                    continue
            assert captured is not None, "no event emitted"
            ev_type, data = captured
            assert ev_type == wayfinder_main.EventType.HOTKEY_CAPTURED
            assert data["code"] == 183
            assert data["modifiers"] == []
            assert "SCIMITAR" in data["device"]
            assert wayfinder_main._HOTKEY_CAPTURE["armed"] is False
            assert q.empty(), "press must not ALSO fire HOTKEY_PRESSED"
        finally:
            stop.set()
            t.join(timeout=3)
            os.close(r)
            os.close(w)

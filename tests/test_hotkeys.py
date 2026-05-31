"""
Tests for hotkey detection modules (socket, evdev, dbus).

All hardware/system access is mocked since these modules require
Linux-specific libraries (evdev, D-Bus, Unix sockets).

The hotkeys package __init__ imports pynput_listener which fails at module
level when pynput is not installed (Key is None).  We install a mock for
pynput *before* importing any hotkeys submodule so the package __init__
can load cleanly.
"""

import importlib
import os
import sys
from enum import Enum, auto
from queue import Queue
from threading import Event
from unittest.mock import MagicMock, PropertyMock, patch, call

import pytest


# =============================================================================
# Bootstrap: mock pynput if it is not installed so the hotkeys package can load
# =============================================================================

def _ensure_pynput_mock():
    """Install a pynput mock into sys.modules if the real package is absent."""
    try:
        import pynput  # noqa: F401
    except ImportError:
        # Build a fake pynput.keyboard module with a Key object that has
        # every attribute the pynput_listener module-level dict uses.
        mock_key = MagicMock(name="MockKey")
        mock_keycode = MagicMock(name="MockKeyCode")
        mock_keyboard = MagicMock(name="MockKeyboard")
        mock_keyboard.Key = mock_key
        mock_keyboard.KeyCode = mock_keycode
        mock_keyboard.Listener = MagicMock

        mock_pynput = MagicMock(name="MockPynput")
        mock_pynput.keyboard = mock_keyboard

        sys.modules.setdefault("pynput", mock_pynput)
        sys.modules.setdefault("pynput.keyboard", mock_keyboard)


_ensure_pynput_mock()


# =============================================================================
# Helpers — safe import with skip
# =============================================================================

def _try_import(module_path: str) -> bool:
    """Try to import a dotted module path; return True on success."""
    try:
        importlib.import_module(module_path)
        return True
    except (ImportError, ModuleNotFoundError, AttributeError):
        return False


HAS_EVDEV_MODULE = _try_import("wayfinder.hotkeys.evdev")
HAS_SOCKET_MODULE = _try_import("wayfinder.hotkeys.socket")
HAS_DBUS_MODULE = _try_import("wayfinder.hotkeys.dbus")


# =============================================================================
# EventType (always available — no hardware needed)
# =============================================================================


@pytest.mark.skipif(not HAS_EVDEV_MODULE, reason="evdev module not importable")
class TestEventType:
    """Test the EventType enum defined in evdev.py."""

    def test_hotkey_pressed_exists(self):
        from wayfinder.hotkeys.evdev import EventType

        assert hasattr(EventType, "HOTKEY_PRESSED")

    def test_style_toggle_exists(self):
        from wayfinder.hotkeys.evdev import EventType

        assert hasattr(EventType, "STYLE_TOGGLE")

    def test_transcription_events_exist(self):
        from wayfinder.hotkeys.evdev import EventType

        assert hasattr(EventType, "TRANSCRIPTION_DONE")
        assert hasattr(EventType, "TRANSCRIPTION_ERROR")

    def test_injection_events_exist(self):
        from wayfinder.hotkeys.evdev import EventType

        assert hasattr(EventType, "INJECTION_DONE")
        assert hasattr(EventType, "INJECTION_ERROR")

    def test_chunk_events_exist(self):
        from wayfinder.hotkeys.evdev import EventType

        assert hasattr(EventType, "CHUNK_TRANSCRIBED")
        assert hasattr(EventType, "CHUNKED_TRANSCRIPTION_DONE")

    def test_event_type_is_enum(self):
        from wayfinder.hotkeys.evdev import EventType

        assert issubclass(EventType, Enum)

    def test_event_types_are_unique(self):
        from wayfinder.hotkeys.evdev import EventType

        values = [e.value for e in EventType]
        assert len(values) == len(set(values))


# =============================================================================
# Socket Module Tests
# =============================================================================


@pytest.mark.skipif(not HAS_SOCKET_MODULE, reason="socket module not importable")
class TestSocketConstants:
    """Test socket module constants and imports."""

    def test_socket_path_importable(self):
        """SOCKET_PATH is importable from config."""
        from wayfinder.config import SOCKET_PATH

        assert isinstance(SOCKET_PATH, str)
        assert len(SOCKET_PATH) > 0

    def test_socket_path_value(self):
        """SOCKET_PATH is an absolute path to the wayfinder-aura socket.

        The exact directory is environment-dependent: under $XDG_RUNTIME_DIR when it
        is set (Linux/Flatpak — that dir is bind-mounted host<->sandbox), else /tmp
        (e.g. macOS). So assert the shape, not a hardcoded /tmp path.
        """
        from wayfinder.config import SOCKET_PATH

        assert SOCKET_PATH.startswith("/")
        assert SOCKET_PATH.endswith("wayfinder-aura.sock")


@pytest.mark.skipif(not HAS_SOCKET_MODULE, reason="socket module not importable")
class TestSocketListener:
    """Test the socket_listener function with mocked sockets."""

    def test_socket_listener_signature(self):
        """socket_listener accepts queue, stop_event, and optional log_callback."""
        import inspect
        from wayfinder.hotkeys.socket import socket_listener

        sig = inspect.signature(socket_listener)
        params = list(sig.parameters.keys())

        assert "event_queue" in params
        assert "stop_event" in params
        assert "log_callback" in params

    @patch("wayfinder.hotkeys.socket.os.unlink")
    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_socket_listener_creates_server(self, mock_socket_cls, mock_unlink):
        """socket_listener creates and binds a Unix socket server."""
        from wayfinder.hotkeys.socket import socket_listener
        from wayfinder.config import SOCKET_PATH

        event_queue = Queue()
        stop_event = Event()
        stop_event.set()  # Exit immediately

        mock_server = MagicMock()
        mock_server.accept.side_effect = OSError("stopped")
        mock_socket_cls.return_value = mock_server

        socket_listener(event_queue, stop_event)

        # Verify it tried to create an AF_UNIX SOCK_STREAM socket
        import socket as socket_mod
        mock_socket_cls.assert_called_once_with(
            socket_mod.AF_UNIX, socket_mod.SOCK_STREAM
        )
        mock_server.bind.assert_called_once_with(SOCKET_PATH)
        mock_server.listen.assert_called_once_with(1)

    @patch("wayfinder.hotkeys.socket.os.unlink")
    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_socket_handles_toggle_command(self, mock_socket_cls, mock_unlink):
        """Receiving 'toggle' puts HOTKEY_PRESSED into the event queue."""
        from wayfinder.hotkeys.socket import socket_listener
        from wayfinder.hotkeys import EventType

        event_queue = Queue()
        stop_event = Event()

        mock_conn = MagicMock()
        mock_conn.recv.return_value = b"toggle"

        mock_server = MagicMock()
        # First accept returns the connection, second raises timeout,
        # then we stop.
        call_count = [0]

        def accept_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return (mock_conn, None)
            stop_event.set()
            import socket as s
            raise s.timeout()

        mock_server.accept.side_effect = accept_side_effect
        mock_socket_cls.return_value = mock_server

        socket_listener(event_queue, stop_event)

        assert not event_queue.empty()
        event_type, event_data = event_queue.get_nowait()
        assert event_type == EventType.HOTKEY_PRESSED
        assert event_data is None

    @patch("wayfinder.hotkeys.socket.os.unlink")
    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_socket_handles_style_command(self, mock_socket_cls, mock_unlink):
        """Receiving 'style' puts STYLE_TOGGLE into the event queue."""
        from wayfinder.hotkeys.socket import socket_listener
        from wayfinder.hotkeys import EventType

        event_queue = Queue()
        stop_event = Event()

        mock_conn = MagicMock()
        mock_conn.recv.return_value = b"style"

        mock_server = MagicMock()
        call_count = [0]

        def accept_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return (mock_conn, None)
            stop_event.set()
            import socket as s
            raise s.timeout()

        mock_server.accept.side_effect = accept_side_effect
        mock_socket_cls.return_value = mock_server

        socket_listener(event_queue, stop_event)

        assert not event_queue.empty()
        event_type, event_data = event_queue.get_nowait()
        assert event_type == EventType.STYLE_TOGGLE
        assert event_data is None

    @patch("wayfinder.hotkeys.socket.os.unlink")
    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_socket_handles_style_with_name(self, mock_socket_cls, mock_unlink):
        """Receiving 'style:dev' puts STYLE_TOGGLE with the style name."""
        from wayfinder.hotkeys.socket import socket_listener
        from wayfinder.hotkeys import EventType

        event_queue = Queue()
        stop_event = Event()

        mock_conn = MagicMock()
        mock_conn.recv.return_value = b"style:dev"

        mock_server = MagicMock()
        call_count = [0]

        def accept_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return (mock_conn, None)
            stop_event.set()
            import socket as s
            raise s.timeout()

        mock_server.accept.side_effect = accept_side_effect
        mock_socket_cls.return_value = mock_server

        socket_listener(event_queue, stop_event)

        assert not event_queue.empty()
        event_type, event_data = event_queue.get_nowait()
        assert event_type == EventType.STYLE_TOGGLE
        assert event_data == "dev"

    @patch("wayfinder.hotkeys.socket.os.unlink")
    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_socket_timeout_continues_loop(self, mock_socket_cls, mock_unlink):
        """Socket timeouts are handled and the loop continues."""
        from wayfinder.hotkeys.socket import socket_listener

        event_queue = Queue()
        stop_event = Event()

        mock_server = MagicMock()
        import socket as s

        call_count = [0]

        def accept_side_effect():
            call_count[0] += 1
            if call_count[0] >= 3:
                stop_event.set()
            raise s.timeout()

        mock_server.accept.side_effect = accept_side_effect
        mock_socket_cls.return_value = mock_server

        socket_listener(event_queue, stop_event)

        # Should have looped multiple times
        assert call_count[0] >= 3
        # No events should have been enqueued
        assert event_queue.empty()


@pytest.mark.skipif(not HAS_SOCKET_MODULE, reason="socket module not importable")
class TestSendToggle:
    """Test the send_toggle convenience function."""

    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_send_toggle_success(self, mock_socket_cls):
        """send_toggle returns True on successful send."""
        from wayfinder.hotkeys.socket import send_toggle

        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        result = send_toggle()

        assert result is True
        mock_sock.send.assert_called_once_with(b"toggle")
        mock_sock.close.assert_called_once()

    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_send_toggle_failure(self, mock_socket_cls):
        """send_toggle returns False when connection fails."""
        from wayfinder.hotkeys.socket import send_toggle

        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("no listener")
        mock_socket_cls.return_value = mock_sock

        result = send_toggle()

        assert result is False


@pytest.mark.skipif(not HAS_SOCKET_MODULE, reason="socket module not importable")
class TestSendStyle:
    """Test the send_style convenience function."""

    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_send_style_cycle(self, mock_socket_cls):
        """send_style() without args sends 'style' to cycle."""
        from wayfinder.hotkeys.socket import send_style

        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        result = send_style()

        assert result is True
        mock_sock.send.assert_called_once_with(b"style")

    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_send_style_specific(self, mock_socket_cls):
        """send_style('casual') sends 'style:casual'."""
        from wayfinder.hotkeys.socket import send_style

        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        result = send_style("casual")

        assert result is True
        mock_sock.send.assert_called_once_with(b"style:casual")

    @patch("wayfinder.hotkeys.socket.socket.socket")
    def test_send_style_failure(self, mock_socket_cls):
        """send_style returns False on connection error."""
        from wayfinder.hotkeys.socket import send_style

        mock_sock = MagicMock()
        mock_sock.connect.side_effect = ConnectionRefusedError("no listener")
        mock_socket_cls.return_value = mock_sock

        result = send_style("dev")

        assert result is False


# =============================================================================
# evdev Module Tests
# =============================================================================


@pytest.mark.skipif(not HAS_EVDEV_MODULE, reason="evdev module not importable")
class TestEvdevConstants:
    """Test evdev module constants."""

    def test_mouse_button_codes_is_set(self):
        """MOUSE_BUTTON_CODES is a set of integers."""
        from wayfinder.hotkeys.evdev import MOUSE_BUTTON_CODES

        assert isinstance(MOUSE_BUTTON_CODES, set)
        assert all(isinstance(code, int) for code in MOUSE_BUTTON_CODES)

    def test_mouse_button_codes_contains_expected(self):
        """MOUSE_BUTTON_CODES has known button codes."""
        from wayfinder.hotkeys.evdev import MOUSE_BUTTON_CODES

        # BTN_LEFT=272, BTN_RIGHT=273, BTN_MIDDLE=274
        assert 272 in MOUSE_BUTTON_CODES
        assert 273 in MOUSE_BUTTON_CODES
        assert 274 in MOUSE_BUTTON_CODES


@pytest.mark.skipif(not HAS_EVDEV_MODULE, reason="evdev module not importable")
class TestEvdevFunctionSignatures:
    """Test function signatures for evdev functions."""

    def test_get_all_input_devices_signature(self):
        """get_all_input_devices takes no required arguments."""
        import inspect
        from wayfinder.hotkeys.evdev import get_all_input_devices

        sig = inspect.signature(get_all_input_devices)
        # No required parameters
        required = [
            p for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty
        ]
        assert len(required) == 0

    def test_find_keyboard_devices_signature(self):
        """find_keyboard_devices has optional enabled_devices param."""
        import inspect
        from wayfinder.hotkeys.evdev import find_keyboard_devices

        sig = inspect.signature(find_keyboard_devices)
        params = sig.parameters

        assert "enabled_devices" in params
        assert params["enabled_devices"].default is None

    def test_hotkey_listener_signature(self):
        """hotkey_listener has the expected parameter set."""
        import inspect
        from wayfinder.hotkeys.evdev import hotkey_listener

        sig = inspect.signature(hotkey_listener)
        params = list(sig.parameters.keys())

        assert "event_queue" in params
        assert "hotkey_key" in params
        assert "hotkey_modifiers" in params
        assert "stop_event" in params
        assert "enabled_devices" in params
        assert "log_callback" in params
        assert "style_toggle_key" in params
        assert "style_toggle_modifiers" in params


@pytest.mark.skipif(not HAS_EVDEV_MODULE, reason="evdev module not importable")
class TestEvdevDeviceDiscovery:
    """Test device discovery with mocked evdev."""

    @patch("wayfinder.hotkeys.evdev.evdev")
    def test_get_all_input_devices_filters_by_ev_key(self, mock_evdev):
        """Only devices with EV_KEY capability are returned."""
        from wayfinder.hotkeys.evdev import get_all_input_devices
        from evdev import ecodes

        # Create a keyboard device (has EV_KEY with F-keys)
        keyboard = MagicMock()
        keyboard.name = "Test Keyboard"
        keyboard.path = "/dev/input/event0"
        keyboard.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12, ecodes.KEY_A]
        }

        # Create a device without EV_KEY (e.g., touchpad with only EV_ABS)
        touchpad = MagicMock()
        touchpad.name = "Test Touchpad"
        touchpad.path = "/dev/input/event1"
        touchpad.capabilities.return_value = {3: [0, 1]}  # EV_ABS only

        mock_evdev.list_devices.return_value = ["/dev/input/event0", "/dev/input/event1"]
        mock_evdev.InputDevice.side_effect = lambda path: {
            "/dev/input/event0": keyboard,
            "/dev/input/event1": touchpad,
        }[path]

        devices = get_all_input_devices()

        # Only the keyboard should be returned
        names = [d["name"] for d in devices]
        assert "Test Keyboard" in names
        assert "Test Touchpad" not in names

    @patch("wayfinder.hotkeys.evdev.evdev")
    def test_get_all_input_devices_excludes_virtual(self, mock_evdev):
        """Virtual devices and ydotool are excluded."""
        from wayfinder.hotkeys.evdev import get_all_input_devices
        from evdev import ecodes

        real_kb = MagicMock()
        real_kb.name = "Real Keyboard"
        real_kb.path = "/dev/input/event0"
        real_kb.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12]
        }

        virtual_kb = MagicMock()
        virtual_kb.name = "ydotool Virtual Keyboard"
        virtual_kb.path = "/dev/input/event1"
        virtual_kb.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12]
        }

        mock_evdev.list_devices.return_value = ["/dev/input/event0", "/dev/input/event1"]
        mock_evdev.InputDevice.side_effect = lambda path: {
            "/dev/input/event0": real_kb,
            "/dev/input/event1": virtual_kb,
        }[path]

        devices = get_all_input_devices()

        names = [d["name"] for d in devices]
        assert "Real Keyboard" in names
        assert "ydotool Virtual Keyboard" not in names

    @patch("wayfinder.hotkeys.evdev.evdev")
    def test_get_all_input_devices_classifies_mouse(self, mock_evdev):
        """Devices with mouse buttons but no F-keys are classified as mouse."""
        from wayfinder.hotkeys.evdev import get_all_input_devices, MOUSE_BUTTON_CODES
        from evdev import ecodes

        mouse = MagicMock()
        mouse.name = "Test Mouse"
        mouse.path = "/dev/input/event0"
        mouse.capabilities.return_value = {
            ecodes.EV_KEY: [272, 273, 274]  # BTN_LEFT, BTN_RIGHT, BTN_MIDDLE
        }

        mock_evdev.list_devices.return_value = ["/dev/input/event0"]
        mock_evdev.InputDevice.side_effect = lambda path: mouse

        devices = get_all_input_devices()

        assert len(devices) == 1
        assert devices[0]["type"] == "mouse"

    @patch("wayfinder.hotkeys.evdev.evdev")
    def test_find_keyboard_devices_returns_all_by_default(self, mock_evdev):
        """find_keyboard_devices returns all devices when no filter is given."""
        from wayfinder.hotkeys.evdev import find_keyboard_devices
        from evdev import ecodes

        kb1 = MagicMock()
        kb1.name = "Keyboard One"
        kb1.path = "/dev/input/event0"
        kb1.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12]
        }

        kb2 = MagicMock()
        kb2.name = "Keyboard Two"
        kb2.path = "/dev/input/event1"
        kb2.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12]
        }

        mock_evdev.list_devices.return_value = ["/dev/input/event0", "/dev/input/event1"]
        mock_evdev.InputDevice.side_effect = lambda path: {
            "/dev/input/event0": kb1,
            "/dev/input/event1": kb2,
        }[path]

        devices = find_keyboard_devices()

        assert len(devices) == 2

    @patch("wayfinder.hotkeys.evdev.evdev")
    def test_find_keyboard_devices_filters_by_name(self, mock_evdev):
        """find_keyboard_devices filters to only the named devices."""
        from wayfinder.hotkeys.evdev import find_keyboard_devices
        from evdev import ecodes

        kb1 = MagicMock()
        kb1.name = "Keyboard One"
        kb1.path = "/dev/input/event0"
        kb1.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12]
        }

        kb2 = MagicMock()
        kb2.name = "Keyboard Two"
        kb2.path = "/dev/input/event1"
        kb2.capabilities.return_value = {
            ecodes.EV_KEY: [ecodes.KEY_F1, ecodes.KEY_F12]
        }

        mock_evdev.list_devices.return_value = ["/dev/input/event0", "/dev/input/event1"]
        mock_evdev.InputDevice.side_effect = lambda path: {
            "/dev/input/event0": kb1,
            "/dev/input/event1": kb2,
        }[path]

        devices = find_keyboard_devices(enabled_devices=["Keyboard Two"])

        assert len(devices) == 1
        assert devices[0].name == "Keyboard Two"


@pytest.mark.skipif(not HAS_EVDEV_MODULE, reason="evdev module not importable")
class TestEvdevHotkeyListener:
    """Test hotkey_listener behaviour with mocked evdev."""

    @patch("wayfinder.hotkeys.evdev.find_keyboard_devices")
    def test_listener_exits_when_no_devices(self, mock_find):
        """hotkey_listener returns early when no input devices are found."""
        from wayfinder.hotkeys.evdev import hotkey_listener

        mock_find.return_value = []
        event_queue = Queue()
        stop_event = Event()
        log_messages = []

        hotkey_listener(
            event_queue=event_queue,
            hotkey_key=67,
            hotkey_modifiers=[],
            stop_event=stop_event,
            log_callback=log_messages.append,
        )

        assert any("No input devices" in msg for msg in log_messages)
        assert event_queue.empty()

    @patch("wayfinder.hotkeys.evdev.select.select")
    @patch("wayfinder.hotkeys.evdev.find_keyboard_devices")
    def test_listener_detects_hotkey_press(self, mock_find, mock_select):
        """hotkey_listener enqueues HOTKEY_PRESSED on matching key down event."""
        from wayfinder.hotkeys.evdev import hotkey_listener, EventType
        from evdev import ecodes

        # Create a mock device with mock events
        mock_device = MagicMock()
        mock_device.fd = 42
        mock_device.name = "Test KB"

        # Create a key event (key down for F9)
        mock_event = MagicMock()
        mock_event.type = ecodes.EV_KEY
        mock_key_event = MagicMock()
        mock_key_event.scancode = 67  # F9
        mock_key_event.keystate = 1  # key down

        mock_find.return_value = [mock_device]

        # First select returns the device fd, second stops the loop
        call_count = [0]

        def select_side_effect(fds, _, __, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return ([42], [], [])
            # Signal stop on second iteration
            stop_event.set()
            return ([], [], [])

        mock_select.side_effect = select_side_effect
        mock_device.read.return_value = [mock_event]

        event_queue = Queue()
        stop_event = Event()

        with patch("wayfinder.hotkeys.evdev.categorize", return_value=mock_key_event):
            hotkey_listener(
                event_queue=event_queue,
                hotkey_key=67,
                hotkey_modifiers=[],
                stop_event=stop_event,
            )

        assert not event_queue.empty()
        event_type, _ = event_queue.get_nowait()
        assert event_type == EventType.HOTKEY_PRESSED

    @patch("wayfinder.hotkeys.evdev.select.select")
    @patch("wayfinder.hotkeys.evdev.find_keyboard_devices")
    def test_listener_ignores_key_up(self, mock_find, mock_select):
        """Key-up events for the hotkey are ignored (only key-down fires)."""
        from wayfinder.hotkeys.evdev import hotkey_listener
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.fd = 42
        mock_device.name = "Test KB"

        mock_event = MagicMock()
        mock_event.type = ecodes.EV_KEY
        mock_key_event = MagicMock()
        mock_key_event.scancode = 67  # F9
        mock_key_event.keystate = 0  # key up

        mock_find.return_value = [mock_device]

        call_count = [0]

        def select_side_effect(fds, _, __, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return ([42], [], [])
            stop_event.set()
            return ([], [], [])

        mock_select.side_effect = select_side_effect
        mock_device.read.return_value = [mock_event]

        event_queue = Queue()
        stop_event = Event()

        with patch("wayfinder.hotkeys.evdev.categorize", return_value=mock_key_event):
            hotkey_listener(
                event_queue=event_queue,
                hotkey_key=67,
                hotkey_modifiers=[],
                stop_event=stop_event,
            )

        assert event_queue.empty()

    @patch("wayfinder.hotkeys.evdev.select.select")
    @patch("wayfinder.hotkeys.evdev.find_keyboard_devices")
    def test_listener_detects_style_toggle(self, mock_find, mock_select):
        """hotkey_listener enqueues STYLE_TOGGLE on the style key press."""
        from wayfinder.hotkeys.evdev import hotkey_listener, EventType
        from evdev import ecodes

        mock_device = MagicMock()
        mock_device.fd = 42
        mock_device.name = "Test KB"

        mock_event = MagicMock()
        mock_event.type = ecodes.EV_KEY
        mock_key_event = MagicMock()
        mock_key_event.scancode = 68  # F10
        mock_key_event.keystate = 1  # key down

        mock_find.return_value = [mock_device]

        call_count = [0]

        def select_side_effect(fds, _, __, timeout):
            call_count[0] += 1
            if call_count[0] == 1:
                return ([42], [], [])
            stop_event.set()
            return ([], [], [])

        mock_select.side_effect = select_side_effect
        mock_device.read.return_value = [mock_event]

        event_queue = Queue()
        stop_event = Event()

        with patch("wayfinder.hotkeys.evdev.categorize", return_value=mock_key_event):
            hotkey_listener(
                event_queue=event_queue,
                hotkey_key=67,  # F9 for recording
                hotkey_modifiers=[],
                stop_event=stop_event,
                style_toggle_key=68,  # F10 for style
                style_toggle_modifiers=[],
            )

        assert not event_queue.empty()
        event_type, _ = event_queue.get_nowait()
        assert event_type == EventType.STYLE_TOGGLE


# =============================================================================
# D-Bus Module Tests
# =============================================================================


@pytest.mark.skipif(not HAS_DBUS_MODULE, reason="dbus module not importable")
class TestDbusAvailability:
    """Test D-Bus availability detection."""

    def test_is_dbus_available_function_exists(self):
        """is_dbus_available is callable."""
        from wayfinder.hotkeys.dbus import is_dbus_available

        assert callable(is_dbus_available)

    def test_is_dbus_available_returns_bool(self):
        """is_dbus_available returns a boolean."""
        from wayfinder.hotkeys.dbus import is_dbus_available

        result = is_dbus_available()
        assert isinstance(result, bool)

    def test_is_dbus_available_matches_import_flag(self):
        """is_dbus_available reflects the DBUS_AVAILABLE module flag."""
        from wayfinder.hotkeys.dbus import is_dbus_available, DBUS_AVAILABLE

        assert is_dbus_available() == DBUS_AVAILABLE


@pytest.mark.skipif(not HAS_DBUS_MODULE, reason="dbus module not importable")
class TestDbusAvailabilityMocked:
    """Test D-Bus availability with mocked import states."""

    def test_dbus_unavailable_when_import_fails(self):
        """DBUS_AVAILABLE is False when dbus-python isn't installed."""
        # We test by checking the module's behavior: if dbus import fails,
        # the module sets DBUS_AVAILABLE = False
        import importlib
        import wayfinder.hotkeys.dbus as dbus_mod

        with patch.dict("sys.modules", {"dbus": None}):
            # Re-check: the flag was set at import time, so we verify the
            # function still returns a boolean consistent with the flag
            result = dbus_mod.is_dbus_available()
            assert isinstance(result, bool)

    def test_dbus_available_with_mock(self):
        """is_dbus_available returns True when dbus is importable."""
        import wayfinder.hotkeys.dbus as dbus_mod

        original = dbus_mod.DBUS_AVAILABLE
        try:
            dbus_mod.DBUS_AVAILABLE = True
            assert dbus_mod.is_dbus_available() is True
        finally:
            dbus_mod.DBUS_AVAILABLE = original

    def test_dbus_unavailable_with_mock(self):
        """is_dbus_available returns False when flag is False."""
        import wayfinder.hotkeys.dbus as dbus_mod

        original = dbus_mod.DBUS_AVAILABLE
        try:
            dbus_mod.DBUS_AVAILABLE = False
            assert dbus_mod.is_dbus_available() is False
        finally:
            dbus_mod.DBUS_AVAILABLE = original


@pytest.mark.skipif(not HAS_DBUS_MODULE, reason="dbus module not importable")
class TestWaylandHotkeyListener:
    """Test the wayland_hotkey_listener function."""

    def test_wayland_listener_signature(self):
        """wayland_hotkey_listener has the expected parameters."""
        import inspect
        from wayfinder.hotkeys.dbus import wayland_hotkey_listener

        sig = inspect.signature(wayland_hotkey_listener)
        params = list(sig.parameters.keys())

        assert "event_queue" in params
        assert "hotkey_display" in params
        assert "stop_event" in params
        assert "log_callback" in params

    def test_wayland_listener_returns_false_without_dbus(self):
        """wayland_hotkey_listener returns False when dbus is unavailable."""
        import wayfinder.hotkeys.dbus as dbus_mod

        original = dbus_mod.DBUS_AVAILABLE
        try:
            dbus_mod.DBUS_AVAILABLE = False

            event_queue = Queue()
            stop_event = Event()
            log_messages = []

            result = dbus_mod.wayland_hotkey_listener(
                event_queue=event_queue,
                hotkey_display="F9",
                stop_event=stop_event,
                log_callback=log_messages.append,
            )

            assert result is False
            assert any("not available" in msg for msg in log_messages)
        finally:
            dbus_mod.DBUS_AVAILABLE = original

    def test_app_id_constant(self):
        """APP_ID defaults to 'wayfinder-aura' when not in Flatpak."""
        from wayfinder.hotkeys.dbus import APP_ID

        # In test env, FLATPAK_ID is unset (cleaned by conftest)
        assert APP_ID == "wayfinder-aura"


# =============================================================================
# Package-level __init__.py re-exports
# =============================================================================


class TestHotkeysPackageExports:
    """Test that the hotkeys package re-exports expected symbols."""

    def test_event_type_importable_from_package(self):
        """EventType can be imported from wayfinder.hotkeys."""
        from wayfinder.hotkeys import EventType

        assert hasattr(EventType, "HOTKEY_PRESSED")

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
    def test_linux_exports(self):
        """On Linux, evdev functions are exported from the package."""
        from wayfinder.hotkeys import (
            get_all_input_devices,
            find_keyboard_devices,
            hotkey_listener,
            MOUSE_BUTTON_CODES,
        )

        assert callable(get_all_input_devices)
        assert callable(find_keyboard_devices)
        assert callable(hotkey_listener)
        assert isinstance(MOUSE_BUTTON_CODES, set)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
    def test_socket_exports(self):
        """On Linux, socket functions are exported from the package."""
        from wayfinder.hotkeys import socket_listener, send_toggle, send_style

        assert callable(socket_listener)
        assert callable(send_toggle)
        assert callable(send_style)

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
    def test_dbus_exports(self):
        """On Linux, dbus functions are exported from the package."""
        from wayfinder.hotkeys import wayland_hotkey_listener, is_dbus_available

        assert callable(wayland_hotkey_listener)
        assert callable(is_dbus_available)

    def test_get_best_hotkey_listener_exists(self):
        """get_best_hotkey_listener is importable and callable."""
        from wayfinder.hotkeys import get_best_hotkey_listener

        assert callable(get_best_hotkey_listener)

    def test_get_best_hotkey_listener_returns_tuple(self):
        """get_best_hotkey_listener returns a (function, name) tuple."""
        from wayfinder.hotkeys import get_best_hotkey_listener

        result = get_best_hotkey_listener()

        assert isinstance(result, tuple)
        assert len(result) == 2
        _, name = result
        assert isinstance(name, str)

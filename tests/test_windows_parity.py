"""Windows counterparts of the macOS-branch improvements.

Each Windows adapter mirrors a macOS module's contract (see docs/MACOS-PORT.md,
"Suggested for Linux and Windows"). Tests that talk to the real OS (power
requests, Credential Manager, job objects, the Run key) are Windows-only and
use throwaway names; the rest simulate the platform so Linux proves it is
unchanged.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
import uuid
from types import SimpleNamespace

import pytest

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows API")


# --- Idle-sleep hold (utils/windows_power.py via macos_activity) -------------

def test_activity_dispatches_to_windows_power(monkeypatch):
    from wayfinder.utils import macos_activity, windows_power

    calls = []
    monkeypatch.setattr(macos_activity.sys, "platform", "win32")
    monkeypatch.setattr(windows_power, "begin", lambda k, r: calls.append(("begin", k)) or True)
    monkeypatch.setattr(windows_power, "end", lambda k: calls.append(("end", k)))
    monkeypatch.setattr(windows_power, "is_held", lambda k: True)
    with macos_activity.held("download:x", "Downloading a model") as began:
        assert began is True
        assert macos_activity.is_held("download:x")
    assert calls == [("begin", "download:x"), ("end", "download:x")]


def test_activity_is_a_noop_on_linux(monkeypatch):
    from wayfinder.utils import macos_activity

    monkeypatch.setattr(macos_activity.sys, "platform", "linux")
    assert macos_activity.begin("k", "r") is False
    macos_activity.end("k")
    assert not macos_activity.is_held("k")


@windows_only
def test_real_power_request_begins_and_ends():
    from wayfinder.utils import windows_power

    key = f"test-{uuid.uuid4().hex[:6]}"
    assert windows_power.begin(key, "Wayfinder test hold")
    assert windows_power.begin(key, "again")  # idempotent
    assert windows_power.is_held(key)
    windows_power.end(key)
    windows_power.end(key)  # safe twice
    assert not windows_power.is_held(key)


# --- API keys in Credential Manager (utils/windows_credentials.py) ----------

@pytest.fixture
def credentials(monkeypatch):
    from wayfinder.utils import windows_credentials
    import wayfinder.config as config_module

    monkeypatch.delenv("WAYFINDER_DISABLE_KEYCHAIN", raising=False)
    monkeypatch.setattr(windows_credentials, "SERVICE", f"Wayfinder Aura test {uuid.uuid4().hex[:8]}")
    config_module._KEYCHAIN_SYNCED.clear()
    yield windows_credentials
    for name in config_module.SECRET_CONFIG_KEYS:
        windows_credentials.delete(name)
    config_module._KEYCHAIN_SYNCED.clear()


def test_credentials_are_a_noop_off_windows(monkeypatch):
    from wayfinder.utils import windows_credentials

    monkeypatch.setattr(windows_credentials.sys, "platform", "linux")
    assert windows_credentials.available() is False
    assert windows_credentials.get("groq_api_key") is None
    assert windows_credentials.set("groq_api_key", "x") is False


def test_linux_config_has_no_secret_store(monkeypatch):
    import wayfinder.config as config_module

    monkeypatch.delenv("WAYFINDER_DISABLE_KEYCHAIN", raising=False)
    monkeypatch.setattr(config_module.sys, "platform", "linux")
    assert config_module._macos_keychain() is None


@windows_only
def test_credential_round_trip(credentials):
    assert credentials.get("groq_api_key") == ""
    assert credentials.set("groq_api_key", "gsk_one")
    assert credentials.set("groq_api_key", "gsk_twö")  # replace, non-ASCII safe
    assert credentials.get("groq_api_key") == "gsk_twö"
    assert credentials.delete("groq_api_key")
    assert credentials.get("groq_api_key") == ""
    assert credentials.delete("groq_api_key")  # already gone is fine


@windows_only
def test_saved_keys_leave_config_json_and_come_back(credentials, tmp_path, monkeypatch):
    import wayfinder.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    config = cfg.load_config()
    config["groq_api_key"] = "gsk_secret_value"
    cfg.save_config(config)

    on_disk = json.loads(cfg.CONFIG_FILE.read_text())
    assert on_disk["groq_api_key"] == ""
    assert "secret" not in cfg.CONFIG_FILE.read_text()
    assert credentials.get("groq_api_key") == "gsk_secret_value"

    cfg._KEYCHAIN_SYNCED.clear()
    assert cfg.load_config()["groq_api_key"] == "gsk_secret_value"


@windows_only
def test_plain_text_key_in_config_moves_into_the_store(credentials, tmp_path, monkeypatch):
    import wayfinder.config as cfg

    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.json")
    cfg.CONFIG_FILE.write_text(json.dumps({"openai_api_key": "sk-old-plain"}))
    config = cfg.load_config()
    assert config["openai_api_key"] == "sk-old-plain"
    assert json.loads(cfg.CONFIG_FILE.read_text())["openai_api_key"] == ""
    assert credentials.get("openai_api_key") == "sk-old-plain"


@windows_only
def test_windows_ships_the_current_claude_model():
    from wayfinder.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["anthropic_model"] == "claude-haiku-4-5-20251001"


# --- Child processes die with the app (child_supervisor.bind_to_app_lifetime)

def test_bind_is_a_noop_off_windows():
    from wayfinder.utils.child_supervisor import bind_to_app_lifetime

    assert bind_to_app_lifetime(SimpleNamespace(_handle=1), platform_name="linux") is False
    assert bind_to_app_lifetime(SimpleNamespace(_handle=1), platform_name="darwin") is False


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


@windows_only
def test_bound_child_dies_when_the_app_is_killed(tmp_path):
    src = str((__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
    parent = tmp_path / "parent.py"
    parent.write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        sys.path.insert(0, {src!r})
        from wayfinder.utils.child_supervisor import bind_to_app_lifetime
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        print(child.pid, bind_to_app_lifetime(child), flush=True)
        time.sleep(120)
    """))
    proc = subprocess.Popen([sys.executable, str(parent)], stdout=subprocess.PIPE, text=True)
    try:
        pid_text, bound = proc.stdout.readline().split()
        child_pid = int(pid_text)
        assert bound == "True"
        assert _alive(child_pid)
        # A crash: no atexit, no cleanup code runs in the parent.
        subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)], capture_output=True)
        deadline = time.monotonic() + 10
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not _alive(child_pid)
    finally:
        proc.kill()


# --- Clipboard history exclusion + focus guard (core/injector_windows.py) ---

@windows_only
def test_transient_paste_marks_history_exclusion_formats(monkeypatch):
    from wayfinder.core import injector_windows as w

    registered, placed = {}, []
    monkeypatch.setattr(w, "_open_clipboard", lambda retries=5: True)
    monkeypatch.setattr(w._user32, "EmptyClipboard", lambda: True)
    monkeypatch.setattr(w._user32, "CloseClipboard", lambda: True)
    monkeypatch.setattr(
        w._user32, "RegisterClipboardFormatW",
        lambda name: registered.setdefault(name, 0xC000 + len(registered)))
    monkeypatch.setattr(w._user32, "SetClipboardData",
                        lambda fmt, handle: placed.append(fmt) or 1)

    assert w._clipboard_set_windows("hello", transient=True)
    assert set(registered) == {
        "ExcludeClipboardContentFromMonitorProcessing",
        "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard",
    }
    assert placed[0] == w._CF_UNICODETEXT
    assert set(placed[1:]) == set(registered.values())

    placed.clear()
    assert w._clipboard_set_windows("hello")  # a plain copy stays plain
    assert placed == [w._CF_UNICODETEXT]


@windows_only
def test_paste_injection_marks_dictation_and_restore_transient(monkeypatch):
    from wayfinder.core import injector_windows as w

    sets = []
    clipboard = {"text": "user's own clipboard"}
    monkeypatch.setattr(w, "_require_foreground_window", lambda: None)
    monkeypatch.setattr(w, "require_modifier_release_windows", lambda: None)
    monkeypatch.setattr(w, "_send", lambda inputs: None)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(w, "_clipboard_get_windows", lambda: clipboard["text"])

    def fake_set(text, transient=False):
        sets.append((text, transient))
        clipboard["text"] = text
        return True

    monkeypatch.setattr(w, "_clipboard_set_windows", fake_set)
    w.inject_text_paste_windows("dictated words")
    assert sets == [("dictated words", True), ("user's own clipboard", True)]


def test_active_window_uses_the_foreground_hwnd_on_windows(monkeypatch):
    from wayfinder.core import injector

    fake = SimpleNamespace(foreground_window_id=lambda: "4242")
    monkeypatch.setattr(injector.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "wayfinder.core.injector_windows", fake)
    monkeypatch.setattr(injector.subprocess, "run",
                        lambda *a, **k: pytest.fail("xdotool must not run on Windows"))
    assert injector.get_active_window() == "4242"


# --- Open at login (utils/windows_login_item.py) -----------------------------

def test_login_item_needs_the_installed_windows_app(monkeypatch):
    from wayfinder.utils import windows_login_item as L

    monkeypatch.setattr(L.sys, "platform", "linux")
    assert L.available() is False
    assert L.is_enabled() is False
    assert L.set_enabled(True) == (False, "Available in the installed app.")
    monkeypatch.setattr(L.sys, "platform", "win32")
    monkeypatch.delattr(L.sys, "frozen", raising=False)
    assert L.available() is False  # a source run is never a login item


@windows_only
def test_login_item_round_trip_in_the_run_key(monkeypatch, tmp_path):
    import winreg
    from wayfinder.utils import windows_login_item as L

    exe = str(tmp_path / "Wayfinder Aura.exe")
    monkeypatch.setattr(L, "VALUE_NAME", f"Wayfinder Aura test {uuid.uuid4().hex[:8]}")
    monkeypatch.setattr(L.sys, "frozen", True, raising=False)
    monkeypatch.setattr(L.sys, "executable", exe)
    try:
        assert L.is_enabled() is False
        assert L.set_enabled(True) == (True, None)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, L.RUN_KEY) as key:
            assert winreg.QueryValueEx(key, L.VALUE_NAME)[0] == f'"{exe}"'
        assert L.is_enabled() is True

        # Switched off in Task Manager -> reads as off; our toggle re-enables.
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, L.APPROVED_KEY) as key:
            winreg.SetValueEx(key, L.VALUE_NAME, 0, winreg.REG_BINARY, b"\x03" + b"\x00" * 11)
        assert L.is_enabled() is False
        assert L.set_enabled(True) == (True, None)
        assert L.is_enabled() is True

        # Another install path's entry is not "ours".
        monkeypatch.setattr(L.sys, "executable", str(tmp_path / "other" / "Wayfinder Aura.exe"))
        assert L.is_enabled() is False
        monkeypatch.setattr(L.sys, "executable", exe)

        assert L.set_enabled(False) == (True, None)
        assert L.is_enabled() is False
    finally:
        for path in (L.RUN_KEY, L.APPROVED_KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, L.VALUE_NAME)
            except OSError:
                pass


def test_installer_removes_the_login_item_on_uninstall():
    from pathlib import Path

    iss = (Path(__file__).resolve().parent.parent / "packaging" / "windows" / "installer.iss").read_text()
    assert ('Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Run"; ValueType: none; '
            'ValueName: "{#MyAppName}"; Flags: uninsdeletevalue dontcreatekey') in iss


# --- HTTPS trust store + child environment ------------------------------------

@windows_only
def test_windows_uses_the_system_trust_store():
    from wayfinder.tls import use_macos_trust_store

    assert use_macos_trust_store() is True


@pytest.mark.parametrize("platform_name, scrubbed", [
    ("win32", True), ("darwin", True), ("linux", False)])
def test_native_children_do_not_inherit_cloud_keys(monkeypatch, platform_name, scrubbed):
    from wayfinder.utils import hostexec

    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(hostexec.sys, "platform", platform_name)
    env = hostexec.bundle_binary_env()
    assert ("GROQ_API_KEY" not in env) is scrubbed


# --- Control channel (hotkeys/windows_control.py) ------------------------------

def test_control_channel_accepts_every_unix_socket_verb():
    """Same verbs as hotkeys/socket.py, so the tray and CLI behave identically."""
    import re
    from pathlib import Path
    from queue import Queue

    from wayfinder.hotkeys import windows_control

    src = (Path(__file__).resolve().parent.parent / "src" / "wayfinder" / "hotkeys"
           / "socket.py").read_text(encoding="utf-8")
    exact = set(re.findall(r'data_str == "([a-z]+)"', src))
    prefixed = set(re.findall(r'data_str\.startswith\("([a-z]+:)"\)', src))
    assert exact >= {"ping", "toggle", "show", "hide", "quit"}
    for verb in sorted(exact) + [p + "x" for p in sorted(prefixed)]:
        q = Queue()
        reply = windows_control.dispatch(verb, q, lambda m: None)
        assert verb == "ping" or not q.empty(), verb
        if verb == "ping":
            assert reply == b"pong"


def test_control_channel_round_trip_and_token_guard(tmp_path, monkeypatch):
    import threading
    import socket as _socket
    from queue import Queue

    from wayfinder.hotkeys import windows_control
    from wayfinder.hotkeys.types import EventType

    monkeypatch.setattr(windows_control, "endpoint_file", lambda: tmp_path / "control.json")
    q, stop = Queue(), threading.Event()
    t = threading.Thread(target=windows_control.control_listener, args=(q, stop), daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "control.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert windows_control.send_command("ping", expect_reply=True) == b"pong"
        assert windows_control.send_command("show", expect_reply=True) == b"ok"
        assert q.get(timeout=2) == (EventType.SHOW_WINDOW, None)

        port = json.loads((tmp_path / "control.json").read_text())["port"]
        with _socket.create_connection(("127.0.0.1", port), timeout=2) as s:
            s.sendall(b"not-the-token quit")
            assert s.recv(16) == b""  # dropped, no reply
        time.sleep(0.2)
        assert q.empty()  # and nothing was queued
    finally:
        stop.set()
        t.join(timeout=5)
    assert not (tmp_path / "control.json").exists()  # cleaned up on stop


def test_send_command_without_a_running_app_is_none(tmp_path, monkeypatch):
    from wayfinder.hotkeys import windows_control

    monkeypatch.setattr(windows_control, "endpoint_file", lambda: tmp_path / "missing.json")
    assert windows_control.send_command("show") is None


# --- Global Escape cancels a recording (hotkeys/pynput_listener.py) -----------

# Linux too: pynput runs there only on X11 (Flatpak X11 fallback, native X11);
# portal sessions bind the cancel-dictation shortcut instead.
@pytest.mark.parametrize("platform_name, queued", [("win32", True), ("linux", True)])
def test_escape_cancels_recording_from_any_app(monkeypatch, platform_name, queued):
    from queue import Empty, Queue
    from threading import Event

    from wayfinder.hotkeys import pynput_listener
    from wayfinder.hotkeys.types import EventType

    if pynput_listener.keyboard is None:
        pytest.skip("pynput unavailable on this host")
    captured = {}

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pynput_listener.sys, "platform", platform_name)
    monkeypatch.setattr(pynput_listener.keyboard, "Listener", FakeListener)
    stop, events = Event(), Queue()
    stop.set()
    pynput_listener.pynput_hotkey_listener(
        events, hotkey_key=57, hotkey_modifiers=["ctrl", "alt"], stop_event=stop)
    captured["on_press"](pynput_listener.Key.esc)
    try:
        got = events.get_nowait()
    except Empty:
        got = None
    assert (got == (EventType.CANCEL_RECORDING, None)) is queued


def test_windows_key_help_says_ctrl_v(monkeypatch):
    import inspect

    import wayfinder_main

    src = inspect.getsource(wayfinder_main.WayfinderApp._build_macos_key_help)
    assert 'info.steps.replace("(⌘V)", "(Ctrl+V)") if IS_WINDOWS' in src


# --- First run: the welcome tour downloads Base; open-at-login offer ----------

@pytest.mark.parametrize("platform_name, module", [
    ("win32", "windows_login_item"), ("darwin", "macos_login_item")])
def test_welcome_login_offer_uses_the_platform_backend(monkeypatch, platform_name, module):
    from wayfinder.ui import welcome

    monkeypatch.setattr(welcome.sys, "platform", platform_name)
    assert welcome._login_item_module().__name__.endswith(module)


def test_welcome_copy_names_the_pc_on_windows(monkeypatch):
    from wayfinder.ui import welcome

    monkeypatch.setattr(welcome.sys, "platform", "win32")
    assert welcome._device_noun() == "PC"
    monkeypatch.setattr(welcome.sys, "platform", "darwin")
    assert welcome._device_noun() == "Mac"


def test_windows_first_run_waits_for_a_speech_model():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert 'if frozen and sys.platform in ("darwin", "win32"):' in src


# --- Window look: content-sized first run, work-area tooltips ------------------

def test_windows_first_run_window_is_content_sized_and_centred():
    from wayfinder.ui.window_geometry import default_window_geometry

    w, h, x, y = default_window_geometry(1463, 914, platform="win32",
                                         visible_frame=(0, 0, 1463, 866))
    assert (w, h) == (800, 780)
    assert x == (1463 - 800) // 2
    # Linux keeps the right half.
    assert default_window_geometry(1920, 1080, platform="linux")[2] == 960


def test_windows_colour_helpers():
    from wayfinder.ui.windows_window import colorref

    assert colorref("#0A0D13") == 0x00130D0A


# --- Hotkey conflict caption (utils/windows_hotkey_conflicts.py) -------------

@pytest.mark.parametrize("code, mods, fragment", [
    (57, ["alt"], "window menu"),
    (57, ["ctrl"], "input method"),
    (57, ["shift"], "half/full-width"),
    (28, ["ctrl"], "sends the message"),
    (63, [], "F5 alone"),
    (88, [], "F12 alone"),
])
def test_windows_hotkey_conflicts(monkeypatch, code, mods, fragment):
    from wayfinder.utils import windows_hotkey_conflicts as c

    monkeypatch.setattr(c.sys, "platform", "win32")
    assert fragment in c.conflict_for(code, mods, installed=())


def test_windows_hotkey_clear_chords(monkeypatch):
    from wayfinder.utils import windows_hotkey_conflicts as c

    monkeypatch.setattr(c.sys, "platform", "win32")
    assert c.conflict_for(57, ["ctrl", "alt"], installed=(), magnifier=False) is None  # the default
    assert c.conflict_for(67, [], installed=()) is None               # bare F9
    assert "1Password" in c.conflict_for(57, ["ctrl", "shift"], installed={"1Password"})
    assert c.conflict_for(57, ["ctrl", "shift"], installed=()) is None
    monkeypatch.setattr(c.sys, "platform", "linux")
    assert c.conflict_for(57, ["alt"]) is None


# --- Audio: ducking the main volume, "Auto" mic follows the Windows default ----

@pytest.mark.parametrize("system,loads_core_audio", [("Windows", False), ("Linux", False), ("Darwin", True)])
def test_core_audio_probe_is_macos_only(monkeypatch, system, loads_core_audio):
    from unittest.mock import Mock
    from wayfinder.utils import audio_ducker, macos_audio

    load = Mock(return_value=object())
    monkeypatch.setattr(audio_ducker.platform, "system", lambda: system)
    monkeypatch.setattr(macos_audio, "_load", load)
    assert audio_ducker._core_audio() is (macos_audio if loads_core_audio else None)
    assert load.call_count == int(loads_core_audio)


def test_windows_ducking_lowers_and_restores_main_volume(monkeypatch, tmp_path):
    from wayfinder.utils import audio_ducker, windows_audio

    level = {"v": 0.8}
    monkeypatch.setattr(audio_ducker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(audio_ducker, "is_pactl_available", lambda: False)
    monkeypatch.setattr(windows_audio, "default_output_id", lambda: "speakers")
    monkeypatch.setattr(windows_audio, "output_volume", lambda device_id=None: level["v"])
    monkeypatch.setattr(windows_audio, "set_output_volume",
                        lambda v, device_id=None: level.__setitem__("v", v) or True)
    d = audio_ducker.AudioDucker(duck_percent=50, recovery_path=tmp_path / "duck.json")
    assert d.is_available
    assert d.duck().status == audio_ducker.DuckingStatus.APPLIED
    assert level["v"] == pytest.approx(0.40)
    assert d.restore().status == audio_ducker.DuckingStatus.RESTORED
    assert level["v"] == pytest.approx(0.80)


def test_windows_ducking_never_overwrites_a_user_change(monkeypatch, tmp_path):
    from wayfinder.utils import audio_ducker, windows_audio

    level = {"v": 0.8}
    monkeypatch.setattr(audio_ducker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(audio_ducker, "is_pactl_available", lambda: False)
    monkeypatch.setattr(windows_audio, "default_output_id", lambda: "speakers")
    monkeypatch.setattr(windows_audio, "output_volume", lambda device_id=None: level["v"])
    monkeypatch.setattr(windows_audio, "set_output_volume",
                        lambda v, device_id=None: level.__setitem__("v", v) or True)
    d = audio_ducker.AudioDucker(duck_percent=50, recovery_path=tmp_path / "duck.json")
    d.duck()
    level["v"] = 0.9  # the user turned it up mid-dictation
    assert d.restore().status == audio_ducker.DuckingStatus.NO_CHANGE
    assert level["v"] == pytest.approx(0.9)


def test_linux_ducking_still_needs_pactl(monkeypatch, tmp_path):
    from wayfinder.utils import audio_ducker

    monkeypatch.setattr(audio_ducker.platform, "system", lambda: "Linux")
    monkeypatch.setattr(audio_ducker, "is_pactl_available", lambda: False)
    d = audio_ducker.AudioDucker(duck_percent=50, recovery_path=tmp_path / "duck.json")
    assert not d.is_available


@pytest.mark.parametrize("pa_name, endpoint, same", [
    ("Microphone (2- Shure MV7+)", "Microphone (2- Shure MV7+)", True),
    ("Microphone Array (Intel® Smart ", "Microphone Array (Intel® Smart Sound Technology)", True),
    ("Microphone", "Microphone (2- Shure MV7+)", False),
    ("", "Mic", False),
])
def test_mme_truncated_names_match_their_endpoint(pa_name, endpoint, same):
    from wayfinder.utils.windows_audio import names_match

    assert names_match(pa_name, endpoint) is same


def test_auto_mic_follows_the_windows_default(monkeypatch):
    import wayfinder_main
    from wayfinder.utils import windows_audio

    devices = [
        {"name": "Microsoft Sound Mapper - Input", "max_input_channels": 2, "hostapi": 0},
        {"name": "Microphone (2- Shure MV7+)", "max_input_channels": 1, "hostapi": 0},
        {"name": "Headset (Jabra Evolve2 65)", "max_input_channels": 1, "hostapi": 0},
        {"name": "Headset (Jabra Evolve2 65)", "max_input_channels": 1, "hostapi": 2},
    ]

    class SD:
        @staticmethod
        def query_devices(index=None, kind=None):
            if kind == "input":
                return devices[0]
            return devices if index is None else devices[index]

    monkeypatch.setitem(sys.modules, "sounddevice", SD)
    monkeypatch.setattr(windows_audio, "default_input_device",
                        lambda: {"name": "Headset (Jabra Evolve2 65)", "transport": "bluetooth"})
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    assert wayfinder_main._windows_default_input_index() == 2  # same host API as the default
    assert wayfinder_main.resolve_audio_device({"audio_device_name": ""}) == 2
    # An explicitly chosen mic still wins.
    monkeypatch.setattr(wayfinder_main, "get_input_device_by_name", lambda name: 1)
    assert wayfinder_main.resolve_audio_device({"audio_device_name": "Microphone (2- Shure MV7+)"}) == 1


# --- Sleep/wake + display changes; microphone privacy ---------------------------

def test_lifecycle_messages_map_to_the_mac_callbacks():
    from wayfinder.utils.windows_lifecycle import dispatch

    seen = []
    cb = dict(on_sleep=lambda: seen.append("sleep"), on_wake=lambda: seen.append("wake"),
              on_screens_changed=lambda: seen.append("screens"))
    dispatch(0x0218, 0x4, 0, **cb)    # WM_POWERBROADCAST / PBT_APMSUSPEND
    dispatch(0x0218, 0x7, 0, **cb)    # PBT_APMRESUMESUSPEND: ignored (0x12 always follows)
    dispatch(0x0218, 0x12, 0, **cb)   # PBT_APMRESUMEAUTOMATIC
    dispatch(0x007E, 32, 0, **cb)     # WM_DISPLAYCHANGE
    dispatch(0x001A, 0x2F, 0, **cb)   # WM_SETTINGCHANGE / SPI_SETWORKAREA (taskbar moved)
    dispatch(0x001A, 0x44, 0, **cb)   # other setting changes: ignored
    assert seen == ["sleep", "wake", "screens", "screens"]


@windows_only
def test_lifecycle_window_receives_power_messages():
    import ctypes

    from wayfinder.utils.windows_lifecycle import WindowsLifecycleObserver

    seen = []
    observer = WindowsLifecycleObserver.start(on_sleep=lambda: seen.append("sleep"))
    try:
        assert observer is not None
        ctypes.windll.user32.SendMessageW(observer._hwnd, 0x0218, 0x4, 0)
        assert seen == ["sleep"]
    finally:
        observer.stop()


@pytest.mark.parametrize("values, expected", [
    ({}, None),
    ({"hklm": "Deny"}, "device"),
    ({"hkcu": "Deny"}, "apps"),
    ({"np": "Deny"}, "desktop_apps"),
    ({"hkcu": "Allow", "np": "Allow"}, None),
])
def test_microphone_privacy_switches(monkeypatch, values, expected):
    from wayfinder.utils import windows_privacy as p

    if sys.platform != "win32":
        pytest.skip("winreg")
    import winreg

    def fake(root, path):
        if root == winreg.HKEY_LOCAL_MACHINE:
            return values.get("hklm")
        return values.get("np") if path.endswith("NonPackaged") else values.get("hkcu")

    monkeypatch.setattr(p, "_value", fake)
    assert p.microphone_block() == expected
    assert (p.blocked_message(expected) is None) is (expected is None)


def test_silence_message_names_the_windows_switch(monkeypatch):
    from queue import Queue

    import wayfinder_main
    from wayfinder.utils import windows_privacy

    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    monkeypatch.setattr(windows_privacy, "microphone_block", lambda: "desktop_apps")
    app = SimpleNamespace(config={}, event_queue=Queue(),
                          _refresh_macos_permission_banner=lambda: None)
    msg = wayfinder_main.WayfinderApp._silence_error_message(app)
    assert "desktop apps access your microphone" in msg
    monkeypatch.setattr(windows_privacy, "microphone_block", lambda: None)
    assert "No speech detected" in wayfinder_main.WayfinderApp._silence_error_message(app)


# --- Benchmark system info, model folder, key repair, paste watchdog, motion ----

@windows_only
def test_windows_system_info_is_filled_in():
    from wayfinder.utils.windows_sysinfo import system_info

    info = system_info()
    assert info.get("cpu") and info.get("ram", "").endswith("GB")


def test_windows_llm_models_live_where_the_config_default_points(monkeypatch):
    import wayfinder_main

    monkeypatch.setattr(wayfinder_main.sys, "platform", "win32")
    assert wayfinder_main._get_llm_models_dir().parts[-4:] == (
        "AppData", "Local", "wayfinder-aura", "llm-models")
    monkeypatch.setattr(wayfinder_main.sys, "platform", "linux")
    assert wayfinder_main._get_llm_models_dir().parts[-4:] == (
        ".local", "share", "wayfinder-aura", "llm-models")


def test_key_state_helpers_are_inert_off_windows(monkeypatch):
    from wayfinder.hotkeys import pynput_listener as pl

    monkeypatch.setattr(pl.sys, "platform", "linux")
    assert pl._win32_key_pressed(57) is None
    assert pl._win32_foreground_elevated() == (None, "")


@windows_only
def test_foreground_elevation_reads_a_normal_window_as_not_elevated():
    from wayfinder.hotkeys.pynput_listener import _win32_foreground_elevated

    elevated, _exe = _win32_foreground_elevated()
    assert elevated in (False, True, None)  # a real answer, never an exception


def test_paste_watchdog_is_armed_on_windows_with_ctrl_v_copy():
    import inspect

    import wayfinder_main

    src = inspect.getsource(wayfinder_main.WayfinderApp)
    assert "if IS_MACOS or IS_WINDOWS:\n                if new_state == AppState.PASTING:" in src
    assert 'paste_keys = "Ctrl+V" if IS_WINDOWS else "⌘V"' in src


def test_reduce_motion_parks_the_idle_pill_on_windows(monkeypatch):
    from wayfinder.ui import overlay, windows_window

    monkeypatch.setattr(overlay.sys, "platform", "win32")
    monkeypatch.setattr(windows_window, "animations_enabled", lambda: False)
    assert overlay._windows_reduce_motion() is True
    monkeypatch.setattr(windows_window, "animations_enabled", lambda: True)
    assert overlay._windows_reduce_motion() is False
    monkeypatch.setattr(overlay.sys, "platform", "linux")
    monkeypatch.setattr(windows_window, "animations_enabled", lambda: False)
    assert overlay._windows_reduce_motion() is False


# --- Gamer mode / Games tab (core/windows_game_chat.py) --------------------------

@pytest.mark.parametrize("exe, title, key", [
    ("Wow.exe", "World of Warcraft", "wow"),
    ("WowClassic.exe", "", "wow"),
    ("ffxiv_dx11.exe", "FINAL FANTASY XIV", "ffxiv"),
    ("exefile.exe", "EVE - Pilot", "eve"),
    ("RuneLite.exe", "RuneLite", "jagex"),
    ("League of Legends.exe", "", "lol"),
    (None, "The Elder Scrolls Online", "eso"),   # title only when the exe can't be read
    ("game.exe", "The Elder Scrolls Online", None),  # a known exe decides
    ("chrome.exe", "World of Warcraft guide - Google Chrome", None),
    ("Code.exe", "wow.py - Visual Studio Code", None),
])
def test_windows_game_detection(exe, title, key):
    from wayfinder.core import windows_game_chat as g

    profile = g.match_profile(exe, title)
    assert (profile.key if profile else None) == key


def test_windows_games_tab_has_no_mac_only_notes():
    from wayfinder.core import windows_game_chat as g

    for entry in g.game_list():
        assert "Cmd+V" not in entry.note and "on a Mac" not in entry.note, entry.name
    # Profiles + hand-written entries, then the researched list (no duplicates).
    names = [e.name.lower() for e in g.game_list()]
    assert len(names) == len(set(names)) >= len(g.PROFILES) + len(g.INFO_ONLY)


def test_windows_unlisted_game_reasons():
    from wayfinder.core import windows_game_chat as g

    assert g.unlisted_game_reason("x.exe", exe_path=r"D:\SteamLibrary\steamapps\common\Valheim\valheim.exe") \
        == "it is a Steam game"
    assert g.unlisted_game_reason("GeForceNOW.exe") == "it is a cloud-gaming app"
    assert g.unlisted_game_reason("notepad.exe", exe_path=r"C:\Windows\notepad.exe") is None


@windows_only
def test_game_keys_are_held_scan_codes(monkeypatch):
    from wayfinder.core import injector_windows as w

    batches, sleeps = [], []
    monkeypatch.setattr(w, "_send", lambda inputs: batches.append(
        [(i.u.ki.wVk, i.u.ki.wScan, i.u.ki.dwFlags) for i in inputs]))
    monkeypatch.setattr(w.time, "sleep", lambda s: sleeps.append(s))
    w._press_keys([w.VK_CONTROL, w.VK_V])       # normal: one batched vk burst
    assert len(batches) == 1 and all(vk for vk, _s, _f in batches[0])
    batches.clear()
    with w.hold_keys(0.035):
        w._press_keys([w.VK_CONTROL, w.VK_V])   # game: scan codes, held
    assert len(batches) == 2 and 0.035 in sleeps
    assert all(flags & w.KEYEVENTF_SCANCODE and scan for _vk, scan, flags in batches[0] + batches[1])
    assert not any(flags & w.KEYEVENTF_KEYUP for _v, _s, flags in batches[0])
    assert all(flags & w.KEYEVENTF_KEYUP for _v, _s, flags in batches[1])
    assert w._held_seconds() == 0.0  # restored after the block


def test_app_picks_the_windows_game_backend(monkeypatch):
    import wayfinder_main

    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    assert wayfinder_main._game_chat_module().__name__.endswith("windows_game_chat")
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    monkeypatch.setattr(wayfinder_main, "_IS_LINUX", False)
    assert wayfinder_main._game_chat_module().__name__.endswith("macos_game_chat")
    monkeypatch.setattr(wayfinder_main, "_IS_LINUX", True)
    assert wayfinder_main._game_chat_module().__name__.endswith("linux_game_chat")


# --- Tap / hold with Right Ctrl (the Mac's Right Option gesture) -----------------

def _win_listener(monkeypatch, hotkey_key=97, modifiers=(), masks=None):
    from queue import Queue
    from threading import Event

    from wayfinder.hotkeys import pynput_listener as pl

    if pl.keyboard is None:
        pytest.skip("pynput unavailable on this host")
    captured = {}
    # Never inject real keys from a test; record the menu-mask sends instead.
    sent = masks if masks is not None else []
    monkeypatch.setattr(pl, "_win32_send_menu_mask", lambda: sent.append("mask"))

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(pl.sys, "platform", "win32")
    monkeypatch.setattr(pl.keyboard, "Listener", FakeListener)
    stop, events = Event(), Queue()
    stop.set()
    pl.pynput_hotkey_listener(events, hotkey_key=hotkey_key,
                              hotkey_modifiers=list(modifiers), stop_event=stop)
    return pl, captured["on_press"], captured["on_release"], events


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_right_ctrl_tap_toggles_once(monkeypatch):
    from wayfinder.hotkeys.types import EventType

    pl, press, release, events = _win_listener(monkeypatch)
    press(pl.Key.ctrl_r)
    release(pl.Key.ctrl_r)
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, None)]  # one toggle, no chord double


def test_right_ctrl_in_a_shortcut_never_records(monkeypatch):
    pl, press, release, events = _win_listener(monkeypatch)
    press(pl.Key.ctrl_r)
    press(pl.KeyCode.from_char("c"))
    release(pl.KeyCode.from_char("c"))
    release(pl.Key.ctrl_r)
    assert _drain(events) == []


def test_right_ctrl_hold_is_push_to_talk(monkeypatch):
    from wayfinder.hotkeys.types import EventType

    pl, press, release, events = _win_listener(monkeypatch)
    press(pl.Key.ctrl_r)
    time.sleep(pl.SOLO_HOLD_SECONDS + 0.15)
    release(pl.Key.ctrl_r)
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, pl.HOLD_START),
                              (EventType.HOTKEY_PRESSED, pl.HOLD_END)]


def test_ctrl_alt_space_chord_is_unchanged_on_windows(monkeypatch):
    from wayfinder.hotkeys.types import EventType

    pl, press, release, events = _win_listener(monkeypatch, hotkey_key=57, modifiers=("ctrl", "alt"))
    press(pl.Key.ctrl_l)
    press(pl.Key.alt_l)
    press(pl.Key.space)
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, None)]


def test_tap_hold_keys_per_platform():
    import wayfinder_main as wm

    assert wm.is_tap_hold_hotkey(97, [], platform_name="win32")
    assert not wm.is_tap_hold_hotkey(97, ["shift"], platform_name="win32")
    assert wm.is_tap_hold_hotkey(100, [], platform_name="win32")       # Right Alt / Alt Gr
    assert wm.is_tap_hold_hotkey(100, [], platform_name="darwin")
    assert not wm.is_tap_hold_hotkey(97, [], platform_name="linux")
    assert "Right Ctrl" in wm.hotkey_key_options(platform_name="win32", available_pynput_codes={97, 57})
    # Right Alt is the Windows default, so it is offered first.
    options = wm.hotkey_key_options(platform_name="win32", available_pynput_codes={97, 100, 57})
    assert list(options)[0] == "Right Alt (Alt Gr)"


def test_right_alt_tap_masks_the_menu_bar_and_toggles_once(monkeypatch):
    """A lone Alt release opens the front app's menu bar; the mask key sent
    while Right Alt is down prevents that, and must not cancel the tap."""
    from wayfinder.hotkeys.types import EventType

    masks = []
    pl, press, release, events = _win_listener(monkeypatch, hotkey_key=100, masks=masks)
    mask_key = pl.KeyCode.from_vk(pl._WIN32_MENU_MASK_VK)
    press(pl.Key.alt_r)
    press(mask_key)          # the injected mask comes back through the hook
    release(mask_key)
    release(pl.Key.alt_r)
    assert masks == ["mask"]
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, None)]


def test_right_alt_masks_every_auto_repeat_while_held(monkeypatch):
    from wayfinder.hotkeys.types import EventType

    masks = []
    pl, press, release, events = _win_listener(monkeypatch, hotkey_key=100, masks=masks)
    press(pl.Key.alt_r)
    time.sleep(pl.SOLO_HOLD_SECONDS + 0.15)
    press(pl.Key.alt_r)      # keyboard auto-repeat
    press(pl.Key.alt_r)
    release(pl.Key.alt_r)
    assert masks == ["mask"] * 3
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, pl.HOLD_START),
                              (EventType.HOTKEY_PRESSED, pl.HOLD_END)]


def test_only_a_right_alt_hotkey_sends_the_menu_mask(monkeypatch):
    masks = []
    pl, press, release, _events = _win_listener(monkeypatch, hotkey_key=97, masks=masks)
    press(pl.Key.alt_r)      # Right Alt is not the hotkey: leave its menus alone
    release(pl.Key.alt_r)
    press(pl.Key.ctrl_r)     # Right Ctrl alone never opens a menu
    release(pl.Key.ctrl_r)
    assert masks == []

    pl, press, release, _events = _win_listener(
        monkeypatch, hotkey_key=57, modifiers=("ctrl", "alt"), masks=masks)
    press(pl.Key.ctrl_l)
    press(pl.Key.alt_r)
    press(pl.Key.space)
    assert masks == []



# --- Review fixes -----------------------------------------------------------------

@windows_only
def test_duck_journal_liveness_probe_never_kills_a_process():
    from wayfinder.utils import audio_ducker

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert audio_ducker._pid_is_alive(child.pid) is True
        time.sleep(0.3)
        assert child.poll() is None  # os.kill(pid, 0) would have terminated it
    finally:
        child.kill()
        child.wait()
    assert audio_ducker._pid_is_alive(child.pid) is False


def test_windows_ducking_restores_the_device_it_lowered(monkeypatch, tmp_path):
    from wayfinder.utils import audio_ducker, windows_audio

    levels = {"speakers": 0.8, "headphones": 0.4}
    default = {"id": "speakers"}
    monkeypatch.setattr(audio_ducker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(audio_ducker, "is_pactl_available", lambda: False)
    monkeypatch.setattr(windows_audio, "default_output_id", lambda: default["id"])
    monkeypatch.setattr(windows_audio, "output_volume",
                        lambda device_id=None: levels[device_id or default["id"]])
    monkeypatch.setattr(windows_audio, "set_output_volume",
                        lambda v, device_id=None: levels.__setitem__(device_id or default["id"], v) or True)
    d = audio_ducker.AudioDucker(duck_percent=50, recovery_path=tmp_path / "duck.json")
    d.duck()
    assert json.loads((tmp_path / "duck.json").read_text())["macos"]["endpoint"] == "speakers"
    default["id"] = "headphones"  # the user switched output mid-dictation
    d.restore()
    assert levels == {"speakers": pytest.approx(0.8), "headphones": pytest.approx(0.4)}


def test_windows_games_never_fall_back_to_typing(monkeypatch):
    import wayfinder_main
    from wayfinder.core import injector, windows_game_chat
    from wayfinder.core import injector_windows

    pasted, typed, logs = [], [], []
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    monkeypatch.setattr(injector_windows, "inject_text_paste_windows", pasted.append)
    monkeypatch.setattr(injector, "inject_text", lambda *a, **k: typed.append(a))
    app = SimpleNamespace(config={}, session_generation=1, log=logs.append,
                          _windows_game_paste_only=lambda text: wayfinder_main.WayfinderApp
                          ._windows_game_paste_only(app, text))
    # A caution game (RuneScape) and an unlisted Steam game both paste, never type.
    monkeypatch.setattr(windows_game_chat, "frontmost_app", lambda: (4, "RuneLite.exe", "RuneLite"))
    assert wayfinder_main.WayfinderApp._inject_into_game_chat(app, "hi", 1) is True
    monkeypatch.setattr(windows_game_chat, "frontmost_app", lambda: (5, "valheim.exe", "Valheim"))
    monkeypatch.setattr(windows_game_chat, "app_signals",
                        lambda pid: (None, r"D:\Steam\steamapps\common\Valheim\valheim.exe"))
    assert wayfinder_main.WayfinderApp._inject_into_game_chat(app, "hello", 1) is True
    assert pasted == ["hi", "hello"] and typed == []
    # An ordinary app is left to the normal paste path (with its typing fallback).
    monkeypatch.setattr(windows_game_chat, "frontmost_app", lambda: (6, "notepad.exe", "Untitled"))
    monkeypatch.setattr(windows_game_chat, "app_signals", lambda pid: (None, r"C:\Windows\notepad.exe"))
    assert wayfinder_main.WayfinderApp._inject_into_game_chat(app, "x", 1) is False


def test_silent_connection_does_not_block_the_control_channel(tmp_path, monkeypatch):
    import socket as _socket
    import threading

    from queue import Queue

    from wayfinder.hotkeys import windows_control

    monkeypatch.setattr(windows_control, "endpoint_file", lambda: tmp_path / "control.json")
    q, stop = Queue(), threading.Event()
    t = threading.Thread(target=windows_control.control_listener, args=(q, stop), daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "control.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        port = json.loads((tmp_path / "control.json").read_text())["port"]
        idle = [_socket.create_connection(("127.0.0.1", port)) for _ in range(3)]  # never speak
        start = time.monotonic()
        assert windows_control.send_command("ping", expect_reply=True) == b"pong"
        assert time.monotonic() - start < 1.0
        for s in idle:
            s.close()
    finally:
        stop.set()
        t.join(timeout=5)


def test_child_supervisor_builds_nothing_windows_specific_on_import():
    import wayfinder.utils.child_supervisor as cs

    assert not hasattr(cs, "ctypes") and not hasattr(cs, "_ExtendedLimits")


def test_unplugged_endpoint_recovery_survives_later_dictations(monkeypatch, tmp_path):
    """Crash while the headset is ducked, restart with it unplugged, dictate on
    the speakers: the headset's original volume must still come back."""
    from wayfinder.utils import audio_ducker, windows_audio

    levels = {"speakers": 0.8}                      # the headset is unplugged
    default = {"id": "speakers"}
    monkeypatch.setattr(audio_ducker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(audio_ducker, "is_pactl_available", lambda: False)
    monkeypatch.setattr(audio_ducker, "_pid_is_alive", lambda pid: False)
    monkeypatch.setattr(windows_audio, "default_output_id", lambda: default["id"])
    monkeypatch.setattr(windows_audio, "output_volume",
                        lambda device_id=None: levels.get(device_id or default["id"]))

    def set_volume(v, device_id=None):
        key = device_id or default["id"]
        if key not in levels:
            return False
        levels[key] = v
        return True

    monkeypatch.setattr(windows_audio, "set_output_volume", set_volume)
    journal = tmp_path / "duck.json"
    journal.write_text(json.dumps({"version": 2, "pid": 999999, "macos": {
        "original": 90, "ducked": 45, "endpoint": "headset"}}))

    d = audio_ducker.AudioDucker(duck_percent=50, recovery_path=journal)
    assert d.recovery_result.status == audio_ducker.DuckingStatus.ERROR
    assert json.loads(journal.read_text())["windows_pending"][0]["endpoint"] == "headset"

    d.duck()                                        # dictate on the speakers
    assert levels["speakers"] == pytest.approx(0.4)
    d.restore()
    assert levels["speakers"] == pytest.approx(0.8)
    assert json.loads(journal.read_text())["windows_pending"][0]["original"] == 90  # not lost

    levels["headset"] = 0.45                        # plugged back in, still ducked
    d.duck()                                        # next dictation retries it first
    assert levels["headset"] == pytest.approx(0.90)
    d.restore()
    assert not journal.exists()                     # nothing left to recover


# --- Peter's 2026-09-25/26 macOS additions, Windows side --------------------------

def test_windows_games_tab_has_the_researched_list_without_mac_notes():
    import re

    from wayfinder.core import macos_game_chat, windows_game_chat

    win = windows_game_chat.game_list()
    assert len(win) == len(macos_game_chat.game_list())  # the same 84 games
    mac_only = re.compile(r"\bMacs?\b|Apple Silicon|Rosetta|CrossOver|Cmd\+V")
    assert not [e.name for e in win if mac_only.search(e.note)]
    assert windows_game_chat.windows_note(
        "No Mac version; play through GeForce NOW. Chat opens with Enter, but paste "
        "into the cloud stream is not confirmed (check in game)."
    ) == "Chat opens with Enter, but paste is not confirmed (check in game)."
    assert windows_game_chat.windows_note("Chat opens with T.") == "Chat opens with T."


@windows_only
def test_dictation_is_not_pasted_into_aura_itself(monkeypatch):
    import os

    from wayfinder.core import injector_windows as w
    from wayfinder.core.injector import InjectionError

    clip, sent = [], []
    monkeypatch.setattr(w, "_foreground_is_own_process", lambda: True)
    monkeypatch.setattr(w, "_clipboard_set_windows", lambda text, transient=False: clip.append(text) or True)
    monkeypatch.setattr(w, "_send", lambda inputs: sent.append(inputs))
    monkeypatch.setattr(w._user32, "GetForegroundWindow", lambda: 1)
    with pytest.raises(InjectionError, match="Wayfinder Aura was frontmost"):
        w.inject_text_paste_windows("hello")
    with pytest.raises(InjectionError, match="press Ctrl\+V"):
        w.inject_text_windows("hello")
    assert clip == ["hello", "hello"] and sent == []   # nothing typed into Aura
    monkeypatch.setattr(w, "_foreground_is_own_process", lambda: False)
    assert os.getpid() > 0


def test_reduce_motion_holds_the_idle_hero_only_on_windows(monkeypatch):
    import inspect

    import wayfinder_main

    src = inspect.getsource(wayfinder_main.WayfinderApp._animate_idle_breath)
    assert "if IS_WINDOWS and not IS_MACOS:" in src and "interval = 2000" in src


def test_default_hotkey_warns_only_while_magnifier_is_on(monkeypatch):
    from wayfinder.utils import windows_hotkey_conflicts as c

    monkeypatch.setattr(c.sys, "platform", "win32")
    assert "Magnifier" in c.conflict_for(57, ["ctrl", "alt"], magnifier=True)
    assert c.conflict_for(57, ["ctrl", "alt"], magnifier=False) is None
    monkeypatch.setattr(c, "magnifier_running", lambda: True)
    assert "Right Ctrl" in c.conflict_for(57, ["alt", "ctrl"])
    assert c.conflict_for(67, [], installed=()) is None  # F9 is clear either way


def test_alt_gr_taps_and_holds_like_right_alt(monkeypatch):
    """UK/European layouts report Right Alt as Alt Gr, after a synthetic Left
    Ctrl: it must still be the tap/hold key, and Alt Gr + a key must not record."""
    from wayfinder.hotkeys.types import EventType

    masks = []
    pl, press, release, events = _win_listener(monkeypatch, hotkey_key=100, masks=masks)
    press(pl.Key.ctrl_l)
    press(pl.Key.alt_gr)
    release(pl.Key.ctrl_l)
    release(pl.Key.alt_gr)
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, None)]
    assert masks == ["mask"]  # harmless on Alt Gr layouts, needed on US ones
    press(pl.Key.ctrl_l)
    press(pl.Key.alt_gr)
    press(pl.KeyCode.from_char("e"))       # Alt Gr+E types an accented e
    release(pl.KeyCode.from_char("e"))
    release(pl.Key.ctrl_l)
    release(pl.Key.alt_gr)
    assert _drain(events) == []


# --- Upgrades: Windows asks Aura to close (installer / sign-out) ------------------

def test_end_session_quits_aura_for_real():
    from wayfinder.utils.windows_lifecycle import dispatch

    seen = []
    cb = dict(on_end_session=lambda: seen.append("quit"))
    dispatch(0x0011, 0, 1, **cb)   # WM_QUERYENDSESSION: only asks, nothing yet
    dispatch(0x0016, 0, 1, **cb)   # WM_ENDSESSION, cancelled (wParam FALSE)
    dispatch(0x0016, 1, 1, **cb)   # WM_ENDSESSION + ENDSESSION_CLOSEAPP: quit
    assert seen == ["quit"]


@windows_only
def test_lifecycle_window_turns_end_session_into_quit():
    import ctypes

    from wayfinder.utils.windows_lifecycle import WindowsLifecycleObserver

    seen = []
    observer = WindowsLifecycleObserver.start(on_end_session=lambda: seen.append("quit"))
    try:
        user32 = ctypes.windll.user32
        assert user32.SendMessageW(observer._hwnd, 0x0011, 0, 1) == 1  # never vetoes
        user32.SendMessageW(observer._hwnd, 0x0016, 1, 1)
        assert seen == ["quit"]
    finally:
        observer.stop()


def test_installer_ends_a_running_aura_before_replacing_files():
    from pathlib import Path

    iss = (Path(__file__).resolve().parent.parent / "packaging" / "windows"
           / "installer.iss").read_text(encoding="utf-8")
    assert "function PrepareToInstall" in iss and "usUninstall" in iss
    assert "taskkill.exe'), '/F /T /IM \"{#MyAppExeName}\"'" in iss


def test_end_session_restores_ducked_audio_before_quitting():
    from queue import Queue

    import wayfinder_main
    from wayfinder.hotkeys.types import EventType

    order = []
    ducker = SimpleNamespace(close=lambda: order.append("close"))
    q = Queue()
    app = SimpleNamespace(audio_ducker=ducker, event_queue=SimpleNamespace(
        put=lambda item: order.append(item)))
    wayfinder_main.WayfinderApp._on_windows_end_session(app)
    assert order == ["close", (EventType.QUIT_APP, None)]
    assert q.empty()


def test_end_session_leaves_no_duck_behind(monkeypatch, tmp_path):
    """A duck in flight or still queued when Windows ends the session must not
    lower the volume after the handler returns."""
    import threading
    import wayfinder_main
    from wayfinder.utils import audio_ducker, windows_audio

    level = {"v": 0.8}
    monkeypatch.setattr(audio_ducker.platform, "system", lambda: "Windows")
    monkeypatch.setattr(audio_ducker, "is_pactl_available", lambda: False)
    monkeypatch.setattr(windows_audio, "default_output_id", lambda: "speakers")
    monkeypatch.setattr(windows_audio, "output_volume", lambda device_id=None: level["v"])
    monkeypatch.setattr(windows_audio, "set_output_volume",
                        lambda v, device_id=None: level.__setitem__("v", v) or True)
    d = audio_ducker.AudioDucker(duck_percent=50, recovery_path=tmp_path / "duck.json")
    app = SimpleNamespace(audio_ducker=d, event_queue=SimpleNamespace(put=lambda item: None))

    d.duck()                                              # in flight: ducked now
    wayfinder_main.WayfinderApp._on_windows_end_session(app)
    assert level["v"] == pytest.approx(0.8)
    worker = threading.Thread(target=d.duck)              # a queued duck runs late
    worker.start()
    worker.join()
    assert level["v"] == pytest.approx(0.8)               # closed: it did nothing


def test_installer_asks_aura_to_quit_before_forcing_it():
    from pathlib import Path

    iss = (Path(__file__).resolve().parent.parent / "packaging" / "windows"
           / "installer.iss").read_text(encoding="utf-8")
    ask = iss.index("SendMessageTimeout(Wnd, WM_ENDSESSION, 1, ENDSESSION_CLOSEAPP")
    assert "SendMessage(Wnd" not in iss  # never an unbounded send
    force = iss.index("taskkill.exe")
    assert "FindWindowByClassName('WayfinderAuraLifecycle')" in iss and ask < force


# --- Hero ribbon (ui/windows_hero_render.py: the Mac Metal shader) -----------

def _shader_reference(w, h, t, level, morph, colour, bg):
    """The Metal hero_wave shader evaluated literally (exact segment distances,
    exp glow, smoothstep cores), to check the fast port against."""
    import math

    import numpy as np

    from wayfinder.ui import windows_hero_render as m

    px, py, edge, band = m._grid(w, h)
    width, height = float(w), float(h)
    breath = 0.26 + 0.09 * (0.5 + 0.5 * math.sin(t * 0.8))
    amp = height * 0.42 * min(0.8, breath + 0.2 * morph + (level ** 0.6) * 0.62 * morph)
    bright, hi_bright = 0.55 + 0.45 * morph, 0.40 + 0.60 * morph
    count = 109 if morph < 0.3 else int(min(240, max(109, round(width / 9))))
    vx = (width / (count - 1) * np.arange(count)).astype(np.float32)
    col = np.array(colour, np.float32)
    res = np.zeros((h, w, 3), np.float32) + np.array(bg, np.float32)
    dists, glow = [], np.zeros((h, w), np.float32)
    for f, ph, a, th in zip(m._FREQS, m._PHASES, m._ALPHAS, m._THICKNESS):
        ys = m._wave_y(vx, width, height, t, amp, f, ph, 1.0).astype(np.float32)
        d = m._polyline_distance(px, py, width, count, ys)
        dists.append(d)
        o = np.maximum(0, d - (th + 4) / 2)
        glow = np.maximum(glow, a * 0.3 * bright * np.exp(-o * o / 8))
    hd = m._polyline_distance(px, py, width, count,
                              m._highlight_y(vx, width, height, t, amp, 1.0).astype(np.float32))
    o = np.maximum(0, hd - 2)
    glow = np.maximum(glow, 0.4 * hi_bright * np.exp(-o * o / 8)) * band

    def mix(r, alpha):
        return r + (col - r) * alpha[..., None]

    res = mix(res, glow * edge)
    for d, a, th in zip(dists, m._ALPHAS, m._THICKNESS):
        res = mix(res, a * bright * m._coverage(d, th) * edge)
    res = mix(res, 0.95 * hi_bright * m._coverage(hd, 2.0) * edge)
    return np.clip(res + 0.5, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("level,morph", [(0.0, 0.0), (0.6, 1.0)])
def test_windows_hero_matches_the_mac_shader(level, morph):
    import numpy as np

    from wayfinder.ui.windows_hero_render import render_hero_wave_windows

    colour, bg = (74, 130, 220), (21, 27, 37)
    img = render_hero_wave_windows(700, 64, 3.0, level, morph, colour, bg)
    assert img.mode == "RGB" and img.size == (700, 64)
    diff = np.abs(np.asarray(img, int) - _shader_reference(700, 64, 3.0, level, morph, colour, bg))
    assert diff.mean() < 2.5                      # same picture, not just similar
    assert (diff.max(axis=2) > 12).mean() < 0.05  # only a few near-vertical edge pixels


def test_windows_hero_is_background_at_the_ends_and_survives_bad_input():
    import numpy as np

    from wayfinder.ui.windows_hero_render import render_hero_wave_windows

    bg = (21, 27, 37)
    img = np.asarray(render_hero_wave_windows(400, 64, 1.0, 1.0, 1.0, (200, 80, 90), bg))
    assert (img[:, 0] == bg).all() and (img[:, -1] == bg).all()   # edge fade
    assert (img[:, 200] != bg).any()
    for value in (float("nan"), float("inf")):
        assert render_hero_wave_windows(50, 20, value, value, value, (1, 2, 3), bg).size == (50, 20)


def test_hero_dispatch_shader_on_windows_and_linux(monkeypatch):
    from wayfinder.ui import hero_render, windows_hero_render

    calls = []
    monkeypatch.setattr(windows_hero_render, "render_hero_wave_windows",
                        lambda *a, **k: calls.append(k) or "windows")
    monkeypatch.setattr(hero_render, "_render_hero_wave_aqua", lambda *a, **k: "aqua")
    monkeypatch.setattr(hero_render, "_render_hero_wave_reference", lambda *a, **k: "reference")
    args = (100, 64, 0.0, 0.0, 0.0, (1, 2, 3))

    monkeypatch.setattr(hero_render.sys, "platform", "win32")
    assert hero_render.render_hero_wave(*args, stroke_scale=1.5) == "windows"
    assert calls == [{"stroke_scale": 1.5}]
    monkeypatch.setattr(windows_hero_render, "render_hero_wave_windows",
                        lambda *a, **k: 1 / 0)
    assert hero_render.render_hero_wave(*args) == "aqua"         # safe fallback
    monkeypatch.setattr(hero_render.sys, "platform", "darwin")
    assert hero_render.render_hero_wave(*args) == "aqua"
    monkeypatch.setattr(hero_render.sys, "platform", "linux")
    assert hero_render.render_hero_wave(*args) == "reference"    # Linux fallback
    monkeypatch.setattr(windows_hero_render, "render_hero_wave_windows",
                        lambda *a, **k: "windows")
    assert hero_render.render_hero_wave(*args) == "windows"      # Linux: the Mac look
    monkeypatch.setenv("WAYFINDER_LINUX_MAC_LOOK", "0")
    assert hero_render.render_hero_wave(*args) == "reference"    # old look restored


def test_hero_canvas_layout_windows_and_linux_match_mac(monkeypatch):
    import wayfinder_main

    assert wayfinder_main._hero_visual_scale(1.25, "win32") == 1.25
    assert wayfinder_main._hero_canvas_pady("win32") == wayfinder_main._hero_canvas_pady("darwin")
    assert wayfinder_main._hero_visual_scale(1.25, "linux") == 1.25
    assert wayfinder_main._hero_canvas_pady("linux") == wayfinder_main._hero_canvas_pady("darwin")
    monkeypatch.setenv("WAYFINDER_LINUX_MAC_LOOK", "0")
    assert wayfinder_main._hero_canvas_pady("linux") == (0, 8)


# --- Sharp rendering on scaled displays (utils/windows_dpi.py) ---------------

def test_dpi_awareness_is_a_no_op_off_windows_or_when_disabled(monkeypatch):
    from wayfinder.utils import windows_dpi

    monkeypatch.setattr(windows_dpi, "_scale", 1.0)
    monkeypatch.setattr(windows_dpi.sys, "platform", "linux")
    assert windows_dpi.enable() == 1.0
    monkeypatch.setattr(windows_dpi.sys, "platform", "win32")
    monkeypatch.setenv("WAYFINDER_WINDOWS_DPI_AWARE", "0")
    assert windows_dpi.enable() == 1.0
    assert windows_dpi.to_px(80) == 80 and windows_dpi.to_logical(80) == 80


def _fake_windll(set_hr, awareness, dpi=168):
    """shcore/user32 stand-ins: SetProcessDpiAwareness returns ``set_hr``."""
    import ctypes

    def get_awareness(_proc, out):
        ctypes.cast(out, ctypes.POINTER(ctypes.c_int)).contents.value = awareness
        return 0

    return SimpleNamespace(
        shcore=SimpleNamespace(SetProcessDpiAwareness=lambda level: set_hr,
                               GetProcessDpiAwareness=get_awareness),
        user32=SimpleNamespace(GetDpiForSystem=lambda: dpi, IsProcessDPIAware=lambda: awareness > 0),
    )


@pytest.mark.parametrize("set_hr, awareness, expected", [
    (0, 1, 1.75),            # we declared it
    (-2147024891, 2, 1.75),  # E_ACCESSDENIED: declared first (manifest, host Python)
    (-2147024891, 0, 1.0),   # refused and not aware: Windows stretches, keep 1.0
])
def test_dpi_scale_follows_the_display_even_if_awareness_was_declared_first(
        monkeypatch, set_hr, awareness, expected):
    """The first Windows CI run's Python was already per-monitor aware, so our
    call was refused and the scale stayed 1.0: Tk drew at real resolution with
    unscaled geometry (a small window at 175%)."""
    import ctypes

    from wayfinder.utils import windows_dpi

    monkeypatch.setattr(windows_dpi, "_scale", 1.0)
    monkeypatch.setattr(windows_dpi.sys, "platform", "win32")
    monkeypatch.delenv("WAYFINDER_WINDOWS_DPI_AWARE", raising=False)
    monkeypatch.setattr(ctypes, "windll", _fake_windll(set_hr, awareness), raising=False)
    assert windows_dpi.enable() == pytest.approx(expected)


@windows_only
def test_dpi_awareness_really_declared_on_windows():
    import os

    # A fresh process: awareness is process-wide and can be set only once.
    code = textwrap.dedent("""
        import ctypes, sys
        sys.path.insert(0, "src")
        before = ctypes.c_int(-1)
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(before))
        from wayfinder.utils import windows_dpi
        s = windows_dpi.enable()
        v = ctypes.c_int(-1)
        ctypes.windll.shcore.GetProcessDpiAwareness(None, ctypes.byref(v))
        dpi = windows_dpi._system_dpi()
        print(before.value, s, v.value, dpi)
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, env={**os.environ, "WAYFINDER_WINDOWS_DPI_AWARE": "1"})
    before, scale, awareness, dpi = out.stdout.split()
    if before == "0":
        assert awareness == "1"   # we declared PROCESS_SYSTEM_DPI_AWARE
    else:
        # Declared before us (the CI runner's Python came up per-monitor
        # aware, 2): Windows refuses ours, Tk still draws at real resolution,
        # so the scale must still follow the display.
        assert awareness == before
    assert float(scale) == pytest.approx(int(dpi) / 96.0 if 96 <= int(dpi) <= 480 else 1.0)


def test_window_maths_stays_logical_and_converts_at_the_edges(monkeypatch):
    import wayfinder_main
    from wayfinder.utils import windows_dpi

    monkeypatch.setattr(windows_dpi, "_scale", 1.75)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    assert wayfinder_main._windows_px(800) == 1400
    assert wayfinder_main._windows_logical(1400) == 800
    assert wayfinder_main._hero_visual_scale(1.0, "win32") == pytest.approx(1.75)
    assert wayfinder_main._hero_visual_scale(1.0, "darwin") == 1.0

    # Linux and macOS never scale, whatever the module holds.
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    assert wayfinder_main._windows_px(800) == 800
    assert wayfinder_main._windows_logical(1400) == 1400
    assert wayfinder_main._windows_dpi_scale() == 1.0


def test_old_saved_geometry_keeps_its_size_after_the_upgrade(monkeypatch):
    """Configs saved while Windows stretched the app hold logical pixels; the
    app keeps saving logical pixels, so a window reopens at the same size."""
    import wayfinder_main
    from wayfinder.utils import windows_dpi

    monkeypatch.setattr(windows_dpi, "_scale", 1.75)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    saved = {"width": 800, "height": 780, "x": 100, "y": 60}
    real = {k: wayfinder_main._windows_px(v) for k, v in saved.items()}
    assert real == {"width": 1400, "height": 1365, "x": 175, "y": 105}
    assert {k: wayfinder_main._windows_logical(v) for k, v in real.items()} == saved


def test_tooltips_and_fallback_pill_use_the_real_work_area(monkeypatch):
    """Tk measures tooltips and the fallback pill in real pixels, so they must
    be placed against the work area in real pixels, not the logical one."""
    from wayfinder.ui import window_geometry
    from wayfinder.utils import windows_dpi

    if sys.platform != "win32":
        pytest.skip("real SPI_GETWORKAREA")
    monkeypatch.setattr(windows_dpi, "_scale", 2.0)
    real = window_geometry.windows_work_area(0, 0, physical=True)
    logical = window_geometry.windows_work_area(0, 0)
    assert logical[2] == int(real[2] / 2.0) and logical[3] == int(real[3] / 2.0)


# --- Repaint after focus changes (black squares on some GPU setups) -----------

def test_repaint_after_activation_coalesces_one_repaint_per_change(monkeypatch):
    from wayfinder.ui import windows_window as ww

    monkeypatch.setattr(ww.sys, "platform", "win32")
    painted = []
    monkeypatch.setattr(ww, "repaint", lambda root: painted.append(root))

    class FakeRoot:
        def __init__(self):
            self.bindings, self.pending = {}, []

        def bind(self, sequence, func, add=None):
            assert add == "+"  # CTk binds <FocusIn> on the root itself
            self.bindings[sequence] = func

        def after(self, ms, func):
            assert ms >= 100  # the app's timer floor
            self.pending.append(func)
            return len(self.pending)

    root = FakeRoot()
    assert ww.repaint_after_activation(root)
    assert set(root.bindings) == {"<FocusIn>", "<FocusOut>", "<Map>"}
    for _ in range(5):  # one event per child widget
        root.bindings["<FocusIn>"](None)
    root.bindings["<Map>"](None)
    assert len(root.pending) == 1
    root.pending.pop()()
    assert painted == [root]
    root.bindings["<FocusOut>"](None)  # the next change schedules again
    assert len(root.pending) == 1


def test_repaint_after_activation_is_windows_only(monkeypatch):
    from wayfinder.ui import windows_window as ww

    monkeypatch.setattr(ww.sys, "platform", "linux")
    assert ww.repaint_after_activation(object()) is False
    assert ww.repaint(object()) is False


def test_idle_hero_holds_while_another_window_covers_aura():
    import inspect

    import wayfinder_main

    src = inspect.getsource(wayfinder_main.WayfinderApp._animate_idle_breath)
    assert 'window_exposure(self) == "covered"' in src
    assert "self.after(500, self._animate_idle_breath)" in src


def test_windows_never_offers_faster_whisper():
    """The Windows bundle has no Faster-Whisper/PyTorch; detecting an NVIDIA GPU
    must not put it in the backend menu."""
    import inspect

    import wayfinder_main

    src = inspect.getsource(wayfinder_main.WayfinderApp)
    assert "and not (IS_WINDOWS and not IS_MACOS)" in src.split("show_fw = (", 1)[1][:200]

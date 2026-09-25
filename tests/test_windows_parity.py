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

@pytest.mark.parametrize("platform_name, queued", [("win32", True), ("linux", False)])
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
    assert c.conflict_for(57, ["ctrl", "alt"], installed=()) is None  # the default
    assert c.conflict_for(67, [], installed=()) is None               # bare F9
    assert "1Password" in c.conflict_for(57, ["ctrl", "shift"], installed={"1Password"})
    assert c.conflict_for(57, ["ctrl", "shift"], installed=()) is None
    monkeypatch.setattr(c.sys, "platform", "linux")
    assert c.conflict_for(57, ["alt"]) is None


# --- Audio: ducking the main volume, "Auto" mic follows the Windows default ----

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
    assert len(g.game_list()) == len(g.PROFILES) + len(g.INFO_ONLY)


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
    assert wayfinder_main._game_chat_module().__name__.endswith("macos_game_chat")


# --- Tap / hold with Right Ctrl (the Mac's Right Option gesture) -----------------

def _win_listener(monkeypatch, hotkey_key=97, modifiers=()):
    from queue import Queue
    from threading import Event

    from wayfinder.hotkeys import pynput_listener as pl

    if pl.keyboard is None:
        pytest.skip("pynput unavailable on this host")
    captured = {}

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
    assert not wm.is_tap_hold_hotkey(100, [], platform_name="win32")   # Right Alt = AltGr
    assert wm.is_tap_hold_hotkey(100, [], platform_name="darwin")
    assert not wm.is_tap_hold_hotkey(97, [], platform_name="linux")
    assert "Right Ctrl" in wm.hotkey_key_options(platform_name="win32", available_pynput_codes={97, 57})



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

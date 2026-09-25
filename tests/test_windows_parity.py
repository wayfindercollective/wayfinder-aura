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

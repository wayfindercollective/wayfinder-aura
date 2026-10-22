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
    monkeypatch.setattr(w, "_open_clipboard", lambda retries=5, owner=None: True)
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


def test_paste_injection_marks_dictation_and_restore_transient(monkeypatch):
    # Over the fake user32 on every platform: a Windows-only stub of
    # _clipboard_set_windows lacked its restoring keyword (Sol mac r13), and
    # the deferred restore swallowed the TypeError.
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    w.inject_text_paste_windows("dictated words")
    # The dictation's own write no longer goes through _write_open_clipboard
    # (Finding 1/2 / Sol mac r16: it is prepared and set directly, alongside
    # the mark and a rollback copy, under the snapshot's own open) but it is
    # still marked transient.
    assert rig.fake.writes == ["dictated words"]
    assert len(rig.fake.formats) == 4   # 3 history-exclusion hints + the owner mark
    pending[0]()                         # the deferred restore, run now
    # The restore no longer goes through _write_open_clipboard either
    # (Finding 3 Sol mac r17): it prepares its own previous-text AND
    # dictation-fallback blocks directly, so it can put the dictation back
    # if the previous-text write fails, something _write_open_clipboard's
    # single-block contract can't express. It still marks its write
    # transient — no owner mark this time, only the 3 history-exclusion hints.
    assert rig.fake.writes == ["dictated words", "theirs"]
    assert len(rig.fake.formats) == 3
    assert rig.clipboard == "theirs"


def test_a_failed_paste_leaves_the_dictation_on_the_clipboard(monkeypatch):
    """The error tells the user to press Ctrl+V themselves; restoring the old
    clipboard after a failed paste made that paste the wrong text."""
    from wayfinder.core.injector import InjectionError

    rig, w = _windows_send_rig(monkeypatch)

    def refused(_keys):
        raise InjectionError("SendInput injected 0/4 events")

    monkeypatch.setattr(w, "_press_keys", refused)
    with pytest.raises(InjectionError):
        w.inject_text_paste_windows("dictated words")
    assert rig.clipboard == "dictated words"


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
    # Every frozen build (Mac, PC and the Linux AppImage) waits for a model.
    assert "            if frozen:\n" in src
    assert "frozen_runtime_ready = app._has_usable_whisper_model()" in src


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
    # The hold threshold fires from a timer thread, which a busy CI host can
    # run well after SOLO_HOLD_SECONDS (v1.2.0-beta.3's macOS smoke released
    # first and saw nothing): release only once the hold has begun.
    started = events.get(timeout=5)
    release(pl.Key.ctrl_r)
    assert [started] + _drain(events) == [(EventType.HOTKEY_PRESSED, pl.HOLD_START),
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
    # The hold fires on a timer thread. Wait for its event before testing
    # repeats; a fixed sleep can release Alt first on a busy CI host.
    assert events.get(timeout=2) == (EventType.HOTKEY_PRESSED, pl.HOLD_START)
    press(pl.Key.alt_r)      # keyboard auto-repeat
    press(pl.Key.alt_r)
    release(pl.Key.alt_r)
    assert masks == ["mask"] * 3
    assert _drain(events) == [(EventType.HOTKEY_PRESSED, pl.HOLD_END)]


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


# --- Paste diagnostics (core/injector_windows.py, observation only) ---------

def _diag(monkeypatch, target="Code.exe/Chrome_WidgetWin_1"):
    from wayfinder.core import injector_windows as iw

    monkeypatch.setattr(iw, "_window_app", lambda hwnd: target)
    monkeypatch.setattr(iw, "_user32", SimpleNamespace(GetForegroundWindow=lambda: 1))
    return iw._PasteDiagnostics("hello world")


def test_paste_line_flags_the_target_reading_after_the_old_clipboard_is_back(monkeypatch):
    d = _diag(monkeypatch)
    d.previous("old text")
    d._marks.update(mods=0, mods_free=1, set=2, ctrl_v=40, restored=120)
    d._reads = [(30.0, "DefenderSessionHelper.exe/WDClip"), (300.0, "Code.exe/Chrome_MessageWindow")]
    line = d._line("Code.exe/Chrome_WidgetWin_1")
    assert line.startswith("🔎 Paste ⚠ 11 chars → Code.exe/")
    assert "old clipboard 8 chars" in line and "old clipboard back at 120ms" in line
    assert "DefenderSessionHelper.exe/WDClip at 30ms (background)" in line
    assert "Code.exe/Chrome_MessageWindow at 300ms LATE READ" in line
    assert "hello" not in line and "old text" not in line   # never the text itself


def test_paste_line_is_quiet_when_the_target_reads_in_time(monkeypatch):
    d = _diag(monkeypatch)
    d.previous(None)
    d._marks.update(ctrl_v=40)
    d._reads = [(60.0, "Code.exe/Chrome_MessageWindow")]
    line = d._line("Aura/TkTopLevel")
    assert "⚠" not in line and "LATE" not in line
    assert "old clipboard none" in line and "front after: Aura/TkTopLevel" in line


def test_paste_still_runs_the_same_steps_and_reports_failures(monkeypatch):
    from wayfinder.core import injector_windows as iw
    from wayfinder.core.injector import InjectionError

    fake = _FakeWindows(clipboard="old")
    lines = []
    monkeypatch.setattr(iw, "_user32", fake)
    monkeypatch.setattr(iw, "_kernel32", fake)
    monkeypatch.setattr(iw, "_paste_reporter", lines.append)
    monkeypatch.setattr(iw, "_window_app", lambda hwnd: "app.exe/Cls")
    monkeypatch.setattr(iw, "_require_foreground_window", lambda: None)
    monkeypatch.setattr(iw, "_refuse_own_window", lambda text: None)
    monkeypatch.setattr(iw, "require_modifier_release_windows", lambda: None)
    monkeypatch.setattr(iw, "_WATCH_SECONDS", 0.05)
    monkeypatch.setattr(iw.time, "sleep", lambda s: None)
    pending = []
    monkeypatch.setattr(iw._pending_restore, "schedule", pending.append)

    iw.inject_text_paste_windows("hi")
    pending.pop()()
    assert fake.writes == ["hi", "old"]
    deadline = time.time() + 2
    while not lines and time.time() < deadline:
        time.sleep(0.01)
    assert lines and "2 chars → app.exe/Cls" in lines[0]

    lines.clear()
    fake.writes.clear()
    fake.clipboard = "old"
    calls = {"text": 0}

    def on_set(fmt, _handle):
        if fmt == iw._CF_UNICODETEXT:
            calls["text"] += 1
            return calls["text"] == 1   # the dictation's own write fails; the rollback still works

    fake.on_set = on_set
    with pytest.raises(InjectionError):
        iw.inject_text_paste_windows("hi")
    deadline = time.time() + 2
    while not lines and time.time() < deadline:
        time.sleep(0.01)
    assert lines and "FAILED InjectionError" in lines[0]


def test_a_diagnostics_failure_never_stops_the_paste(monkeypatch):
    from wayfinder.core import injector_windows as iw

    fake = _FakeWindows(clipboard="old")
    monkeypatch.setattr(iw, "_user32", fake)
    monkeypatch.setattr(iw, "_kernel32", fake)
    monkeypatch.setattr(iw, "_paste_reporter", lambda line: None)
    monkeypatch.setattr(iw, "_require_foreground_window", lambda: None)
    monkeypatch.setattr(iw, "_refuse_own_window", lambda text: None)
    monkeypatch.setattr(iw, "require_modifier_release_windows", lambda: None)
    monkeypatch.setattr(iw.time, "sleep", lambda s: None)
    pending = []
    monkeypatch.setattr(iw._pending_restore, "schedule", pending.append)

    def no_threads(*a, **k):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(iw.threading, "Thread", no_threads)
    iw.inject_text_paste_windows("hi")
    assert pending and fake.writes == ["hi"]
    pending.pop()()
    assert fake.writes == ["hi", "old"]

    fake.writes.clear()
    fake.clipboard = "old"
    monkeypatch.setattr(iw, "_PasteDiagnostics", no_threads)   # even construction failing
    iw.inject_text_paste_windows("hi")
    assert pending and fake.writes == ["hi"]
    pending.pop()()
    assert fake.writes == ["hi", "old"]



# --- Deferred clipboard restore (the Mac's DeferredRestore) -----------------

def _real_timer_rig(monkeypatch, clipboard="user text"):
    """injector_windows over a real _FakeWindows, but — unlike
    _windows_send_rig — with the real clipboard-restore timer and real
    time.sleep, for tests about RESTORE_DELAY_S and the timer itself."""
    from wayfinder.core import injector_windows as iw

    fake = _FakeWindows(clipboard=clipboard)
    monkeypatch.setattr(iw, "_user32", fake)
    monkeypatch.setattr(iw, "_kernel32", fake)
    monkeypatch.setattr(iw, "_paste_reporter", None)
    monkeypatch.setattr(iw, "_require_foreground_window", lambda: None)
    monkeypatch.setattr(iw, "_refuse_own_window", lambda text: None)
    monkeypatch.setattr(iw, "require_modifier_release_windows", lambda: None)
    return fake, iw


def test_old_clipboard_comes_back_after_the_app_had_time_to_read(monkeypatch):
    """VS Code read the clipboard 30-90 ms after Ctrl+V; a restore at 80 ms
    made it paste the old clipboard. The restore now waits ~0.8 s."""
    fake, iw = _real_timer_rig(monkeypatch, "user text")
    assert iw.RESTORE_DELAY_S >= 0.5
    iw.inject_text_paste_windows("dictation")
    assert fake.clipboard == "dictation"        # still there for a slow reader
    time.sleep(iw.RESTORE_DELAY_S + 0.4)
    assert fake.clipboard == "user text"        # then put back on its own


def test_back_to_back_dictations_never_capture_auras_own_text(monkeypatch):
    fake, iw = _real_timer_rig(monkeypatch, "user text")
    iw.inject_text_paste_windows("first")
    iw.inject_text_paste_windows("second")  # flushes the first restore first
    iw._pending_restore.flush()
    assert fake.clipboard == "user text"


def test_a_new_copy_before_the_restore_is_kept(monkeypatch):
    fake, iw = _real_timer_rig(monkeypatch, "user text")
    iw.inject_text_paste_windows("dictation")
    assert fake.copy("something the user just copied")
    iw._pending_restore.flush()
    assert fake.clipboard == "something the user just copied"



def test_copying_the_same_words_before_the_restore_is_kept(monkeypatch):
    """"Copy last transcription" (or Ctrl+C on the pasted text) puts the same
    words back: the mark, not the text, shows it is the user's — a real copy
    clears Aura's private format even when the words match."""
    fake, iw = _real_timer_rig(monkeypatch, "user text")
    iw.inject_text_paste_windows("dictation")
    assert fake.copy("dictation")   # the user's own copy of the same words
    iw._pending_restore.flush()
    assert fake.clipboard == "dictation"


def test_next_paste_waits_for_a_restore_the_timer_is_already_running(monkeypatch):
    """Game chat pastes part 2 ~0.85 s after part 1, right as part 1's timer
    restore runs. The second paste must wait for it, not race it."""
    import threading

    fake, iw = _real_timer_rig(monkeypatch, "USER")
    pasted = []
    monkeypatch.setattr(iw, "_press_keys", lambda vks: pasted.append(fake.clipboard))
    real_get = iw._clipboard_get_windows
    started = threading.Event()

    def slow_get():
        if threading.current_thread() is not threading.main_thread():
            started.set()
            time.sleep(0.15)                # a slow restore, mid-flight
        return real_get()

    monkeypatch.setattr(iw, "_clipboard_get_windows", slow_get)
    iw.inject_text_paste_windows("part1")
    assert started.wait(iw.RESTORE_DELAY_S + 1)        # the timer restore began
    iw.inject_text_paste_windows("part2")              # lands mid-restore
    iw._pending_restore.flush()
    assert pasted == ["part1", "part2"]                # never the user's text
    assert fake.clipboard == "USER"                    # and it comes back


def test_quitting_flushes_a_pending_clipboard_restore(monkeypatch):
    """Quit ends with os._exit (no atexit), so quit paths flush explicitly."""
    import inspect
    import sys as _sys

    import wayfinder_main

    calls = []
    fake = SimpleNamespace(_pending_restore=SimpleNamespace(flush=lambda: calls.append(1)))
    monkeypatch.setitem(_sys.modules, "wayfinder.core.injector_windows", fake)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", True)
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    wayfinder_main._flush_windows_clipboard_restore()
    assert calls == [1]
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)       # Linux/macOS: no-op
    wayfinder_main._flush_windows_clipboard_restore()
    assert calls == [1]
    quit_src = inspect.getsource(wayfinder_main.WayfinderApp.quit_app)
    assert quit_src.index("_flush_windows_clipboard_restore()") < quit_src.index("os._exit(0)")


def test_clipboard_read_watcher_is_off_unless_asked_for(monkeypatch):
    """It polls every 0.5 ms for 1.5 s per paste (CLAUDE.md rule 1): the
    paste_diagnostics_watch setting turns it on while chasing a report."""
    from wayfinder.config import DEFAULT_CONFIG
    from wayfinder.core import injector_windows as iw

    assert DEFAULT_CONFIG["paste_diagnostics_watch"] is False
    monkeypatch.setattr(iw, "_watch_reads", False)
    monkeypatch.setattr(iw, "_paste_reporter", None)
    iw.set_paste_reporter(lambda line: None)
    d = _diag(monkeypatch)
    d.start_watch()
    assert d._watch is None
    d._marks.update(ctrl_v=40)
    assert "no clipboard read seen" not in d._line("Code.exe/Chrome_WidgetWin_1")

    iw.set_paste_reporter(lambda line: None, watch_reads=True)
    monkeypatch.setattr(iw, "_WATCH_SECONDS", 0.01)
    monkeypatch.setattr(iw, "_user32", SimpleNamespace(GetForegroundWindow=lambda: 1,
                                                       GetOpenClipboardWindow=lambda: None))
    d = iw._PasteDiagnostics("hi")
    d.start_watch()
    assert d._watch is not None
    d._watch.join(2)


def test_a_failed_ctrl_v_leaves_the_dictation_and_schedules_no_restore(monkeypatch):
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)

    def refuse(vks):
        raise RuntimeError("SendInput blocked")

    monkeypatch.setattr(w, "_press_keys", refuse)
    with pytest.raises(RuntimeError):
        w.inject_text_paste_windows("dictated")
    assert pending == [] and rig.clipboard == "dictated"   # the user can still press Ctrl+V


# --- Pill + power diagnostics (overlay acks, windows_lifecycle) -------------

def test_power_events_are_reported_without_changing_sleep_and_wake():
    import ctypes

    from wayfinder.utils import windows_lifecycle as lc

    seen, slept, woke = [], [], []
    kw = dict(on_power_event=seen.append, on_sleep=lambda: slept.append(1),
              on_wake=lambda: woke.append(1))
    lc.dispatch(lc._WM_POWERBROADCAST, lc._PBT_APMSUSPEND, 0, **kw)
    lc.dispatch(lc._WM_POWERBROADCAST, lc._PBT_APMRESUMEAUTOMATIC, 0, **kw)
    for state in (0, 1, 2):
        buf = ctypes.create_string_buffer(
            lc._GUID_CONSOLE_DISPLAY_STATE + (4).to_bytes(4, "little") + state.to_bytes(4, "little"))
        lc.dispatch(lc._WM_POWERBROADCAST, lc._PBT_POWERSETTINGCHANGE, ctypes.addressof(buf), **kw)
    assert seen == ["going to sleep", "woke up", "display off", "display on", "display dimmed"]
    assert slept == [1] and woke == [1]      # the existing handlers, unchanged
    # Without the diagnostics callback nothing new happens.
    lc.dispatch(lc._WM_POWERBROADCAST, lc._PBT_POWERSETTINGCHANGE, 0, on_wake=lambda: woke.append(2))
    assert woke == [1]


def _pill_controller(lines):
    import wayfinder_main

    ctl = wayfinder_main.OverlayController.__new__(wayfinder_main.OverlayController)
    ctl._log_callback = lines.append
    return ctl


def _ack(state, **diag):
    base = {"frames": 30, "last_frame_ms": 20, "visible": True, "minimized": False,
            "opacity": 1.0, "rect": [600, 816, 262, 52], "screen": "DISPLAY1",
            "dpr": 1.75, "avail": [0, 0, 1463, 866], "full": [0, 0, 1463, 914],
            "settling": False}
    base.update(diag)
    _ACK_SEQ[0] += 1
    return {"ack": True, "nonce": str(_ACK_SEQ[0]), "state": state, "diag": base}


_ACK_SEQ = [0]


def test_pill_line_is_quiet_when_the_pill_is_drawing_on_screen():
    lines = []
    ctl = _pill_controller(lines)
    ctl._log_pill_diag(_ack("listening"))
    ctl._log_pill_diag(_ack("processing"))
    assert lines[1].startswith("🫧 Pill → processing · window 600,816 262x52 on DISPLAY1")
    assert "30 frames during listening" in lines[1] and "⚠" not in "".join(lines)


def test_pill_line_flags_a_pill_that_stopped_drawing_or_left_the_screen():
    lines = []
    ctl = _pill_controller(lines)
    ctl._log_pill_diag(_ack("listening"))
    ctl._log_pill_diag(_ack("processing", frames=0))
    assert "⚠" in lines[1] and "painted nothing while listening" in lines[1]
    ctl._log_pill_diag(_ack("ready", rect=[600, 1428, 262, 52]))
    assert "outside the display" in lines[2]
    ctl._log_pill_diag(_ack("listening", visible=False))
    assert "window hidden" in lines[3]


def test_pill_line_no_false_alarms_for_glow_offset_startup_or_resends():
    lines = []
    ctl = _pill_controller(lines)
    # Glow hanging below the usable area / pill moved over the taskbar: fine.
    ctl._log_pill_diag(_ack("ready", rect=[600, 840, 262, 66]))
    # Fading in at start-up (opacity 0, boot hold): "starting up", no warning.
    ctl._log_pill_diag(_ack("ready", opacity=0.0, settling=True))
    assert "⚠" not in "".join(lines) and "starting up" in lines[1]
    # A resent command acked twice logs once.
    ack = _ack("listening")
    ctl._log_pill_diag(ack)
    ctl._log_pill_diag(ack)
    assert len(lines) == 3


@pytest.mark.parametrize("at", ["modifier-wait", "clipboard-write"])
def test_paste_retired_during_its_waits_pastes_nothing(monkeypatch, at):
    # Sol mac r11 (the Mac finding, on Windows too): a reset while the paste
    # waited for the hotkey's modifiers still pasted the retired text. Over a
    # fake SendInput: the check sits in _send (Sol mac r12).
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)

    def wait():
        rig.retired = at == "modifier-wait"

    def written(text):
        rig.retired = True

    monkeypatch.setattr(w, "require_modifier_release_windows", wait)
    rig.fake.on_write = written
    with pytest.raises(injector.InjectionCancelled):
        w.inject_text_paste_windows("retired words", lambda: rig.retired)
    assert rig.sent == []
    assert rig.clipboard == "theirs"
    assert rig.fake.writes == ([] if at == "modifier-wait" else ["retired words", "theirs"])


@pytest.mark.parametrize("paste_error", ["cancelled", "failed"])
def test_a_retired_windows_paste_types_nothing_instead(monkeypatch, paste_error):
    # The typing fallback is for a failed paste, not a retired dictation:
    # neither one the paste refused nor one retired while it failed.
    from wayfinder.core import injector
    from wayfinder.core import injector_windows

    typed = []

    def paste(text, cancelled=None):
        if paste_error == "cancelled":
            raise injector.InjectionCancelled("the dictation was retired: nothing injected")
        raise injector.InjectionError("Could not write to the Windows clipboard for paste.")

    monkeypatch.setattr(injector.sys, "platform", "win32")
    monkeypatch.setattr(injector_windows, "inject_text_paste_windows", paste)
    monkeypatch.setattr(injector_windows, "inject_text_windows", lambda *a, **k: typed.append(a))
    with pytest.raises(injector.InjectionCancelled):
        injector.inject_text("retired words", cancelled=lambda: True)
    assert typed == []


@pytest.mark.parametrize("retire", [True, False])
def test_auto_enter_retired_during_its_wait_sends_nothing(monkeypatch, retire):
    # A direct call's *cancelled* is asked at the send, after the modifier
    # wait, over a fake SendInput (Sol mac r12).
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    monkeypatch.setattr(w, "_held_seconds", lambda: 0)
    monkeypatch.setattr(w, "require_modifier_release_windows",
                        lambda: setattr(rig, "retired", retire))
    if retire:
        with pytest.raises(injector.InjectionCancelled):
            w.press_enter_windows(lambda: rig.retired)
        assert rig.sent == []
    else:
        w.press_enter_windows(lambda: rig.retired)
        assert [vk for vk, _scan, _up in rig.sent] == [w.VK_RETURN, w.VK_RETURN]


def test_the_clipboard_paste_hands_windows_its_cancellation(monkeypatch):
    # do_inject's explicit clipboard paste (Sol mac r12): the Windows paste
    # gets the dictation's cancellation, so it can ask after its own waits.
    from wayfinder.core import injector
    from wayfinder.core import injector_windows

    given = []
    monkeypatch.setattr(injector.sys, "platform", "win32")
    monkeypatch.setattr(injector_windows, "inject_text_paste_windows",
                        lambda text, cancelled=None: given.append(cancelled))

    def retired():
        return False
    injector.inject_text_clipboard_paste("live words", cancelled=retired)
    assert given == [retired]



class _FakeWindows:
    """user32 + kernel32 for injector_windows, so tests drive its real
    SendInput and clipboard code. The clipboard keeps Windows' rules, as
    measured on node 4 (Windows 11): one holder at a time (another app's copy
    fails while Aura has it open; a script's with no window fails only while
    a window holds it), the change counter moves at each change and again at
    the close after one, reads never move it, and the data outlives the
    window that wrote it."""

    def __init__(self, clipboard="theirs"):
        self.sent, self.writes, self.events = [], [], []
        self.clipboard, self.seq = clipboard, 1
        self.opened = self._changed = False
        self.holder = None
        self.on_open = self.on_write = self.on_send = self.on_set = self.on_alloc = None
        self.on_lock = None
        self.windows, self.formats, self._names = set(), {}, {}
        self.no_windows = False
        self._memory = {}
        self.freed = []                 # handles GlobalFree was called with
        self.foreground = 1             # a truthy HWND, for diagnostics' own use

    def CreateWindowExW(self, _ex, cls, _name, _style, _x, _y, _w, _h, parent, *_rest):
        assert cls == "STATIC" and parent == -3     # message-only
        if self.no_windows:
            return None
        hwnd = 0x1000 + len(self.events)
        self.windows.add(hwnd)
        self.events.append("window")
        return hwnd

    def DestroyWindow(self, hwnd):
        assert hwnd in self.windows and self.holder != hwnd, "destroyed a window still holding"
        self.windows.discard(hwnd)
        self.events.append("destroy")
        return 1

    def GetForegroundWindow(self):
        return self.foreground

    def MapVirtualKeyW(self, vk, _map):
        return vk   # a fake scan code: good enough to tell keys apart

    def SendInput(self, n, arr, _size):
        from wayfinder.core import injector_windows as w
        accepted = n
        if self.on_send is not None:
            result = self.on_send(n)
            if isinstance(result, int):     # None/other: every event lands, as before
                accepted = result
        self.sent.extend((arr[i].u.ki.wVk, arr[i].u.ki.wScan,
                          bool(arr[i].u.ki.dwFlags & w.KEYEVENTF_KEYUP)) for i in range(accepted))
        return accepted

    def OpenClipboard(self, hwnd):
        if self.on_open is not None and self.on_open() is False:
            return 0
        if self.opened:
            return 0
        assert hwnd is None or hwnd in self.windows
        self.opened, self.holder = True, hwnd
        self.events.append("open")
        return 1

    def CloseClipboard(self):
        assert self.opened, "closed a clipboard Aura did not hold"
        self.opened, self.holder = False, None
        self.events.append("close")
        if self._changed:
            self.seq, self._changed = self.seq + 1, False
        return 1

    def EmptyClipboard(self):
        assert self.opened
        self.events.append("empty")
        self.clipboard, self.seq, self._changed = None, self.seq + 1, True
        self.formats.clear()
        return 1

    def SetClipboardData(self, fmt, handle):
        import ctypes
        assert self.opened
        if self.on_set is not None and self.on_set(fmt, handle):
            self.events.append(("set-failed", fmt))
            return 0
        if fmt == 13:
            self.clipboard = ctypes.wstring_at(handle)
            self.writes.append(self.clipboard)
            self.events.append(("set", self.clipboard))
            if self.on_write is not None:
                self.on_write(self.clipboard)
        else:
            self.formats[fmt] = bytes(self._memory[handle])
        self.seq, self._changed = self.seq + 1, True
        return handle

    def GetClipboardData(self, fmt):
        import ctypes
        assert self.opened
        self.events.append("read")
        if fmt != 13:
            if fmt not in self.formats:
                return 0
            return self._keep(ctypes.create_string_buffer(self.formats[fmt], len(self.formats[fmt])))
        if self.clipboard is None:
            return 0
        return self._keep(ctypes.create_unicode_buffer(self.clipboard))

    def IsClipboardFormatAvailable(self, fmt):
        assert self.opened
        if fmt != 13:
            return 1 if fmt in self.formats else 0
        return 1 if self.clipboard is not None else 0

    def GetClipboardSequenceNumber(self):
        return self.seq

    def RegisterClipboardFormatW(self, name):
        return self._names.setdefault(name, 0xC000 + len(self._names))

    def copy(self, text):
        """Another app's Ctrl+C: it fails while Aura holds the clipboard."""
        if self.opened:
            return False
        self.clipboard, self.seq = text, self.seq + 3
        self.formats.clear()
        return True

    def copy_without_a_window(self, text):
        """A script's copy that opens with no owner: Windows lets it through
        a hold with no owner, never one by a window (node 4)."""
        if self.opened and self.holder is not None:
            return False
        self.clipboard, self.seq = text, self.seq + 3
        self.formats.clear()
        return True

    # kernel32
    def GlobalAlloc(self, _flags, size):
        import ctypes
        if self.on_alloc is not None and self.on_alloc():
            return 0
        return self._keep(ctypes.create_string_buffer(size))

    def GlobalLock(self, handle):
        if self.on_lock is not None and self.on_lock(handle):
            return 0
        return handle

    def GlobalUnlock(self, _handle):
        return 1

    def GlobalFree(self, handle):
        self.freed.append(handle)
        self._memory.pop(handle, None)
        return 0   # NULL on success, as the real GlobalFree

    def GlobalSize(self, handle):
        import ctypes
        return ctypes.sizeof(self._memory[handle])

    def _keep(self, buffer):
        import ctypes
        address = ctypes.addressof(buffer)
        self._memory[address] = buffer
        return address


class _Rig(SimpleNamespace):
    clipboard = property(lambda self: self.fake.clipboard)
    seq = property(lambda self: self.fake.seq)


def _windows_send_rig(monkeypatch):
    """injector_windows over a fake user32/kernel32 (_FakeWindows); returns rig."""
    import ctypes
    import time as _time
    from wayfinder.core import injector_windows as w

    fake = _FakeWindows()
    rig = _Rig(fake=fake, sent=fake.sent, retired=False, on_sleep=None, own_window=False)

    def sleep(seconds):
        if rig.on_sleep is not None:
            rig.on_sleep()

    # ctypes.get_last_error is Windows-only; a partial SendInput (Finding 3 /
    # Sol mac r16) reads it, so a non-Windows test run needs it stubbed.
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    monkeypatch.setattr(w, "_user32", fake)
    monkeypatch.setattr(w, "_kernel32", fake)
    monkeypatch.setattr(w, "time", SimpleNamespace(sleep=sleep, monotonic=_time.monotonic,
                                                   time=_time.time, perf_counter=_time.perf_counter))
    monkeypatch.setattr(w, "_paste_reporter", None)
    monkeypatch.setattr(w, "_require_foreground_window", lambda: None)
    monkeypatch.setattr(w, "_foreground_is_own_process", lambda: rig.own_window)
    monkeypatch.setattr(w, "require_modifier_release_windows", lambda: None)
    monkeypatch.setattr(w._pending_restore, "schedule", lambda restore: None)
    return rig, w


def test_a_retired_dictations_key_press_never_reaches_send_input(monkeypatch):
    # Sol mac r12: the check sits in _send itself, so no wait above it (a
    # modifier wait, a typing pace, a clipboard settle) can let a key through.
    # A release always goes out: a key left down repeats in the target app.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    with injector.cancellable(lambda: True):
        with pytest.raises(injector.InjectionCancelled):
            w._send(w._vkey_inputs(w.VK_RETURN))
        assert rig.sent == []
        w._send([w._keyboard_input(wVk=w.VK_RETURN, flags=w.KEYEVENTF_KEYUP)])
    assert rig.sent == [(w.VK_RETURN, 0, True)]


def test_paced_windows_typing_stops_at_a_retirement(monkeypatch):
    # Sol mac r12: the typing fallback's paced sends went on across sleeps.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    rig.on_sleep = lambda: setattr(rig, "retired", True)   # Escape after "a"
    with injector.cancellable(lambda: rig.retired):
        with pytest.raises(injector.InjectionCancelled):
            w.inject_text_windows("abc", "normal")
    downs = [event for event in rig.sent if not event[2]]
    assert len(downs) == 1                                  # "a" only


@pytest.mark.parametrize("scope", ["explicit", "scoped"])
@pytest.mark.parametrize("user_copied", [False, True])
def test_a_windows_paste_retired_during_its_settle(monkeypatch, scope, user_copied):
    # Sol mac r12: the settle check asked only an explicit cancellation (a
    # game chat's paste passes it through the scope), and its restore wrote
    # the old clipboard over whatever the user had copied meanwhile.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)

    def settle():
        if user_copied:
            assert rig.fake.copy("mine")                    # the user's Ctrl+C
        rig.retired = True

    rig.on_sleep = settle
    cancelled = lambda: rig.retired   # noqa: E731
    with injector.cancellable(cancelled if scope == "scoped" else None):
        with pytest.raises(injector.InjectionCancelled):
            w.inject_text_paste_windows("retired words",
                                        cancelled if scope == "explicit" else None)
    assert rig.sent == []                                   # no Ctrl+V
    assert rig.clipboard == ("mine" if user_copied else "theirs")


def test_aura_frontmost_leaves_no_retired_text_on_the_windows_clipboard(monkeypatch):
    # Sol mac r12: the own-window refusal wrote the transcript to the
    # clipboard for a manual paste, a retired dictation's included.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    rig.own_window = True
    with injector.cancellable(lambda: True):
        with pytest.raises(injector.InjectionCancelled):
            w.inject_text_windows("retired words")
    assert rig.clipboard == "theirs" and rig.sent == []
    with pytest.raises(injector.InjectionError, match="on the clipboard"):
        w.inject_text_windows("live words")
    assert rig.clipboard == "live words"


def test_each_send_input_batch_with_a_press_asks(monkeypatch):
    # Sol mac r13: _send asked once before its chunk loop, so a retirement
    # during the first SendInput still sent the next chunks' presses. A key
    # an earlier chunk left down goes back up.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    shift = w._keyboard_input(wVk=w.VK_SHIFT)
    shift_up = w._keyboard_input(wVk=w.VK_SHIFT, flags=w.KEYEVENTF_KEYUP)
    pairs = [i for ch in "ab" * 300 for i in w._char_inputs(ch)]
    rig.fake.on_send = lambda n: setattr(rig, "retired", True)
    with injector.cancellable(lambda: rig.retired):
        with pytest.raises(injector.InjectionCancelled):
            w._send([shift] + pairs + [shift_up])
    # One chunk (Shift and 255½ characters), then the key-ups for what it
    # left down: the split character's, then Shift's.
    assert len(rig.sent) == w._SEND_CHUNK + 2
    assert rig.sent[w._SEND_CHUNK - 1][2] is False
    assert rig.sent[-2] == (0, rig.sent[w._SEND_CHUNK - 1][1], True)
    assert rig.sent[-1] == (w.VK_SHIFT, 0, True)
    assert sum(1 for e in rig.sent if e[0] == w.VK_SHIFT and not e[2]) == 1


def test_a_live_long_send_goes_out_whole(monkeypatch):
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    pairs = [i for ch in "ab" * 300 for i in w._char_inputs(ch)]
    with injector.cancellable(lambda: False):
        w._send(pairs)
    assert len(rig.sent) == len(pairs)


def test_a_partial_ctrl_v_batch_releases_the_held_keys_and_skips_typing(monkeypatch):
    # Finding 3 (Sol mac r16): a partial SendInput (e.g. 2 of the 4 batched
    # Ctrl+V events accepted — Ctrl-down and V-down) raised InjectionError
    # but left Ctrl and V held down, and the typing fallback then ran
    # although the paste may have already landed.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    typed = []
    monkeypatch.setattr(w, "inject_text_windows", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(injector.sys, "platform", "win32")
    rig.fake.on_send = lambda n: 2 if n == 4 else n   # only the Ctrl+V batch is partial

    with pytest.raises(injector.InjectionError) as excinfo:
        injector.inject_text("dictation")
    assert excinfo.value.uncertain_delivery is True
    assert typed == []                                 # no typing fallback after an uncertain paste
    # Ctrl and V, both left down by the 2 accepted events, come back up —
    # last pressed first.
    assert rig.sent[-2:] == [(w.VK_V, 0, True), (w.VK_CONTROL, 0, True)]


def test_a_fully_rejected_ctrl_v_batch_is_not_uncertain_and_still_falls_back(monkeypatch):
    # Finding 3: nothing was accepted at all, so nothing was pressed and the
    # typing fallback is safe to run, same as before this fix.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    typed = []
    monkeypatch.setattr(w, "inject_text_windows", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(injector.sys, "platform", "win32")
    rig.fake.on_send = lambda n: 0

    injector.inject_text("dictation")
    assert typed == [("dictation", "instant")]
    assert rig.sent == []          # nothing was ever accepted, so nothing needed releasing


def test_inject_text_surfaces_an_uncertain_paste_instead_of_typing(monkeypatch):
    # Finding 3: injector.py's win32 branch, in isolation — a paste whose
    # Ctrl+V may have partially landed must not be followed by the typing
    # fallback, which could duplicate it.
    from wayfinder.core import injector
    from wayfinder.core import injector_windows

    typed = []

    def paste(text, cancelled=None):
        err = injector.InjectionError("SendInput injected 2/4 events (WinError 5).")
        err.uncertain_delivery = True
        raise err

    monkeypatch.setattr(injector.sys, "platform", "win32")
    monkeypatch.setattr(injector_windows, "inject_text_paste_windows", paste)
    monkeypatch.setattr(injector_windows, "inject_text_windows", lambda *a, **k: typed.append(a))
    with pytest.raises(injector.InjectionError, match="2/4"):
        injector.inject_text("maybe pasted")
    assert typed == []


def test_a_held_release_failure_is_uncertain_through_inject_text(monkeypatch):
    # Finding 2 (Sol mac r17): hold_keys() sends the Ctrl+V press and the
    # release as two separate _send calls. The press already lands fully —
    # Ctrl+V has fired — before the release is even attempted, so a failed
    # release must still stop inject_text's typing fallback from running,
    # not just the isolated _press_keys case above: a real caller sees this
    # through inject_text, where a "clean" failure would otherwise retype
    # a paste that already landed.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    typed = []
    monkeypatch.setattr(w, "inject_text_windows", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(injector.sys, "platform", "win32")
    calls = [0]

    def press_whole_then_split_release(n):
        calls[0] += 1
        return n if calls[0] == 1 else 1   # the press batch goes out whole

    rig.fake.on_send = press_whole_then_split_release

    with w.hold_keys(0.05):
        with pytest.raises(injector.InjectionError) as excinfo:
            injector.inject_text("dictation")
    assert excinfo.value.uncertain_delivery is True
    assert typed == []   # no typing fallback after an already-delivered press


def test_a_partial_hold_keys_release_batch_retries_its_unsent_key_ups(monkeypatch):
    # Finding 3: hold_keys() sends presses and releases as two separate
    # _send calls. If the release batch itself only partially lands, the
    # unsent releases must still go out — _releases_for sees no press in
    # THIS call's own inputs, so without the extra retry a key stays down
    # in the game.
    #
    # Finding 2 (Sol mac r17): the press batch below goes out WHOLE first —
    # Ctrl and V are already down in the target app, so the paste has
    # already fired — before the release batch is even sent. A failed
    # release after that is therefore never a clean "nothing happened": it
    # must carry uncertain_delivery, same as Finding 3's own partial-press
    # case, even though this call's own accepted events are releases only.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    calls = [0]

    def press_whole_then_split_release(n):
        calls[0] += 1
        return n if calls[0] == 1 else 1   # the press batch goes out whole

    rig.fake.on_send = press_whole_then_split_release
    with injector.cancellable(lambda: False):
        with w.hold_keys(0.05):
            with pytest.raises(injector.InjectionError) as excinfo:
                w._press_keys([w.VK_CONTROL, w.VK_V])
    assert excinfo.value.uncertain_delivery is True   # the press already landed
    assert rig.sent == [
        (0, w.VK_CONTROL, False), (0, w.VK_V, False),   # pressed, held
        (0, w.VK_V, True),                              # released: accepted
        (0, w.VK_CONTROL, True),                        # released: retried
    ]


def test_a_failed_release_retry_does_not_mask_the_original_error(monkeypatch):
    # Finding 3: the retry goes through _send_quietly, which must swallow
    # its own failure rather than replace why the original send stopped.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    calls = [0]

    def press_whole_then_always_short(n):
        calls[0] += 1
        return n if calls[0] == 1 else max(0, n - 1)   # every call after the press is short

    rig.fake.on_send = press_whole_then_always_short
    with injector.cancellable(lambda: False):
        with w.hold_keys(0.05):
            with pytest.raises(injector.InjectionError, match=r"injected 1/2"):
                w._press_keys([w.VK_CONTROL, w.VK_V])



def test_a_send_input_that_refuses_everything_retries_its_releases_once(monkeypatch):
    # Sol mac r16: the release retry goes out once; a SendInput that keeps
    # refusing must not make the retry retry itself without end.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    calls = [0]

    def half_then_nothing(n):
        calls[0] += 1
        return 2 if calls[0] == 1 else 0   # Ctrl and V down, then nothing at all

    rig.fake.on_send = half_then_nothing
    with injector.cancellable(lambda: False):
        with pytest.raises(injector.InjectionError, match=r"injected 2/4"):
            w._press_keys([w.VK_CONTROL, w.VK_V])
    assert calls[0] == 2   # the batch, then one try at the releases

@pytest.mark.parametrize("retire", [True, False])
def test_a_windows_clipboard_write_asks_after_its_open_retries(monkeypatch, retire):
    # Sol mac r13: OpenClipboard retries while another app holds the
    # clipboard; a retirement then still emptied and wrote it.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    busy = [True]

    def open_attempt():
        if busy[0]:
            busy[0] = False
            return False                                  # another app has it
        return None

    rig.fake.on_open = open_attempt
    rig.on_sleep = lambda: setattr(rig, "retired", retire)
    with injector.cancellable(lambda: rig.retired):
        if retire:
            with pytest.raises(injector.InjectionCancelled):
                w._clipboard_set_windows("retired words", transient=True)
            assert rig.fake.events == ["window", "open", "close", "destroy"]
            assert rig.clipboard == "theirs"
        else:
            assert w._clipboard_set_windows("live words", transient=True)
            assert rig.clipboard == "live words"
    assert not rig.fake.opened


@pytest.mark.parametrize("path", ["paste", "own-window"])
def test_a_windows_dictation_retired_during_a_clipboard_read_writes_nothing(monkeypatch, path):
    # Sol mac r13: retired while the paste read the user's clipboard, or while
    # the own-window refusal waited for it, the write still went out.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    rig.own_window = path == "own-window"
    rig.fake.on_open = lambda: setattr(rig, "retired", True)
    with injector.cancellable(lambda: rig.retired):
        with pytest.raises(injector.InjectionCancelled):
            if path == "paste":
                w.inject_text_paste_windows("retired words")
            else:
                w.inject_text_windows("retired words")
    assert rig.fake.writes == [] and rig.clipboard == "theirs" and rig.sent == []


def _pasted_with_a_pending_restore(monkeypatch):
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    w.inject_text_paste_windows("dictation")
    assert rig.clipboard == "dictation" and len(pending) == 1
    rig.fake.events.clear()
    return rig, w, pending[0]


def test_the_windows_restore_checks_and_writes_under_one_open(monkeypatch):
    # Sol mac r13: the check and the restore opened the clipboard apart, so a
    # copy between them was overwritten. While Aura holds it, no app can copy.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    restore()
    assert rig.clipboard == "theirs"
    # the mark, the text, then the write; the window outlives the hold
    assert rig.fake.events == ["window", "open", "read", "read", "empty", ("set", "theirs"),
                               "close", "destroy"]


def test_a_restore_allocation_failure_touches_nothing_and_the_retry_restores(monkeypatch):
    # Finding 3 (Sol mac r17): every block the restore could need is
    # allocated before anything on the clipboard is touched, so a failed
    # GlobalAlloc never empties the clipboard; the clipboard is still Aura's
    # afterwards, so the bounded retry puts the user's text back.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    rig.fake.freed.clear()   # drop the initial paste's own unused-rollback free
    lines = []
    monkeypatch.setattr(w, "_paste_reporter", lines.append)
    calls = [0]

    def fail_first_alloc():
        calls[0] += 1
        return calls[0] == 1   # the first attempt's previous-text block only

    rig.fake.on_alloc = fail_first_alloc
    restore()
    assert rig.clipboard == "theirs" and rig.fake.writes == ["dictation", "theirs"]
    assert rig.fake.events.count("empty") == 1   # only the attempt that could write
    # attempt 1 frees its dictation and mark blocks; attempt 2 its unused fallbacks
    assert len(rig.fake.freed) == 4
    assert len(lines) == 1 and "prepare the clipboard restore" in lines[0] and "theirs" not in lines[0]


def test_a_restore_that_can_never_allocate_leaves_the_dictation_after_bounded_tries(monkeypatch):
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    lines, sleeps = [], []
    monkeypatch.setattr(w, "_paste_reporter", lines.append)
    rig.on_sleep = lambda: sleeps.append(1)
    rig.fake.on_alloc = lambda: True
    restore()
    assert rig.clipboard == "dictation" and rig.fake.writes == ["dictation"]
    assert "empty" not in rig.fake.events
    assert len(lines) == 1 + w.RESTORE_RETRIES and len(sleeps) == w.RESTORE_RETRIES


def test_a_restore_global_lock_failure_leaves_nothing_leaked(monkeypatch):
    # Finding 3 + Finding 5 (Sol mac r17): GlobalAlloc can succeed while
    # GlobalLock fails. _global_copy frees that one block itself (Finding
    # 5); the restore's OTHER, successfully allocated blocks must not leak
    # either once it gives up on preparing them all.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    rig.fake.freed.clear()   # drop the initial paste's own unused-rollback free
    calls = [0]

    def fail_first_lock(_handle):
        calls[0] += 1
        # Calls 1-2 are the pre-restore untouched check's own reads (the
        # mark, then the dictated text) — both must succeed to reach the
        # restore's own prepare step. Call 3 is the previous-text block's lock.
        return calls[0] == 3

    rig.fake.on_lock = fail_first_lock
    restore()
    assert rig.clipboard == "theirs"   # the retry
    assert rig.fake.events.count("empty") == 1
    # attempt 1: the failed-lock block plus the unused dictation and mark
    # blocks; attempt 2: its unused dictation and mark fallbacks
    assert len(rig.fake.freed) == 5


def test_a_restore_set_clipboard_data_failure_puts_the_dictation_back_then_retries(monkeypatch):
    # Finding 3 (Sol mac r17): if SetClipboardData(previous) fails after
    # EmptyClipboard, the dictation and its mark prepared alongside go back
    # in rather than leaving the clipboard empty, and the retry then
    # restores the user's text.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    rig.fake.freed.clear()   # drop the initial paste's own unused-rollback free
    calls = {"text": 0}

    def on_set(fmt, _handle):
        if fmt == w._CF_UNICODETEXT:
            calls["text"] += 1
            return calls["text"] == 1   # only the first previous-text write fails
        return False

    rig.fake.on_set = on_set
    restore()
    assert rig.clipboard == "theirs"
    assert rig.fake.writes == ["dictation", "dictation", "theirs"]  # paste, rollback, retry
    assert rig.fake.events.count("empty") == 2
    # attempt 1: the refused previous block; attempt 2: its unused fallbacks
    assert len(rig.fake.freed) == 3


def test_a_rollback_that_loses_the_mark_stops_retrying(monkeypatch):
    # Without the mark nothing proves the clipboard is still Aura's, so the
    # retry must not write over what might now be the user's.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    mark_fmt = rig.fake.RegisterClipboardFormatW(w._OWNER_MARK_FORMAT)
    calls = {"text": 0}

    def on_set(fmt, _handle):
        if fmt == w._CF_UNICODETEXT:
            calls["text"] += 1
            return calls["text"] == 1
        return fmt == mark_fmt   # the mark cannot go back

    rig.fake.on_set = on_set
    sleeps = []
    rig.on_sleep = lambda: sleeps.append(1)
    restore()
    assert rig.clipboard == "dictation"
    assert rig.fake.writes == ["dictation", "dictation"]
    assert rig.fake.events.count("empty") == 1   # no second attempt wrote
    # one wait, then the next attempt's own check finds no mark and stops
    assert sleeps == [1]


def test_the_windows_restore_keeps_a_same_words_copy_made_while_it_waited(monkeypatch):
    # Sol mac r13: the counter was read before the clipboard opened; the user
    # copying the same words while it waited looked untouched, and the restore
    # overwrote their copy.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    busy = [True]

    def open_attempt():
        if busy[0]:
            busy[0] = False
            return False
        return None

    rig.fake.on_open = open_attempt
    rig.on_sleep = lambda: rig.fake.copy("dictation")     # the same words, copied
    restore()
    assert rig.fake.writes == ["dictation"]               # no "theirs" over it
    assert rig.clipboard == "dictation" and not rig.fake.opened


def test_a_script_with_no_window_cannot_copy_into_the_restore(monkeypatch):
    # Sol mac r14: a hold with no owner let another ownerless OpenClipboard
    # through, so a script's copy could land between the check and the write.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    attempts, read = [], rig.fake.GetClipboardData

    def read_while_a_script_copies(fmt):
        attempts.append(rig.fake.copy_without_a_window("script's"))
        return read(fmt)

    monkeypatch.setattr(rig.fake, "GetClipboardData", read_while_a_script_copies)
    restore()
    assert attempts and not any(attempts) and rig.clipboard == "theirs"
    assert rig.fake.copy_without_a_window("script's")      # free again after


def test_a_same_words_copy_right_after_the_write_is_kept(monkeypatch):
    # Sol mac r14: the counter was read after the close, so a copy of the same
    # words in between became Aura's, and the restore overwrote it.
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    close, copied = rig.fake.CloseClipboard, []

    def close_then_the_user_copies():
        close()
        if rig.clipboard == "dictation" and not copied:     # once, at the first close
            copied.append(rig.fake.copy("dictation"))

    monkeypatch.setattr(rig.fake, "CloseClipboard", close_then_the_user_copies)
    w.inject_text_paste_windows("dictation")
    pending[0]()
    assert copied == [True]
    assert rig.fake.writes == ["dictation"] and rig.clipboard == "dictation"


def test_no_owner_window_refuses_the_paste(monkeypatch):
    # Finding 5: an ownerless hold does not exclude another ownerless
    # opener, so a paste write with no owner window is refused through the
    # ordinary write-failure path instead of writing unprotected.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    rig.fake.no_windows = True
    with pytest.raises(injector.InjectionError, match="Could not write"):
        w.inject_text_paste_windows("dictation")
    assert rig.clipboard == "theirs" and rig.fake.writes == [] and rig.sent == []
    assert "window" not in rig.fake.events                 # never even tried to open one


def test_no_owner_window_refuses_a_plain_copy(monkeypatch):
    # Finding 5: a copy on its own (copy-only mode) never opens an ownerless
    # hold either; it reports the write failed and leaves the clipboard be.
    rig, w = _windows_send_rig(monkeypatch)
    rig.fake.no_windows = True
    before = list(rig.fake.events)
    assert w._clipboard_set_windows("dictation") is False
    assert rig.fake.events == before and rig.clipboard == "theirs"

def test_no_owner_window_at_restore_leaves_an_ownerless_copy_alone(monkeypatch):
    # Finding 5: no window at restore time must never fall back to an
    # ownerless hold — the restore does not even try to open the clipboard,
    # so whatever another ownerless opener copies into the gap survives.
    rig, w, restore = _pasted_with_a_pending_restore(monkeypatch)
    lines = []
    monkeypatch.setattr(w, "_paste_reporter", lines.append)
    rig.fake.no_windows = True
    assert rig.fake.copy_without_a_window("script's")       # lands in the gap
    restore()
    assert rig.fake.events == []                            # no open attempted at all
    assert rig.fake.writes == ["dictation"]                 # nothing written by the restore
    assert rig.clipboard == "script's"                       # the ownerless copy survives
    assert lines and "no clipboard owner window" in lines[0] and "script's" not in lines[0]


def test_the_snapshot_and_the_write_share_one_open(monkeypatch):
    # Finding 1 (Sol mac r16): the old snapshot opened and closed the
    # clipboard on its own, then the write opened again — a gap in which
    # another app's copy would be invisible to the snapshot and lost when
    # the restore later put the stale snapshot back over it. Reading the
    # snapshot while already open removes that gap.
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    w.inject_text_paste_windows("dictation")
    assert rig.fake.events == [
        "window", "open", "read", "empty", ("set", "dictation"), "close", "destroy",
    ]


def test_an_unreadable_snapshot_raises_and_falls_back_to_typing(monkeypatch):
    # Finding 4 (Sol mac r17): GlobalLock failing on the clipboard's OWN
    # CF_UNICODETEXT handle used to read back through _read_open_clipboard as
    # previous=None — indistinguishable from "nothing on the clipboard" — so
    # the paste went ahead, emptied an unreadable clipboard, and scheduled no
    # restore. A format that IS there (GetClipboardData returned a handle)
    # but cannot be read must raise before anything destructive, so
    # inject_text falls back to typing exactly as it does for any other
    # refused paste.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    typed = []
    monkeypatch.setattr(w, "inject_text_windows", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(injector.sys, "platform", "win32")
    calls = [0]

    def fail_first_lock(_handle):
        calls[0] += 1
        return calls[0] == 1   # only the snapshot's own lock fails

    rig.fake.on_lock = fail_first_lock

    injector.inject_text("dictation")
    assert typed == [("dictation", "instant")]
    assert "empty" not in rig.fake.events and rig.fake.writes == [] and rig.sent == []
    assert rig.clipboard == "theirs"   # never touched


def test_global_copy_frees_the_block_when_the_lock_fails(monkeypatch):
    # Finding 5 (Sol mac r17): GlobalAlloc succeeding but GlobalLock failing
    # used to leak the block — nothing ever freed it.
    rig, w = _windows_send_rig(monkeypatch)
    rig.fake.on_lock = lambda _handle: True
    assert w._global_copy(b"hello") is None
    assert len(rig.fake.freed) == 1


def test_an_allocation_failure_refuses_before_anything_is_written(monkeypatch):
    # Finding 2 (Sol mac r16): every global memory block the write or its
    # rollback could need — the text, the mark, and (when there was a
    # previous text) its rollback copy — is allocated before EmptyClipboard,
    # so a failed GlobalAlloc anywhere in that prepare step never touches
    # the clipboard. Whatever did allocate is freed instead of leaked.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    calls = [0]

    def fail_second_alloc():
        calls[0] += 1
        return calls[0] == 2   # the mark's block; text's and previous's still allocate

    rig.fake.on_alloc = fail_second_alloc
    with pytest.raises(injector.InjectionError, match="Could not write"):
        w.inject_text_paste_windows("dictation")
    assert rig.clipboard == "theirs" and rig.fake.writes == [] and rig.sent == []
    assert "empty" not in rig.fake.events
    assert len(rig.fake.freed) == 2   # the text and previous blocks that did allocate



def test_a_dictation_retired_after_the_blocks_are_ready_frees_them(monkeypatch):
    # Sol mac r16: the retirement check sits after the allocations, so a
    # retired dictation hands back every block it prepared and writes nothing.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    allocs, retired = [0], [False]

    def retire_at_the_last_alloc():
        allocs[0] += 1
        retired[0] = allocs[0] == 3   # text, mark, then the previous text's copy
        return False

    rig.fake.on_alloc = retire_at_the_last_alloc
    with pytest.raises(injector.InjectionCancelled):
        w.inject_text_paste_windows("dictation", lambda: retired[0])
    assert rig.clipboard == "theirs" and rig.fake.writes == [] and rig.sent == []
    assert "empty" not in rig.fake.events
    assert len(rig.fake.freed) == 3

def test_the_unused_rollback_block_is_freed_on_a_successful_paste(monkeypatch):
    # Finding 2: the previous-clipboard rollback block is prepared "just in
    # case" before the destructive write; when it is never needed, it must
    # not leak.
    rig, w = _windows_send_rig(monkeypatch)
    w.inject_text_paste_windows("dictation")
    assert len(rig.fake.freed) == 1   # the previous block, never used for a rollback


def test_a_failed_mark_restores_the_previous_clipboard_in_place(monkeypatch):
    # Finding 6 (still) + Finding 2 (Sol mac r16): without a mark nothing
    # proves later the clipboard is still Aura's, so refuse instead of
    # pasting unmarked — but the rollback now replaces the text format in
    # place instead of emptying and writing again, so the clipboard is never
    # briefly empty in between.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    mark_fmt = rig.fake.RegisterClipboardFormatW(w._OWNER_MARK_FORMAT)
    rig.fake.on_set = lambda fmt, _handle: fmt == mark_fmt
    with pytest.raises(injector.InjectionError, match="Could not write"):
        w.inject_text_paste_windows("dictation")
    assert pending == [] and rig.sent == []                   # no restore scheduled, no Ctrl+V
    assert rig.clipboard == "theirs"                           # rolled back in place
    assert rig.fake.writes == ["dictation", "theirs"]          # the only two CF_UNICODETEXT writes
    assert rig.fake.events.count("open") == 1 and rig.fake.events.count("close") == 1


def test_a_failed_text_write_restores_the_previous_clipboard_in_place(monkeypatch):
    # Finding 2: the same in-place rollback applies when the dictation's own
    # text write is what fails, not just the mark.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    calls = {"text": 0}

    def on_set(fmt, _handle):
        if fmt == w._CF_UNICODETEXT:
            calls["text"] += 1
            return calls["text"] == 1   # the dictation's own write fails; the rollback succeeds
        return False

    rig.fake.on_set = on_set
    with pytest.raises(injector.InjectionError, match="Could not write"):
        w.inject_text_paste_windows("dictation")
    assert pending == [] and rig.sent == []
    assert rig.clipboard == "theirs"
    assert rig.fake.events.count("open") == 1


def test_when_there_was_nothing_on_the_clipboard_the_rollback_is_just_empty(monkeypatch):
    # Finding 2: when previous is None (the clipboard had no text before),
    # an EmptyClipboard is the right rollback — there is no text format to
    # put back in place.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    rig.fake.clipboard = None
    mark_fmt = rig.fake.RegisterClipboardFormatW(w._OWNER_MARK_FORMAT)
    rig.fake.on_set = lambda fmt, _handle: fmt == mark_fmt
    with pytest.raises(injector.InjectionError, match="Could not write"):
        w.inject_text_paste_windows("dictation")
    assert rig.clipboard is None
    assert rig.fake.writes == ["dictation"]


def test_a_failed_mark_and_a_failed_rollback_keeps_the_dictation_and_logs(monkeypatch):
    # Finding 2 (Sol mac r16): if even the in-place rollback fails, the
    # clipboard is left holding the dictation rather than emptied — never
    # left empty — and one diagnostic line says so, with no clipboard
    # content in it.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    lines = []
    monkeypatch.setattr(w, "_paste_reporter", lines.append)
    pending = []
    monkeypatch.setattr(w._pending_restore, "schedule", pending.append)
    mark_fmt = rig.fake.RegisterClipboardFormatW(w._OWNER_MARK_FORMAT)
    calls = {"text": 0}

    def on_set(fmt, _handle):
        if fmt == mark_fmt:
            return True                 # the mark write fails
        if fmt == w._CF_UNICODETEXT:
            calls["text"] += 1
            return calls["text"] > 1    # the dictation's write succeeds; the rollback's doesn't
        return False

    rig.fake.on_set = on_set
    with pytest.raises(injector.InjectionError, match="Could not write"):
        w.inject_text_paste_windows("dictation")
    assert pending == [] and rig.sent == []
    assert rig.clipboard == "dictation"      # never empty, never back to "theirs"
    assert any("could not put the clipboard back" in line for line in lines)


def test_the_cancel_restore_goes_out_under_a_retired_scope(monkeypatch):
    # Restores are never refused: the user's clipboard comes back even though
    # the dictation that wrote it is retired.
    from wayfinder.core import injector
    rig, w = _windows_send_rig(monkeypatch)
    rig.fake.on_write = lambda text: setattr(rig, "retired", text == "retired words")
    with injector.cancellable(lambda: rig.retired):
        with pytest.raises(injector.InjectionCancelled):
            w.inject_text_paste_windows("retired words")
    assert rig.fake.writes == ["retired words", "theirs"] and rig.sent == []

#!/usr/bin/env python3
"""Capture Flathub/AppStream store screenshots for Wayfinder Aura.

Launches the app with an isolated free-tier profile, drives tabs via the
control socket, captures via xdg-desktop-portal, then crops the app window
and composites it onto a clean brand background at 1920×1080.

Usage (from repo root, on a KDE Wayland session)::

    python3 scripts/capture_store_screenshots.py
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "screenshots"
TARGET_W, TARGET_H = 1920, 1080
BG = (0x0D, 0x11, 0x17)  # theme bg_base
BASE_MODEL = Path.home() / "whisper.cpp" / "models" / "ggml-base.en.bin"
if not BASE_MODEL.is_file():
    BASE_MODEL = (
        REPO / "flatpak" / "build-dir" / "files" / "share" / "whisper-models" / "ggml-base.en.bin"
    )

SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "wayfinder-aura" / "wayfinder-aura.sock"
STATUS = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "wayfinder-aura" / "status.json"
PID_FILE = Path("/tmp/wfa-store-shot.pid")


def _send(cmd: str, timeout: float = 3.0) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(SOCKET))
        sock.sendall(cmd.encode("utf-8"))
        try:
            sock.recv(16)
        except socket.timeout:
            pass


def _ping() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(SOCKET))
            sock.sendall(b"ping")
            return sock.recv(16).startswith(b"pong")
    except OSError:
        return False


def _status() -> dict | None:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _await_tab(tab: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _status()
        if s and s.get("tab") == tab:
            return True
        time.sleep(0.08)
    return False


def _await_state(state: str, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = _status()
        if s and s.get("state") == state:
            return True
        time.sleep(0.08)
    return False


def _await_socket(timeout: float = 50.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ping():
            return True
        time.sleep(0.25)
    return False


def _quit_existing() -> None:
    if _ping():
        print("  quitting existing instance…")
        try:
            _send("quit")
        except OSError:
            pass
        deadline = time.time() + 8
        while time.time() < deadline and _ping():
            time.sleep(0.2)
    if PID_FILE.is_file():
        try:
            os.kill(int(PID_FILE.read_text().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)
    time.sleep(0.6)


def _write_config(cfg_dir: Path, *, welcome_done: bool) -> None:
    cfg_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "setup_completed": True,
        "welcome_completed": welcome_done,
        "start_minimized": False,
        "processing_mode": "local",
        "model_path": str(BASE_MODEL.expanduser()),
        "use_gpu": False,
        "ui_scale": 1.0,
        "window_geometry": {
            "width": 1100,
            "height": 820,
            "x": 400,
            "y": 120,
        },
        "hotkey_key": 60,
        "hotkey_modifiers": ["super"],
        "style_toggle_key": 61,
        "style_toggle_modifiers": ["super"],
        "output_tone": "professional",
        "gpu_nudge_dismissed": True,
        "check_for_model_updates": False,
    }
    (cfg_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def _start_black_backdrop() -> subprocess.Popen | None:
    """Fullscreen solid brand-color window so transparent window chrome doesn't show the IDE."""
    py = REPO / "venv-gpu" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)
    code = r"""
import sys
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette

app = QApplication(sys.argv)
w = QWidget()
w.setWindowTitle("wfa-shot-backdrop")
w.setWindowFlags(
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnBottomHint
    | Qt.WindowType.Tool
)
pal = w.palette()
pal.setColor(QPalette.ColorRole.Window, QColor("#0D1117"))
w.setPalette(pal)
w.setAutoFillBackground(True)
w.showFullScreen()
app.exec()
"""
    try:
        return subprocess.Popen(
            [str(py), "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        print(f"  backdrop skipped: {exc}")
        return None


def _launch(cfg_home: Path, data_home: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(cfg_home)
    env["XDG_DATA_HOME"] = str(data_home)
    env["WAYFINDER_DEMO_TRANSCRIPT"] = (
        "Local voice dictation for Linux — your words appear wherever your cursor is."
    )
    py = REPO / "venv-gpu" / "bin" / "python"
    if not py.is_file():
        py = Path(sys.executable)
    proc = subprocess.Popen(
        [str(py), str(REPO / "main.py")],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return proc


def _window_geom(name_substr: str = "Wayfinder Aura") -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) in screenshot-pixel space via wmctrl, if available."""
    try:
        r = subprocess.run(["wmctrl", "-lG"], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    for line in r.stdout.splitlines():
        if name_substr not in line:
            continue
        # id desktop x y w h host title...
        parts = line.split(None, 7)
        if len(parts) < 6:
            continue
        try:
            x, y, w, h = map(int, parts[2:6])
            return x, y, w, h
        except ValueError:
            continue
    return None


def _portal_fullscreen() -> Path:
    """Fullscreen capture via xdg-desktop-portal; returns path to PNG (often 4K)."""
    from gi.repository import Gio, GLib

    loop = GLib.MainLoop()
    result: dict = {}
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    token = f"wfa{int(time.time() * 1000)}"

    def call_done(obj, res, _data=None):
        try:
            out = obj.call_finish(res)
            handle = out.unpack()[0]

            def on_signal(
                _connection,
                _sender_name,
                _object_path,
                _interface_name,
                _signal_name,
                parameters,
                _user_data,
            ):
                response, results = parameters.unpack()
                result["response"] = response
                result["results"] = results
                loop.quit()

            bus.signal_subscribe(
                None,
                "org.freedesktop.portal.Request",
                "Response",
                handle,
                None,
                Gio.DBusSignalFlags.NONE,
                on_signal,
                None,
            )
        except Exception as exc:
            result["error"] = str(exc)
            loop.quit()

    proxy = Gio.DBusProxy.new_sync(
        bus,
        Gio.DBusProxyFlags.NONE,
        None,
        "org.freedesktop.portal.Desktop",
        "/org/freedesktop/portal/desktop",
        "org.freedesktop.portal.Screenshot",
        None,
    )
    opts = {
        "interactive": GLib.Variant("b", False),
        "handle_token": GLib.Variant("s", token),
    }
    proxy.call(
        "Screenshot",
        GLib.Variant("(sa{sv})", ("", opts)),
        Gio.DBusCallFlags.NONE,
        20000,
        None,
        call_done,
        None,
    )
    GLib.timeout_add_seconds(25, loop.quit)
    loop.run()

    if result.get("error"):
        raise RuntimeError(result["error"])
    if result.get("response") != 0:
        raise RuntimeError(f"portal denied: {result}")
    uri = (result.get("results") or {}).get("uri")
    if not uri:
        raise RuntimeError(f"no uri: {result}")
    src = Path(str(uri).removeprefix("file://"))
    if not src.is_file():
        raise RuntimeError(f"missing {src}")
    return src


def _refine_window_bbox(full: Image.Image, geom: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    """Expand/shift wmctrl geom so the full CTk chrome (incl. left sidebar) is inside.

    On KDE HiDPI + XWayland, wmctrl's frame sometimes sits a bit right of the
    painted client, clipping the sidebar. Prefer a content-aware bbox: find the
    darkest UI card near the reported geom and expand to a tight pad.
    """
    fw, fh = full.size
    if geom is None:
        # Center fallback
        w, h = int(fw * 0.45), int(fh * 0.70)
        return (fw - w) // 2, (fh - h) // 2, w, h

    x, y, w, h = geom
    # Generous pad to absorb frame/client mismatch (left bias — sidebar).
    pad_l, pad_r, pad_t, pad_b = int(w * 0.38), int(w * 0.10), int(h * 0.14), int(h * 0.12)
    x0 = max(0, x - pad_l)
    y0 = max(0, y - pad_t)
    x1 = min(fw, x + w + pad_r)
    y1 = min(fh, y + h + pad_b)

    # Optional: tighten to non-bg pixels inside the padded region so we don't
    # keep empty brand backdrop (bg is #0D1117 ≈ (13,17,23)).
    region = full.crop((x0, y0, x1, y1))
    px = region.load()
    rw, rh = region.size
    min_x, min_y, max_x, max_y = rw, rh, 0, 0
    found = False
    for yy in range(0, rh, 2):
        for xx in range(0, rw, 2):
            r, g, b = px[xx, yy][:3]
            # UI surfaces are lighter than pure bg_base; skip pure black backdrop.
            if r + g + b > 55:
                found = True
                if xx < min_x:
                    min_x = xx
                if yy < min_y:
                    min_y = yy
                if xx > max_x:
                    max_x = xx
                if yy > max_y:
                    max_y = yy
    if found and max_x > min_x and max_y > min_y:
        margin = 28
        return (
            max(0, x0 + min_x - margin),
            max(0, y0 + min_y - margin),
            min(fw - (x0 + min_x - margin), max_x - min_x + 2 * margin),
            min(fh - (y0 + min_y - margin), max_y - min_y + 2 * margin),
        )
    return x0, y0, x1 - x0, y1 - y0


def _composite_window(full: Image.Image, geom: tuple[int, int, int, int] | None) -> Image.Image:
    """Crop the app window onto a brand-color 1920×1080 canvas."""
    fw, fh = full.size
    canvas = Image.new("RGB", (TARGET_W, TARGET_H), BG)

    x, y, w, h = _refine_window_bbox(full, geom)
    x = max(0, min(x, fw - 1))
    y = max(0, min(y, fh - 1))
    w = max(1, min(w, fw - x))
    h = max(1, min(h, fh - y))
    crop = full.crop((x, y, x + w, y + h))

    pad = 48
    max_w, max_h = TARGET_W - pad * 2, TARGET_H - pad * 2
    scale = min(max_w / crop.width, max_h / crop.height)
    nw, nh = max(1, int(crop.width * scale)), max(1, int(crop.height * scale))
    crop = crop.resize((nw, nh), Image.Resampling.LANCZOS)
    ox, oy = (TARGET_W - nw) // 2, (TARGET_H - nh) // 2
    canvas.paste(crop, (ox, oy))
    return canvas


def _capture_store_png(out_path: Path, *, window_name: str = "Wayfinder Aura", settle: float = 0.9) -> None:
    time.sleep(settle)
    geom = _window_geom(window_name)
    full_path = _portal_fullscreen()
    with Image.open(full_path) as full:
        full = full.convert("RGB")
        composed = _composite_window(full, geom)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    composed.save(out_path, format="PNG", optimize=True)
    print(f"  wrote {out_path.name} geom={geom} from {full_path.name}")


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if _ping():
            _send("quit")
            time.sleep(0.7)
    except OSError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    PID_FILE.unlink(missing_ok=True)


def _session(welcome_done: bool, shots: list) -> None:
    with tempfile.TemporaryDirectory(prefix="wfa-shot-cfg-") as cfg_root, \
         tempfile.TemporaryDirectory(prefix="wfa-shot-data-") as data_root:
        cfg_home = Path(cfg_root)
        data_home = Path(data_root)
        _write_config(cfg_home / "wayfinder-aura", welcome_done=welcome_done)
        print(f"  profile welcome_completed={welcome_done}")
        backdrop = _start_black_backdrop()
        time.sleep(0.5)
        proc = _launch(cfg_home, data_home)
        try:
            if not _await_socket(50):
                raise RuntimeError("app did not open control socket")
            time.sleep(1.4)
            try:
                _send("show")
            except OSError:
                pass
            time.sleep(0.7)
            for name, action in shots:
                print(f"  capture {name}…")
                action()
                _capture_store_png(OUT / name)
        finally:
            _stop(proc)
            if backdrop is not None:
                try:
                    os.killpg(backdrop.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        backdrop.terminate()
                    except Exception:
                        pass
            time.sleep(0.5)


def main() -> int:
    if not BASE_MODEL.is_file():
        print(f"error: free model not found at {BASE_MODEL}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    print("Stopping any running instance…")
    _quit_existing()

    def go_recording():
        _send("tab:dictate")
        _await_tab("dictate")
        time.sleep(0.5)
        # Ensure idle first, then start a clean recording session.
        st = _status()
        if st and st.get("state") == "RECORDING":
            _send("toggle")
            time.sleep(0.5)
        _send("toggle")
        ok = _await_state("RECORDING", timeout=5.0)
        print(f"    recording state={'ok' if ok else 'NOT REACHED'} status={_status()}")
        time.sleep(1.0)

    def go_settings():
        st = _status()
        if st and st.get("state") == "RECORDING":
            _send("toggle")
            time.sleep(0.4)
        _send("tab:settings")
        _await_tab("settings")
        time.sleep(0.6)

    def go_style():
        _send("tab:style")
        _await_tab("style")
        time.sleep(0.6)

    def go_welcome():
        time.sleep(1.6)  # main.py schedules welcome ~800ms after map
        try:
            _send("tab:dictate")
        except OSError:
            pass
        time.sleep(0.9)

    print("Session A — returning free user…")
    _session(
        welcome_done=True,
        shots=[
            ("main-window.png", go_recording),
            ("settings.png", go_settings),
            ("style.png", go_style),
            # Overlay product still: same recording UI (floating indicator is
            # tiny; the in-window recording chrome is the marketable surface).
            ("overlay.png", go_recording),
        ],
    )

    print("Session B — welcome…")
    _session(welcome_done=False, shots=[("welcome.png", go_welcome)])

    print("Done.")
    missing = False
    for name in (
        "main-window.png",
        "settings.png",
        "overlay.png",
        "style.png",
        "welcome.png",
    ):
        p = OUT / name
        if not p.is_file():
            print(f"  MISSING {name}", file=sys.stderr)
            missing = True
            continue
        with Image.open(p) as im:
            print(f"  {name}: {im.size[0]}x{im.size[1]}")
            if im.size != (TARGET_W, TARGET_H):
                missing = True
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())

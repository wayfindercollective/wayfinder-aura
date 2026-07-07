#!/usr/bin/env python3
"""Non-invasive host preflight for Wayfinder Aura release signoff.

This does not click keys or inject text. It verifies the machine-level
prerequisites for the remaining manual QA: Wayland/X11 session signals, host
injection tools, ydotool daemon, Wayfinder control socket, Flatpak access, and
Vulkan GPU visibility. When launched from a foreign Flatpak-hosted editor, it
uses flatpak-spawn for host-only commands.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

WAYFINDER_APP_IDS = {"io.wayfindercollective.WayfinderAura"}


def should_use_flatpak_spawn(env: dict[str, str] | None = None) -> bool:
    env = env or os.environ
    flatpak_id = env.get("FLATPAK_ID", "")
    return bool(flatpak_id and flatpak_id not in WAYFINDER_APP_IDS and shutil.which("flatpak-spawn"))


def run_shell(script: str, *, host: bool = False, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    cmd = ["sh", "-lc", script]
    if host:
        cmd = ["flatpak-spawn", "--host", *cmd]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def command_stdout(script: str, *, host: bool = False, timeout: int = 10) -> str:
    result = run_shell(script, host=host, timeout=timeout)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def command_exists(name: str, *, host: bool = False) -> str:
    return command_stdout(f"command -v {name} || true", host=host)


def socket_ping(path: Path, timeout: float = 1.0) -> str:
    if not path.exists():
        return "missing"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(b"ping")
            return "pong" if client.recv(16) == b"pong" else "unexpected-reply"
    except OSError:
        return "unreachable"


def host_socket_ping(path: Path, timeout: float = 1.0) -> str:
    script = f"""
import pathlib
import socket

path = pathlib.Path({str(path)!r})
if not path.exists():
    print("missing")
    raise SystemExit(0)
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout({timeout!r})
        client.connect(str(path))
        client.sendall(b"ping")
        print("pong" if client.recv(16) == b"pong" else "unexpected-reply")
except OSError:
    print("unreachable")
"""
    result = run_shell(f"python3 - <<'PY'\n{script}\nPY", host=True, timeout=5)
    if result.returncode != 0:
        return "unreachable"
    return result.stdout.strip() or "unreachable"


def parse_vulkan_devices(summary: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if line.startswith("GPU") and line.endswith(":"):
            if current:
                devices.append(current)
            current = {"slot": line[:-1]}
            continue
        if current is None or "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if key in {"deviceType", "deviceName", "driverName", "driverInfo"}:
            current[key] = value
    if current:
        devices.append(current)
    return devices


def detect_dedicated_gpu(lspci_text: str, vulkan_devices: list[dict[str, str]]) -> dict[str, Any]:
    discrete = [device for device in vulkan_devices if device.get("deviceType") == "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU"]
    if discrete:
        return {"present": True, "source": "vulkan", "devices": discrete}

    likely = []
    for line in lspci_text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ("nvidia", "radeon rx", "arc ")) and "vga" in lowered:
            likely.append(line)
    return {"present": bool(likely), "source": "lspci", "devices": likely}


def collect_preflight() -> dict[str, Any]:
    host = should_use_flatpak_spawn()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%s" % os.getuid())
    wayfinder_socket = Path(runtime_dir) / "wayfinder-aura" / "wayfinder-aura.sock"

    session = {
        "XDG_SESSION_TYPE": os.environ.get("XDG_SESSION_TYPE", ""),
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", ""),
        "DISPLAY": os.environ.get("DISPLAY", ""),
        "XDG_CURRENT_DESKTOP": os.environ.get("XDG_CURRENT_DESKTOP", ""),
        "KDE_FULL_SESSION": os.environ.get("KDE_FULL_SESSION", ""),
        "FLATPAK_ID": os.environ.get("FLATPAK_ID", ""),
        "using_flatpak_spawn_host": host,
    }

    tools = {name: command_exists(name, host=host) for name in ("wtype", "xdotool", "ydotool", "flatpak")}
    ydotool_socket = command_stdout("test -S /run/ydotool/ydotool.sock && echo present || echo missing", host=host)
    lspci_text = command_stdout("lspci -nn | grep -Ei 'vga|3d|display' || true", host=host)
    vulkan_summary = command_stdout("vulkaninfo --summary 2>/dev/null || true", host=host, timeout=20)
    vulkan_devices = parse_vulkan_devices(vulkan_summary)

    os_release = command_stdout("cat /etc/os-release 2>/dev/null || true", host=host)
    is_steamos = "ID=steamos" in os_release or "VARIANT_ID=steamdeck" in os_release

    return {
        "session": session,
        "tools": tools,
        "ydotool_socket": ydotool_socket,
        "wayfinder_socket": {
            "path": str(wayfinder_socket),
            "ping": host_socket_ping(wayfinder_socket) if host else socket_ping(wayfinder_socket),
        },
        "gpu": {
            "lspci": lspci_text.splitlines(),
            "vulkan_devices": vulkan_devices,
            "dedicated_gpu": detect_dedicated_gpu(lspci_text, vulkan_devices),
        },
        "steam_deck": {"detected": is_steamos},
    }


def summarize(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    session = report["session"]
    session_type = session.get("XDG_SESSION_TYPE") or ("wayland" if session.get("WAYLAND_DISPLAY") else "")
    desktop = session.get("XDG_CURRENT_DESKTOP") or "unknown"
    rows: list[tuple[str, str, str]] = []

    rows.append(("OK" if session_type in {"wayland", "x11"} else "WARN", "session", f"{session_type or 'unknown'} / {desktop}"))
    if session.get("FLATPAK_ID") and session.get("FLATPAK_ID") not in WAYFINDER_APP_IDS:
        detail = f"foreign Flatpak parent {session['FLATPAK_ID']}; host checks via flatpak-spawn={session['using_flatpak_spawn_host']}"
        rows.append(("OK" if session.get("using_flatpak_spawn_host") else "WARN", "host access", detail))

    tools = report["tools"]
    for tool in ("wtype", "xdotool", "ydotool", "flatpak"):
        rows.append(("OK" if tools.get(tool) else "WARN", f"host tool {tool}", tools.get(tool) or "missing"))

    rows.append(("OK" if report["ydotool_socket"] == "present" else "WARN", "ydotool socket", report["ydotool_socket"]))
    wayfinder_ping = report["wayfinder_socket"]["ping"]
    rows.append(("OK" if wayfinder_ping == "pong" else "WARN", "Wayfinder control socket", wayfinder_ping))

    dgpu = report["gpu"]["dedicated_gpu"]
    if dgpu["present"]:
        rows.append(("OK", "dedicated GPU", json.dumps(dgpu["devices"], ensure_ascii=False)))
    else:
        rows.append(("WARN", "dedicated GPU", "not detected"))

    vulkan_devices = report["gpu"]["vulkan_devices"]
    rows.append(("OK" if vulkan_devices else "WARN", "Vulkan devices", json.dumps(vulkan_devices, ensure_ascii=False)))
    rows.append(("INFO", "Steam Deck", "detected" if report["steam_deck"]["detected"] else "not detected on this host"))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="exit nonzero if a prerequisite is WARN")
    args = parser.parse_args(argv)

    report = collect_preflight()
    rows = summarize(report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("Wayfinder Aura ship preflight")
        for status, name, detail in rows:
            print(f"[{status:4}] {name}: {detail}")

    if args.strict and any(status == "WARN" for status, _name, _detail in rows):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

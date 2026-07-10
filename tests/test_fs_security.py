"""Tests for owner-only filesystem helpers and security hardening entry points."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path
from queue import Queue
from threading import Event
from unittest.mock import MagicMock, patch

import pytest


class TestAtomicWrite:
    def test_atomic_write_json_mode_0600(self, tmp_path: Path):
        from wayfinder.utils.fs_security import atomic_write_json

        target = tmp_path / "secret.json"
        atomic_write_json(target, {"a": 1}, mode=0o600)
        assert target.exists()
        assert (target.stat().st_mode & 0o777) == 0o600
        assert json.loads(target.read_text()) == {"a": 1}

    def test_restrict_owner_only_repairs_644(self, tmp_path: Path):
        from wayfinder.utils.fs_security import restrict_owner_only

        p = tmp_path / "x.json"
        p.write_text("{}")
        os.chmod(p, 0o644)
        restrict_owner_only(p)
        assert (p.stat().st_mode & 0o777) == 0o600


class TestAppTempDir:
    def test_get_app_temp_dir_is_0700(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        from wayfinder.utils.fs_security import get_app_temp_dir, cleanup_app_temp_dir

        d = get_app_temp_dir()
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700
        f = d / "orphan.wav"
        f.write_bytes(b"RIFF")
        assert cleanup_app_temp_dir() >= 1
        assert not f.exists()


class TestOwnerOnlyLogging:
    def test_configure_logging_file_is_0600(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
        # Reset logging config state
        import wayfinder.utils.logging as logmod

        logmod._configured = False
        root = __import__("logging").getLogger("wayfinder")
        root.handlers.clear()

        logmod.configure_logging(log_to_console=False, log_to_file=True)
        log_file = tmp_path / "cache" / "wayfinder-aura" / "logs" / "wayfinder.log"
        assert log_file.exists()
        assert (log_file.stat().st_mode & 0o777) == 0o600

    def test_rollover_keeps_owner_only(self, tmp_path: Path):
        from wayfinder.utils.logging import OwnerOnlyRotatingFileHandler
        import logging

        path = tmp_path / "roll.log"
        handler = OwnerOnlyRotatingFileHandler(
            path, maxBytes=50, backupCount=2, encoding="utf-8"
        )
        logger = logging.getLogger("wayfinder_test_roll")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        logger.propagate = False
        for i in range(30):
            logger.info("x" * 20 + str(i))
        handler.close()
        assert (path.stat().st_mode & 0o777) == 0o600
        rotated = Path(str(path) + ".1")
        if rotated.exists():
            assert (rotated.stat().st_mode & 0o777) == 0o600


class TestOverlayProcessHelpers:
    def test_is_our_overlay_pid_rejects_wrong_uid_or_cmd(self):
        from wayfinder.utils.overlay_process import is_our_overlay_pid

        # Our own shell/python pid is not an overlay script
        assert is_our_overlay_pid(os.getpid()) is False

    def test_pkill_overlay_absent_from_wayfinder_main(self):
        """Regression: no pattern-based overlay kills (including split constructs)."""
        root = Path(__file__).resolve().parents[1]
        main = (root / "wayfinder_main.py").read_text(encoding="utf-8")
        # Executable-looking pkill invocations (ignore comments mentioning the word).
        bad = []
        for i, line in enumerate(main.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            code = stripped.split("#")[0]
            if "pkill" in code and "overlay" in code:
                bad.append((i, stripped))
            if "pkill" in code and "overlay.py" in code:
                bad.append((i, stripped))
        assert bad == [], f"pkill+overlay still present: {bad}"
        assert '["pkill"' not in main
        assert '["pkill",' not in main
        assert "-f\", \"overlay" not in main
        assert "-f', 'overlay" not in main


class TestSocketModeReal:
    def test_socket_bind_mode_0600(self, tmp_path: Path, monkeypatch):
        sock_dir = tmp_path / "wayfinder-aura"
        sock_path = sock_dir / "wayfinder-aura.sock"
        monkeypatch.setattr("wayfinder.hotkeys.socket.SOCKET_PATH", str(sock_path))
        monkeypatch.setattr("wayfinder.config.SOCKET_PATH", str(sock_path))

        from wayfinder.hotkeys.socket import socket_listener
        from wayfinder.hotkeys.types import EventType

        q: Queue = Queue()
        stop = Event()

        t = threading.Thread(
            target=socket_listener, args=(q, stop, None), daemon=True
        )
        t.start()
        # Wait for bind
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.05)
        assert sock_path.exists(), "socket was not created"
        mode = sock_path.stat().st_mode & 0o777
        assert mode == 0o600, f"socket mode {oct(mode)}"
        assert (sock_dir.stat().st_mode & 0o777) == 0o700

        # ping
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(str(sock_path))
        client.sendall(b"ping")
        assert client.recv(16) == b"pong"
        client.close()

        stop.set()
        t.join(timeout=3)

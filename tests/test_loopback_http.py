"""Loopback requests must never pass through an HTTP proxy on macOS."""
import http.server
import sys
import threading
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.utils import loopback_http  # noqa: E402


def _serve(body: bytes, hits: list):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def servers():
    origin_hits, proxy_hits = [], []
    origin = _serve(b"origin", origin_hits)
    proxy = _serve(b"PROXY", proxy_hits)
    yield origin, proxy, origin_hits, proxy_hits
    origin.shutdown()
    proxy.shutdown()


def test_macos_bypasses_a_configured_proxy(servers, monkeypatch):
    origin, proxy, origin_hits, proxy_hits = servers
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    url = f"http://127.0.0.1:{origin.server_port}/inference"
    # Sanity: plain urlopen really would have used the proxy.
    assert urllib.request.urlopen(url, timeout=5).read() == b"PROXY"
    proxy_hits.clear()
    with patch.object(loopback_http.sys, "platform", "darwin"):
        assert loopback_http.urlopen_loopback(url, timeout=5).read() == b"origin"
    assert proxy_hits == [] and origin_hits == ["/inference"]


def test_no_proxy_configured_uses_plain_urlopen(monkeypatch):
    calls = []
    monkeypatch.setattr(loopback_http.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(loopback_http.urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append((req, timeout)) or "ok")
    with patch.object(loopback_http.sys, "platform", "darwin"):
        assert loopback_http.urlopen_loopback("http://127.0.0.1:1/", timeout=2) == "ok"
    assert calls == [("http://127.0.0.1:1/", 2)]


def test_linux_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(loopback_http.urllib.request, "getproxies",
                        lambda: {"http": "http://proxy:3128"})
    monkeypatch.setattr(loopback_http.urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append(req) or "ok")
    with patch.object(loopback_http.sys, "platform", "linux"):
        assert loopback_http.urlopen_loopback("http://127.0.0.1:1/") == "ok"
    assert calls == ["http://127.0.0.1:1/"]

"""Loopback requests must never pass through an HTTP proxy."""
import http.server
import sys
import threading
import urllib.request
from pathlib import Path

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

    class Server(http.server.HTTPServer):
        def server_bind(self):  # skip the reverse-DNS getfqdn() stall
            import socketserver
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    server = Server(("127.0.0.1", 0), Handler)
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


def test_bypasses_an_environment_proxy(servers, monkeypatch):
    """Linux and macOS: http_proxy applies to 127.0.0.1 unless no_proxy lists it."""
    origin, proxy, origin_hits, proxy_hits = servers
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    url = f"http://127.0.0.1:{origin.server_port}/inference"
    # Sanity: a default opener really would have used the proxy. (Built fresh:
    # urlopen caches its opener, and so its proxy settings, on first use.)
    assert urllib.request.build_opener().open(url, timeout=5).read() == b"PROXY"
    proxy_hits.clear()
    assert loopback_http.urlopen_loopback(url, timeout=5).read() == b"origin"
    assert proxy_hits == [] and origin_hits == ["/inference"]


def test_no_proxy_configured_uses_plain_urlopen(monkeypatch):
    calls = []
    monkeypatch.setattr(loopback_http.urllib.request, "getproxies", lambda: {})
    monkeypatch.setattr(loopback_http.urllib.request, "urlopen",
                        lambda req, timeout=None: calls.append((req, timeout)) or "ok")
    assert loopback_http.urlopen_loopback("http://127.0.0.1:1/", timeout=2) == "ok"
    assert calls == [("http://127.0.0.1:1/", 2)]


def test_bypasses_a_system_proxy(servers, monkeypatch):
    """Windows registry / macOS System Settings proxies arrive via getproxies()."""
    origin, proxy, origin_hits, proxy_hits = servers
    monkeypatch.setattr(loopback_http.urllib.request, "getproxies",
                        lambda: {"http": f"http://127.0.0.1:{proxy.server_port}"})
    url = f"http://127.0.0.1:{origin.server_port}/inference"
    assert loopback_http.urlopen_loopback(url, timeout=5).read() == b"origin"
    assert proxy_hits == [] and origin_hits == ["/inference"]


def test_unreadable_proxy_settings_bypass(servers, monkeypatch):
    origin, _proxy, origin_hits, _proxy_hits = servers

    def broken():
        raise OSError("proxy settings unreadable")

    monkeypatch.setattr(loopback_http.urllib.request, "getproxies", broken)
    url = f"http://127.0.0.1:{origin.server_port}/inference"
    assert loopback_http.urlopen_loopback(url, timeout=5).read() == b"origin"
    assert origin_hits == ["/inference"]

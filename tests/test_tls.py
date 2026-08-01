"""Portable TLS trust-store selection and packaged probe coverage."""

from __future__ import annotations

import urllib.error
from pathlib import Path

from wayfinder import tls


def _bundle(path: Path) -> Path:
    path.write_text("-----BEGIN CERTIFICATE-----\ntest\n", encoding="utf-8")
    return path


def test_configure_tls_uses_packaged_bundle_for_all_python_clients(tmp_path: Path):
    bundled = _bundle(tmp_path / "cacert.pem")
    env: dict[str, str] = {}

    selected = tls.configure_tls_ca_bundle(environ=env, candidates=[bundled])

    assert selected == bundled
    assert env["SSL_CERT_FILE"] == str(bundled)
    assert env["REQUESTS_CA_BUNDLE"] == str(bundled)


def test_configure_tls_respects_valid_override_and_repairs_missing_peer(tmp_path: Path):
    custom = _bundle(tmp_path / "custom.pem")
    bundled = _bundle(tmp_path / "bundled.pem")
    env = {"SSL_CERT_FILE": str(custom)}

    selected = tls.configure_tls_ca_bundle(environ=env, candidates=[bundled])

    assert selected == custom
    assert env["SSL_CERT_FILE"] == str(custom)
    assert env["REQUESTS_CA_BUNDLE"] == str(custom)


def test_configure_tls_replaces_stale_environment_paths(tmp_path: Path):
    bundled = _bundle(tmp_path / "cacert.pem")
    env = {
        "SSL_CERT_FILE": str(tmp_path / "missing-ssl.pem"),
        "REQUESTS_CA_BUNDLE": str(tmp_path / "missing-requests.pem"),
    }

    selected = tls.configure_tls_ca_bundle(environ=env, candidates=[bundled])

    assert selected == bundled
    assert env == {
        "SSL_CERT_FILE": str(bundled),
        "REQUESTS_CA_BUNDLE": str(bundled),
    }


def test_probe_treats_authenticated_http_error_as_success(tmp_path: Path, monkeypatch):
    bundled = _bundle(tmp_path / "cacert.pem")
    monkeypatch.setattr(tls, "configure_tls_ca_bundle", lambda: bundled)

    def forbidden(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://cdn.example",
            403,
            "Forbidden",
            {},
            None,
        )

    monkeypatch.setattr(tls.urllib.request, "urlopen", forbidden)

    assert tls.probe_tls("https://cdn.example") == (403, bundled)

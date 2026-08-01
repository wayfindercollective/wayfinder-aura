"""Portable TLS certificate configuration for packaged Wayfinder Aura builds.

PyInstaller bundles Python and OpenSSL from the build runner. OpenSSL's
compiled-in default certificate paths therefore describe the build distro, not
necessarily the machine running the AppImage (Ubuntu paths on Bazzite/Fedora is
the common failure). ``certifi`` is already bundled, so point every Python HTTP
client at that verified CA bundle before the first network request.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, MutableMapping


TLS_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
DEFAULT_TLS_PROBE_URL = "https://wayfinder-models-cdn.peter-7b5.workers.dev"


def _usable_ca_file(value: str | os.PathLike[str] | None) -> Path | None:
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def ca_bundle_candidates(bundle_root: Path | None = None) -> list[Path]:
    """Return portable CA candidates in strongest-to-weakest preference order."""
    candidates: list[Path] = []

    if bundle_root is None and getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    if bundle_root is not None:
        candidates.append(Path(bundle_root) / "certifi" / "cacert.pem")

    try:
        import certifi

        candidates.append(Path(certifi.where()))
    except (ImportError, OSError):
        pass

    # Source installs and unusual sandbox runtimes can use either distro family.
    candidates.extend(
        (
            Path("/etc/ssl/certs/ca-certificates.crt"),
            Path("/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"),
            Path("/etc/ssl/cert.pem"),
        )
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def configure_tls_ca_bundle(
    *,
    environ: MutableMapping[str, str] | None = None,
    candidates: Iterable[Path] | None = None,
) -> Path | None:
    """Configure urllib, requests, and httpx to use one readable CA bundle.

    Existing valid overrides are respected. Missing or stale overrides are
    repaired from the embedded certifi bundle first, then common system paths.
    TLS verification is never disabled.
    """
    env = os.environ if environ is None else environ

    selected = next(
        (
            path
            for name in TLS_CA_ENV_VARS
            if (path := _usable_ca_file(env.get(name))) is not None
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                path
                for candidate in (candidates if candidates is not None else ca_bundle_candidates())
                if (path := _usable_ca_file(candidate)) is not None
            ),
            None,
        )
    if selected is None:
        return None

    selected_text = str(selected)
    for name in TLS_CA_ENV_VARS:
        if _usable_ca_file(env.get(name)) is None:
            env[name] = selected_text
    return selected


def probe_tls(url: str = DEFAULT_TLS_PROBE_URL, timeout: float = 15.0) -> tuple[int, Path]:
    """Complete a verified TLS handshake and return its HTTP status and CA path.

    The production model CDN intentionally rejects unauthenticated requests.
    Any HTTP response, including 401/403, proves DNS + TCP + certificate
    verification completed; network and TLS errors still propagate.
    """
    ca_bundle = configure_tls_ca_bundle()
    if ca_bundle is None:
        raise RuntimeError("No readable TLS CA bundle is available")

    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "Wayfinder-Aura-TLS-Probe/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), ca_bundle
    except urllib.error.HTTPError as exc:
        return int(exc.code), ca_bundle

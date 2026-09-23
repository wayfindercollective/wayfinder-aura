"""Open requests to Aura's own loopback servers without any HTTP proxy.

``urllib.request.urlopen`` applies the system proxy on macOS, and the default
macOS bypass list (``*.local, 169.254/16``) does not cover 127.0.0.1 or
localhost. With a proxy configured (Charles, Proxyman, a corporate PAC/proxy)
the dictation audio POSTed to whisper-server and the text sent to the cleanup
server were routed through it — a privacy leak, and a broken dictation when
the proxy cannot reach the user's loopback.

macOS: when a proxy is configured, loopback requests go through an opener that
has no proxy handler. With no proxy configured (the usual case) the plain
``urllib.request.urlopen`` is used, so behaviour is unchanged. Other platforms
always use ``urlopen`` unchanged (Linux follow-up noted in
docs/LINUX-FOLLOWUPS-FROM-MACOS.md).
"""

from __future__ import annotations

import sys
import urllib.request

_NO_PROXY_OPENER = None


def _no_proxy_opener():
    global _NO_PROXY_OPENER
    if _NO_PROXY_OPENER is None:
        _NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _NO_PROXY_OPENER


def urlopen_loopback(request, timeout=None):
    """``urlopen`` for http://127.0.0.1 servers Aura itself started."""
    if sys.platform == "darwin":
        try:
            proxied = bool(urllib.request.getproxies())
        except Exception:
            proxied = True  # cannot tell: never risk sending audio to a proxy
        if proxied:
            return _no_proxy_opener().open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)

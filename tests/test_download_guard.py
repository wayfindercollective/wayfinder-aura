"""Size bounds and https-only transport for model downloads.

These guards bound what a bad origin can do *before* the digest gets a chance to
reject it: stream more bytes than the model has, or redirect the transfer to
plaintext http (where `requests` would keep an Authorization header on a
same-host downgrade).
"""

from __future__ import annotations

import pytest

from wayfinder.core.download_guard import (
    DownloadBounds,
    DownloadSizeMismatch,
    InsecureRedirect,
    https_only_get,
    require_https,
)


class TestRequireHttps:
    def test_accepts_https(self):
        require_https("https://cdn.example/v1/objects/whisper/m.bin")

    @pytest.mark.parametrize(
        "url",
        [
            "http://cdn.example/m.bin",
            "file:///etc/passwd",
            "ftp://cdn.example/m.bin",
            "HTTP://cdn.example/m.bin",
        ],
    )
    def test_rejects_everything_else(self, url):
        with pytest.raises(InsecureRedirect):
            require_https(url)


class TestDownloadBounds:
    def test_trusted_size_wins_over_a_lying_content_length(self):
        bounds = DownloadBounds(1000)
        with pytest.raises(DownloadSizeMismatch, match="server offered"):
            bounds.check_header(999)

    def test_agreeing_sizes_pass(self):
        assert DownloadBounds(1000).check_header(1000) == 1000

    def test_unknown_content_length_falls_back_to_trusted(self):
        assert DownloadBounds(1000).check_header(0) == 1000

    def test_no_trusted_size_uses_content_length(self):
        bounds = DownloadBounds(None)
        assert bounds.check_header(4242) == 4242
        bounds.check_progress(4242)
        bounds.check_complete(4242)

    def test_endless_stream_is_cut_off_at_the_limit(self):
        """A compromised origin must not be able to fill the disk pre-hash."""
        bounds = DownloadBounds(1000)
        bounds.check_header(1000)
        bounds.check_progress(1000)
        with pytest.raises(DownloadSizeMismatch, match="exceeded"):
            bounds.check_progress(1001)

    def test_short_transfer_is_rejected_before_hashing(self):
        bounds = DownloadBounds(1000)
        bounds.check_header(1000)
        with pytest.raises(DownloadSizeMismatch, match="incomplete"):
            bounds.check_complete(999)

    def test_an_absurd_declared_size_does_not_raise_the_ceiling(self):
        """A catalog-supplied size is only as trustworthy as the catalog.

        Without this, a hostile entry declares a multi-terabyte bound and the
        digest — which only runs after the whole transfer — is far too late to
        stop the disk filling.
        """
        bounds = DownloadBounds(50 << 40)  # 50 TiB
        bounds.check_progress(DownloadBounds.MAX_UNBOUNDED_BYTES)
        with pytest.raises(DownloadSizeMismatch, match="exceeded"):
            bounds.check_progress(DownloadBounds.MAX_UNBOUNDED_BYTES + 1)

    def test_content_length_bounds_the_stream_when_no_trusted_size(self):
        bounds = DownloadBounds(None)
        bounds.check_header(500)
        with pytest.raises(DownloadSizeMismatch, match="exceeded"):
            bounds.check_progress(501)


class _Resp:
    def __init__(self, status_code, url, location=None):
        self.status_code = status_code
        self.url = url
        self.headers = {"Location": location} if location else {}
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    """Records every URL fetched so redirect handling can be asserted."""

    def __init__(self, script):
        self._script = list(script)
        self.seen = []

    def get(self, url, **kwargs):
        self.seen.append((url, kwargs.get("allow_redirects")))
        return self._script.pop(0)


class TestHttpsOnlyGet:
    def test_returns_a_non_redirect_response(self):
        final = _Resp(200, "https://cdn.example/m.bin")
        session = _Session([final])
        assert https_only_get(session, "https://cdn.example/m.bin", timeout=5) is final
        # redirects are followed by hand, never by requests
        assert session.seen == [("https://cdn.example/m.bin", False)]

    def test_follows_an_https_redirect(self):
        session = _Session([
            _Resp(302, "https://cdn.example/m.bin", "https://r2.example/blob"),
            _Resp(200, "https://r2.example/blob"),
        ])
        response = https_only_get(session, "https://cdn.example/m.bin", timeout=5)
        assert response.status_code == 200
        assert [u for u, _ in session.seen] == [
            "https://cdn.example/m.bin",
            "https://r2.example/blob",
        ]

    def test_refuses_a_downgrade_to_http(self):
        session = _Session([
            _Resp(302, "https://cdn.example/m.bin", "http://cdn.example/m.bin"),
        ])
        with pytest.raises(InsecureRedirect):
            https_only_get(session, "https://cdn.example/m.bin", timeout=5)
        # the plaintext hop is never requested
        assert [u for u, _ in session.seen] == ["https://cdn.example/m.bin"]

    def test_resolves_a_relative_location(self):
        session = _Session([
            _Resp(302, "https://cdn.example/v1/m.bin", "/blobs/m.bin"),
            _Resp(200, "https://cdn.example/blobs/m.bin"),
        ])
        https_only_get(session, "https://cdn.example/v1/m.bin", timeout=5)
        assert session.seen[-1][0] == "https://cdn.example/blobs/m.bin"

    def test_refuses_a_non_https_starting_url(self):
        session = _Session([])
        with pytest.raises(InsecureRedirect):
            https_only_get(session, "http://cdn.example/m.bin", timeout=5)
        assert session.seen == []

    def test_gives_up_on_a_redirect_loop(self):
        loop = [
            _Resp(302, "https://cdn.example/m.bin", "https://cdn.example/m.bin")
            for _ in range(10)
        ]
        with pytest.raises(DownloadSizeMismatch, match="too many redirects"):
            https_only_get(_Session(loop), "https://cdn.example/m.bin", timeout=5)

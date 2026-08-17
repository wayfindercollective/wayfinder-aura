"""Shared guards for model downloads: size bounds and https-only transport.

Three downloaders fetch model weights (the whisper panel's `ModelDownloader`,
the inline llama.cpp downloader, and `core/setup.py::_download_model_file`).
They are structurally different — urllib vs requests, different progress
plumbing — but they need identical rules, so the rules live here rather than in
three copies that drift:

* **Size is bounded before the hash.** A digest proves what arrived, but only
  after it has all arrived. An origin that streams endlessly would burn a
  customer's bandwidth and disk before verification ever fails, so a stream that
  exceeds the size we shipped is cut off immediately (2026-08-17 audit review).
* **A download never leaves https.** Both clients follow redirects; without a
  check, "https catalog URL" can be redirected to plaintext http, and on a
  same-host downgrade `requests` would keep the Authorization header.

Neither guard replaces the digest check — they bound the damage a bad origin can
do before the digest gets a chance to reject it.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


class InsecureRedirect(ValueError):
    """A download tried to leave https."""


class DownloadSizeMismatch(ValueError):
    """The transfer's size disagreed with what we expected.

    This is a *transport* failure — unlike a digest mismatch it is worth one
    automatic retry, because a truncated body is usually a dropped connection.
    """


def require_https(url: str) -> None:
    """Raise InsecureRedirect unless *url* is https."""
    if urlparse(url).scheme.lower() != "https":
        raise InsecureRedirect(f"refusing a non-https download URL: {url}")


class DownloadBounds:
    """Enforces the expected byte count across a streaming download.

    ``trusted_size`` is the size we shipped in the catalog (authoritative when
    present). ``content_length`` is what the server claims. When we have both
    and they disagree, the object is not the one we pinned and there is no point
    transferring it at all.
    """

    #: Backstop for a stream with neither a trusted size nor a Content-Length
    #: (chunked transfer). The largest model we ship is ~3.1 GB, so this only
    #: ever trips on something that has no business being a model file — but
    #: without it such a stream is unbounded until the disk fills.
    MAX_UNBOUNDED_BYTES = 8 << 30  # 8 GiB

    def __init__(self, trusted_size: Optional[int] = None):
        self.trusted_size = trusted_size if (trusted_size or 0) > 0 else None
        # A size that came from a remote catalog is only as trustworthy as that
        # catalog, so the ceiling applies to it too — otherwise a hostile entry
        # declares a multi-terabyte bound and the digest, which only runs after
        # the transfer, is far too late to prevent filling the disk.
        if self.trusted_size and self.trusted_size > self.MAX_UNBOUNDED_BYTES:
            self.trusted_size = None
        self.limit: Optional[int] = self.trusted_size

    def check_header(self, content_length: Optional[int]) -> Optional[int]:
        """Reconcile the server's Content-Length with the trusted size.

        Returns the total to use for progress display, or None if unknown.
        """
        declared = content_length if (content_length or 0) > 0 else None
        if self.trusted_size and declared and declared != self.trusted_size:
            raise DownloadSizeMismatch(
                f"server offered {declared} bytes but this model is "
                f"{self.trusted_size} bytes"
            )
        if self.limit is None:
            self.limit = declared
        return self.trusted_size or declared

    def check_progress(self, downloaded: int) -> None:
        """Abort a stream that has already produced more than it should."""
        ceiling = min(
            self.limit if self.limit is not None else self.MAX_UNBOUNDED_BYTES,
            self.MAX_UNBOUNDED_BYTES,
        )
        if downloaded > ceiling:
            raise DownloadSizeMismatch(
                f"download exceeded the expected {ceiling} bytes — aborting"
            )

    def check_complete(self, downloaded: int) -> None:
        """Reject a short transfer before anything is hashed or renamed."""
        expected = self.trusted_size or self.limit
        if expected is not None and downloaded != expected:
            raise DownloadSizeMismatch(
                f"incomplete download ({downloaded} of {expected} bytes) — "
                "the connection dropped"
            )


def https_only_get(
    session, url: str, *, timeout, max_redirects: int = 5, headers_for=None
):
    """`requests` GET that follows redirects itself, refusing any non-https hop.

    `allow_redirects=True` would follow a downgrade to http before we could see
    it — and on a same-host downgrade `requests` keeps the Authorization header,
    which would put a license bearer on the wire in clear text.

    ``session`` is anything exposing ``.get`` — a ``requests.Session`` or the
    ``requests`` module itself.

    ``headers_for(url)`` is recomputed for **every hop**, which is what keeps a
    license bearer off a third-party host: putting Authorization on the session
    would reattach it to each redirect target, and stripping it from an
    already-sent request is too late. Callers pass the same helper that decides
    the bearer belongs only on the Models CDN origin.
    """
    require_https(url)
    for _ in range(max_redirects + 1):
        headers = headers_for(url) if headers_for is not None else None
        response = session.get(
            url, stream=True, timeout=timeout, allow_redirects=False, headers=headers
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            raise DownloadSizeMismatch("redirect without a Location header")
        url = response_url_join(response.url, location)
        require_https(url)
    raise DownloadSizeMismatch("too many redirects")


def response_url_join(base: str, location: str) -> str:
    """Resolve a possibly-relative Location against the URL that returned it."""
    from urllib.parse import urljoin

    return urljoin(base, location)


class HttpsOnlyRedirectHandler:
    """Mixin for urllib's HTTPRedirectHandler enforcing https on every hop.

    Used by the whisper downloader, which additionally strips Authorization on
    cross-origin redirects; this only adds the scheme rule.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

"""ModelDownloader install/remove/re-download hygiene (no network)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# Import after path is the repo root (pytest cwd)
import wayfinder_main as wm


@pytest.fixture
def dl(tmp_path, monkeypatch):
    """Downloader pinned to an isolated writable models dir."""
    models = tmp_path / "whisper-models"
    models.mkdir()
    monkeypatch.setattr(wm, "_get_whisper_models_dir", lambda: models)
    # Only search our temp dir (+ a fake bundled dir we control)
    bundled = tmp_path / "app-share"
    bundled.mkdir()
    monkeypatch.setattr(
        wm,
        "_whisper_model_search_dirs",
        lambda: [models, bundled],
    )
    d = wm.ModelDownloader(models_dir=models)
    return d, models, bundled


def _touch(path: Path, data: bytes = b"ggml-fake"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_is_installed_finds_download_and_bundled(dl):
    d, models, bundled = dl
    mid = "base.en"
    filename = wm.WHISPER_CPP_MODELS[mid]["filename"]
    assert d.is_installed(mid) is False
    _touch(models / filename)
    assert d.is_installed(mid) is True
    assert d.is_removable(mid) is True


def test_bundled_only_not_removable(dl):
    d, models, bundled = dl
    mid = "base.en"
    filename = wm.WHISPER_CPP_MODELS[mid]["filename"]
    # Pretend bundled is under /app by patching _is_bundled_dir
    _touch(bundled / filename)
    with patch.object(wm.ModelDownloader, "_is_bundled_dir", return_value=True):
        # is_installed uses _resolve which finds bundled
        assert d.is_installed(mid) is True
        assert d.is_removable(mid) is False
        result = d.delete_model(mid)
        assert result["ok"] is False
        assert result["skipped_bundled"] is True
        assert (bundled / filename).exists()


def test_delete_removes_writable_and_partial(dl):
    d, models, bundled = dl
    mid = "small.en"
    filename = wm.WHISPER_CPP_MODELS[mid]["filename"]
    full = models / filename
    partial = models / f"{filename}.downloading"
    _touch(full, b"full")
    _touch(partial, b"partial")
    result = d.delete_model(mid)
    assert result["ok"] is True
    assert not full.exists()
    assert not partial.exists()
    assert d.is_installed(mid) is False
    assert d.is_removable(mid) is False


def test_delete_unknown_model(dl):
    d, _, _ = dl
    result = d.delete_model("not-a-real-model")
    assert result["ok"] is False
    assert "Unknown" in (result["error"] or "")


def _unpin_digest(monkeypatch, model_id):
    """Drop the entry's pinned sha256 and size so a stub payload can stand in.

    Both the live catalog and the shipped snapshot have to be patched: the
    snapshot is what supplies the trusted size bound and the fallback URL, and
    it is deliberately immune to catalog edits.
    """
    entry = {
        k: v
        for k, v in wm.WHISPER_CPP_MODELS[model_id].items()
        if k not in ("sha256", "size_bytes")
    }
    monkeypatch.setitem(wm.WHISPER_CPP_MODELS, model_id, entry)
    monkeypatch.setitem(wm._SHIPPED_WHISPER_MODELS, model_id, entry)


def _pin_to_payload(monkeypatch, model_id, payload, *, gated=False):
    """Make the shipped facts describe *payload* exactly (digest + byte count)."""
    import hashlib

    entry = dict(wm.WHISPER_CPP_MODELS[model_id])
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    entry["size_bytes"] = len(payload)
    if gated:
        entry["requires_feature"] = "large_models"
    else:
        entry.pop("requires_feature", None)
    monkeypatch.setitem(wm.WHISPER_CPP_MODELS, model_id, entry)
    monkeypatch.setitem(wm._SHIPPED_WHISPER_MODELS, model_id, entry)
    return entry


def test_every_builtin_model_pins_a_digest():
    """Ratchet: a shipped model without a sha256 is an unverified download."""
    from wayfinder.model_catalog import safe_sha256

    for catalog in (wm.WHISPER_CPP_MODELS, wm.LLM_GGUF_MODELS):
        for model_id, info in catalog.items():
            assert safe_sha256(info.get("sha256")), f"{model_id} has no valid sha256"


def test_digest_mismatch_refuses_to_install(dl, monkeypatch):
    """A swapped model body must never be renamed into place (audit F-C)."""
    import hashlib
    import urllib.request

    d, models, _ = dl
    mid = "base.en"
    # Shipped facts describe the REAL weights; the server returns something else
    # of the same length, so only the digest can catch it.
    entry = _pin_to_payload(monkeypatch, mid, b"the real weights")
    dest = models / entry["filename"]

    class _Resp:
        headers = {"Content-Length": "16"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return b"SWAPPED WEIGHTS!"  # same length, wrong bytes

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: type(
        "_O", (), {"open": lambda self, *a, **k: _Resp()})())

    errors, completed = [], []
    d.download_model(mid, complete_callback=completed.append, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    # The user sees prose, not hex; the digests go to the log.
    assert errors and "doesn't match" in errors[-1]
    assert "sha256" not in errors[-1].lower()
    assert not completed
    assert not dest.exists()
    # and no partial left behind
    assert not (models / f"{entry['filename']}.downloading").exists()


def test_cancel_during_checksum_does_not_install(dl, monkeypatch):
    """Cancel pressed while hashing must not end with an installed model."""
    import hashlib
    import urllib.request

    d, models, _ = dl
    mid = "base.en"
    payload = b"SOME MODEL BYTES"
    entry = _pin_to_payload(monkeypatch, mid, payload)  # would otherwise succeed
    dest = models / entry["filename"]

    class _Resp:
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if getattr(self, "_done", False):
                # End of stream: user hits Cancel just as hashing starts.
                d._cancel_requested = True
                return b""
            self._done = True
            return payload

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: type(
        "_O", (), {"open": lambda self, *a, **k: _Resp()})())

    errors, completed = [], []
    d.download_model(mid, complete_callback=completed.append, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert errors and "cancelled" in errors[-1].lower()
    assert not completed
    assert not dest.exists()
    assert not (models / f"{entry['filename']}.downloading").exists()


class _ScriptedOpener:
    """urllib opener double that records every URL and replays scripted results."""

    def __init__(self, script):
        self._script = list(script)
        self.urls = []

    def open(self, request, timeout=0):
        self.urls.append(request.full_url)
        outcome = self._script.pop(0) if self._script else self._script_default()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _script_default(self):
        raise AssertionError("more requests than the test scripted")


def _body(payload):
    class _Resp:
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return payload

    return _Resp()


def test_transport_failure_retries_once_then_succeeds(dl, monkeypatch):
    """A dropped connection is worth one retry — that is the flaky case."""
    import urllib.error
    import urllib.request

    d, models, _ = dl
    mid = "base.en"
    payload = b"REAL WEIGHTS 123"
    entry = _pin_to_payload(monkeypatch, mid, payload)

    opener = _ScriptedOpener([urllib.error.URLError("connection reset"), _body(payload)])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

    completed, errors = [], []
    d.download_model(mid, complete_callback=completed.append, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert completed and not errors
    assert (models / entry["filename"]).exists()
    assert len(opener.urls) == 2, "expected exactly one retry"


def test_digest_mismatch_is_not_retried(dl, monkeypatch):
    """A correctly sized body with the wrong digest is not a flaky network.

    Re-fetching gigabytes from the same origin only delays the real error, so
    the mismatch surfaces after a single attempt.
    """
    import urllib.request

    d, models, _ = dl
    mid = "base.en"
    _pin_to_payload(monkeypatch, mid, b"REAL WEIGHTS 123")

    opener = _ScriptedOpener([_body(b"SWAPPED BYTES!!!")])  # same length
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

    errors = []
    d.download_model(mid, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert errors and "doesn't match" in errors[-1]
    assert len(opener.urls) == 1, "a digest mismatch must not trigger a refetch"


def test_wrong_content_length_never_transfers_the_body(dl, monkeypatch):
    """If the server's size disagrees with the pin, do not spend the bandwidth."""
    import urllib.request

    d, models, _ = dl
    mid = "base.en"
    entry = _pin_to_payload(monkeypatch, mid, b"REAL WEIGHTS 123")

    class _WrongSize:
        headers = {"Content-Length": "999999999"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            raise AssertionError("body must not be read after a size mismatch")

    opener = _ScriptedOpener([_WrongSize(), _WrongSize()])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

    errors = []
    d.download_model(mid, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert errors and "bytes" in errors[-1]
    assert not (models / entry["filename"]).exists()


def test_unshipped_entry_is_bounded_by_its_declared_size(dl, monkeypatch):
    """A model we never shipped still bounds its transfer.

    It has no entry in the shipped trust map, so the bound comes from the
    `size_bytes` the merge required it to declare — self-declared and therefore
    weaker than a pin, but it still stops an open-ended stream.
    """
    import urllib.request

    d, models, _ = dl
    mid = "brand-new-model"
    entry = {
        "name": "Brand New",
        "filename": "ggml-brand-new.bin",
        "cdn_object": "whisper/ggml-brand-new.bin",
        "sha256": "c" * 64,
        "size_bytes": 32,
    }
    monkeypatch.setitem(wm.WHISPER_CPP_MODELS, mid, entry)
    monkeypatch.setattr("wayfinder.license.transcription_model_allowed", lambda *a: True)

    class _Endless:
        headers = {}  # chunked: no Content-Length at all

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"\x00" * 4096  # never ends

    opener = _ScriptedOpener([_Endless(), _Endless()])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

    errors = []
    d.download_model(mid, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=10)

    assert errors and "exceeded" in errors[-1]
    assert not (models / entry["filename"]).exists()


def test_paid_model_never_falls_back_off_the_cdn(dl, monkeypatch):
    """Ultra weights stay on the authenticated origin even when it fails.

    Falling back to the public mirror would route paid weights around the
    Bearer check — the licensing boundary, not just a security one.
    """
    import urllib.error
    import urllib.request

    d, _models, _ = dl
    mid = "large-v3"
    entry = _pin_to_payload(monkeypatch, mid, b"PAID WEIGHTS 42", gated=True)
    assert entry["requires_feature"] == "large_models"

    gate = type("UltraGate", (), {
        "has_feature": lambda self, _f: True,
        "get_bearer_token": lambda self: "token",
    })()
    monkeypatch.setattr("wayfinder.license.get_feature_gate", lambda: gate)
    monkeypatch.setattr("wayfinder.license.transcription_model_allowed", lambda *a: True)

    opener = _ScriptedOpener([
        urllib.error.URLError("cdn down"),
        urllib.error.URLError("cdn down"),
    ])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

    errors = []
    d.download_model(mid, error_callback=errors.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert errors
    assert len(opener.urls) == 2
    assert all("huggingface.co" not in u for u in opener.urls), opener.urls


def test_free_model_falls_back_to_the_shipped_url(dl, monkeypatch):
    """An ungated model may retry against the revision-pinned URL we shipped."""
    import urllib.error
    import urllib.request

    d, models, _ = dl
    mid = "base.en"
    payload = b"REAL WEIGHTS 123"
    entry = _pin_to_payload(monkeypatch, mid, payload)
    assert entry.get("requires_feature") is None

    opener = _ScriptedOpener([urllib.error.URLError("cdn down"), _body(payload)])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)

    completed = []
    d.download_model(mid, complete_callback=completed.append)
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert completed
    assert (models / entry["filename"]).exists()
    assert "huggingface.co" in opener.urls[1], opener.urls


def test_redownload_replaces_existing_file(dl, monkeypatch):
    """download_model must overwrite an existing dest (re-get after remove/partial)."""
    d, models, _ = dl
    mid = "base.en"
    _unpin_digest(monkeypatch, mid)
    filename = wm.WHISPER_CPP_MODELS[mid]["filename"]
    dest = models / filename
    _touch(dest, b"OLD")

    # Fake urllib to write a small payload through the real download_thread path
    class _Resp:
        headers = {"Content-Length": "3"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            if getattr(self, "_done", False):
                return b""
            self._done = True
            return b"NEW"

    import urllib.request

    class _Opener:
        def open(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: _Opener())
    done = {}

    def on_complete(path):
        done["path"] = path

    d.download_model(mid, complete_callback=on_complete)
    # Join the background thread
    if d._current_download is not None:
        d._current_download.join(timeout=5)

    assert dest.exists()
    assert dest.read_bytes() == b"NEW"
    assert "path" in done


def test_free_download_rejects_every_non_base_speech_model(dl, monkeypatch):
    d, models, _ = dl
    gate = type(
        "FreeGate",
        (),
        {
            "has_feature": lambda self, _feature: False,
            "get_bearer_token": lambda self: None,
        },
    )()
    monkeypatch.setattr("wayfinder.license.get_feature_gate", lambda: gate)

    for model_id, info in wm.WHISPER_CPP_MODELS.items():
        if model_id in ("base", "base.en"):
            assert info.get("requires_feature") is None
            continue
        assert info.get("requires_feature") == "large_models"
        errors = []
        d.download_model(model_id, error_callback=errors.append)
        if d._current_download is not None:
            d._current_download.join(timeout=5)
        assert errors and "Ultra" in errors[-1]
        assert not (models / info["filename"]).exists()

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
    mid = "tiny.en"
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


def test_redownload_replaces_existing_file(dl, monkeypatch):
    """download_model must overwrite an existing dest (re-get after remove/partial)."""
    d, models, _ = dl
    mid = "tiny.en"
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

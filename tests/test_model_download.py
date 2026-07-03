"""Inline model-downloader tests — progress / cancel / error / retry, no network.

Two downloaders ship in wayfinder_main.py (NOT src/wayfinder/core, despite the
plan's hint — the setup.py downloader is already covered by test_e2e_setup.py):

  * ``ModelDownloader`` — the whisper-models panel's downloader (urllib-based,
    threaded, with progress/complete/error callbacks and a cancel flag).
  * ``_download_selected_llamacpp_model`` / ``_cancel_llamacpp_download`` —
    the inline llama.cpp GGUF downloader (requests-based, threaded, drives the
    Settings UI directly). Driven here as unbound methods against a stub self.

Everything network/thread-related is mocked or made synchronous so the tests are
deterministic: no real HTTP, no sleeping, no daemon-thread races.
"""

from __future__ import annotations

import threading
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

wayfinder_main = pytest.importorskip("wayfinder_main")

WApp = wayfinder_main.WayfinderApp


def _wait(event, timeout=5):
    assert event.wait(timeout=timeout), "download callback never fired"


# ===========================================================================
# ModelDownloader (whisper models panel)
# ===========================================================================

class FakeUrlResponse:
    """urllib response context-manager stub yielding fixed chunks."""

    def __init__(self, chunks, total, on_read=None):
        self._chunks = list(chunks)
        self.headers = {"Content-Length": str(total)}
        self._on_read = on_read

    def read(self, n):
        if self._on_read:
            self._on_read()
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestModelDownloader:
    def test_unknown_model_errors_synchronously(self, tmp_path):
        dl = wayfinder_main.ModelDownloader(models_dir=tmp_path)
        errors = []
        dl.download_model("does-not-exist", error_callback=errors.append)
        # No thread involved for an unknown id.
        assert errors and "Unknown model" in errors[0]

    def test_successful_download_reports_progress_and_completes(self, tmp_path, monkeypatch):
        total = 3 * 1024 * 1024
        chunks = [b"\x00" * (1024 * 1024)] * 3
        monkeypatch.setattr(
            wayfinder_main.urllib.request, "urlopen",
            lambda req, timeout=0: FakeUrlResponse(chunks, total),
        )
        dl = wayfinder_main.ModelDownloader(models_dir=tmp_path)

        progress, done = [], threading.Event()
        completed = {}

        dl.download_model(
            "tiny.en",
            progress_callback=lambda p, d, t: progress.append((p, d, t)),
            complete_callback=lambda path: (completed.update(path=path), done.set()),
            error_callback=lambda e: done.set(),
        )
        dl._current_download.join(timeout=5)
        _wait(done)

        assert "path" in completed, "completion callback not called"
        dest = tmp_path / wayfinder_main.WHISPER_CPP_MODELS["tiny.en"]["filename"]
        assert dest.exists()
        # Progress is monotonic and reaches 1.0 with correct totals.
        assert progress[-1][0] == pytest.approx(1.0)
        assert all(t == total for _p, _d, t in progress)
        assert [d for _p, d, _t in progress] == sorted(d for _p, d, _t in progress)

    def test_cancel_midstream_stops_and_removes_partial(self, tmp_path, monkeypatch):
        total = 10 * 1024 * 1024
        chunks = [b"\x00" * (1024 * 1024)] * 10
        monkeypatch.setattr(
            wayfinder_main.urllib.request, "urlopen",
            lambda req, timeout=0: FakeUrlResponse(chunks, total),
        )
        dl = wayfinder_main.ModelDownloader(models_dir=tmp_path)
        done = threading.Event()
        errors = []

        def on_progress(p, d, t):
            # Ask to cancel after the first chunk lands.
            dl.cancel_download()

        dl.download_model(
            "tiny.en",
            progress_callback=on_progress,
            complete_callback=lambda path: done.set(),
            error_callback=lambda e: (errors.append(e), done.set()),
        )
        dl._current_download.join(timeout=5)
        _wait(done)

        assert errors and "cancelled" in errors[0].lower()
        filename = wayfinder_main.WHISPER_CPP_MODELS["tiny.en"]["filename"]
        # Neither the final file nor the .downloading temp survives a cancel.
        assert not (tmp_path / filename).exists()
        assert not (tmp_path / f"{filename}.downloading").exists()

    def test_network_error_routes_to_error_callback(self, tmp_path, monkeypatch):
        def boom(req, timeout=0):
            raise urllib.error.URLError("name resolution failed")

        monkeypatch.setattr(wayfinder_main.urllib.request, "urlopen", boom)
        dl = wayfinder_main.ModelDownloader(models_dir=tmp_path)
        done, errors = threading.Event(), []
        dl.download_model(
            "tiny.en",
            complete_callback=lambda path: done.set(),
            error_callback=lambda e: (errors.append(e), done.set()),
        )
        dl._current_download.join(timeout=5)
        _wait(done)
        assert errors and "Network error" in errors[0]
        assert not (tmp_path / wayfinder_main.WHISPER_CPP_MODELS["tiny.en"]["filename"]).exists()

    def test_retry_after_error_succeeds(self, tmp_path, monkeypatch):
        filename = wayfinder_main.WHISPER_CPP_MODELS["tiny.en"]["filename"]
        dl = wayfinder_main.ModelDownloader(models_dir=tmp_path)

        # 1) First attempt fails at the network layer.
        monkeypatch.setattr(
            wayfinder_main.urllib.request, "urlopen",
            lambda req, timeout=0: (_ for _ in ()).throw(urllib.error.URLError("down")),
        )
        done1, errors = threading.Event(), []
        dl.download_model("tiny.en",
                          complete_callback=lambda p: done1.set(),
                          error_callback=lambda e: (errors.append(e), done1.set()))
        dl._current_download.join(timeout=5)
        _wait(done1)
        assert errors and not (tmp_path / filename).exists()

        # 2) Retry on the SAME downloader now that the network is back.
        total = 2 * 1024 * 1024
        monkeypatch.setattr(
            wayfinder_main.urllib.request, "urlopen",
            lambda req, timeout=0: FakeUrlResponse([b"\x00" * (1024 * 1024)] * 2, total),
        )
        done2, completed = threading.Event(), {}
        dl.download_model("tiny.en",
                          complete_callback=lambda p: (completed.update(p=p), done2.set()),
                          error_callback=lambda e: done2.set())
        dl._current_download.join(timeout=5)
        _wait(done2)
        assert "p" in completed
        assert (tmp_path / filename).exists()


# ===========================================================================
# _download_selected_llamacpp_model / _cancel_llamacpp_download (inline UI)
# ===========================================================================

class SyncThread:
    """threading.Thread stand-in whose start() runs the target inline."""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        self._target()


class FakeRequestResponse:
    def __init__(self, chunks, total):
        self._chunks = chunks
        self.headers = {"content-length": str(total)}

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=0):
        return iter(self._chunks)

    def close(self):
        pass


class FakeSession:
    def __init__(self, response=None, error=None):
        self.headers = {}
        self._response = response
        self._error = error

    def get(self, url, **kwargs):
        if self._error:
            raise self._error
        return self._response


class LlamaDownloadApp:
    """Stub self carrying exactly the surface _download_selected_llamacpp_model
    touches. after() runs callbacks inline (paired with SyncThread → the whole
    download resolves synchronously within the call)."""

    _download_selected_llamacpp_model = WApp._download_selected_llamacpp_model
    _cancel_llamacpp_download = WApp._cancel_llamacpp_download

    def __init__(self, selection, model_info, installed=False):
        self._inline_download_active = False
        self._cancel_download = False
        self._llamacpp_model_var = SimpleNamespace(get=lambda: selection)
        self._llamacpp_model_data = {
            selection: {"id": "gemma3-1b", "info": model_info, "installed": installed},
        }
        self._llamacpp_download_btn = MagicMock()
        self._llamacpp_progress_frame = MagicMock()
        self._llamacpp_progress_bar = MagicMock()
        self._llamacpp_progress_bar.winfo_exists.return_value = True
        self._llamacpp_status_label = MagicMock()
        self._llamacpp_status_label.winfo_exists.return_value = True
        self._rebuild_postproc_section = MagicMock()
        self.config = {}
        self.logs = []

    def after(self, delay, fn=None):
        if fn is not None:
            fn()

    def log(self, msg):
        self.logs.append(msg)

    def update_idletasks(self):
        pass


@pytest.fixture
def llama_env(tmp_path, monkeypatch):
    monkeypatch.setattr(wayfinder_main, "_get_llm_models_dir", lambda: tmp_path)
    monkeypatch.setattr(wayfinder_main, "save_config", lambda cfg: None)
    monkeypatch.setattr(wayfinder_main, "threading",
                        SimpleNamespace(Thread=SyncThread))
    model_info = {
        "url": "https://example.invalid/model.gguf",
        "filename": "test-model.gguf",
        "name": "Test GGUF",
        "size": "800 MB",
    }
    return tmp_path, model_info


class TestLlamaCppInlineDownload:
    def test_already_installed_is_a_noop(self, llama_env):
        _tmp, info = llama_env
        app = LlamaDownloadApp("Test GGUF", info, installed=True)
        app._download_selected_llamacpp_model()
        assert app._inline_download_active is False
        app._llamacpp_download_btn.configure.assert_not_called()

    def test_successful_download_writes_file_and_selects_model(self, llama_env, monkeypatch):
        import requests
        tmp, info = llama_env
        total = 2 * 1024 * 1024
        resp = FakeRequestResponse([b"\x00" * (1024 * 1024)] * 2, total)
        monkeypatch.setattr(requests, "Session", lambda: FakeSession(response=resp))

        app = LlamaDownloadApp("Test GGUF", info)
        app._download_selected_llamacpp_model()

        model_file = tmp / info["filename"]
        assert model_file.exists()
        # The downloaded model becomes the selected one, and the run is unlocked.
        assert app.config["llama_cpp_model_path"] == str(model_file)
        assert app._inline_download_active is False
        app._llamacpp_progress_bar.set.assert_any_call(1.0)

    def test_network_error_unlocks_and_offers_retry(self, llama_env, monkeypatch):
        import requests
        tmp, info = llama_env
        monkeypatch.setattr(requests, "Session",
                            lambda: FakeSession(error=Exception("Connection refused")))

        app = LlamaDownloadApp("Test GGUF", info)
        app._download_selected_llamacpp_model()

        assert app._inline_download_active is False           # unlocked for retry
        assert not (tmp / info["filename"]).exists()
        # The button is rebuilt as a Retry that re-invokes the same downloader.
        retry_kwargs = app._llamacpp_download_btn.configure.call_args_list[-1].kwargs
        assert retry_kwargs.get("text") == "Retry"
        assert retry_kwargs.get("command") == app._download_selected_llamacpp_model

    def test_cancel_midstream_aborts_without_writing_model(self, llama_env, monkeypatch):
        import requests
        tmp, info = llama_env

        app = LlamaDownloadApp("Test GGUF", info)

        def gen():
            yield b"\x00" * 1024
            app._cancel_download = True   # user hit Cancel after the first chunk
            yield b"\x00" * 1024

        resp = FakeRequestResponse(gen(), 4096)
        monkeypatch.setattr(requests, "Session", lambda: FakeSession(response=resp))

        app._download_selected_llamacpp_model()

        assert app._inline_download_active is False
        assert not (tmp / info["filename"]).exists()
        assert any("cancel" in m.lower() for m in app.logs)

    def test_retry_after_error_then_success(self, llama_env, monkeypatch):
        import requests
        tmp, info = llama_env
        app = LlamaDownloadApp("Test GGUF", info)

        # First attempt: network error.
        monkeypatch.setattr(requests, "Session",
                            lambda: FakeSession(error=Exception("boom")))
        app._download_selected_llamacpp_model()
        assert app._inline_download_active is False
        assert not (tmp / info["filename"]).exists()

        # Retry: healthy connection → file lands + selected.
        resp = FakeRequestResponse([b"\x00" * (1024 * 1024)] * 2, 2 * 1024 * 1024)
        monkeypatch.setattr(requests, "Session", lambda: FakeSession(response=resp))
        app._download_selected_llamacpp_model()
        assert (tmp / info["filename"]).exists()
        assert app.config["llama_cpp_model_path"] == str(tmp / info["filename"])

    def test_cancel_flag_setter(self, llama_env):
        _tmp, info = llama_env
        app = LlamaDownloadApp("Test GGUF", info)
        app._cancel_llamacpp_download()
        assert app._cancel_download is True
        app._llamacpp_download_btn.configure.assert_called()  # button shows "Cancelling..."

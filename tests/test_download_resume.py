"""Resuming an interrupted model download, against a real local HTTP server.

Nothing is mocked between the downloader and the socket except the https rule
(the test server is plain http). Payloads are pinned to a real sha256, so a
resume that spliced the wrong bytes fails the same check real installs use.
"""

from __future__ import annotations

import errno
import hashlib
import http.server
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

wayfinder_main = pytest.importorskip("wayfinder_main")

from wayfinder.core import download_guard
from wayfinder.core.download_guard import (
    DownloadBounds,
    DownloadSizeMismatch,
    ResumeRejected,
    can_resume,
    is_disk_full,
    resume_offset,
    resume_start,
    sweep_stale_partials,
)

MB = 1024 * 1024
PAYLOAD = hashlib.sha256(b"seed").digest() * (3 * MB // 32)
SHA = hashlib.sha256(PAYLOAD).hexdigest()
GOOD_SHA = "a" * 64
REPO_ROOT = Path(__file__).resolve().parent.parent


class ModelServer:
    """Serves PAYLOAD following a per-request script; the last step repeats.

    full      honors Range with a correct 206
    ignore    answers every request 200 with the whole file
    cut:N     honors Range, then drops the connection after N bytes
    bad-start 206 whose Content-Range starts somewhere else
    bad-total 206 whose Content-Range describes a different object
    416       Range Not Satisfiable
    401/404   a settled refusal
    503       a transient failure
    insecure  redirects to plain http
    """

    def __init__(self, script):
        self.script = list(script)
        self.ranges = []
        self.on_request = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                outer.ranges.append(self.headers.get("Range"))
                if outer.on_request:
                    outer.on_request()
                step = outer.script.pop(0) if len(outer.script) > 1 else outer.script[0]
                if step == "insecure":
                    self.send_response(302)
                    self.send_header("Location", "http://insecure.invalid/model.bin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if step in ("401", "403", "404", "416", "503"):
                    self.send_response(int(step))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                header = self.headers.get("Range")
                if header and step != "ignore":
                    start = int(header.removeprefix("bytes=").rstrip("-"))
                body = PAYLOAD[start:]
                if step == "ignore" or not header:
                    self.send_response(200)
                else:
                    self.send_response(206)
                    shown, total = start, len(PAYLOAD)
                    if step == "bad-start":
                        shown = start + 1
                    if step == "bad-total":
                        total = len(PAYLOAD) + 7
                    self.send_header(
                        "Content-Range", f"bytes {shown}-{total - 1}/{total}"
                    )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if step.startswith("cut:"):
                    self.wfile.write(body[: int(step[4:])])
                    self.wfile.flush()
                    self.connection.close()
                    return
                self.wfile.write(body)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/model.bin"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def serve(monkeypatch):
    # The server is plain http; https enforcement has its own tests.
    monkeypatch.setattr(download_guard, "require_https", lambda _url: None)
    servers = []

    def _start(*script):
        server = ModelServer(script)
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.close()


# --- ModelDownloader (speech models) ----------------------------------------


def _pin_whisper(monkeypatch, url, *, sha=SHA, size=len(PAYLOAD)):
    entry = {
        k: v
        for k, v in wayfinder_main.WHISPER_CPP_MODELS["base.en"].items()
        if k != "cdn_object"
    }
    entry.update(url=url, sha256=sha, size_bytes=size)
    if sha is None:
        entry.pop("sha256")
    monkeypatch.setitem(wayfinder_main.WHISPER_CPP_MODELS, "base.en", entry)
    monkeypatch.setitem(wayfinder_main._SHIPPED_WHISPER_MODELS, "base.en", entry)
    return entry


def _run_whisper(models_dir, **callbacks):
    downloader = wayfinder_main.ModelDownloader(models_dir=models_dir)
    outcome = SimpleNamespace(errors=[], completed=[], progress=[], downloader=downloader)
    downloader.download_model(
        "base.en",
        progress_callback=lambda p, done, total: outcome.progress.append((p, done, total)),
        complete_callback=outcome.completed.append,
        error_callback=outcome.errors.append,
        **callbacks,
    )
    downloader._current_download.join(timeout=30)
    return outcome


def _paths(models_dir, entry):
    return models_dir / entry["filename"], models_dir / f"{entry['filename']}.downloading"


def test_a_dropped_download_keeps_its_bytes_and_the_next_run_resumes(tmp_path, serve, monkeypatch):
    server = serve(f"cut:{MB}", f"cut:{MB // 2}", "full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)

    # Both attempts of the first run drop mid-transfer.
    first = _run_whisper(tmp_path)
    assert first.errors and not first.completed
    assert first.downloader.last_error_retryable is True
    assert not dest.exists()
    assert partial.read_bytes() == PAYLOAD[: MB + MB // 2]
    assert server.ranges == [None, f"bytes={MB}-"]

    # A new downloader (as after a relaunch) carries on from there.
    second = _run_whisper(tmp_path)
    assert second.completed == [str(dest)] and not second.errors
    assert server.ranges[-1] == f"bytes={MB + MB // 2}-"
    assert dest.read_bytes() == PAYLOAD
    assert not partial.exists()


def test_progress_counts_the_bytes_already_on_disk(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[: 2 * MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.completed
    assert outcome.progress[0][1] > 2 * MB
    assert outcome.progress[0][2] == len(PAYLOAD)
    assert outcome.progress[-1][0] == pytest.approx(1.0)


def test_a_server_that_ignores_range_restarts_from_zero(tmp_path, serve, monkeypatch):
    # The deployed Worker's behaviour until its Range support ships.
    server = serve("ignore")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.completed and not outcome.errors
    assert server.ranges == [f"bytes={MB}-"]
    assert dest.read_bytes() == PAYLOAD


@pytest.mark.parametrize("step", ["bad-start", "bad-total"])
def test_a_206_for_the_wrong_bytes_is_refused_and_restarted(tmp_path, serve, monkeypatch, step):
    server = serve(step, "full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.completed and not outcome.errors
    assert server.ranges == [f"bytes={MB}-", None]
    assert dest.read_bytes() == PAYLOAD


def test_416_discards_the_partial_and_restarts(tmp_path, serve, monkeypatch):
    server = serve("416", "full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.completed and not outcome.errors
    assert server.ranges == [f"bytes={MB}-", None]
    assert dest.read_bytes() == PAYLOAD


def test_an_oversized_partial_is_discarded_without_a_range(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD + b"extra")

    outcome = _run_whisper(tmp_path)

    assert outcome.completed
    assert server.ranges == [None]
    assert dest.read_bytes() == PAYLOAD


def test_a_complete_partial_is_verified_without_touching_the_network(tmp_path, serve, monkeypatch):
    # Death between the last byte and the rename: nothing is left to fetch.
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD)

    outcome = _run_whisper(tmp_path)

    assert outcome.completed
    assert server.ranges == []
    assert dest.read_bytes() == PAYLOAD


def test_a_corrupt_partial_gets_one_clean_refetch(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(b"\xff" * MB)

    outcome = _run_whisper(tmp_path)

    assert outcome.completed and not outcome.errors
    assert server.ranges == [f"bytes={MB}-", None]
    assert dest.read_bytes() == PAYLOAD
    assert not partial.exists()


def test_a_corrupt_complete_partial_gets_one_clean_refetch(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(b"\xff" * len(PAYLOAD))

    outcome = _run_whisper(tmp_path)

    assert outcome.completed
    assert server.ranges == [None]
    assert dest.read_bytes() == PAYLOAD


def test_a_mismatch_on_the_clean_refetch_is_settled(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url, sha="0" * 64)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.errors and not outcome.completed
    assert outcome.downloader.last_error_retryable is False
    assert server.ranges == [f"bytes={MB}-", None]
    assert not dest.exists()
    assert not partial.exists()


def test_a_mismatch_with_no_resume_is_not_refetched(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url, sha="0" * 64)
    _, partial = _paths(tmp_path, entry)

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert server.ranges == [None]
    assert not partial.exists()


def _fill_the_disk(monkeypatch, *, after=0):
    """open() on a partial hands back a file whose writes hit ENOSPC."""
    real_open = open

    class _FullFile:
        def __init__(self, handle):
            self._handle = handle
            self._left = after

        def write(self, data):
            if self._left <= 0:
                raise OSError(errno.ENOSPC, "No space left on device")
            self._left -= 1
            return self._handle.write(data)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

    def fake_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        name = str(path)
        if "b" in mode and ("w" in mode or "a" in mode) and name.endswith((".downloading", ".tmp")):
            return _FullFile(handle)
        return handle

    monkeypatch.setattr("builtins.open", fake_open)


def test_a_full_disk_is_settled_and_frees_the_partial(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])
    _fill_the_disk(monkeypatch)

    outcome = _run_whisper(tmp_path)

    assert outcome.errors and "space" in outcome.errors[0].lower()
    assert outcome.downloader.last_error_retryable is False
    assert len(server.ranges) == 1  # no second attempt on a full disk
    assert not partial.exists() and not dest.exists()


def test_a_model_without_a_digest_never_resumes(tmp_path, serve, monkeypatch):
    # Nothing could catch a bad splice, so it stays all-or-nothing.
    server = serve(f"cut:{MB}")
    entry = _pin_whisper(monkeypatch, server.url, sha=None)
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert server.ranges == [None, None]
    assert not partial.exists()


def test_a_settled_refusal_is_not_retryable_and_drops_the_partial(tmp_path, serve, monkeypatch):
    server = serve("401")
    entry = _pin_whisper(monkeypatch, server.url)
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert outcome.downloader.last_error_retryable is False
    assert len(server.ranges) == 1
    assert not partial.exists()


def test_a_503_keeps_the_partial_and_is_retryable(tmp_path, serve, monkeypatch):
    server = serve("503")
    entry = _pin_whisper(monkeypatch, server.url)
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert outcome.downloader.last_error_retryable is True
    assert partial.read_bytes() == PAYLOAD[:MB]


def test_a_404_keeps_the_marker_alive_but_a_401_or_403_does_not(tmp_path, serve, monkeypatch):
    for step, retryable in (("404", True), ("401", False), ("403", False)):
        server = serve(step)
        entry = _pin_whisper(monkeypatch, server.url)
        outcome = _run_whisper(tmp_path)
        assert outcome.errors
        assert outcome.downloader.last_error_retryable is retryable, step
        assert len(server.ranges) == 1


def test_a_404_on_a_resume_keeps_the_partial_for_the_retry_it_promises(tmp_path, serve, monkeypatch):
    server = serve("404")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert outcome.downloader.last_error_retryable is True
    assert partial.read_bytes() == PAYLOAD[:MB]
    assert not dest.exists()


def test_a_4xx_after_a_dropped_attempt_keeps_the_partial_and_the_transport_error(
    tmp_path, serve, monkeypatch
):
    server = serve(f"cut:{MB}", "404")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)

    outcome = _run_whisper(tmp_path)

    assert outcome.errors and "404" not in outcome.errors[0]
    assert outcome.downloader.last_error_retryable is True
    assert partial.read_bytes() == PAYLOAD[:MB]
    assert not dest.exists()


def test_an_insecure_redirect_is_settled(tmp_path, serve, monkeypatch):
    server = serve("insecure")
    entry = _pin_whisper(monkeypatch, server.url)
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])
    # Keep the https rule live for the redirect target; the server itself is http.
    monkeypatch.setattr(
        download_guard,
        "require_https",
        lambda url: (_ for _ in ()).throw(download_guard.InsecureRedirect(url))
        if "insecure.invalid" in url
        else None,
    )

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert outcome.downloader.last_error_retryable is False
    assert len(server.ranges) == 1
    assert not partial.exists()


def test_cancel_during_a_dead_network_reports_cancelled_and_deletes(tmp_path, serve, monkeypatch):
    server = serve("503")
    entry = _pin_whisper(monkeypatch, server.url)
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])
    downloader = wayfinder_main.ModelDownloader(models_dir=tmp_path)
    server.on_request = downloader.cancel_download
    errors = []

    downloader.download_model("base.en", error_callback=errors.append)
    downloader._current_download.join(timeout=30)

    assert errors and "cancelled" in errors[0].lower()
    assert downloader.last_error_retryable is False
    assert len(server.ranges) == 1  # no second attempt after Cancel
    assert not partial.exists()


def test_a_malformed_digest_never_resumes(tmp_path, serve, monkeypatch):
    # verify_model_digest ignores a malformed pin, so nothing would catch a splice.
    server = serve(f"cut:{MB}")
    entry = _pin_whisper(monkeypatch, server.url, sha="not-a-digest")
    _, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])

    outcome = _run_whisper(tmp_path)

    assert outcome.errors
    assert server.ranges == [None, None]
    assert not partial.exists()


def test_removing_a_partial_only_model_works_but_never_while_it_downloads(tmp_path, monkeypatch):
    entry = wayfinder_main.WHISPER_CPP_MODELS["base.en"]
    monkeypatch.setattr(wayfinder_main, "_get_whisper_models_dir", lambda: tmp_path)
    monkeypatch.setattr(wayfinder_main, "_whisper_model_search_dirs", lambda: [tmp_path])
    partial = tmp_path / f"{entry['filename']}.downloading"
    partial.write_bytes(b"partial")
    downloader = wayfinder_main.ModelDownloader(models_dir=tmp_path)
    assert downloader.has_only_partial("base.en") is True

    monkeypatch.setattr(wayfinder_main.ModelDownloader, "_in_flight", {"base.en"})
    assert downloader.has_only_partial("base.en") is False
    downloader.delete_model("base.en")
    assert partial.exists()

    monkeypatch.setattr(wayfinder_main.ModelDownloader, "_in_flight", set())
    assert downloader.delete_model("base.en")["ok"] is True
    assert not partial.exists()
    assert downloader.has_only_partial("base.en") is False

    (tmp_path / entry["filename"]).write_bytes(b"full")
    partial.write_bytes(b"partial")
    assert downloader.has_only_partial("base.en") is False


def test_cancel_still_deletes_the_partial(tmp_path, serve, monkeypatch):
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    partial.write_bytes(PAYLOAD[:MB])
    downloader = wayfinder_main.ModelDownloader(models_dir=tmp_path)
    errors = []

    downloader.download_model(
        "base.en",
        progress_callback=lambda *_a: downloader.cancel_download(),
        error_callback=errors.append,
    )
    downloader._current_download.join(timeout=30)

    assert errors and "cancelled" in errors[0].lower()
    assert downloader.last_error_retryable is False
    assert not dest.exists()
    assert not partial.exists()


def test_a_second_copy_of_a_running_download_counts_as_retryable(tmp_path, monkeypatch):
    # It says nothing about the model, so it must not end the Ultra setup's retry.
    monkeypatch.setattr(wayfinder_main.ModelDownloader, "_in_flight", {"base.en"})
    downloader = wayfinder_main.ModelDownloader(models_dir=tmp_path)
    errors = []
    downloader.download_model("base.en", error_callback=errors.append)
    assert "already downloading" in errors[0]
    assert downloader.last_error_retryable is True


def test_the_process_dying_mid_download_leaves_a_partial_the_next_run_resumes(
    tmp_path, serve, monkeypatch
):
    """The reported bug: quitting Aura mid-download used to lose everything."""
    server = serve("full")
    entry = _pin_whisper(monkeypatch, server.url)
    dest, partial = _paths(tmp_path, entry)
    child = textwrap.dedent(
        f"""
        import os, sys
        sys.path[:0] = [{str(REPO_ROOT)!r}, {str(REPO_ROOT / "src")!r}]
        import wayfinder_main as wm
        from pathlib import Path
        from wayfinder.core import download_guard
        download_guard.require_https = lambda _url: None
        entry = {{k: v for k, v in wm.WHISPER_CPP_MODELS["base.en"].items() if k != "cdn_object"}}
        entry.update(url={server.url!r}, sha256={SHA!r}, size_bytes={len(PAYLOAD)})
        wm.WHISPER_CPP_MODELS["base.en"] = entry
        wm._SHIPPED_WHISPER_MODELS["base.en"] = entry
        d = wm.ModelDownloader(models_dir=Path({str(tmp_path)!r}))
        d.download_model("base.en", progress_callback=lambda *_a: os._exit(9))
        d._current_download.join(timeout=60)
        os._exit(3)
        """
    )
    env = dict(os.environ, HOME=str(tmp_path / "home"), QT_QPA_PLATFORM="offscreen")
    (tmp_path / "home").mkdir()
    died = subprocess.run([sys.executable, "-c", child], env=env, timeout=120)
    assert died.returncode == 9

    kept = partial.stat().st_size
    assert 0 < kept < len(PAYLOAD)
    assert partial.read_bytes() == PAYLOAD[:kept]

    outcome = _run_whisper(tmp_path)

    assert outcome.completed
    assert server.ranges[-1] == f"bytes={kept}-"
    assert dest.read_bytes() == PAYLOAD


# --- The inline cleanup-model downloader ------------------------------------


class _Inline:
    """threading.Thread stand-in whose start() runs the target inline."""

    def __init__(self, target=None, daemon=None, **_kwargs):
        self._target = target

    def start(self):
        self._target()


class _CleanupApp:
    _download_selected_llamacpp_model = wayfinder_main.WayfinderApp._download_selected_llamacpp_model
    _cancel_llamacpp_download = wayfinder_main.WayfinderApp._cancel_llamacpp_download
    _activate_downloaded_model = wayfinder_main.WayfinderApp._activate_downloaded_model
    _apply_downloaded_model = wayfinder_main.WayfinderApp._apply_downloaded_model
    _cleanup_model_is_unlocked = wayfinder_main.WayfinderApp._cleanup_model_is_unlocked

    def __init__(self, model_info):
        self.app_state = wayfinder_main.AppState.IDLE
        self.feature_gate = SimpleNamespace(has_feature=lambda _feature: True)
        self._update_compatibility_banner = MagicMock()
        self._inline_download_active = False
        self._cancel_download = False
        self._llamacpp_model_var = SimpleNamespace(get=lambda: "Test GGUF")
        self._llamacpp_model_data = {
            "Test GGUF": {"id": "gemma3-1b", "info": model_info, "installed": False, "unlocked": True},
        }
        self._llamacpp_download_btn = MagicMock()
        self._llamacpp_progress_frame = MagicMock()
        self._llamacpp_progress_bar = MagicMock()
        self._llamacpp_status_label = MagicMock()
        self._rebuild_postproc_section = MagicMock()
        self.config = {}
        self.logs = []

    def after(self, _delay, fn=None):
        if fn is not None:
            fn()

    def log(self, message):
        self.logs.append(message)

    def update_idletasks(self):
        pass


@pytest.fixture
def cleanup_model(tmp_path, serve, monkeypatch):
    monkeypatch.setattr(wayfinder_main, "_get_llm_models_dir", lambda: tmp_path)
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)
    monkeypatch.setattr(wayfinder_main, "threading", SimpleNamespace(Thread=_Inline))

    def _make(*script, **pins):
        server = serve(*script)
        info = {
            "url": server.url,
            "filename": "test-model.gguf",
            "name": "Test GGUF",
            "size": "3 MB",
            "size_bytes": len(PAYLOAD),
            "sha256": SHA,
        }
        info.update(pins)
        return server, info, tmp_path / "test-model.gguf", tmp_path / "test-model.tmp"

    return _make


def test_cleanup_download_resumes_after_a_drop(cleanup_model):
    server, info, dest, partial = cleanup_model(f"cut:{MB}", f"cut:{MB // 2}", "full")

    _CleanupApp(info)._download_selected_llamacpp_model()
    assert not dest.exists()
    assert partial.read_bytes() == PAYLOAD[: MB + MB // 2]

    app = _CleanupApp(info)
    app._download_selected_llamacpp_model()
    assert server.ranges == [None, f"bytes={MB}-", f"bytes={MB + MB // 2}-"]
    assert dest.read_bytes() == PAYLOAD
    assert not partial.exists()
    assert app.config["llama_cpp_model_path"] == str(dest)


def test_cleanup_download_restarts_when_the_server_ignores_range(cleanup_model):
    server, info, dest, partial = cleanup_model("ignore")
    partial.write_bytes(PAYLOAD[:MB])

    _CleanupApp(info)._download_selected_llamacpp_model()

    assert server.ranges == [f"bytes={MB}-"]
    assert dest.read_bytes() == PAYLOAD


@pytest.mark.parametrize("step", ["416", "bad-start", "bad-total"])
def test_cleanup_download_restarts_on_a_refused_resume(cleanup_model, step):
    server, info, dest, partial = cleanup_model(step, "full")
    partial.write_bytes(PAYLOAD[:MB])

    _CleanupApp(info)._download_selected_llamacpp_model()

    assert server.ranges == [f"bytes={MB}-", None]
    assert dest.read_bytes() == PAYLOAD


def test_cleanup_corrupt_partial_gets_one_clean_refetch(cleanup_model):
    server, info, dest, partial = cleanup_model("full")
    partial.write_bytes(b"\xff" * MB)

    _CleanupApp(info)._download_selected_llamacpp_model()

    assert server.ranges == [f"bytes={MB}-", None]
    assert dest.read_bytes() == PAYLOAD
    assert not partial.exists()


def test_cleanup_mismatch_on_the_clean_refetch_is_settled(cleanup_model):
    server, info, dest, partial = cleanup_model("full", sha256="0" * 64)
    partial.write_bytes(PAYLOAD[:MB])

    _CleanupApp(info)._download_selected_llamacpp_model()

    assert server.ranges == [f"bytes={MB}-", None]
    assert not dest.exists()
    assert not partial.exists()


def test_cleanup_full_disk_is_settled_and_frees_the_partial(cleanup_model, monkeypatch):
    server, info, dest, partial = cleanup_model("full")
    partial.write_bytes(PAYLOAD[:MB])
    _fill_the_disk(monkeypatch)
    app = _CleanupApp(info)

    app._download_selected_llamacpp_model()

    assert any("space" in message.lower() for message in app.logs)
    assert len(server.ranges) == 1
    assert not partial.exists() and not dest.exists()


def test_cleanup_404_on_a_resume_keeps_the_partial(cleanup_model):
    server, info, dest, partial = cleanup_model("404")
    partial.write_bytes(PAYLOAD[:MB])
    app = _CleanupApp(info)

    app._download_selected_llamacpp_model()

    assert partial.read_bytes() == PAYLOAD[:MB]
    assert not dest.exists()


def test_cleanup_401_on_a_resume_drops_the_partial(cleanup_model):
    server, info, dest, partial = cleanup_model("401")
    partial.write_bytes(PAYLOAD[:MB])
    app = _CleanupApp(info)

    app._download_selected_llamacpp_model()

    assert not partial.exists() and not dest.exists()


def test_cleanup_4xx_after_a_drop_keeps_the_partial(cleanup_model):
    server, info, dest, partial = cleanup_model(f"cut:{MB}", "404")
    app = _CleanupApp(info)

    app._download_selected_llamacpp_model()

    assert partial.read_bytes() == PAYLOAD[:MB]
    assert not any("404" in message for message in app.logs)


def test_cleanup_cancel_during_a_dead_network_deletes(cleanup_model):
    server, info, dest, partial = cleanup_model("503")
    partial.write_bytes(PAYLOAD[:MB])
    app = _CleanupApp(info)
    server.on_request = lambda: setattr(app, "_cancel_download", True)

    app._download_selected_llamacpp_model()

    assert any("cancel" in message.lower() for message in app.logs)
    assert len(server.ranges) == 1
    assert not partial.exists()


def test_cleanup_malformed_digest_never_resumes(cleanup_model):
    server, info, dest, partial = cleanup_model("full", sha256="not-a-digest")
    partial.write_bytes(b"\xff" * MB)

    _CleanupApp(info)._download_selected_llamacpp_model()

    assert server.ranges == [None]
    assert dest.read_bytes() == PAYLOAD


def test_cleanup_download_does_not_resume_without_a_digest(cleanup_model):
    server, info, dest, partial = cleanup_model("full", sha256=None)
    partial.write_bytes(b"\xff" * MB)

    _CleanupApp(info)._download_selected_llamacpp_model()

    assert server.ranges == [None]
    assert dest.read_bytes() == PAYLOAD


def test_cleanup_cancel_deletes_the_partial(cleanup_model):
    server, info, dest, partial = cleanup_model("full")
    partial.write_bytes(PAYLOAD[:MB])
    app = _CleanupApp(info)
    app._cancel_download = False
    app.update_idletasks = lambda: setattr(app, "_cancel_download", True)

    app._download_selected_llamacpp_model()

    assert not dest.exists()
    assert not partial.exists()


# --- The guard's rules ------------------------------------------------------


def test_resume_offset_needs_a_size_and_a_digest(tmp_path):
    partial = tmp_path / "m.downloading"
    partial.write_bytes(b"x" * 10)
    assert resume_offset(partial, 100, GOOD_SHA) == 10
    assert resume_offset(partial, 10, GOOD_SHA) == 10
    assert resume_offset(partial, None, GOOD_SHA) == 0
    assert not partial.exists()

    partial.write_bytes(b"x" * 10)
    assert resume_offset(partial, 100, None) == 0
    assert not partial.exists()

    partial.write_bytes(b"x" * 10)
    assert resume_offset(partial, 9, GOOD_SHA) == 0
    assert not partial.exists()

    partial.write_bytes(b"x" * 10)
    assert resume_offset(partial, 100, "abc") == 0
    assert not partial.exists()

    assert resume_offset(tmp_path / "missing", 100, GOOD_SHA) == 0


def test_can_resume_needs_a_size_and_a_well_formed_digest():
    assert can_resume(100, GOOD_SHA)
    assert can_resume(100, "sha256:" + GOOD_SHA.upper())
    assert not can_resume(None, GOOD_SHA)
    assert not can_resume(100, None)
    assert not can_resume(100, "abc")


def _aged(path, days):
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def test_sweep_removes_only_old_partials_it_was_named(tmp_path):
    old = tmp_path / "ggml-a.bin.downloading"
    recent = tmp_path / "ggml-b.bin.downloading"
    foreign = tmp_path / "another-programs-file.bin.downloading"
    for path in (old, recent, foreign):
        path.write_bytes(b"x")
    for path in (old, foreign):
        _aged(path, 31)
    _aged(recent, 29)

    removed = sweep_stale_partials(tmp_path, {old.name, recent.name})

    assert removed == [old]
    assert not old.exists()
    assert recent.exists() and foreign.exists()


def test_sweep_tolerates_a_missing_directory(tmp_path):
    assert sweep_stale_partials(tmp_path / "nope", {"a.tmp"}) == []


def test_is_disk_full_only_for_a_full_disk():
    assert is_disk_full(OSError(errno.ENOSPC, "full"))
    assert not is_disk_full(OSError(errno.EACCES, "denied"))
    assert not is_disk_full(ValueError("x"))


def test_resume_start_accepts_only_the_exact_continuation():
    good = "bytes 10-99/100"
    assert resume_start(206, good, 90, 10, 100) == 10
    assert resume_start(206, good, None, 10, 100) == 10
    assert resume_start(200, None, 100, 10, 100) == 0


@pytest.mark.parametrize(
    "content_range, content_length",
    [
        (None, 90),
        ("garbage", 90),
        ("bytes 11-99/100", 89),
        ("bytes 10-99/101", 90),
        ("bytes 10-50/100", 41),
        ("bytes 10-99/*", 90),
        ("bytes 10-99/100", 80),
    ],
)
def test_resume_start_refuses_everything_else(content_range, content_length):
    with pytest.raises(ResumeRejected):
        resume_start(206, content_range, content_length, 10, 100)


def test_a_refused_resume_counts_as_a_transport_failure():
    assert issubclass(ResumeRejected, DownloadSizeMismatch)


def test_bounds_count_the_whole_file_when_resuming():
    bounds = DownloadBounds(100)
    assert bounds.check_header(10 + 90) == 100
    bounds.check_progress(100)
    bounds.check_complete(100)
    with pytest.raises(DownloadSizeMismatch):
        bounds.check_progress(101)

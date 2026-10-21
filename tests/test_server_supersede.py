"""A CPU/GPU switch or Quit while whisper-server is still loading its model.

These run the real _start_server/shutdown against a child process that never
becomes ready (a model load that takes longer than the test), so a shutdown or
switch that waits for the load shows up as a timing failure, not a mock count.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import wayfinder_main
from wayfinder.core.transcriber import ServerStartSuperseded, WhisperServerBackend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shebang fake server")

# Long enough that waiting for the load would fail every timing bound below.
STARTUP_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def _clean_server_state():
    yield
    proc = WhisperServerBackend._server_process
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    WhisperServerBackend._server_process = None
    WhisperServerBackend._server_model_path = ""
    WhisperServerBackend._server_use_gpu = None
    WhisperServerBackend._server_disabled = False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def slow_server(tmp_path):
    """A whisper-server that never finishes loading, and a backend for it."""
    binary = tmp_path / "whisper-server"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    binary.chmod(0o755)
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"\x00")
    port = _free_port()

    def backend(use_gpu=True):
        return WhisperServerBackend(
            whisper_server_binary=str(binary), model_path=str(model), port=port,
            use_gpu=use_gpu, startup_timeout=STARTUP_TIMEOUT,
        )

    return backend


def _start_in_background(backend):
    outcome = {}

    def run():
        try:
            backend._start_server()
            outcome["result"] = "started"
        except Exception as exc:  # recorded for the assertions
            outcome["result"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while WhisperServerBackend._server_process is None:
        assert time.monotonic() < deadline, "the fake server never spawned"
        time.sleep(0.01)
    return thread, WhisperServerBackend._server_process, outcome


def test_quit_during_a_model_load_does_not_wait_for_the_load(slow_server):
    thread, child, outcome = _start_in_background(slow_server())

    started = time.monotonic()
    WhisperServerBackend.shutdown()
    elapsed = time.monotonic() - started

    assert elapsed < 3, f"shutdown waited {elapsed:.1f}s for a load it no longer wants"
    thread.join(3)
    assert not thread.is_alive()
    assert isinstance(outcome["result"], ServerStartSuperseded)
    assert child.poll() is not None, "the loading child must not outlive the shutdown"
    assert WhisperServerBackend._server_process is None
    # A cancelled load is not a broken server: dictation keeps the fast path.
    assert WhisperServerBackend._server_disabled is False


def test_supersede_is_instant_and_the_load_stops_its_own_child(slow_server):
    thread, child, outcome = _start_in_background(slow_server())

    started = time.monotonic()
    switch = WhisperServerBackend.supersede()
    assert time.monotonic() - started < 0.05

    thread.join(3)
    assert not thread.is_alive()
    assert isinstance(outcome["result"], ServerStartSuperseded)
    assert child.poll() is not None
    assert WhisperServerBackend._server_disabled is False
    WhisperServerBackend.shutdown(before_epoch=switch)  # nothing left to stop


def test_the_old_mode_teardown_keeps_a_server_started_after_the_switch():
    def sleeper():
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    old = sleeper()
    WhisperServerBackend._server_epoch = WhisperServerBackend._epoch
    WhisperServerBackend._server_process = old
    switch = WhisperServerBackend.supersede()
    WhisperServerBackend.shutdown(before_epoch=switch)
    assert old.poll() is not None and WhisperServerBackend._server_process is None

    switch = WhisperServerBackend.supersede()
    new = sleeper()  # a dictation starts the new mode before the teardown runs
    WhisperServerBackend._server_epoch = WhisperServerBackend._epoch
    WhisperServerBackend._server_process = new
    WhisperServerBackend.shutdown(before_epoch=switch)
    assert new.poll() is None and WhisperServerBackend._server_process is new


def test_a_dictation_whose_server_start_is_cancelled_still_transcribes(slow_server, tmp_path):
    backend = slow_server()
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    cli = SimpleNamespace(transcribe=lambda path, context: "hello there")
    backend._cli_backend = cli

    def cancel_once_spawned():
        deadline = time.monotonic() + 5
        while WhisperServerBackend._server_process is None and time.monotonic() < deadline:
            time.sleep(0.01)
        WhisperServerBackend.supersede()

    threading.Thread(target=cancel_once_spawned, daemon=True).start()
    assert backend.transcribe(str(audio)) == "hello there"
    assert WhisperServerBackend._server_disabled is False


def test_switching_gpu_off_mid_load_never_blocks_the_tk_thread(slow_server, monkeypatch):
    # Sol PR 2 r1: GPU on started a warm-up; GPU off during its model load
    # called shutdown() on the Tk thread, which waited for the whole load.
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    post_warmed, released = [], []
    monkeypatch.setattr(
        "wayfinder.core.transcriber.warm_up_transcription",
        lambda config: slow_server(use_gpu=config["use_gpu"]).warm_up(),
    )
    monkeypatch.setattr(
        "wayfinder.core.postprocessor.warm_up_postprocessing",
        lambda config: post_warmed.append(config["use_gpu"]),
    )
    monkeypatch.setattr(
        wayfinder_main, "_release_cleanup_residency",
        lambda **kwargs: released.append(kwargs),
    )
    app = SimpleNamespace(
        config={"use_gpu": True, "whisper_server_mode": True,
                "transcription_backend": "whisper_cpp"},
        log=lambda _message: None,
    )
    wayfinder_main.WayfinderApp._warm_up_transcription(app, startup=False)
    deadline = time.monotonic() + 5
    while WhisperServerBackend._server_process is None:
        assert time.monotonic() < deadline, "the warm-up never spawned its server"
        time.sleep(0.01)
    child = WhisperServerBackend._server_process

    app.config["use_gpu"] = False
    started = time.monotonic()
    wayfinder_main.WayfinderApp._apply_transcription_hardware_change(app, False)
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"the switch blocked the Tk thread for {elapsed:.1f}s"
    assert app._model_teardown_done.wait(5), "the old mode's teardown never finished"
    assert child.poll() is not None, "the old mode's loading server must be stopped"
    assert WhisperServerBackend._server_process is None
    assert WhisperServerBackend._server_disabled is False
    assert post_warmed == [], "a superseded warm-up must not start GPU cleanup"
    assert len(released) == 1 and released[0]["llama_before_epoch"] is not None

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


def _ready_once_spawned(_self, _port, timeout=2):
    """A fake readiness probe: our child counts as loaded once it is running."""
    proc = WhisperServerBackend._server_process
    return proc is not None and proc.poll() is None


def _barrier_app(monkeypatch):
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    return SimpleNamespace(
        config={"use_gpu": True, "whisper_server_mode": True,
                "transcription_backend": "whisper_cpp"},
        log=lambda _message: None,
    )


def _switch_gpu_off_inside(app, in_stage, switched):
    """Land GPU off while the warm-up sits between a stage's generation check
    and that stage reading its server epoch (Sol PR 2 r2)."""
    assert in_stage.wait(5), "the warm-up never reached the stage"
    app.config["use_gpu"] = False
    wayfinder_main.WayfinderApp._apply_transcription_hardware_change(app, False)
    switched.set()
    assert app._model_teardown_done.wait(10), "the old mode's teardown never finished"


def test_a_switch_after_the_asr_stage_check_cancels_that_load(tmp_path, monkeypatch):
    # The GPU load used to read the switch's new epoch, so the old mode's
    # teardown took it for a new-mode server and kept it resident. Runs the
    # real warm_up_transcription -> get_backend -> _start_server.
    from wayfinder.core import transcriber

    (tmp_path / "whisper-cli").write_text("#!/bin/sh\n")
    (tmp_path / "whisper-cli").chmod(0o755)
    server = tmp_path / "whisper-server"
    server.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    server.chmod(0o755)
    (tmp_path / "ggml-base.en.bin").write_bytes(b"\x00")
    monkeypatch.setattr(WhisperServerBackend, "_is_our_server", _ready_once_spawned)
    in_stage, switched = threading.Event(), threading.Event()
    real_get_backend = transcriber.get_backend

    def build_after_the_switch(config):
        in_stage.set()
        switched.wait(5)
        return real_get_backend(config)

    monkeypatch.setattr(transcriber, "get_backend", build_after_the_switch)
    monkeypatch.setattr("wayfinder.core.postprocessor.warm_up_postprocessing",
                        lambda config: None)
    monkeypatch.setattr(wayfinder_main, "_release_cleanup_residency", lambda **_kw: None)
    app = _barrier_app(monkeypatch)
    app.config.update(whisper_binary=str(tmp_path / "whisper-cli"),
                      model_path=str(tmp_path / "ggml-base.en.bin"),
                      whisper_server_port=_free_port())

    wayfinder_main.WayfinderApp._warm_up_transcription(app, startup=False)
    _switch_gpu_off_inside(app, in_stage, switched)

    assert WhisperServerBackend._server_process is None, (
        "the warm-up's old-mode server outlived the switch")
    assert WhisperServerBackend._server_disabled is False


def test_a_dictation_snapshot_reads_its_epochs_before_its_settings(monkeypatch):
    # A switch writes the config, then supersedes. Read in that order too, a
    # snapshot can only carry an older epoch than its settings, never a newer.
    from wayfinder.core.llama_server import LOAD_EPOCH_KEY as LLAMA_KEY, LlamaServerManager
    from wayfinder.core.transcriber import LOAD_EPOCH_KEY as WHISPER_KEY

    before = (WhisperServerBackend.load_epoch(), LlamaServerManager.load_epoch())

    def read_during_a_switch(_app):
        WhisperServerBackend.supersede()
        LlamaServerManager.supersede()
        return {"use_gpu": False}

    monkeypatch.setattr(wayfinder_main.WayfinderApp, "_asr_config_for_dictation_unstamped",
                        staticmethod(read_during_a_switch))
    config = wayfinder_main.WayfinderApp._asr_config_for_dictation(SimpleNamespace())
    assert (config[WHISPER_KEY], config[LLAMA_KEY]) == before


class _FakeLlama:
    pid = -1

    def __init__(self):
        self.stdout = SimpleNamespace(readline=lambda: b"", close=lambda: None)
        self._rc = None

    def poll(self):
        return self._rc

    def terminate(self):
        self._rc = 0

    kill = terminate

    def wait(self, timeout=None):
        return self._rc


def test_a_switch_after_the_cleanup_stage_check_cancels_that_load(monkeypatch, tmp_path):
    from wayfinder.core.llama_server import (
        LOAD_EPOCH_KEY, LlamaServerError, LlamaServerManager,
    )

    binary, model = tmp_path / "llama-server", tmp_path / "m.gguf"
    binary.touch()
    model.touch()
    spawned, outcome = [], []
    monkeypatch.setattr(LlamaServerManager, "_find_port", lambda: 8179)
    monkeypatch.setattr(LlamaServerManager, "_wait_ready", lambda *_a, **_kw: True)
    monkeypatch.setattr(LlamaServerManager, "_serving_model", lambda *_a: True)
    monkeypatch.setattr(LlamaServerManager, "_owns_listener", lambda *_a: True)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *_a, **_kw: spawned.append(_FakeLlama()) or spawned[-1])
    monkeypatch.setattr("wayfinder.core.transcriber.warm_up_transcription",
                        lambda config: True)
    in_stage, switched = threading.Event(), threading.Event()

    def warm_cleanup(config):
        in_stage.set()
        switched.wait(5)
        try:
            LlamaServerManager.ensure(str(binary), str(model), 2048, 4,
                                      99 if config["use_gpu"] else 0,
                                      load_epoch=config.get(LOAD_EPOCH_KEY))
        except LlamaServerError as exc:
            outcome.append(exc)

    monkeypatch.setattr("wayfinder.core.postprocessor.warm_up_postprocessing", warm_cleanup)
    app = _barrier_app(monkeypatch)
    LlamaServerManager.reset_for_tests()
    try:
        wayfinder_main.WayfinderApp._warm_up_transcription(app, startup=False)
        _switch_gpu_off_inside(app, in_stage, switched)
        assert spawned == [] and LlamaServerManager._process is None, (
            "the GPU cleanup warm-up's server outlived the switch to CPU")
        assert len(outcome) == 1 and LlamaServerManager._disabled is False
    finally:
        LlamaServerManager.reset_for_tests()


def test_a_backend_configured_before_a_switch_leaves_the_new_server_alone(slow_server):
    stale = slow_server(use_gpu=True)
    WhisperServerBackend.supersede()
    new = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    WhisperServerBackend._server_epoch = WhisperServerBackend._epoch
    WhisperServerBackend._server_process = new
    with pytest.raises(ServerStartSuperseded):
        stale._start_server()
    assert new.poll() is None and WhisperServerBackend._server_process is new


def test_the_cleanup_style_warm_up_is_stamped(monkeypatch):
    from wayfinder.core.llama_server import LOAD_EPOCH_KEY, LlamaServerManager

    started = []
    monkeypatch.setattr("wayfinder.core.postprocessor.cleanup_model_needed", lambda _cfg: True)
    monkeypatch.setattr(
        wayfinder_main.threading, "Thread",
        lambda target, args=(), **_kw: SimpleNamespace(start=lambda: started.append(args)))
    wayfinder_main.WayfinderApp._sync_cleanup_residency(SimpleNamespace(config={"use_gpu": True}))
    assert started[0][0][LOAD_EPOCH_KEY] == LlamaServerManager.load_epoch()

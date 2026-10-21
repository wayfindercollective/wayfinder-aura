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
from pathlib import Path
from types import SimpleNamespace

import pytest

import wayfinder_main
from wayfinder.core.transcriber import (
    ServerStartSuperseded,
    TranscriptionError,
    WhisperServerBackend,
)

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
    WhisperServerBackend._server_disabled_epoch = None


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


def _cleanup_setup(tmp_path, monkeypatch):
    """A cleanup config whose server starts, through the real get_backend()
    and ensure(), spawn a fake child that is ready at once."""
    from wayfinder.core.llama_server import LlamaServerManager

    for name in ("llama-simple", "llama-server", "m.gguf"):
        (tmp_path / name).touch()
    spawned = []
    monkeypatch.setattr(LlamaServerManager, "_find_port", lambda: 8179)
    monkeypatch.setattr(LlamaServerManager, "_wait_ready", lambda *_a, **_kw: True)
    monkeypatch.setattr(LlamaServerManager, "_serving_model", lambda *_a: True)
    monkeypatch.setattr(LlamaServerManager, "_owns_listener", lambda *_a: True)
    monkeypatch.setattr(LlamaServerManager, "still_ours", lambda *_a: True)
    monkeypatch.setattr(LlamaServerManager, "complete",
                        lambda **_kw: {"content": "x", "stop_type": "eos"})
    monkeypatch.setattr(subprocess, "Popen",
                        lambda *_a, **_kw: spawned.append(_FakeLlama()) or spawned[-1])
    monkeypatch.setattr(wayfinder_main, "save_config", lambda _cfg: None)
    config = {"post_processing_backend": "llama_cpp", "llama_cpp_use_cli": True,
              "llama_cpp_binary": str(tmp_path / "llama-simple"),
              "llama_cpp_model_path": str(tmp_path / "m.gguf"),
              "llama_cpp_residency": "auto"}
    return config, spawned


def _snapshot(config):
    """What a dictation reads: the epochs first, then the settings."""
    epochs = wayfinder_main._server_load_epochs()
    snapshot = dict(config)
    snapshot.update(epochs)
    return snapshot


def _clean_up_from(snapshot):
    from wayfinder.core import postprocessor
    return postprocessor.get_backend(snapshot)._server_generate("prompt", 8)


@pytest.fixture
def _llama_state():
    from wayfinder.core.llama_server import LlamaServerManager
    LlamaServerManager.reset_for_tests()
    yield LlamaServerManager
    LlamaServerManager.reset_for_tests()


def test_a_cleanup_read_before_save_memory_starts_no_resident_server(
        tmp_path, monkeypatch, _llama_state):
    config, spawned = _cleanup_setup(tmp_path, monkeypatch)
    snapshot = _snapshot(config)   # the dictation began with the model resident
    app = SimpleNamespace(config=config, log=lambda *_a: None,
                          _residency_map={"Save memory (loads per use)": "save_memory"})
    wayfinder_main.WayfinderApp._on_residency_changed(app, "Save memory (loads per use)")
    # Its cleanup runs after the user chose Save memory.
    assert _clean_up_from(snapshot) is None
    assert spawned == [] and _llama_state._process is None
    assert _llama_state._consecutive_failures == 0


def test_a_cleanup_read_before_a_model_change_does_not_reload_the_old_model(
        tmp_path, monkeypatch, _llama_state):
    config, spawned = _cleanup_setup(tmp_path, monkeypatch)
    assert _clean_up_from(_snapshot(config)) is not None
    old = spawned[-1]
    snapshot = _snapshot(config)
    (tmp_path / "new.gguf").touch()
    config["llama_cpp_model_path"] = str(tmp_path / "new.gguf")
    wayfinder_main._release_cleanup_residency()   # what every model change calls
    assert old.poll() is not None
    assert _clean_up_from(snapshot) is None
    assert len(spawned) == 1 and _llama_state._process is None


def test_internal_stops_and_old_mode_teardowns_keep_current_snapshots(
        tmp_path, monkeypatch, _llama_state):
    config, spawned = _cleanup_setup(tmp_path, monkeypatch)
    switch_epoch = _llama_state.supersede()   # a CPU/GPU switch
    snapshot = _snapshot(config)              # read in the new mode
    wayfinder_main._release_cleanup_residency(llama_before_epoch=switch_epoch)
    _llama_state.shutdown()                   # a failure or timeout restart
    assert _clean_up_from(snapshot) is not None
    assert len(spawned) == 1 and _llama_state._process is spawned[0]


def test_a_quick_switch_back_to_a_cleanup_style_keeps_its_warm_server(
        tmp_path, monkeypatch, _llama_state):
    config, spawned = _cleanup_setup(tmp_path, monkeypatch)
    queued = []
    monkeypatch.setattr(
        wayfinder_main.threading, "Thread",
        lambda target, args=(), kwargs=None, **_kw: SimpleNamespace(
            start=lambda: queued.append((target, args, kwargs or {}))))
    needed = [False]
    monkeypatch.setattr("wayfinder.core.postprocessor.cleanup_model_needed",
                        lambda _cfg: needed[0])
    app = SimpleNamespace(config=config)
    wayfinder_main.WayfinderApp._sync_cleanup_residency(app)   # to Normal
    needed[0] = True
    wayfinder_main.WayfinderApp._sync_cleanup_residency(app)   # straight back
    (release, _, release_kwargs), (_, warm_args, _) = queued
    assert _clean_up_from(warm_args[0]) is not None   # the warm-up runs first
    release(**release_kwargs)                          # the release runs late
    assert len(spawned) == 1 and spawned[0].poll() is None
    assert _llama_state._process is spawned[0]


@pytest.mark.parametrize("terminal", [False, True])
def test_an_intent_release_spares_the_new_model_a_fresh_request_started(
        tmp_path, monkeypatch, _llama_state, terminal):
    # Sol PR 2 r4: the release superseded, a fresh request started the newly
    # chosen model before the stop took the lock, and the plain stop killed it.
    config, spawned = _cleanup_setup(tmp_path, monkeypatch)
    assert _clean_up_from(_snapshot(config)) is not None   # the old model, resident
    (tmp_path / "new.gguf").touch()
    config["llama_cpp_model_path"] = str(tmp_path / "new.gguf")
    loading, loaded = threading.Event(), threading.Event()

    def slow_ready(*_a, **_kw):
        loading.set()
        return loaded.wait(10)

    monkeypatch.setattr(_llama_state, "_wait_ready", slow_ready)
    real_supersede = _llama_state.supersede.__func__
    fresh = []

    def supersede_then_dictate(cls):
        cutoff = real_supersede(cls)
        if not fresh:   # a dictation right after the change, in the new model
            fresh.append(threading.Thread(
                target=lambda: _clean_up_from(_snapshot(config)), daemon=True))
            fresh[0].start()
            if loading.wait(1 if terminal else 5):
                threading.Timer(0.3, loaded.set).start()
        return cutoff

    monkeypatch.setattr(_llama_state, "supersede", classmethod(supersede_then_dictate))
    wayfinder_main._release_cleanup_residency(terminal=terminal)
    fresh[0].join(10)
    assert spawned[0].poll() is not None   # the old model is gone either way
    if terminal:   # Quit admits no start after it: the dictation's is refused
        assert len(spawned) == 1 and _llama_state._process is None
    else:
        assert spawned[1].poll() is None and _llama_state._process is spawned[1]


def test_a_stale_backend_missing_its_old_model_does_not_disable_the_new_mode(slow_server):
    # Sol PR 2 r4: its availability check ran first and disabled the server
    # for every dictation in the new mode.
    stale = slow_server(use_gpu=True)
    WhisperServerBackend.supersede()
    Path(stale.model_path).unlink()
    with pytest.raises(ServerStartSuperseded):
        stale._start_server()
    assert WhisperServerBackend._server_disabled is False
    assert not WhisperServerBackend._disabled_now()


def test_a_failed_whisper_start_disables_only_the_mode_it_was_configured_in(tmp_path):
    binary = tmp_path / "whisper-server"
    binary.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    binary.chmod(0o755)
    model = tmp_path / "ggml-base.bin"
    model.write_bytes(b"\x00")
    backend = WhisperServerBackend(
        whisper_server_binary=str(binary), model_path=str(model), port=_free_port(),
        use_gpu=False, startup_timeout=STARTUP_TIMEOUT)
    with pytest.raises(TranscriptionError):
        backend._start_server()
    assert WhisperServerBackend._disabled_now()
    WhisperServerBackend.supersede()   # GPU on, or another model
    assert not WhisperServerBackend._disabled_now()


@pytest.mark.parametrize("how", ["plain", "terminal", "at exit"])
def test_a_plain_whisper_shutdown_spares_a_start_that_read_the_new_settings(
        slow_server, how):
    # A start holding the lock that read the settings after this shutdown's
    # own supersede is already in the new mode.
    slow_server()   # creates the server lock
    held = threading.Event()
    started = {}

    def start_in_the_new_mode():
        with WhisperServerBackend._server_lock:
            epoch = WhisperServerBackend._epoch
            held.set()
            deadline = time.monotonic() + 5
            while WhisperServerBackend._epoch == epoch and time.monotonic() < deadline:
                time.sleep(0.01)
            started["proc"] = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"])
            WhisperServerBackend._server_epoch = WhisperServerBackend._epoch
            WhisperServerBackend._server_process = started["proc"]

    holder = threading.Thread(target=start_in_the_new_mode, daemon=True)
    holder.start()
    assert held.wait(5)
    if how == "at exit":
        WhisperServerBackend._shutdown_at_exit()
    else:
        WhisperServerBackend.shutdown(terminal=how == "terminal")
    holder.join(5)
    new = started["proc"]
    if how != "plain":
        assert new.poll() is not None and WhisperServerBackend._server_process is None
    else:
        assert new.poll() is None and WhisperServerBackend._server_process is new


def _one_attempt_backend(tmp_path, script, startup_timeout=STARTUP_TIMEOUT):
    binary = tmp_path / "whisper-server"
    binary.write_text(f"#!{sys.executable}\n{script}\n")
    binary.chmod(0o755)
    model = tmp_path / "ggml-base.bin"
    model.write_bytes(b"\x00")
    backend = WhisperServerBackend(
        whisper_server_binary=str(binary), model_path=str(model), port=_free_port(),
        use_gpu=False, startup_timeout=startup_timeout)
    attempts = backend._server_cmd_attempts
    backend._server_cmd_attempts = lambda port: attempts(port)[:1]   # the last rung
    return backend


def test_a_switch_as_the_last_attempt_dies_is_not_a_failed_start(tmp_path, monkeypatch):
    # The loop's own checks ran before the switch's kill reached the child.
    backend = _one_attempt_backend(tmp_path, "import sys\nsys.exit(3)")

    def switch_then_read(cls):
        WhisperServerBackend.supersede()
        return ""

    monkeypatch.setattr(WhisperServerBackend, "_server_output", classmethod(switch_then_read))
    with pytest.raises(ServerStartSuperseded):
        backend._start_server()
    assert WhisperServerBackend._server_disabled is False


def test_a_switch_as_the_startup_wait_runs_out_is_not_a_hang(tmp_path, monkeypatch):
    backend = _one_attempt_backend(tmp_path, "import time\ntime.sleep(60)", startup_timeout=0.5)
    probed = []

    def not_ready(_self, _port, timeout=2):
        if WhisperServerBackend._server_process is not None:   # the readiness probe
            probed.append(True)
        return False

    class _SwitchAfterTheLastProbe(subprocess.Popen):
        def poll(self):
            if probed and not getattr(self, "_switched", False):
                self._switched = True
                WhisperServerBackend.supersede()
            return super().poll()

    monkeypatch.setattr(WhisperServerBackend, "_is_our_server", not_ready)
    monkeypatch.setattr(subprocess, "Popen", _SwitchAfterTheLastProbe)
    with pytest.raises(ServerStartSuperseded):
        backend._start_server()
    assert probed, "the readiness loop never probed"
    assert WhisperServerBackend._server_disabled is False


def _main_body(marker):
    src = Path(wayfinder_main.__file__).read_text()
    indent = len(marker) - len(marker.lstrip(" "))
    return src.split(marker, 1)[1].split("\n" + " " * indent + "def ", 1)[0]


@pytest.mark.parametrize("marker", ["    def quit_app(self, icon=None, item=None):",
                                    "    def cleanup_all():",
                                    "    def _restart_for_overlay_change(self):"])
def test_quit_and_restart_stop_everything_a_racing_start_too(marker):
    # Their plain shutdowns would spare a server a dictation started while
    # they waited for the lock, and leave it running after the app is gone.
    body = _main_body(marker)
    assert "WhisperServerBackend.shutdown(terminal=True)" in body
    assert "WhisperServerBackend.shutdown()" not in body
    if "overlay" not in marker:
        assert "_release_cleanup_residency(terminal=True)" in body
        assert "_release_cleanup_residency()" not in body
    else:
        # The re-exec'd app starts its own cleanup server; a failed exec is
        # still this process, which needs both servers back.
        assert "LlamaServerManager.close()" in body
        assert "WhisperServerBackend.reopen()" in body and "LlamaServerManager.reopen()" in body


def test_the_terminal_release_closes_the_cleanup_server():
    body = _main_body("def _release_cleanup_residency(")
    assert "LlamaServerManager.close()" in body


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_no_whisper_start_is_admitted_after_a_terminal_shutdown(slow_server):
    # Sol PR 2 r5: a dictation that read its settings just after Quit's
    # supersede started a server once the shutdown was done, and os._exit()
    # then orphaned it.
    slow_server()   # creates the server lock
    WhisperServerBackend.shutdown(terminal=True)
    late = slow_server()   # read under the epoch the Quit left behind
    with pytest.raises(ServerStartSuperseded):
        late._start_server()
    assert WhisperServerBackend._server_process is None
    assert not WhisperServerBackend._disabled_now()
    WhisperServerBackend.reopen()   # a restart that did not exit
    thread, proc, outcome = _start_in_background(slow_server())
    assert proc.poll() is None
    WhisperServerBackend.supersede()
    thread.join(10)


def test_the_closed_flag_alone_refuses_a_start_waiting_for_the_lock(slow_server):
    # Independent of the epoch check, which also catches every start that
    # read its settings before a terminal shutdown's supersede.
    late = slow_server()
    outcome = {}

    def start():
        try:
            late._start_server()
            outcome["result"] = "started"
        except Exception as exc:  # recorded for the assertions
            outcome["result"] = exc

    with WhisperServerBackend._server_lock:   # the terminal stop holds it
        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        time.sleep(0.3)   # past the entry checks, waiting for the lock
        WhisperServerBackend._closed = True
    thread.join(5)
    assert isinstance(outcome.get("result"), ServerStartSuperseded)
    assert WhisperServerBackend._server_process is None


def test_a_server_reused_after_quick_switches_survives_their_teardowns(
        slow_server, monkeypatch):
    # Sol PR 2 r5: GPU on, off and on again before the teardowns ran. A
    # dictation reused the resident server, and the off switch's teardown,
    # still seeing the old mode's spawn, stopped it under the dictation.
    slow_server()
    resident = _sleeper()
    WhisperServerBackend._server_epoch = WhisperServerBackend._epoch
    WhisperServerBackend._server_process = resident
    off = WhisperServerBackend.supersede()
    on = WhisperServerBackend.supersede()
    monkeypatch.setattr(WhisperServerBackend, "_server_reusable", lambda self: True)
    slow_server()._start_server()   # the dictation, configured after both
    WhisperServerBackend.shutdown(before_epoch=off)
    WhisperServerBackend.shutdown(before_epoch=on)
    assert resident.poll() is None and WhisperServerBackend._server_process is resident
    # Not reused by the next mode, it goes with that mode's teardown.
    WhisperServerBackend.shutdown(before_epoch=WhisperServerBackend.supersede())
    assert resident.poll() is not None

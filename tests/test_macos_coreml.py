"""Core ML is explicit, asset-validated, and behind the existing GPU gate."""
import contextlib
import io
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayfinder.utils import macos_coreml


def prepared(tmp_path, monkeypatch):
    from wayfinder.utils.macos_coreml_convert import TEMPLATE_VERSION, model_identity
    monkeypatch.setattr(macos_coreml, "_conversion_root", lambda: tmp_path / "derived")
    cli = tmp_path / "bin" / "whisper-cli"
    twin = cli.parent / "whisper-coreml" / "whisper-cli"
    twin.parent.mkdir(parents=True)
    for file in (cli, twin, twin.with_name("whisper-server")):
        file.write_bytes(b"test executable")
        file.chmod(0o700)
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"test model")
    converted = macos_coreml.converted_model_path(str(model))
    converted.parent.mkdir(parents=True)
    os.link(model, converted)  # no symlink privilege required on Windows CI
    asset = macos_coreml.encoder_path(str(converted))
    (asset / "weights").mkdir(parents=True)
    for name in ("coremldata.bin", "model.mil", "weights/weight.bin"):
        (asset / name).write_bytes(b"test asset")
    (asset / macos_coreml.COREML_MANIFEST).write_text(json.dumps({
        "format": 2, "source": model_identity(model), "template": TEMPLATE_VERSION,
    }))
    return cli, twin, model, asset


@pytest.mark.parametrize("name", ["ggml-large-v3-turbo.bin", "ggml-large-v3-turbo-q5_0.bin", "ggml-large-v3-turbo-q8_0.bin"])
def test_matches_native_sidecar_lookup(name):
    assert macos_coreml.encoder_path("/models/" + name) == Path("/models/ggml-large-v3-turbo-encoder.mlmodelc")


def test_prepared_macos_twin(tmp_path, monkeypatch):
    cli, twin, model, _ = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    assert macos_coreml.prepared_twin(str(cli), str(model)) == str(twin)


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_never_select_coreml(tmp_path, monkeypatch, platform):
    cli, _, model, _ = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform=platform))
    assert macos_coreml.coreml_twin(str(cli), str(model)) is None


@pytest.mark.parametrize("missing", ["coremldata.bin", "model.mil", "weights/weight.bin", "aura-coreml.json"])
def test_partial_assets_fall_back_to_metal(tmp_path, monkeypatch, missing):
    cli, _, model, asset = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    (asset / missing).unlink()
    assert macos_coreml.coreml_twin(str(cli), str(model)) is None


@pytest.mark.parametrize("manifest", ["invalid JSON", "[]", '{"format":2}', '{"format":1,"checkpoint_sha256":"wrong"}'])
def test_unverified_conversion_falls_back(tmp_path, monkeypatch, manifest):
    cli, _, model, asset = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    (asset / macos_coreml.COREML_MANIFEST).write_text(manifest)
    assert macos_coreml.coreml_twin(str(cli), str(model)) is None


@pytest.mark.parametrize("platform,gpu,licensed,opt_in,selected", [
    ("darwin", True, True, True, True),
    ("darwin", True, False, True, False),
    ("darwin", False, True, True, False),
    ("darwin", True, True, False, False),
    ("linux", True, True, True, False),
    ("win32", True, True, True, False),
])
def test_selection_is_opt_in_and_entitlement_bound(monkeypatch, platform, gpu, licensed, opt_in, selected):
    calls = selection_env(monkeypatch, platform=platform, licensed=licensed)
    server_state(monkeypatch, **COREML_LOADED)
    backend = get_backend(use_gpu=gpu, whisper_coreml_encoder=opt_in)
    assert bool(calls) is selected
    assert ("whisper-coreml" in backend.whisper_server_binary) is selected
    assert (backend.metal_fallback is not None) is selected
    assert backend.startup_timeout == 30.0  # no start budget sized for a device compile
    assert backend.use_gpu is (gpu and licensed)


# --- Cold ANE cache: no dictation waits for a Core ML load ---------------------

METAL_SERVER, COREML_SERVER = "/native/bin/whisper-server", "/native/bin/whisper-coreml/whisper-server"
SOURCE_MODEL, CONVERTED_MODEL = "/models/ggml-large-v3-turbo-q5_0.bin", "/derived/ggml-large-v3-turbo-q5_0.bin"


class Alive:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


COREML_LOADED = dict(_server_process=Alive(), _server_binary=COREML_SERVER, _server_model_path=CONVERTED_MODEL,
                     _server_use_gpu=True, _server_threads=4)


def server_state(monkeypatch, **state):
    """Private copy of WhisperServerBackend's class-level server, restored afterwards."""
    from wayfinder.core.transcriber import WhisperServerBackend
    cls = WhisperServerBackend
    for name in ("_server_process", "_server_port", "_server_model_path", "_server_binary", "_server_use_gpu",
                 "_server_threads", "_server_log_tail", "_server_log_thread", "_server_disabled"):
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(cls, "_server_lock", threading.Lock())
    cls._server_process, cls._server_disabled = None, False
    for name, value in state.items():
        setattr(cls, name, value)
    return cls


def selection_env(monkeypatch, platform="darwin", licensed=True):
    """get_backend() on a licensed Mac whose comparison selected Core ML."""
    from wayfinder.core import transcriber
    from wayfinder.license import FeatureGate
    calls = []
    monkeypatch.setattr(transcriber, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(FeatureGate, "has_feature", lambda self, feature: licensed)
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda configured: "/native/bin/whisper-cli")
    monkeypatch.setattr(transcriber.WhisperServerBackend, "is_available", lambda self: True)
    monkeypatch.setattr(macos_coreml, "coreml_twin",
                        lambda cli, model: calls.append((cli, model)) or "/native/bin/whisper-coreml/whisper-cli")
    monkeypatch.setattr(macos_coreml, "converted_model_path", lambda model: Path(CONVERTED_MODEL))
    return calls


def get_backend(**settings):
    from wayfinder.core import transcriber
    return transcriber.get_backend({"transcription_backend": "whisper_cpp", "model_path": SOURCE_MODEL,
                                    "use_gpu": True, "threads": 4, **settings})


@pytest.mark.parametrize("resident", [
    {},  # launch: nothing loaded yet
    dict(_server_process=Alive(), _server_binary=METAL_SERVER, _server_model_path=SOURCE_MODEL,
         _server_use_gpu=True, _server_threads=4),
    {**COREML_LOADED, "_server_process": Alive(returncode=-9)},  # it exited
    {**COREML_LOADED, "_server_threads": 8},  # a restart would be needed
], ids=["none", "metal", "exited", "other-threads"])
def test_selected_coreml_serves_only_once_its_server_is_loaded(monkeypatch, resident):
    selection_env(monkeypatch)
    server_state(monkeypatch, **resident)
    backend = get_backend()
    assert (backend.whisper_server_binary, backend.model_path) == (METAL_SERVER, SOURCE_MODEL)
    assert backend.metal_fallback is None and backend.startup_timeout == 30.0
    server_state(monkeypatch, **COREML_LOADED)
    backend = get_backend()
    assert (backend.whisper_server_binary, backend.model_path) == (COREML_SERVER, CONVERTED_MODEL)
    assert (backend.metal_fallback.whisper_server_binary, backend.metal_fallback.model_path) == (METAL_SERVER, SOURCE_MODEL)


def test_cli_mode_never_loads_coreml_per_dictation(monkeypatch):
    from wayfinder.core.transcriber import WhisperCppBackend
    selection_env(monkeypatch)
    server_state(monkeypatch, **COREML_LOADED)
    backend = get_backend(whisper_server_mode=False)
    assert isinstance(backend, WhisperCppBackend)
    assert (backend.whisper_binary, backend.model_path) == ("/native/bin/whisper-cli", SOURCE_MODEL)


@pytest.fixture
def dictation_server(monkeypatch, tmp_path):
    """A Core ML backend from get_backend() plus fakes that record every spawn.

    A Core ML child would sit in its device compile for minutes, so the test
    fails if one is ever spawned; Metal answers at once.
    """
    import json as _json
    from wayfinder.core import transcriber
    selection_env(monkeypatch)
    cls = server_state(monkeypatch, **COREML_LOADED)
    backend = get_backend(whisper_server_timeout=5)
    audio = tmp_path / "dictation.wav"
    audio.write_bytes(b"RIFF")
    env = SimpleNamespace(backend=backend, cls=cls, spawned=[], posts=[], failures=0, audio=str(audio))

    def popen(command, **kwargs):
        assert "whisper-coreml" not in command[0], "a dictation started the Core ML server"
        env.spawned.append(command[0])
        return SimpleNamespace(poll=lambda: None, stdout=io.BytesIO(), pid=4242)

    def urlopen(request, timeout=None):
        env.posts.append(request.full_url)
        if env.failures:
            env.failures -= 1
            raise TimeoutError("inference wedged")
        return io.BytesIO(_json.dumps({"text": "hello"}).encode())

    monkeypatch.setattr(transcriber.subprocess, "Popen", popen)
    monkeypatch.setattr(transcriber, "wrap_macos_child_command", lambda command, env: (command, env))
    monkeypatch.setattr(transcriber, "urlopen_loopback", urlopen)
    monkeypatch.setattr(cls, "_find_available_port", lambda self, probe_timeout=2, reuse_ok=True: 8200)
    monkeypatch.setattr(cls, "_is_our_server", lambda self, port, timeout=2: True)
    monkeypatch.setattr(cls, "_stop_server_internal", classmethod(lambda c, deadline=None: setattr(c, "_server_process", None)))
    monkeypatch.setattr(cls, "_server_port", 8199)
    return env


def test_loaded_coreml_serves_without_any_start(dictation_server):
    assert dictation_server.backend.metal_fallback is not None  # get_backend() chose Core ML
    assert dictation_server.backend.transcribe(dictation_server.audio) == "hello"
    assert dictation_server.spawned == []
    assert dictation_server.posts == ["http://127.0.0.1:8199/inference"]


def test_coreml_server_gone_before_the_request_falls_to_metal(dictation_server):
    dictation_server.cls._server_process = Alive(returncode=-9)  # exited after get_backend()
    started = time.monotonic()
    assert dictation_server.backend.transcribe(dictation_server.audio) == "hello"
    assert time.monotonic() - started < 5
    assert dictation_server.spawned == [METAL_SERVER]
    assert (dictation_server.cls._server_binary, dictation_server.cls._server_model_path) == (METAL_SERVER, SOURCE_MODEL)
    assert dictation_server.cls._server_disabled is False


def test_recovery_from_a_wedged_coreml_server_restarts_metal(dictation_server):
    dictation_server.failures = 1
    assert dictation_server.backend.transcribe(dictation_server.audio) == "hello"
    assert dictation_server.spawned == [METAL_SERVER]
    assert dictation_server.posts == ["http://127.0.0.1:8199/inference", "http://127.0.0.1:8200/inference"]


def test_cli_salvage_for_coreml_uses_metal(dictation_server):
    backend = dictation_server.backend
    cli = backend._cli_fallback()
    assert cli is backend.metal_fallback._cli_fallback()
    assert cli.model_path == SOURCE_MODEL and "whisper-coreml" not in cli.whisper_binary


def test_automatic_default_is_macos_only():
    import sys
    from wayfinder.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["whisper_coreml_encoder"] is (sys.platform == "darwin")


@pytest.mark.parametrize("metal,coreml,same_words,expected", [
    (1.0, 0.55, True, True), (0.2, 0.5, True, False),
    (1.0, 0.95, True, False), (1.0, 0.55, False, False),
])
def test_faster_and_matching_words_are_required(tmp_path, monkeypatch, metal, coreml, same_words, expected):
    cli, twin, model, _ = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_cache_path", lambda: tmp_path / "selection.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"chip": "test", "os": "test"})
    assert macos_coreml.record_comparison(str(cli), str(model), metal, coreml, same_words) is expected
    assert macos_coreml.coreml_twin(str(cli), str(model)) == (str(twin) if expected else None)


def test_comparison_invalidates_when_model_changes(tmp_path, monkeypatch):
    cli, _, model, _ = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_cache_path", lambda: tmp_path / "selection.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"chip": "test"})
    macos_coreml.record_comparison(str(cli), str(model), 1.0, 0.55, True)
    model.write_bytes(b"changed model")
    assert macos_coreml.comparison(str(cli), str(model)) is None


def test_precision_variants_never_share_converted_weights(tmp_path, monkeypatch):
    _, _, model, _ = prepared(tmp_path, monkeypatch)
    full = model.with_name("ggml-large-v3-turbo.bin")
    full.write_bytes(b"full precision model")
    assert macos_coreml.converted_model_path(str(model)).parent != macos_coreml.converted_model_path(str(full)).parent


def test_legacy_developer_export_not_selected(tmp_path, monkeypatch):
    cli, _, model, asset = prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    (asset / macos_coreml.COREML_MANIFEST).write_text(json.dumps({
        "format": 1, "checkpoint_sha256": macos_coreml.TURBO_CHECKPOINT_SHA256,
        "conversion": "fp32-normalization-load-fp16-compute",
    }))
    assert macos_coreml.prepared_twin(str(cli), str(model)) is None


def test_active_dictation_blocks_switch_before_breadcrumb_changes(monkeypatch):
    import threading
    from unittest.mock import Mock
    from wayfinder.core.transcriber import WhisperServerBackend
    macos_coreml.set_dictation_active(True)
    monkeypatch.setattr(macos_coreml, "_idle", lambda: not macos_coreml._dictation_active.is_set())
    monkeypatch.setattr(WhisperServerBackend, "_server_lock", threading.Lock())
    record = Mock()
    monkeypatch.setattr(macos_coreml, "record_comparison", record)
    try:
        with pytest.raises(InterruptedError):
            macos_coreml._activate_probe(Mock(), "cli", "model", 1.0, .5, True, lambda: False)
        record.assert_not_called()
    finally:
        macos_coreml.set_dictation_active(False)


@pytest.mark.parametrize("preferred", [True, False])
def test_handoff_only_adopts_faster_matching_probe(monkeypatch, preferred):
    import threading
    from unittest.mock import Mock
    from wayfinder.core.transcriber import WhisperServerBackend
    monkeypatch.setattr(macos_coreml, "_idle", lambda: True)
    monkeypatch.setattr(macos_coreml, "record_comparison", lambda *args: preferred)
    stop = Mock()
    monkeypatch.setattr(macos_coreml, "_stop_probe", stop)
    cls = WhisperServerBackend
    old = Mock()
    for name in ("_server_process", "_server_port", "_server_model_path", "_server_binary", "_server_use_gpu",
                 "_server_threads", "_server_log_tail", "_server_log_thread", "_server_disabled"):
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(cls, "_server_lock", threading.Lock())
    cls._server_process = old
    proc = Mock()
    proc.poll.return_value = None
    probe = SimpleNamespace(proc=proc, port=9000, tail=[], reader=Mock(), adopted=False,
                            backend=SimpleNamespace(model_path="converted.bin", whisper_server_binary="coreml-server", threads=4))
    assert macos_coreml._activate_probe(probe, "cli", "model", 1.0, .5, True, lambda: False) is preferred
    assert probe.adopted is preferred
    assert cls._server_process is (proc if preferred else old)
    assert stop.called is preferred


@pytest.mark.parametrize("macos", [True, False])
def test_macos_does_not_hold_completed_indicator_open(monkeypatch, macos):
    import time
    from unittest.mock import Mock
    import wayfinder_main
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", macos)
    app = SimpleNamespace(session_generation=3, log=Mock(), _game_mode=False,
                          _processing_start_time=time.time(), after=Mock(), _finish_injection=Mock())
    wayfinder_main.WayfinderApp.on_injection_done(app, 3)
    assert app._finish_injection.called is macos
    assert app.after.called is (not macos)


def test_native_build_is_separate_and_fallback_enabled():
    root = Path(__file__).resolve().parents[1]
    builder = (root / "packaging/macos/build.py").read_text()
    spec = (root / "wayfinder-aura-macos.spec").read_text()
    assert '"-DWHISPER_COREML=ON"' in builder
    assert '"-DWHISPER_COREML_ALLOW_FALLBACK=ON"' in builder
    assert "bin/whisper-coreml" in spec


def test_export_loads_normalization_before_half_conversion():
    root = Path(__file__).resolve().parents[1]
    exporter = (root / "packaging/macos/export_coreml_encoder.py").read_text()
    assert 'tensor.float() if "_ln." in name or "ln_post." in name' in exporter
    assert "TURBO_CHECKPOINT_SHA256" in exporter
    assert "dtype=np.float32" in exporter
    assert "compute_precision=ct.precision.FLOAT16" in exporter


# --- Preparation backoff and the never-during-recording guarantee ---------------


@pytest.fixture
def activity(monkeypatch, tmp_path):
    """Dictation flag only (no status breadcrumb), private history and child registry."""
    monkeypatch.setattr(macos_coreml, "_idle", lambda: not macos_coreml._dictation_active.is_set())
    monkeypatch.setattr(macos_coreml, "_attempts_path", lambda: tmp_path / "coreml-attempts.json")
    monkeypatch.setattr(macos_coreml, "_children", set())
    macos_coreml.set_dictation_active(False)
    yield tmp_path / "coreml-attempts.json"
    macos_coreml.set_dictation_active(False)


def fake_clock(monkeypatch, start=1_000_000.0):
    now = [start]
    monkeypatch.setattr(macos_coreml, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic, sleep=time.sleep, perf_counter=time.perf_counter))
    return now


def test_failures_back_off_persistently_then_stop(activity, monkeypatch):
    now = fake_clock(monkeypatch)
    hour, day = 3600.0, 24 * 3600.0
    assert macos_coreml._attempt_allowed("key")
    macos_coreml._note_attempt("key", count=True)
    macos_coreml._note_attempt("key", count=False, failure="TimeoutError")
    row = json.loads(activity.read_text())["key"]
    assert row["attempts"] == 1 and row["last_failure"] == "TimeoutError"
    for later in (0.0, hour, day - 1):  # e.g. right after the next dictation or a relaunch
        now[0] = 1_000_000.0 + later
        assert not macos_coreml._attempt_allowed("key")
    now[0] = 1_000_000.0 + day
    assert macos_coreml._attempt_allowed("key")
    macos_coreml._note_attempt("key", count=True)
    now[0] += 7 * day - 1
    assert not macos_coreml._attempt_allowed("key")
    now[0] += 1
    assert macos_coreml._note_attempt("key", count=True) == len(macos_coreml.RETRY_AFTER_SECONDS)
    now[0] += 10 * 365 * day
    assert not macos_coreml._attempt_allowed("key")  # capped for good on this model/Mac


def test_attempt_history_follows_model_binaries_chip_and_os(tmp_path, monkeypatch):
    cli, twin, model, asset = prepared(tmp_path, monkeypatch)
    identity = {"hardware": "MacBookAir10,1", "os": "26.6.2", "arch": "arm64"}
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: dict(identity))
    key = lambda: macos_coreml._attempt_key(str(cli), str(model))
    original = key()
    (asset / "weights/weight.bin").write_bytes(b"reconverted weights")
    assert key() == original  # reconversion does not reset the count
    identity["os"] = "26.7"
    assert key() != original
    identity.update(os="26.6.2", hardware="Mac15,3")
    assert key() != original
    identity["hardware"] = "MacBookAir10,1"
    assert key() == original
    twin.with_name("whisper-server").write_bytes(b"updated app build")
    assert key() != original
    updated_binaries = key()
    model.write_bytes(b"different model revision")
    assert key() not in (original, updated_binaries)


def run_pass(monkeypatch, fake_prepare, order=None, row=None, serving=None):
    """One worker pass with the real attempt history and fakes for the heavy parts.

    ``row`` is an earlier comparison; ``serving`` is what get_backend() returns.
    """
    from wayfinder.core import transcriber
    from wayfinder.utils import macos_activity
    order = [] if order is None else order
    serving = SimpleNamespace(use_gpu=True, metal_fallback=None) if serving is None else serving
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda configured: "/bin/whisper-cli")
    monkeypatch.setattr(transcriber, "get_backend", lambda config: serving)
    monkeypatch.setattr(macos_coreml, "_attempt_key", lambda cli, model: "key")
    monkeypatch.setattr(macos_coreml, "comparison", lambda cli, model: row)

    def quiet(cancelled, seconds=None):
        order.append("quiet")
        return macos_coreml._dictations

    @contextlib.contextmanager
    def held(key, reason):
        order.append("hold")
        yield True

    monkeypatch.setattr(macos_coreml, "_wait_for_quiet", quiet)
    monkeypatch.setattr(macos_activity, "held", held)
    monkeypatch.setattr(macos_coreml, "prepare_comparison", fake_prepare)
    monkeypatch.setattr(macos_coreml, "_latest_config", {"model_path": "/models/ggml-large-v3-turbo-q5_0.bin"})
    macos_coreml._preparation_requested.clear()
    return macos_coreml._preparation_pass()


def test_device_timeout_is_recorded_and_not_retried_after_dictation(activity, monkeypatch, capsys):
    calls, order = [], []

    def times_out(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        order.append("work")
        calls.append(config)
        on_device_attempt()
        raise TimeoutError("Encoder device preparation timed out")

    assert run_pass(monkeypatch, times_out, order) is False
    assert order == ["quiet", "hold", "work"]  # nothing starts, nor is held awake, before a quiet spell
    row = json.loads(activity.read_text())["key"]
    assert row["attempts"] == 1 and row["last_failure"] == "TimeoutError"
    assert "next attempt no sooner than 24 h" in capsys.readouterr().out
    macos_coreml.set_dictation_active(True)
    macos_coreml.set_dictation_active(False)
    assert run_pass(monkeypatch, times_out) is False  # the next dictation, or a relaunch
    assert len(calls) == 1
    assert "postponed" in capsys.readouterr().out


@pytest.mark.parametrize("device_started", [True, False])
def test_yield_counts_only_once_the_device_compile_started(activity, monkeypatch, device_started):
    calls = []

    def yields(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        calls.append(config)
        if device_started:
            on_device_attempt()
        raise InterruptedError("Encoder preparation yielded to dictation")

    assert run_pass(monkeypatch, yields) is True  # retry only after another quiet spell
    rows = json.loads(activity.read_text()) if activity.exists() else {}
    if device_started:
        assert rows["key"] == {"attempts": 1, "last_attempt": rows["key"]["last_attempt"],
                               "last_failure": "yielded to dictation"}
    else:
        assert rows == {}  # conversion/baseline are cancellable, so no attempt is spent
    run_pass(monkeypatch, yields)
    assert len(calls) == (1 if device_started else 2)


def test_conversion_failure_counts_as_an_attempt(activity, monkeypatch):
    def compiler_fails(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        raise RuntimeError("Core ML package compiler exited with 1")

    assert run_pass(monkeypatch, compiler_fails) is False
    assert json.loads(activity.read_text())["key"]["attempts"] == 1


def test_wait_for_quiet_needs_an_unbroken_quiet_spell(activity):
    quiet = 0.4
    macos_coreml.set_dictation_active(True)
    returned = []
    waiter = threading.Thread(target=lambda: returned.append(
        (macos_coreml._wait_for_quiet(lambda: False, quiet), time.monotonic())), daemon=True)
    waiter.start()
    time.sleep(quiet * 1.5)
    assert waiter.is_alive()  # never while recording
    macos_coreml.set_dictation_active(False)
    time.sleep(quiet / 2)
    macos_coreml.set_dictation_active(True)  # a short tap restarts the clock
    tap_ended = time.monotonic()
    macos_coreml.set_dictation_active(False)
    waiter.join(timeout=10)
    assert returned and returned[0][1] - tap_ended >= quiet
    assert returned[0][0] == macos_coreml._dictations


def fake_backend():
    return SimpleNamespace(
        whisper_server_binary="/native/bin/whisper-server", model_path="model.bin", threads=4,
        _server_cmd_attempts=lambda port: [["/native/bin/whisper-server", "--port", str(port)]],
        _drain_server_output=lambda stream, tail: None)


class FakeProbe:
    """Stands in for the supervised probe: stays 'loading' until signalled."""

    def __init__(self, *args, **kwargs):
        self.pid, self.stdout, self.returncode = 4242, io.BytesIO(), None
        self.terminated = threading.Event()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.terminated.set()

    kill = terminate

    def wait(self, timeout=None):
        return self.returncode


def test_no_probe_or_compile_starts_while_dictating(activity, monkeypatch):
    spawned, counted = [], []
    monkeypatch.setattr(macos_coreml.subprocess, "Popen", lambda *a, **k: spawned.append(a) or FakeProbe())
    macos_coreml.set_dictation_active(True)
    with pytest.raises(InterruptedError):
        with macos_coreml._probe_server(fake_backend(), "/native/bin/whisper-server", "model.bin", coreml=True,
                                        cancelled=lambda: False, on_spawn=lambda: counted.append(1)):
            pass
    with pytest.raises(InterruptedError):
        with macos_coreml._preparation_child(["coreml-compile"], {}):
            pass
    assert spawned == [] and counted == []


def test_device_attempt_is_counted_before_the_probe_spawns(activity, monkeypatch):
    order = []

    def spawn(*args, **kwargs):
        order.append("spawn")
        raise OSError("stop here")

    monkeypatch.setattr(macos_coreml.subprocess, "Popen", spawn)
    with pytest.raises(OSError):
        with macos_coreml._probe_server(fake_backend(), "/native/bin/whisper-server", "model.bin", coreml=True,
                                        cancelled=lambda: False, on_spawn=lambda: order.append("counted")):
            pass
    assert order == ["counted", "spawn"]  # a quit or crash mid-compile still counts


@pytest.mark.parametrize("tap", [False, True])
def test_dictation_stops_a_loading_probe_and_yields(activity, monkeypatch, tap):
    probes = []
    monkeypatch.setattr(macos_coreml.subprocess, "Popen", lambda *a, **k: probes.append(FakeProbe()) or probes[-1])
    monkeypatch.setattr(macos_coreml, "_supervised_child_pid", lambda pid, binary: None)  # still loading
    monkeypatch.setattr(macos_coreml, "_READY_POLL_SECONDS", 0.05)
    dictations = macos_coreml._dictations
    outcome = []

    def prepare():
        try:
            with macos_coreml._probe_server(fake_backend(), "/native/bin/whisper-server", "model.bin", coreml=True,
                                            cancelled=lambda: macos_coreml._dictations != dictations):
                outcome.append("ready")
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=prepare, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not macos_coreml._children and time.monotonic() < deadline:  # spawned and registered
        time.sleep(0.01)
    macos_coreml.set_dictation_active(True)
    assert probes[0].terminated.is_set()  # signalled synchronously, not at the next poll
    if tap:  # over before the worker polls again: still a yield, never a "crash"
        macos_coreml.set_dictation_active(False)
    worker.join(timeout=5)
    assert len(outcome) == 1 and isinstance(outcome[0], InterruptedError)
    assert macos_coreml._children == set()


def test_probe_stopped_by_a_tap_between_checks_is_a_yield_not_a_crash(activity, monkeypatch):
    class TappedProbe(FakeProbe):
        tapped = False

        def poll(self):  # the user taps the hotkey exactly while the loop checks the child
            if not self.tapped:
                self.tapped = True
                macos_coreml.set_dictation_active(True)
                macos_coreml.set_dictation_active(False)
            return self.returncode

    monkeypatch.setattr(macos_coreml.subprocess, "Popen", lambda *a, **k: TappedProbe())
    dictations = macos_coreml._dictations
    with pytest.raises(InterruptedError):
        with macos_coreml._probe_server(fake_backend(), "/native/bin/whisper-server", "model.bin", coreml=True,
                                        cancelled=lambda: macos_coreml._dictations != dictations):
            pass


def test_starting_dictation_terminates_a_real_preparation_child(activity):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    with macos_coreml._preparation_child(command, dict(os.environ)) as proc:
        assert proc.poll() is None
        started = time.monotonic()
        macos_coreml.set_dictation_active(True)
        proc.wait(timeout=5)
        assert time.monotonic() - started < 5
    assert proc.returncode != 0


@pytest.mark.parametrize("preferred", [True, False])
def test_only_an_adopted_probe_survives_the_next_dictation(activity, monkeypatch, preferred):
    from unittest.mock import Mock
    from wayfinder.core.transcriber import WhisperServerBackend
    cls = WhisperServerBackend
    for name in ("_server_process", "_server_port", "_server_model_path", "_server_binary", "_server_use_gpu",
                 "_server_threads", "_server_log_tail", "_server_log_thread", "_server_disabled"):
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(cls, "_server_lock", threading.Lock())
    monkeypatch.setattr(macos_coreml, "record_comparison", lambda *args: preferred)
    monkeypatch.setattr(macos_coreml, "_stop_probe", Mock())
    proc = FakeProbe()
    macos_coreml._children.add(proc)
    probe = SimpleNamespace(proc=proc, port=9000, tail=[], reader=Mock(), adopted=False,
                            backend=SimpleNamespace(model_path="converted.bin", whisper_server_binary="coreml-server",
                                                    threads=4))
    macos_coreml._activate_probe(probe, "cli", "model", 1.0, .5, True, lambda: False)
    macos_coreml.set_dictation_active(True)
    assert proc.terminated.is_set() is (not preferred)  # the live dictation server is never stopped


# --- Selected Core ML: loaded in the background each launch, then handed over ---


class LoadingChild(FakeProbe):
    """A Core ML server child: warm loads report at once; a cold one stays in its compile."""

    def __init__(self, warm):
        super().__init__()
        self.stdout = io.BytesIO(b"whisper_init_state: Core ML model loaded\n" if warm else b"")
        self.warm = warm


@pytest.fixture
def launch(activity, monkeypatch, tmp_path):
    """Real files, a recorded Core ML win, and Metal serving as it does after launch."""
    from wayfinder.core import transcriber
    from wayfinder.license import FeatureGate
    from wayfinder.utils import child_supervisor, loopback_http, macos_procinfo
    cli, twin, model, _ = prepared(tmp_path, monkeypatch)
    metal_server = cli.with_name("whisper-server")
    metal_server.write_bytes(b"test executable")
    metal_server.chmod(0o700)
    for module in (macos_coreml, transcriber):
        monkeypatch.setattr(module, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(FeatureGate, "has_feature", lambda self, feature: True)
    monkeypatch.setattr(macos_coreml, "_cache_path", lambda: tmp_path / "coreml-selection.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1", "os": "26.6.2"})
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda configured: str(cli))
    assert macos_coreml.record_comparison(str(cli), str(model), 1.0, 0.55, True)
    config = {"transcription_backend": "whisper_cpp", "model_path": str(model), "use_gpu": True, "threads": 4}
    metal = FakeProbe()
    cls = server_state(monkeypatch, _server_process=metal, _server_binary=str(metal_server),
                       _server_model_path=str(model), _server_use_gpu=True, _server_threads=4)
    children = []
    monkeypatch.setattr(macos_coreml, "_READY_POLL_SECONDS", 0.05)
    monkeypatch.setattr(child_supervisor, "wrap_macos_child_command", lambda command, env: (command, env))
    monkeypatch.setattr(macos_coreml, "_supervised_child_pid", lambda pid, binary: 4243)
    monkeypatch.setattr(macos_procinfo, "pid_listens_on_tcp", lambda pid, port: children[-1].warm)
    monkeypatch.setattr(loopback_http, "urlopen_loopback",
                        lambda url, timeout=None: contextlib.nullcontext(SimpleNamespace(status=200)))
    env = SimpleNamespace(config=config, cli=str(cli), cls=cls, metal=metal, children=children, spawned=[], warm=True,
                          coreml_server=str(twin.with_name("whisper-server")),
                          converted=str(macos_coreml.converted_model_path(str(model))))

    def popen(command, **kwargs):
        env.spawned.append(command[0])
        children.append(LoadingChild(env.warm))
        return children[-1]

    monkeypatch.setattr(macos_coreml.subprocess, "Popen", popen)
    return env


def test_launch_serves_metal_then_hands_over_the_loaded_coreml_server(launch):
    from wayfinder.core.transcriber import get_backend
    dictation = get_backend(launch.config)
    assert dictation.metal_fallback is None and dictation._server_reusable()  # Metal, no restart
    hooks = []
    assert macos_coreml.prepare_comparison(launch.config, dictation, on_device_attempt=lambda: hooks.append("attempt"),
                                           on_device_loaded=lambda: hooks.append("loaded")) is True
    assert launch.spawned == [launch.coreml_server] and hooks == ["attempt", "loaded"]
    assert launch.metal.terminated.is_set()  # the old Metal server, after the swap
    assert (launch.cls._server_process, launch.cls._server_binary, launch.cls._server_model_path) == (
        launch.children[0], launch.coreml_server, launch.converted)
    dictation = get_backend(launch.config)
    assert dictation.whisper_server_binary == launch.coreml_server and dictation._server_reusable()
    macos_coreml.set_dictation_active(True)
    assert not launch.children[0].terminated.is_set()  # now the dictation server


def test_dictation_keeps_metal_while_a_cold_cache_compiles(launch):
    from wayfinder.core.transcriber import get_backend
    launch.warm = False
    outcome = []

    def load():
        try:
            outcome.append(macos_coreml.prepare_comparison(launch.config, get_backend(launch.config)))
        except Exception as exc:
            outcome.append(exc)

    worker = threading.Thread(target=load, daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while not launch.children and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)  # a few readiness polls into the "compile"
    assert worker.is_alive() and not outcome
    dictation = get_backend(launch.config)  # a hotkey now
    assert dictation.metal_fallback is None and dictation._server_reusable()
    assert launch.cls._server_process is launch.metal
    macos_coreml.set_dictation_active(True)  # recording stops the loading child at once
    worker.join(timeout=5)
    assert isinstance(outcome[0], InterruptedError) and launch.children[0].terminated.is_set()
    assert launch.cls._server_process is launch.metal and not launch.metal.terminated.is_set()


def test_metal_win_loads_nothing(launch):
    from wayfinder.core.transcriber import get_backend
    assert not macos_coreml.record_comparison(launch.cli, launch.config["model_path"], 0.5, 0.55, True)
    assert macos_coreml.prepare_comparison(launch.config, get_backend(launch.config)) is False
    assert launch.spawned == []


@pytest.mark.parametrize("row,serving,expected", [
    ({"preferred": True, "same_words": True}, None, ["quiet", "hold", "work"]),
    ({"preferred": True, "same_words": True}, "coreml", []),  # already handed over
    ({"preferred": False, "same_words": True}, None, []),  # Metal measured faster
])
def test_pass_reloads_a_selected_encoder_unless_it_already_serves(activity, monkeypatch, row, serving, expected):
    order = []

    def loads(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        order.append("work")

    current = SimpleNamespace(use_gpu=True, metal_fallback=object() if serving else None)
    assert run_pass(monkeypatch, loads, order, row=row, serving=current) is False
    assert order == expected


def test_warm_loads_never_use_up_the_attempt_history(activity, monkeypatch):
    fake_clock(monkeypatch)
    calls = []

    def warm_load(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        calls.append(config)
        on_device_attempt()
        on_device_loaded()

    for launch_number in range(len(macos_coreml.RETRY_AFTER_SECONDS) + 2):  # one load per launch
        assert run_pass(monkeypatch, warm_load, row={"preferred": True, "same_words": True}) is False
    assert len(calls) == len(macos_coreml.RETRY_AFTER_SECONDS) + 2
    assert not activity.exists() or "key" not in json.loads(activity.read_text())


def test_cold_load_that_times_out_backs_off_and_keeps_metal(activity, monkeypatch, capsys):
    calls = []

    def cold_load(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        calls.append(config)
        on_device_attempt()
        raise TimeoutError("Encoder device preparation timed out")

    row = {"preferred": True, "same_words": True}
    assert run_pass(monkeypatch, cold_load, row=row) is False
    assert json.loads(activity.read_text())["key"]["attempts"] == 1
    assert "next attempt no sooner than 24 h" in capsys.readouterr().out
    assert run_pass(monkeypatch, cold_load, row=row) is False  # a relaunch the same day
    assert len(calls) == 1 and "postponed" in capsys.readouterr().out


def test_yield_after_the_load_finished_retries_without_backoff(activity, monkeypatch):
    calls = []

    def loaded_then_yields(config, backend, *, cancelled, on_device_attempt, on_device_loaded):
        calls.append(config)
        on_device_attempt()
        on_device_loaded()
        raise InterruptedError("Accelerator switch deferred until idle")

    row = {"preferred": True, "same_words": True}
    assert run_pass(monkeypatch, loaded_then_yields, row=row) is True  # after the next quiet spell
    assert run_pass(monkeypatch, loaded_then_yields, row=row) is True  # the compile is cached: no wait
    assert len(calls) == 2

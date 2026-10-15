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
    from wayfinder.core import transcriber
    from wayfinder.license import FeatureGate

    calls = []
    monkeypatch.setattr(transcriber, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(FeatureGate, "has_feature", lambda self, feature: licensed)
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda configured: "/native/bin/whisper-cli")
    monkeypatch.setattr(transcriber.WhisperServerBackend, "is_available", lambda self: True)
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda cli, model: calls.append((cli, model)) or "/native/bin/whisper-coreml/whisper-cli")
    monkeypatch.setattr(macos_coreml, "converted_model_path", lambda model: Path("/derived/ggml-base.en.bin"))
    backend = transcriber.get_backend({
        "transcription_backend": "whisper_cpp", "model_path": "/models/ggml-base.en.bin",
        "use_gpu": gpu, "whisper_coreml_encoder": opt_in,
    })
    assert bool(calls) is selected
    assert ("whisper-coreml" in backend.whisper_server_binary) is selected
    assert backend.startup_timeout == (120.0 if selected else 30.0)
    assert backend.use_gpu is (gpu and licensed)


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


def run_pass(monkeypatch, fake_prepare, order=None):
    """One worker pass with the real attempt history and fakes for the heavy parts."""
    from wayfinder.core import transcriber
    from wayfinder.utils import macos_activity
    order = [] if order is None else order
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda configured: "/bin/whisper-cli")
    monkeypatch.setattr(transcriber, "get_backend", lambda config: SimpleNamespace(use_gpu=True))
    monkeypatch.setattr(macos_coreml, "_attempt_key", lambda cli, model: "key")
    monkeypatch.setattr(macos_coreml, "comparison", lambda cli, model: None)

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

    def times_out(config, backend, *, cancelled, on_device_attempt):
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

    def yields(config, backend, *, cancelled, on_device_attempt):
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
    def compiler_fails(config, backend, *, cancelled, on_device_attempt):
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

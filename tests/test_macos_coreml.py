"""Core ML is explicit, asset-validated, and behind the existing GPU gate."""
import json
import os
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
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda: True)   # already loaded
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
                 "_server_threads", "_server_log_tail", "_server_log_thread", "_server_disabled",
                 "_server_epoch"):
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


def test_a_probe_whose_config_predates_a_switch_is_not_adopted(monkeypatch):
    # The old mode's teardown can run while the comparison is still probing;
    # adopting afterwards would leave its server resident.
    import threading
    from unittest.mock import Mock
    from wayfinder.core.transcriber import WhisperServerBackend
    cls = WhisperServerBackend
    monkeypatch.setattr(macos_coreml, "_idle", lambda: True)
    record = Mock(return_value=True)
    monkeypatch.setattr(macos_coreml, "record_comparison", record)
    monkeypatch.setattr(cls, "_server_lock", threading.Lock())
    monkeypatch.setattr(cls, "_server_process", None)
    monkeypatch.setattr(cls, "_epoch", cls._epoch)
    proc = Mock()
    proc.poll.return_value = None
    probe = SimpleNamespace(proc=proc, port=9000, tail=[], reader=Mock(), adopted=False,
                            backend=SimpleNamespace(model_path="converted.bin", threads=4,
                                                    whisper_server_binary="coreml-server",
                                                    _load_epoch=cls._epoch))
    cls.supersede()
    assert macos_coreml._activate_probe(probe, "cli", "model", 1.0, .5, True, lambda: False) is False
    assert not probe.adopted and cls._server_process is None
    record.assert_not_called()


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


def test_switching_to_cpu_stops_a_comparison_in_flight(tmp_path, monkeypatch):
    # A GPU-mode comparison running when the user switches to CPU used to
    # finish and hot-swap a Core ML (GPU) server in: CPU mode never calls
    # schedule_preparation(), so nothing told the worker.
    import contextlib
    import threading
    import time
    import wayfinder.core.transcriber as transcriber
    from wayfinder.utils import macos_activity

    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"test model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_worker", None)
    monkeypatch.setattr(macos_coreml, "_preparation_requested", threading.Event())
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda cli: True)
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda path: "/bin/whisper-cli")
    monkeypatch.setattr(transcriber, "get_backend", lambda cfg: SimpleNamespace(use_gpu=cfg["use_gpu"]))
    monkeypatch.setattr(macos_activity, "held", lambda *a: contextlib.nullcontext())
    runs, stopped = [], threading.Event()

    def comparison(snapshot, backend, *, cancelled):
        runs.append(snapshot["use_gpu"])
        deadline = time.monotonic() + 5
        while not cancelled() and time.monotonic() < deadline:
            time.sleep(0.01)
        stopped.set()
        return False

    monkeypatch.setattr(macos_coreml, "prepare_comparison", comparison)
    gpu = {"model_path": str(model), "use_gpu": True, "whisper_coreml_encoder": True}
    macos_coreml.schedule_preparation(gpu, SimpleNamespace(use_gpu=True))
    deadline = time.monotonic() + 5
    while not runs:
        assert time.monotonic() < deadline, "the comparison never started"
        time.sleep(0.01)

    macos_coreml.cancel_preparation({**gpu, "use_gpu": False})

    assert stopped.wait(2), "the comparison kept running after the switch to CPU"
    time.sleep(0.3)
    assert runs == [True], "the worker restarted the GPU comparison in CPU mode"


@pytest.fixture
def comparison_rig(tmp_path, monkeypatch):
    """_prepare_comparison up to the probes, on a fake Mac."""
    import wayfinder.core.transcriber as transcriber
    import wayfinder.license as license_mod

    cli = tmp_path / "bin" / "whisper-cli"
    cli.parent.mkdir()
    cli.write_bytes(b"cli")
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"model")
    clip = tmp_path / "probe.wav"
    clip.write_bytes(b"RIFF")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "coreml-failures.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1"})
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda path: str(cli))
    monkeypatch.setattr(license_mod, "get_feature_gate",
                        lambda: SimpleNamespace(has_feature=lambda name: True))
    monkeypatch.setattr(macos_coreml, "prepared_twin", lambda c, m: str(tmp_path / "twin" / "whisper-cli"))
    monkeypatch.setattr(macos_coreml, "comparison", lambda c, m: None)
    monkeypatch.setattr(macos_coreml, "_public_probe", lambda: clip)
    rig = SimpleNamespace(converted=[], probes=[], cli=cli, model=model,
                          config={"model_path": str(model)},
                          backend=SimpleNamespace(use_gpu=True, whisper_server_binary=str(cli.with_name("whisper-server"))))

    def prepare(c, m, *, cancelled):
        rig.converted.append(m)
        return True

    monkeypatch.setattr(macos_coreml, "prepare_encoder", prepare)
    return rig


def test_a_comparison_that_keeps_failing_is_not_retried_every_launch(comparison_rig, monkeypatch):
    # The M1 livelock: the probe timed out on every launch and nothing
    # remembered it, so each launch converted and loaded the encoder again.
    rig = comparison_rig

    def probes(*_a):
        rig.probes.append(1)
        raise TimeoutError("Encoder device preparation timed out")

    monkeypatch.setattr(macos_coreml, "_compare_with_probes", probes)
    for _launch in range(macos_coreml.MAX_COMPARISON_FAILURES):
        with pytest.raises(TimeoutError):
            macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False)
    assert macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False) is False
    assert len(rig.probes) == macos_coreml.MAX_COMPARISON_FAILURES
    assert len(rig.converted) == macos_coreml.MAX_COMPARISON_FAILURES
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == macos_coreml.MAX_COMPARISON_FAILURES


def test_yielding_to_dictation_is_not_a_failure(comparison_rig, monkeypatch):
    rig = comparison_rig

    def probes(*_a):
        raise InterruptedError("Public comparison yielded to dictation")

    monkeypatch.setattr(macos_coreml, "_compare_with_probes", probes)
    for _ in range(3):
        with pytest.raises(InterruptedError):
            macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False)
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 0


def test_a_new_model_file_gets_a_fresh_set_of_attempts(comparison_rig):
    rig = comparison_rig
    for _ in range(macos_coreml.MAX_COMPARISON_FAILURES):
        macos_coreml.record_comparison_failure(str(rig.cli), str(rig.model), "TimeoutError")
    rig.model.write_bytes(b"model, re-downloaded")
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 0


def _probe_backend(command):
    def drain(stream, tail):
        for line in iter(stream.readline, b""):
            tail.append(line.decode(errors="replace"))

    return SimpleNamespace(_server_cmd_attempts=lambda port: [list(command)],
                           _drain_server_output=drain)


def test_a_probe_that_crashes_on_load_is_a_failure_not_a_pause():
    import sys

    backend = _probe_backend([sys.executable, "-c", "print('ggml_metal_init: error'); raise SystemExit(3)"])
    with pytest.raises(RuntimeError, match="probe exited with 3"):
        with macos_coreml._probe_server(backend, "/bin/whisper-server", "/m.bin",
                                        coreml=True, cancelled=lambda: False):
            pass


def test_the_first_core_ml_load_gets_the_neural_engine_compile_time(monkeypatch):
    import sys

    clock = [0.0]
    monkeypatch.setattr(macos_coreml.time, "monotonic", lambda: clock[0])

    def tick(seconds):
        clock[0] += 30.0  # each poll stands for 30 s of Neural Engine compile

    monkeypatch.setattr(macos_coreml.time, "sleep", tick)
    monkeypatch.setattr(macos_coreml.subprocess, "check_output", lambda *a, **k: "")
    backend = _probe_backend([sys.executable, "-c", "import time; time.sleep(30)"])
    with pytest.raises(TimeoutError):
        with macos_coreml._probe_server(backend, "/bin/whisper-server", "/m.bin",
                                        coreml=True, cancelled=lambda: False):
            pass
    # 2.5 min measured on an M1 Air; the old limit was 120 s.
    assert clock[0] >= macos_coreml.COREML_FIRST_LOAD_DEADLINE_S >= 300


def _darwin_selection(monkeypatch, resident):
    from wayfinder.core import transcriber
    from wayfinder.license import FeatureGate

    scheduled = []
    monkeypatch.setattr(transcriber, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(FeatureGate, "has_feature", lambda self, feature: True)
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda configured: "/native/bin/whisper-cli")
    monkeypatch.setattr(transcriber.WhisperServerBackend, "is_available", lambda self: True)
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda cli, model: "/native/bin/whisper-coreml/whisper-cli")
    monkeypatch.setattr(macos_coreml, "converted_model_path", lambda model: Path("/derived/ggml-base.en.bin"))
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda: resident)
    monkeypatch.setattr(macos_coreml, "schedule_preparation",
                        lambda config, backend: scheduled.append((config, backend)))
    config = {"transcription_backend": "whisper_cpp", "model_path": "/models/ggml-base.en.bin",
              "use_gpu": True, "whisper_coreml_encoder": True}
    return transcriber.get_backend(config), config, scheduled


def test_a_chosen_core_ml_loads_behind_metal_after_a_relaunch(monkeypatch):
    # The relaunch on an M1 (2026-10-21): Core ML started straight away and a
    # dictation 1 s after launch waited 10.6 s for its load; Metal is up in 0.5 s.
    backend, config, scheduled = _darwin_selection(monkeypatch, resident=False)
    assert "whisper-coreml" not in backend.whisper_server_binary
    assert backend.model_path == "/models/ggml-base.en.bin" and backend.startup_timeout == 30.0
    assert len(scheduled) == 1   # the worker loads Core ML and hands over
    assert scheduled[0][0] is config and scheduled[0][1] is backend


def test_once_core_ml_serves_dictation_uses_it(monkeypatch):
    backend, _config, scheduled = _darwin_selection(monkeypatch, resident=True)
    assert "whisper-coreml" in backend.whisper_server_binary
    assert scheduled == []


@pytest.mark.parametrize("binary,alive,resident", [
    ("/app/bin/whisper-coreml/whisper-server", True, True),
    ("/app/bin/whisper-server", True, False),            # Metal serves: still hand over
    ("/app/bin/whisper-coreml/whisper-server", False, False),
    (None, True, False)])
def test_only_a_running_core_ml_server_counts_as_resident(monkeypatch, binary, alive, resident):
    from wayfinder.core.transcriber import WhisperServerBackend
    proc = SimpleNamespace(poll=lambda: None if alive else 1)
    monkeypatch.setattr(WhisperServerBackend, "_server_process", proc)
    monkeypatch.setattr(WhisperServerBackend, "_server_binary", binary)
    assert macos_coreml.coreml_resident() is resident
    monkeypatch.setattr(WhisperServerBackend, "_server_process", None)
    assert macos_coreml.coreml_resident() is False


@pytest.mark.parametrize("preferred,resident,handed_off", [
    (True, False, True), (True, True, False), (False, False, False)])
def test_a_compared_model_hands_over_instead_of_comparing_again(
        comparison_rig, monkeypatch, preferred, resident, handed_off):
    rig = comparison_rig
    row = {"preferred": preferred, "same_words": True, "metal_seconds": 2.0, "coreml_seconds": 0.7}
    monkeypatch.setattr(macos_coreml, "comparison", lambda c, m: row)
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda c, m: "twin" if preferred else None)
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda: resident)
    calls = []
    monkeypatch.setattr(macos_coreml, "_hand_off", lambda *a: calls.append(a) or True)
    monkeypatch.setattr(macos_coreml, "_compare_with_probes",
                        lambda *a: pytest.fail("compared again"))
    assert macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False) is handed_off
    assert bool(calls) is handed_off


def _hand_off_rig(monkeypatch, outcomes, alive=True):
    from contextlib import contextmanager
    from unittest.mock import Mock

    proc = Mock()
    proc.poll.return_value = None if alive else 1
    proc.returncode = None if alive else 1
    probe = SimpleNamespace(proc=proc, adopted=False)
    loads = []

    @contextmanager
    def probe_server(backend, binary, model, *, coreml, cancelled):
        loads.append((binary, model, coreml))
        yield probe

    def publish(_probe, _cancelled):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(macos_coreml, "_probe_server", probe_server)
    monkeypatch.setattr(macos_coreml, "_wait_for_idle", lambda cancelled: None)
    monkeypatch.setattr(macos_coreml, "_publish_probe", publish)
    monkeypatch.setattr(macos_coreml, "converted_model_path", lambda model: Path("/derived/m.bin"))
    return loads


def test_the_hand_over_waits_out_a_dictation_without_reloading(monkeypatch):
    outcomes = [InterruptedError("dictation began"), True]
    loads = _hand_off_rig(monkeypatch, outcomes)
    assert macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-coreml/whisper-cli",
                                  "/m.bin", lambda: False) is True
    assert outcomes == [] and len(loads) == 1   # one load, published at the next idle
    assert loads[0] == ("/b/whisper-coreml/whisper-server", "/derived/m.bin", True)


def test_a_switch_cancels_the_hand_over(monkeypatch):
    _hand_off_rig(monkeypatch, [InterruptedError("switched")])
    with pytest.raises(InterruptedError):
        macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-coreml/whisper-cli",
                               "/m.bin", lambda: True)


def test_a_core_ml_server_that_dies_before_taking_over_is_a_failure(monkeypatch):
    # Recorded by _prepare_comparison like a failed comparison, so a Mac where
    # it always dies stops loading it after MAX_COMPARISON_FAILURES.
    _hand_off_rig(monkeypatch, [False], alive=False)
    with pytest.raises(RuntimeError):
        macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-coreml/whisper-cli",
                               "/m.bin", lambda: False)


def test_a_probe_from_before_a_switch_is_left_to_the_new_mode(monkeypatch):
    _hand_off_rig(monkeypatch, [False])
    assert macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-coreml/whisper-cli",
                                  "/m.bin", lambda: False) is False


def test_an_equal_request_leaves_the_running_job_alone(tmp_path, monkeypatch):
    # get_backend() asks on every dictation while Core ML loads; each new ask
    # used to cancel the job and restart the ~11 s load.
    import wayfinder.core.transcriber as transcriber
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda path: "/b/whisper-cli")
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda cli: Path("/b/whisper-coreml/whisper-cli"))
    monkeypatch.setattr(macos_coreml, "_worker", SimpleNamespace(is_alive=lambda: True))
    running = {"model_path": str(model), "use_gpu": True, "whisper_coreml_encoder": True}
    monkeypatch.setattr(macos_coreml, "_latest_config", running)
    macos_coreml._preparation_requested.clear()
    macos_coreml._job_running.set()
    try:
        macos_coreml.schedule_preparation(dict(running), SimpleNamespace(use_gpu=True))
        assert macos_coreml._latest_config is running
        assert not macos_coreml._preparation_requested.is_set()
        changed = dict(running, language="de")
        macos_coreml.schedule_preparation(changed, SimpleNamespace(use_gpu=True))
        assert macos_coreml._latest_config is changed   # a real change still supersedes
        assert macos_coreml._preparation_requested.is_set()
    finally:
        macos_coreml._job_running.clear()
        macos_coreml._preparation_requested.clear()


def test_dictations_during_the_load_do_not_restart_it(tmp_path, monkeypatch):
    # Through the real worker: each dictation's get_backend() repeats the
    # request while Core ML loads, and the job must keep going.
    import contextlib
    import threading
    import time
    import wayfinder.core.transcriber as transcriber
    from wayfinder.utils import macos_activity

    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"test model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_worker", None)
    monkeypatch.setattr(macos_coreml, "_preparation_requested", threading.Event())
    monkeypatch.setattr(macos_coreml, "_job_running", threading.Event())
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda cli: True)
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda path: "/bin/whisper-cli")
    monkeypatch.setattr(transcriber, "get_backend", lambda cfg: SimpleNamespace(use_gpu=cfg["use_gpu"]))
    monkeypatch.setattr(macos_activity, "held", lambda *a: contextlib.nullcontext())
    runs, loading, finished = [], threading.Event(), threading.Event()
    cancelled_seen = []

    def load(snapshot, backend, *, cancelled):
        runs.append(1)
        loading.set()
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            if cancelled():
                cancelled_seen.append(True)
                break
            time.sleep(0.01)
        finished.set()
        return True

    monkeypatch.setattr(macos_coreml, "prepare_comparison", load)
    gpu = {"model_path": str(model), "use_gpu": True, "whisper_coreml_encoder": True}
    macos_coreml.schedule_preparation(gpu, SimpleNamespace(use_gpu=True))
    assert loading.wait(5), "the load never started"
    for _ in range(3):   # three dictations, each with its own snapshot
        macos_coreml.schedule_preparation(dict(gpu), SimpleNamespace(use_gpu=True))
    assert finished.wait(5)
    time.sleep(0.3)
    assert cancelled_seen == [] and runs == [1]

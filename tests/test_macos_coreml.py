"""Core ML is explicit, asset-validated, and behind the existing GPU gate."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wayfinder.utils import macos_coreml

_REAL_SCHEDULE_PREPARATION = macos_coreml.schedule_preparation


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
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda *a: True)   # already loaded
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
                 "_server_epoch", "_server_coreml_key"):
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
    assert cls._server_coreml_key == (("cli", "model") if preferred else None)   # its exit counts
    assert stop.called is preferred


@pytest.mark.parametrize("after", ["switch", "quit"])
def test_a_probe_whose_config_predates_a_switch_is_not_adopted(monkeypatch, after):
    # The old mode's teardown can run while the comparison is still probing;
    # adopting afterwards would leave its server resident. After Quit nothing
    # is adopted, even a probe configured after its supersede.
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
    if after == "switch":
        cls.supersede()
    else:
        monkeypatch.setattr(cls, "_closed", True)
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
                          backend=SimpleNamespace(use_gpu=True, threads=4,
                                                  whisper_server_binary=str(cli.with_name("whisper-server"))))

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
        macos_coreml._session_failures.clear()   # a relaunch
        with pytest.raises(TimeoutError):
            macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False)
    macos_coreml._session_failures.clear()
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
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda *a: resident)
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


CORE_ML_SERVER = "/app/bin/whisper-coreml/whisper-server"
CONVERTED = "/derived/turbo-a.bin"


@pytest.mark.parametrize("change,resident", [
    ({}, True),
    ({"_server_binary": "/app/bin/whisper-server"}, False),   # Metal serves: still hand over
    ({"alive": False}, False),
    ({"_server_binary": None}, False),
    ({"_server_model_path": "/derived/turbo-b.bin"}, False),  # another model's Core ML
    ({"ready": False}, False),                                # still loading
    ({"_server_threads": 8}, False),
    ({"_server_use_gpu": False}, False),
])
def test_only_this_loaded_core_ml_server_counts_as_resident(monkeypatch, change, resident):
    # Sol mac r1: Core ML serving model A counted as resident for model B,
    # so B's backend was chosen directly and the dictation waited out its load.
    from wayfinder.core.transcriber import WhisperServerBackend
    alive = change.get("alive", True)
    proc = SimpleNamespace(poll=lambda: None if alive else 1)
    state = {"_server_process": proc, "_server_binary": CORE_ML_SERVER, "_server_model_path": CONVERTED,
             "_server_threads": 4, "_server_use_gpu": True,
             "_server_ready": proc if change.get("ready", True) else None}
    state.update({k: v for k, v in change.items() if k.startswith("_")})
    for name, value in state.items():
        monkeypatch.setattr(WhisperServerBackend, name, value)
    assert macos_coreml.coreml_resident(CORE_ML_SERVER, CONVERTED, 4) is resident
    monkeypatch.setattr(WhisperServerBackend, "_server_process", None)
    assert macos_coreml.coreml_resident(CORE_ML_SERVER, CONVERTED, 4) is False


@pytest.mark.parametrize("preferred,resident,handed_off", [
    (True, False, True), (True, True, False), (False, False, False)])
def test_a_compared_model_hands_over_instead_of_comparing_again(
        comparison_rig, monkeypatch, preferred, resident, handed_off):
    rig = comparison_rig
    row = {"preferred": preferred, "same_words": True, "metal_seconds": 2.0, "coreml_seconds": 0.7}
    monkeypatch.setattr(macos_coreml, "comparison", lambda c, m: row)
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda c, m: "twin" if preferred else None)
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda *a: resident)
    calls = []
    monkeypatch.setattr(macos_coreml, "_hand_off", lambda *a: calls.append(a) or True)
    monkeypatch.setattr(macos_coreml, "_compare_with_probes",
                        lambda *a: pytest.fail("compared again"))
    assert macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False) is handed_off
    assert bool(calls) is handed_off


class _Loads(list):
    def __init__(self):
        super().__init__()
        self.keys = []


def _hand_off_rig(monkeypatch, outcomes, alive=True):
    from contextlib import contextmanager
    from unittest.mock import Mock

    proc = Mock()
    proc.poll.return_value = None if alive else 1
    proc.returncode = None if alive else 1
    probe = SimpleNamespace(proc=proc, adopted=False)
    loads = _Loads()

    @contextmanager
    def probe_server(backend, binary, model, *, coreml, cancelled):
        loads.append((binary, model, coreml))
        yield probe

    def publish(_probe, _cancelled, coreml_key=None):
        loads.keys.append(coreml_key)
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
    assert macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-cli", "/b/whisper-coreml/whisper-cli",
                                  "/m.bin", lambda: False) is True
    assert outcomes == [] and len(loads) == 1   # one load, published at the next idle
    server, derived, coreml = loads[0]   # paths in the platform's own separators
    assert (Path(server), Path(derived), coreml) == (
        Path("/b/whisper-coreml/whisper-server"), Path("/derived/m.bin"), True)
    assert loads.keys == [("/b/whisper-cli", "/m.bin")] * 2   # the published server's exit counts


def test_a_switch_cancels_the_hand_over(monkeypatch):
    _hand_off_rig(monkeypatch, [InterruptedError("switched")])
    with pytest.raises(InterruptedError):
        macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-cli", "/b/whisper-coreml/whisper-cli",
                               "/m.bin", lambda: True)


def test_a_core_ml_server_that_dies_before_taking_over_is_a_failure(monkeypatch):
    # Recorded by _prepare_comparison like a failed comparison, so a Mac where
    # it always dies stops loading it after MAX_COMPARISON_FAILURES.
    _hand_off_rig(monkeypatch, [False], alive=False)
    with pytest.raises(RuntimeError):
        macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-cli", "/b/whisper-coreml/whisper-cli",
                               "/m.bin", lambda: False)


def test_a_probe_from_before_a_switch_is_left_to_the_new_mode(monkeypatch):
    _hand_off_rig(monkeypatch, [False])
    assert macos_coreml._hand_off(SimpleNamespace(), "/b/whisper-cli", "/b/whisper-coreml/whisper-cli",
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


def test_no_core_ml_preparation_starts_after_quit(monkeypatch):
    import wayfinder.core.transcriber as transcriber
    from wayfinder.license import FeatureGate
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda path: "/b/whisper-cli")
    monkeypatch.setattr(FeatureGate, "has_feature", lambda self, feature: True)
    monkeypatch.setattr(macos_coreml, "comparison_blocked", lambda c, m: None)
    monkeypatch.setattr(macos_coreml, "prepare_encoder",
                        lambda *a, **kw: pytest.fail("a probe started after Quit"))
    monkeypatch.setattr(transcriber.WhisperServerBackend, "_closed", True)
    backend = SimpleNamespace(use_gpu=True, whisper_server_binary="/b/whisper-server")
    assert macos_coreml.prepare_comparison(
        {"model_path": "/m/ggml-large-v3-turbo-q5_0.bin"}, backend, cancelled=lambda: False) is False


@pytest.fixture(autouse=True)
def _a_fresh_session():
    # Session failure guard: each test is its own launch.
    macos_coreml._session_failures.clear()
    macos_coreml._runtime_proven.clear()
    yield
    macos_coreml._session_failures.clear()
    macos_coreml._runtime_proven.clear()


# Sol mac r1: a restart or Quit during a Core ML load or compile.

def _loading_probe_backend(ready_line="Core ML model loaded"):
    import sys
    return _probe_backend([sys.executable, "-u", "-c",
                           f"import time; print({ready_line!r}); time.sleep(30)"])


def _ready_once_listening(monkeypatch, binary):
    """The probe's readiness checks, faked: each tracked probe owns the port
    (asked about any other pid, it does not) and answers /health."""
    import contextlib
    from wayfinder.utils import child_supervisor, loopback_http, macos_procinfo

    monkeypatch.setattr(macos_procinfo, "pid_listens_on_tcp", lambda pid, port: any(
        proc.pid == pid for proc in list(child_supervisor._tracked)))
    monkeypatch.setattr(loopback_http, "urlopen_loopback",
                        lambda *a, **kw: contextlib.nullcontext(SimpleNamespace(status=200)))


@pytest.mark.parametrize("quit_call", ["terminal shutdown", "exit hook"])
def test_quit_stops_a_loading_probe_before_it_returns(monkeypatch, quit_call):
    # An overlay restart re-execs under the same pid: the probe's supervisor
    # never notices, so the terminal shutdown itself must stop the probe.
    import threading
    from wayfinder.core.transcriber import WhisperServerBackend
    from wayfinder.utils import child_supervisor
    monkeypatch.setattr(macos_coreml.subprocess, "check_output", lambda *a, **kw: "")
    outcome = {}

    def load():
        try:
            with macos_coreml._probe_server(_loading_probe_backend(), "/b/whisper-server", "/m.bin",
                                            coreml=True, cancelled=lambda: False):
                outcome["served"] = True
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=load, daemon=True)
    thread.start()
    for _ in range(250):
        if child_supervisor._tracked:
            break
        threading.Event().wait(0.02)
    (proc,) = list(child_supervisor._tracked)
    if quit_call == "terminal shutdown":
        WhisperServerBackend.shutdown(terminal=True)
    else:
        child_supervisor.close_tracked()
    assert proc.poll() is not None   # stopped before the shutdown returned
    thread.join(10)
    assert isinstance(outcome.get("error"), child_supervisor.HelperStopped)


@pytest.mark.parametrize("quit_call", ["terminal shutdown", "exit hook"])
def test_a_hand_over_quit_cut_short_is_not_a_core_ml_failure(comparison_rig, monkeypatch, quit_call):
    # The exit hook stops helpers with no supersede, so the job still looks
    # live: only the mark on the child tells its exit from a crash.
    import threading
    from wayfinder.core.transcriber import WhisperServerBackend
    from wayfinder.utils import child_supervisor
    rig = comparison_rig
    twin = Path(macos_coreml.prepared_twin(str(rig.cli), str(rig.model)))
    monkeypatch.setattr(macos_coreml, "comparison", lambda c, m: {"preferred": True})
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda c, m: str(twin))
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda *a: False)
    monkeypatch.setattr(macos_coreml, "converted_model_path", lambda m: Path("/derived/m.bin"))
    monkeypatch.setattr(macos_coreml, "_wait_for_idle", lambda cancelled: None)
    _ready_once_listening(monkeypatch, str(twin.with_name("whisper-server")))
    loaded, quit_done = threading.Event(), threading.Event()

    def publish(probe, cancelled, decide=None, coreml_key=None):
        loaded.set()
        assert quit_done.wait(10)
        return False

    monkeypatch.setattr(macos_coreml, "_publish_probe", publish)
    backend = _loading_probe_backend()
    for name, value in vars(rig.backend).items():
        setattr(backend, name, value)
    outcome = {}

    def prepare():
        try:
            outcome["result"] = macos_coreml._prepare_comparison(rig.config, backend, cancelled=lambda: False)
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=prepare, daemon=True)
    thread.start()
    assert loaded.wait(10), f"the Core ML probe never loaded: {outcome}"
    if quit_call == "terminal shutdown":
        WhisperServerBackend.shutdown(terminal=True)
    else:
        child_supervisor.close_tracked()
    quit_done.set()
    thread.join(10)
    assert isinstance(outcome.get("error"), InterruptedError)
    child_supervisor.reopen_tracked()
    WhisperServerBackend._closed = False
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 0
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model)) is None


@pytest.mark.skipif(os.name == "nt", reason="the compiler stand-in is a shell script")
def test_a_compile_quit_cut_short_is_not_a_core_ml_failure(tmp_path, monkeypatch):
    import threading
    from wayfinder.utils import child_supervisor, macos_coreml_convert
    cli = tmp_path / "bin" / "whisper-cli"
    cli.parent.mkdir()
    cli.write_bytes(b"cli")   # the failure key stamps it
    compiler = cli.with_name("coreml-compile")
    compiler.write_text("#!/bin/sh\nexec sleep 30\n")
    compiler.chmod(0o700)
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_conversion_root", lambda: tmp_path / "derived")
    monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "coreml-failures.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1"})
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda c: cli)
    monkeypatch.setattr(macos_coreml, "prepared_twin", lambda c, m: None)
    monkeypatch.setattr(macos_coreml_convert, "template_path", lambda: None)
    monkeypatch.setattr(macos_coreml_convert, "load_bindings", lambda path: {"weights_size": 1})
    monkeypatch.setattr(macos_coreml_convert, "populate_package", lambda m, p, cancelled: {"format": 2})
    outcome = {}
    def compile_():
        try:
            outcome["result"] = macos_coreml.prepare_encoder(str(cli), str(model), cancelled=lambda: False)
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(daemon=True, target=compile_)
    thread.start()
    for _ in range(250):
        if child_supervisor._tracked:
            break
        threading.Event().wait(0.02)
    assert child_supervisor._tracked, "the compiler never started"
    child_supervisor.close_tracked()   # the exit hook: no supersede first
    thread.join(10)
    assert outcome == {"result": False}
    assert macos_coreml.comparison_failures(str(cli), str(model)) == 0
    # The same compile failing on its own, after a reopen, is counted.
    child_supervisor.reopen_tracked()
    compiler.write_text("#!/bin/sh\nexit 3\n")
    assert macos_coreml.prepare_encoder(str(cli), str(model), cancelled=lambda: False) is False
    assert macos_coreml.comparison_failures(str(cli), str(model)) == 1



@pytest.mark.skipif(os.name == "nt", reason="the compiler stand-in is a shell script")
@pytest.mark.parametrize("failure", ["exited", "timed-out"])
def test_a_compile_superseded_in_its_accounting_counts_nothing(tmp_path, monkeypatch, failure):
    # Sol mac r12: the compile's failure counts asked no cancellation under
    # the accounting lock; a switch while they waited there still counted.
    import time
    from wayfinder.utils import child_supervisor, macos_coreml_convert
    cli = tmp_path / "bin" / "whisper-cli"
    cli.parent.mkdir()
    cli.write_bytes(b"cli")
    compiler = cli.with_name("coreml-compile")
    compiler.write_text("#!/bin/sh\nexit 3\n" if failure == "exited" else "#!/bin/sh\nexec sleep 30\n")
    compiler.chmod(0o700)
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_conversion_root", lambda: tmp_path / "derived")
    monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "coreml-failures.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1"})
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda c: cli)
    monkeypatch.setattr(macos_coreml, "prepared_twin", lambda c, m: None)
    monkeypatch.setattr(macos_coreml_convert, "template_path", lambda: None)
    monkeypatch.setattr(macos_coreml_convert, "load_bindings", lambda path: {"weights_size": 1})
    monkeypatch.setattr(macos_coreml_convert, "populate_package", lambda m, p, cancelled: {"format": 2})
    if failure == "timed-out":
        real, calls = time.monotonic, [0]

        def monotonic():
            # Advance the compiler's wait clock, after helper admission. The
            # helper registry also reads the clock to record the spawn time.
            if not child_supervisor._tracked:
                return real()
            calls[0] += 1
            return real() + (0 if calls[0] == 1 else 1000)

        monkeypatch.setattr(time, "monotonic", monotonic)
    flag = [False]
    read_failures = macos_coreml._read_failures

    def read():
        flag[0] = flag[0] or bool(child_supervisor._tracked)   # during the compile's count
        return read_failures()

    monkeypatch.setattr(macos_coreml, "_read_failures", read)
    assert macos_coreml.prepare_encoder(str(cli), str(model), cancelled=lambda: flag[0]) is False
    assert flag[0], "the compile's failure was never counted"
    monkeypatch.setattr(macos_coreml, "_read_failures", read_failures)
    assert macos_coreml.comparison_failures(str(cli), str(model)) == 0


def _helper_stopped(pid, within=5.0):
    import time
    deadline = time.monotonic() + within
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as stat:
                if stat.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True   # dead, waiting for init to reap it
        except OSError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
@pytest.mark.parametrize("helper", ["probe", "compile"])
def test_a_core_ml_helper_whose_watcher_died_is_stopped(tmp_path, monkeypatch, helper):
    # Sol mac r6/r7: the probe and compile helpers are the app's own
    # children, so ending the probe or compile stops them with no watcher.
    import signal
    import sys
    from wayfinder.utils import child_supervisor, macos_coreml_convert

    def as_darwin(fn):
        return lambda *args, **kwargs: fn(*args, **{**kwargs, "platform_name": "darwin"})

    spawn = as_darwin(child_supervisor.spawn_supervised)

    def spawn_without_watcher(*args, **kwargs):
        proc = spawn(*args, **kwargs)
        os.kill(proc._aura_watcher.pid, signal.SIGKILL)
        return proc

    monkeypatch.setattr(child_supervisor, "spawn_supervised", spawn_without_watcher)
    for name in ("signal_supervised", "stop_supervised"):
        monkeypatch.setattr(child_supervisor, name, as_darwin(getattr(child_supervisor, name)))
    pid_file = tmp_path / "helper.pid"

    def started():   # superseded once the helper runs
        return pid_file.exists() and bool(pid_file.read_text())
    body = ("import os, signal, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n")
    try:
        if helper == "probe":
            monkeypatch.setattr(macos_coreml.subprocess, "check_output", lambda *a, **k: "")
            backend = _probe_backend([sys.executable, "-c", body])
            with pytest.raises(InterruptedError):
                with macos_coreml._probe_server(backend, "/bin/whisper-server", "/m.bin",
                                                coreml=True, cancelled=started):
                    pass
        else:
            cli = tmp_path / "bin" / "whisper-cli"
            cli.parent.mkdir()
            cli.write_bytes(b"cli")
            compiler = cli.with_name("coreml-compile")
            compiler.write_text(f"#!{sys.executable}\n" + body)
            compiler.chmod(0o700)
            model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
            model.write_bytes(b"model")
            monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
            monkeypatch.setattr(macos_coreml, "_conversion_root", lambda: tmp_path / "derived")
            monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "coreml-failures.json")
            monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1"})
            monkeypatch.setattr(macos_coreml, "_binary_twin", lambda c: cli)
            monkeypatch.setattr(macos_coreml, "prepared_twin", lambda c, m: None)
            monkeypatch.setattr(macos_coreml_convert, "template_path", lambda: None)
            monkeypatch.setattr(macos_coreml_convert, "load_bindings", lambda path: {"weights_size": 1})
            monkeypatch.setattr(macos_coreml_convert, "populate_package", lambda m, p, cancelled: {"format": 2})
            assert macos_coreml.prepare_encoder(str(cli), str(model), cancelled=started) is False
        assert _helper_stopped(int(pid_file.read_text()), within=1), "the helper outlived its work"
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass


def _published_server_rig(monkeypatch):
    import threading
    from unittest.mock import Mock
    from wayfinder.core.transcriber import WhisperServerBackend
    cls = WhisperServerBackend
    for name in ("_server_process", "_server_port", "_server_model_path", "_server_binary", "_server_use_gpu",
                 "_server_threads", "_server_log_tail", "_server_log_thread", "_server_disabled",
                 "_server_epoch", "_server_ready", "_server_coreml_key"):
        monkeypatch.setattr(cls, name, getattr(cls, name))
    monkeypatch.setattr(cls, "_server_lock", threading.Lock())
    monkeypatch.setattr(macos_coreml, "_idle", lambda: True)
    old = Mock()
    cls._server_process = old
    proc = Mock()
    proc.poll.return_value = None
    probe = SimpleNamespace(proc=proc, port=9000, tail=[], reader=Mock(), adopted=False,
                            backend=SimpleNamespace(model_path="converted.bin", threads=4,
                                                    whisper_server_binary="coreml-server",
                                                    _load_epoch=cls._epoch))
    return cls, old, probe


def test_the_retiring_server_is_tracked_until_it_is_gone(monkeypatch):
    # Quit while the old server is still stopping: the shutdown must reach it.
    from wayfinder.utils import child_supervisor
    cls, old, probe = _published_server_rig(monkeypatch)
    tracked_while_stopping = []
    monkeypatch.setattr(macos_coreml, "_stop_probe",
                        lambda proc: tracked_while_stopping.append(proc in child_supervisor._tracked))
    assert macos_coreml._publish_probe(probe, lambda: False) is True
    assert cls._server_process is probe.proc and tracked_while_stopping == [True]
    assert old not in child_supervisor._tracked and probe.proc not in child_supervisor._tracked


def test_no_hand_over_once_the_exit_hook_has_closed_helpers(monkeypatch):
    # Published now, the old server would outlive the exit: leave both to it.
    from wayfinder.utils import child_supervisor
    cls, old, probe = _published_server_rig(monkeypatch)
    stops = []
    monkeypatch.setattr(macos_coreml, "_stop_probe", stops.append)
    child_supervisor.close_tracked()
    assert macos_coreml._publish_probe(probe, lambda: False) is False
    assert cls._server_process is old and stops == [] and probe.adopted is False


# Sol mac r1: a request read before a CPU switch.

def test_a_request_read_before_a_cpu_switch_starts_nothing(tmp_path, monkeypatch):
    import wayfinder.core.transcriber as transcriber
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(transcriber, "_resolve_whisper_cli_binary", lambda path: "/b/whisper-cli")
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda cli: Path("/b/whisper-coreml/whisper-cli"))
    monkeypatch.setattr(macos_coreml, "_worker", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(macos_coreml, "_latest_config", None)
    monkeypatch.setattr(macos_coreml, "_latest_epoch", None)
    gpu = {"model_path": str(model), "use_gpu": True, "whisper_coreml_encoder": True}
    read_under = transcriber.WhisperServerBackend._epoch
    transcriber.WhisperServerBackend.supersede()   # the switch to CPU
    cpu = dict(gpu, use_gpu=False)
    macos_coreml.cancel_preparation(cpu)
    macos_coreml._preparation_requested.clear()    # the worker woke and parked
    try:
        # The dictation that captured GPU settings before the switch, late.
        macos_coreml.schedule_preparation(dict(gpu), SimpleNamespace(use_gpu=True, _load_epoch=read_under))
        assert macos_coreml._latest_config == cpu
        assert not macos_coreml._preparation_requested.is_set()
    finally:
        macos_coreml._preparation_requested.clear()


def test_a_switch_cancels_the_running_job_through_its_epoch(tmp_path, monkeypatch):
    # No new request at all (a CPU switch schedules none): the epoch alone
    # must stop the job, or an unwanted GPU probe loads for ten minutes.
    import contextlib
    import threading
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
    loading, stopped = threading.Event(), threading.Event()

    def load(snapshot, backend, *, cancelled):
        loading.set()
        for _ in range(500):
            if cancelled():
                stopped.set()
                return False
            threading.Event().wait(0.01)
        return True

    monkeypatch.setattr(macos_coreml, "prepare_comparison", load)
    gpu = {"model_path": str(model), "use_gpu": True, "whisper_coreml_encoder": True}
    macos_coreml.schedule_preparation(gpu, SimpleNamespace(use_gpu=True))
    assert loading.wait(5), "the load never started"
    transcriber.WhisperServerBackend.supersede()
    assert stopped.wait(3), "the job ignored the switch"


# Sol mac r1: failure memory that recovers.

def _fail(rig, times):
    for _ in range(times):   # one launch each: a launch counts once
        macos_coreml.record_comparison_failure(str(rig.cli), str(rig.model), "TimeoutError")
        macos_coreml._session_failures.clear()


def test_two_failures_wait_a_day_then_try_again(comparison_rig, monkeypatch):
    rig = comparison_rig
    now = [1_000_000.0]
    monkeypatch.setattr(macos_coreml.time, "time", lambda: now[0])
    _fail(rig, macos_coreml.MAX_COMPARISON_FAILURES)
    now[0] += 3600
    assert "next try within 24 h" in macos_coreml.comparison_blocked(str(rig.cli), str(rig.model))
    now[0] += 24 * 3600
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model)) is None


@pytest.mark.parametrize("failures,hours", [(3, 48), (4, 96), (12, 168)])
def test_the_wait_doubles_up_to_a_week(comparison_rig, monkeypatch, failures, hours):
    rig = comparison_rig
    now = [1_000_000.0]
    monkeypatch.setattr(macos_coreml.time, "time", lambda: now[0])
    _fail(rig, failures)
    now[0] += hours * 3600 - 60
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model))
    now[0] += 120
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model)) is None


def test_one_failure_holds_core_ml_back_for_this_launch_only(comparison_rig):
    # The worker's automatic retries must not spend the whole allowance at once.
    rig = comparison_rig
    macos_coreml.record_comparison_failure(str(rig.cli), str(rig.model), "TimeoutError")
    assert "earlier this session" in macos_coreml.comparison_blocked(str(rig.cli), str(rig.model))
    macos_coreml._session_failures.clear()   # the next launch
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model)) is None


@pytest.mark.parametrize("success", ["comparison", "dictation"])
def test_a_success_ends_the_failure_streak(comparison_rig, monkeypatch, success):
    rig = comparison_rig
    monkeypatch.setattr(macos_coreml, "_cache_path", lambda: rig.cli.parent / "selection.json")
    _fail(rig, macos_coreml.MAX_COMPARISON_FAILURES - 1)
    if success == "comparison":
        monkeypatch.setattr(macos_coreml, "_comparison_key", lambda c, m: "compared")
        macos_coreml.record_comparison(str(rig.cli), str(rig.model), 1.0, 0.55, True)
    else:
        macos_coreml.record_runtime_success((str(rig.cli), str(rig.model)))
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 0
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model)) is None


def test_a_hand_over_alone_does_not_end_the_streak(comparison_rig, monkeypatch):
    # A load proves nothing: a Core ML server that loads but fails every
    # dictation would otherwise be handed over again after each (Sol mac r3).
    rig = comparison_rig
    _fail(rig, macos_coreml.MAX_COMPARISON_FAILURES - 1)
    _hand_off_rig(monkeypatch, [True])
    assert macos_coreml._hand_off(SimpleNamespace(), str(rig.cli), "/b/whisper-coreml/whisper-cli",
                                  str(rig.model), lambda: False) is True
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 1


def test_turning_gpu_on_gives_core_ml_a_fresh_try(comparison_rig, monkeypatch):
    import wayfinder_main
    rig = comparison_rig
    _fail(rig, 5)
    macos_coreml.record_comparison_failure(str(rig.cli), str(rig.model), "TimeoutError")

    class DeferredThread:
        def __init__(self, target, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(wayfinder_main, "IS_WINDOWS", False)
    monkeypatch.setattr(wayfinder_main.threading, "Thread", DeferredThread)
    monkeypatch.setattr("wayfinder.core.transcriber.WhisperServerBackend.shutdown", lambda **_: None)
    monkeypatch.setattr(wayfinder_main, "_release_cleanup_residency", lambda **_: None)
    app = SimpleNamespace(config={"use_gpu": True, "whisper_server_mode": True,
                                  "transcription_backend": "whisper_cpp"}, log=lambda *a: None)
    wayfinder_main.WayfinderApp._apply_transcription_hardware_change(app, True)
    assert macos_coreml.comparison_blocked(str(rig.cli), str(rig.model)) is None


# Sol mac r1: a conversion that always raises.

def test_a_conversion_that_always_raises_is_not_retried_every_launch(comparison_rig, monkeypatch):
    rig = comparison_rig

    def unsupported(c, m, *, cancelled):
        rig.converted.append(m)
        raise ValueError("unsupported GGML tensor type")

    monkeypatch.setattr(macos_coreml, "prepare_encoder", unsupported)
    for _launch in range(macos_coreml.MAX_COMPARISON_FAILURES):
        macos_coreml._session_failures.clear()
        with pytest.raises(ValueError):
            macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False)
    macos_coreml._session_failures.clear()
    assert macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False) is False
    assert len(rig.converted) == macos_coreml.MAX_COMPARISON_FAILURES


def test_a_deferred_conversion_is_not_a_failure(comparison_rig, monkeypatch):
    # Not enough disk, or no compiler: deferred, not broken.
    rig = comparison_rig
    monkeypatch.setattr(macos_coreml, "prepare_encoder", lambda c, m, *, cancelled: False)
    for _ in range(3):
        assert macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False) is False
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 0


# Sol mac r2: a model change retires the old model's job before anything
# loads the new one; a Core ML server is used only while it is loaded.

def _worker_rig(tmp_path, monkeypatch):
    import contextlib
    import threading
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
    return model


def test_choosing_another_model_retires_the_old_job_before_its_warm_up(tmp_path, monkeypatch):
    # A Turbo A probe could publish between B's warm-up and B's own request,
    # stopping B's fresh Metal server; the switch changed no epoch.
    import threading
    import wayfinder_main
    model_a = _worker_rig(tmp_path, monkeypatch)
    model_b = tmp_path / "ggml-large-v3-turbo-q8_0.bin"
    model_b.write_bytes(b"other variant")
    loading, jobs = threading.Event(), []

    def load(snapshot, backend, *, cancelled):
        if snapshot["model_path"] != str(model_a):
            return False   # the new model's own job: not under test here
        jobs.append(cancelled)
        loading.set()
        for _ in range(500):
            if cancelled():
                return False
            threading.Event().wait(0.01)
        return True

    monkeypatch.setattr(macos_coreml, "prepare_comparison", load)
    monkeypatch.setattr(macos_coreml, "reset_comparison_failures",
                        lambda: pytest.fail("a model change reset the failure memory"))
    config = {"model_path": str(model_a), "use_gpu": True, "whisper_coreml_encoder": True}
    macos_coreml.schedule_preparation(dict(config), SimpleNamespace(use_gpu=True))
    assert loading.wait(5), "the old model's job never started"

    seen = []
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(wayfinder_main, "save_config", lambda cfg: None)
    monkeypatch.setattr(wayfinder_main.WayfinderApp, "_refresh_basic_settings", lambda self: None)
    app = SimpleNamespace(
        config=config, log=lambda *a: None, get_model_display=lambda: "Turbo B",
        feature_gate=SimpleNamespace(has_feature=lambda feature: True),
        _warm_up_transcription=lambda *a, **k: seen.append(jobs[0]()),
    )
    wayfinder_main.WayfinderApp._set_active_whisper_model(app, str(model_b))
    assert seen == [True], "the warm-up ran while the old model's job could still publish"


def test_retiring_a_job_waits_out_a_publish_in_flight(monkeypatch):
    # The publish that passed its check first holds _transition_lock; once
    # cancel_preparation returns, nothing of the old job can replace the
    # server the caller starts next.
    import threading
    monkeypatch.setattr(macos_coreml, "_worker", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(macos_coreml, "_preparation_requested", threading.Event())
    in_publish, release, returned = threading.Event(), threading.Event(), threading.Event()

    def publishing():
        with macos_coreml._transition_lock:
            in_publish.set()
            release.wait(5)

    threading.Thread(target=publishing, daemon=True).start()
    assert in_publish.wait(2)
    threading.Thread(target=lambda: (macos_coreml.cancel_preparation({"use_gpu": True}),
                                     returned.set()), daemon=True).start()
    assert not returned.wait(0.3), "returned while a publish of the old job was in flight"
    release.set()
    assert returned.wait(2)


def _body(marker):
    import wayfinder_main
    src = Path(wayfinder_main.__file__).read_text()
    indent = len(marker) - len(marker.lstrip(" "))
    return src.split(marker, 1)[1].split("\n" + " " * indent + "def ", 1)[0]


@pytest.mark.parametrize("marker", [
    "    def _apply_model_from_tray(self, model_name: str):",
    "    def _on_whisper_model_ready(self, path: str | Path | None = None) -> bool:",
    "            def _clear_model_path_if_deleted(model_id: str) -> None:",
])
def test_every_model_change_retires_the_old_job_first(marker):
    # The tray pick, a first-run download and a deleted active model: the
    # same retirement, after the new path is set and before anything loads.
    body = _body(marker)
    retire = body.index("WayfinderApp._retire_coreml_preparation(self)")
    assert body.rindex('self.config["model_path"] =', 0, retire) < retire
    assert retire < body.index("save_config(self.config)")
    if "_warm_up_transcription" in body:
        assert retire < body.index("_warm_up_transcription")


def test_a_resident_core_ml_backend_carries_its_metal_twin(monkeypatch):
    from wayfinder.core.transcriber import WhisperServerBackend
    backend, config, _scheduled = _darwin_selection(monkeypatch, resident=True)
    twin = backend._ready_only_fallback
    assert isinstance(twin, WhisperServerBackend)
    assert twin.whisper_server_binary == "/native/bin/whisper-server"
    assert twin.model_path == "/models/ggml-base.en.bin" and twin.startup_timeout == 30.0
    assert twin._load_epoch == backend._load_epoch and twin.use_gpu
    assert backend._ready_only_config is config
    metal, _config, _scheduled = _darwin_selection(monkeypatch, resident=False)
    assert metal._ready_only_fallback is None


def _resident_core_ml(monkeypatch, tmp_path, alive):
    import wave
    from wayfinder.core import transcriber
    backend, config, scheduled = _darwin_selection(monkeypatch, resident=True)
    cls = transcriber.WhisperServerBackend
    proc = SimpleNamespace(poll=lambda: None if alive else 1)
    for name, value in {"_server_process": proc, "_server_ready": proc, "_server_port": 8178,
                        "_server_model_path": backend.model_path,
                        "_server_binary": backend.whisper_server_binary,
                        "_server_use_gpu": True, "_server_threads": backend.threads,
                        "_server_disabled": False, "_closed": False}.items():
        monkeypatch.setattr(cls, name, value)
    monkeypatch.setattr(transcriber.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("a Core ML server was spawned under a dictation"))
    served = []
    twin = backend._ready_only_fallback
    twin.transcribe = lambda audio, context="": served.append(audio) or "metal words"
    twin.warm_up = lambda: served.append("warm-up") or True
    audio = tmp_path / "speech.wav"
    with wave.open(str(audio), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x01" * 1600)
    return SimpleNamespace(backend=backend, twin=twin, config=config, scheduled=scheduled,
                           served=served, audio=str(audio))


class _Words:
    def __init__(self, text):
        self.text = text

    def read(self):
        return json.dumps({"text": self.text}).encode()


def _runtime_memory(monkeypatch):
    seen = SimpleNamespace(failures=[], successes=0)

    def failed(key, reason, cancelled=None):
        seen.failures.append(reason)
        return len(seen.failures)

    def succeeded(key, cancelled=None):
        seen.successes += 1

    monkeypatch.setattr(macos_coreml, "record_runtime_failure", failed)
    monkeypatch.setattr(macos_coreml, "record_runtime_success", succeeded)
    return seen


def _metal_takes_over(monkeypatch, rig, clock=None):
    """The twin's start: Metal on 8179 replaces the failed Core ML server.
    With a clock it is the slowest start its deadline allows."""
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    starts = []

    def start(deadline=None, force=False, abandoned_at=None):
        starts.append((deadline, force))
        if clock is not None:
            clock[0] = deadline - 1.0
        monkeypatch.setattr(cls, "_server_process", SimpleNamespace(poll=lambda: None))
        monkeypatch.setattr(cls, "_server_port", 8179)
        monkeypatch.setattr(cls, "_server_binary", rig.twin.whisper_server_binary)

    rig.twin._start_server = start
    rig.twin.transcribe = lambda *a, **k: pytest.fail("the recovery restarted the budget in the twin")
    monkeypatch.setattr(cls, "_stop_server_internal", classmethod(lambda cls, deadline=None: None))
    return starts


def test_core_ml_that_died_after_selection_is_not_reloaded_under_the_dictation(monkeypatch, tmp_path):
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=False)
    assert rig.backend.transcribe(rig.audio) == "metal words"
    assert rig.served == [rig.audio]
    assert rig.scheduled == [(rig.config, rig.twin)], "Core ML was not brought back by the hand-over"


def _published_core_ml(monkeypatch, *, exited, key=("/bin/whisper-cli", "/models/turbo.bin")):
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    proc = SimpleNamespace(poll=lambda: 9 if exited else None, wait=lambda timeout=None: 9)
    for name, value in {"_server_process": proc, "_server_coreml_key": key, "_server_model_path": "/derived/turbo.bin",
                        "_server_binary": CORE_ML_SERVER, "_server_use_gpu": True, "_closed": False}.items():
        monkeypatch.setattr(cls, name, value)
    monkeypatch.setattr(transcriber, "signal_supervised", lambda proc, kill=False: None)
    return cls, proc


@pytest.mark.parametrize("exited,closed,key,counted", [
    (True, False, ("/bin/whisper-cli", "/models/turbo.bin"), ["server exited (9)"]),
    (False, False, ("/bin/whisper-cli", "/models/turbo.bin"), []),   # we stopped a live one
    (True, True, ("/bin/whisper-cli", "/models/turbo.bin"), []),     # Quit
    (True, False, None, []),                                       # a Metal server
])
def test_a_core_ml_server_that_exited_on_its_own_counts_once(monkeypatch, exited, closed, key, counted):
    # Sol mac r4: a death while idle was replaced by get_backend's Metal
    # selection without being counted, so idle deaths looped without backoff.
    # Every replacement of a published record goes through this stop.
    seen = _runtime_memory(monkeypatch)
    cls, _proc = _published_core_ml(monkeypatch, exited=exited, key=key)
    monkeypatch.setattr(cls, "_closed", closed)
    cls._stop_server_internal()
    cls._stop_server_internal()
    assert seen.failures == counted
    assert cls._server_coreml_key is None and cls._server_process is None


def test_a_core_ml_death_before_selection_counts_when_metal_replaces_it(monkeypatch):
    # The real Metal start get_backend chose: it stops the dead record first.
    from wayfinder.core import transcriber
    seen = _runtime_memory(monkeypatch)
    backend, _config, _scheduled = _darwin_selection(monkeypatch, resident=False)
    cls, _proc = _published_core_ml(monkeypatch, exited=True)
    monkeypatch.setattr(cls, "_server_lock", __import__("threading").Lock())
    monkeypatch.setattr(cls, "_server_disabled", False)

    def no_spawn(*_args, **_kwargs):
        raise OSError("spawn stubbed")

    monkeypatch.setattr(transcriber.subprocess, "Popen", no_spawn)
    with pytest.raises(OSError, match="spawn stubbed"):
        backend._start_server()
    assert seen.failures == ["server exited (9)"]


def test_a_hand_over_over_a_dead_core_ml_record_counts_its_exit(monkeypatch):
    seen = _runtime_memory(monkeypatch)
    cls, old, probe = _published_server_rig(monkeypatch)
    old.poll.return_value = 9
    monkeypatch.setattr(cls, "_server_coreml_key", ("/bin/whisper-cli", "/models/a.bin"))
    monkeypatch.setattr(macos_coreml, "_stop_probe", lambda proc: None)
    assert macos_coreml._publish_probe(probe, lambda: False, coreml_key=("/bin/whisper-cli", "/models/b.bin"))
    assert seen.failures == ["server exited (9)"]
    assert cls._server_coreml_key == ("/bin/whisper-cli", "/models/b.bin")


def test_a_failed_core_ml_request_recovers_on_metal(monkeypatch, tmp_path):
    # Recovery used to force a cold Core ML restart (~11 s) under the dictation.
    from wayfinder.core import transcriber
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    seen = _runtime_memory(monkeypatch)
    starts = _metal_takes_over(monkeypatch, rig)
    ports = []

    def post(req, timeout):
        ports.append(int(req.full_url.split(":")[2].split("/")[0]))
        if len(ports) == 1:
            raise ConnectionResetError("connection reset")
        return _Words("metal words")

    monkeypatch.setattr(transcriber, "urlopen_loopback", post)
    assert rig.backend.transcribe(rig.audio) == "metal words"
    assert ports == [8178, 8179]
    assert len(starts) == 1 and starts[0][0] is not None and starts[0][1] is True
    assert rig.scheduled == [(rig.config, rig.twin)]
    assert seen.failures == ["request failed (ConnectionResetError)"]


def test_a_core_ml_timeout_cascade_stays_inside_the_processing_watchdog(monkeypatch, tmp_path):
    # Sol mac r3: after a 30 s Core ML timeout the twin's own transcribe()
    # got a fresh request, restart and salvage budget: valid words at 125 s,
    # after the default 120 s PROCESSING watchdog had dropped the session.
    import time as _time
    import wave
    from wayfinder.core import transcriber
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    _runtime_memory(monkeypatch)
    with wave.open(rig.audio, "wb") as wav:   # long enough for the full 30 s request timeout
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x01" * 16000 * 6)
    clock = [1000.0]
    monkeypatch.setattr(_time, "monotonic", lambda: clock[0])
    starts = _metal_takes_over(monkeypatch, rig, clock)
    timeouts = []

    def time_out(req, timeout):
        timeouts.append(timeout)
        clock[0] += timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(transcriber, "urlopen_loopback", time_out)
    salvage = []

    class Cli:
        def transcribe(self, audio, context="", deadline=None):
            salvage.append(deadline - clock[0])
            clock[0] = deadline - 0.1
            return "salvaged words"

    cli = Cli()
    rig.twin._cli_fallback = lambda: cli
    assert rig.backend.transcribe(rig.audio) == "salvaged words"
    assert len(starts) == 1 and len(timeouts) == 2 and timeouts[0] == 30.0
    assert salvage == [transcriber.WhisperServerBackend._CLI_SALVAGE_TOTAL_BUDGET]
    elapsed = clock[0] - 1000.0
    assert elapsed <= 30.0 + 30.0 + transcriber.WhisperServerBackend._CLI_SALVAGE_TOTAL_BUDGET
    assert elapsed < 120.0


def _pick_another_model(monkeypatch, config):
    """What every applied model change runs (WayfinderApp._retire_coreml_preparation)."""
    import wayfinder_main
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    wayfinder_main.WayfinderApp._retire_coreml_preparation(SimpleNamespace(config=config))


@pytest.mark.parametrize("cause", ["quit", "mode switch", "model change", "another recovery", "abandoned"])
def test_a_request_something_else_cut_short_is_not_a_core_ml_failure(monkeypatch, tmp_path, cause):
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    seen = _runtime_memory(monkeypatch)
    starts = _metal_takes_over(monkeypatch, rig)
    monkeypatch.setattr(macos_coreml, "_worker", None)
    monkeypatch.setattr(cls, "_epoch", cls._epoch)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)

    def post(req, timeout):
        if ":8178/" in req.full_url:
            if cause == "quit":
                monkeypatch.setattr(cls, "_closed", True)
            elif cause == "mode switch":
                monkeypatch.setattr(cls, "_epoch", cls._epoch + 1)
            elif cause == "model change":
                _pick_another_model(monkeypatch, {"model_path": "/models/other.bin"})
            elif cause == "abandoned":
                cls.abandon_requests()
            else:
                monkeypatch.setattr(cls, "_server_process", SimpleNamespace(poll=lambda: None))
            raise ConnectionResetError("the server was stopped")
        return _Words("metal words")

    monkeypatch.setattr(transcriber, "urlopen_loopback", post)
    try:
        rig.backend.transcribe(rig.audio)
    except transcriber.TranscriptionError:
        pass   # Quit, a switch and a retired dictation start nothing after them
    assert seen.failures == []
    if cause == "abandoned":
        assert starts == [], "a retired dictation's request restarted a server"


@pytest.mark.parametrize("retire", ["force reset", "cancel", "processing watchdog", "paste watchdog",
                                    "overlay restart", "quit"])
def test_every_retirement_abandons_the_requests_in_flight(retire):
    # Sol mac r4: sleep during PROCESSING retires the dictation through
    # _do_force_reset; its late timeout still counted as a Core ML failure.
    marker = {"force reset": "    def _do_force_reset(self):",
              "cancel": "    def cancel_recording(self) -> bool:",
              "processing watchdog": "    def _on_processing_timeout(self, gen: int, timeout_s: float) -> None:",
              "paste watchdog": "    def _on_paste_timeout(self, gen: int) -> None:",
              "overlay restart": "    def _restart_for_overlay_change(self):",
              "quit": "    def quit_app(self, icon=None, item=None):"}[retire]
    body = _body(marker)
    bump = body.index("self.session_generation += 1")
    assert body.index("_abandon_transcription_requests()", bump) - bump < 60


def test_abandoning_requests_moves_the_backend_counter():
    import wayfinder_main
    from wayfinder.core.transcriber import WhisperServerBackend
    before = WhisperServerBackend._abandoned
    try:
        wayfinder_main._abandon_transcription_requests()
        assert WhisperServerBackend._abandoned == before + 1
    finally:
        WhisperServerBackend._abandoned = before


def test_a_late_timeout_after_sleep_restarts_nothing_and_counts_nothing(monkeypatch, tmp_path):
    # The sleep handler's force reset lands while the request is in flight.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    seen = _runtime_memory(monkeypatch)
    starts = _metal_takes_over(monkeypatch, rig)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
    salvage = []
    rig.twin._cli_fallback = lambda: salvage.append("cli") or pytest.fail("salvaged a retired dictation")

    def post(req, timeout):
        cls.abandon_requests()
        raise TimeoutError("timed out after the Mac woke")

    monkeypatch.setattr(transcriber, "urlopen_loopback", post)
    with pytest.raises(transcriber.TranscriptionError, match="abandoned"):
        rig.backend.transcribe(rig.audio)
    assert starts == [] and salvage == [] and seen.failures == []
    assert rig.scheduled == []


def test_a_retired_dictation_whose_server_start_was_superseded_salvages_nothing(monkeypatch, tmp_path):
    # Force reset, then a model pick: the old backend's start is superseded.
    # A live dictation salvages on whisper-cli; a retired one does nothing.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)

    def superseded(*_args, **_kwargs):
        cls.abandon_requests()
        raise transcriber.ServerStartSuperseded("a model change")

    rig.backend._start_server = superseded
    rig.backend._cli_fallback = lambda: pytest.fail("a retired dictation salvaged on whisper-cli")
    with pytest.raises(transcriber.TranscriptionError, match="abandoned"):
        rig.backend.transcribe(rig.audio)


class _ResetWhileWaiting:
    """A server lock whose acquire is where the reset lands."""
    def __init__(self, cls):
        self.cls = cls

    def acquire(self, blocking=True, timeout=-1):
        self.cls.abandon_requests()
        return True

    def release(self):
        pass


@pytest.mark.parametrize("stage", ["first start waits for the lock", "recovery waits for the lock",
                                   "after the restart", "during the retry"])
def test_a_reset_at_any_stage_stops_the_retired_dictation(monkeypatch, tmp_path, stage):
    # Sol mac r5: abandonment was checked only before the recovery, so a reset
    # landing later let the retired request restart, retry and salvage. Its
    # restart stopped the next dictation's server and spawned another.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
    _runtime_memory(monkeypatch)
    posts = []
    if stage.endswith("waits for the lock"):
        # The real starts: nothing may be stopped or spawned (Popen fails the test).
        monkeypatch.setattr(cls, "_stop_server_internal", classmethod(
            lambda cls, deadline=None: pytest.fail("a retired dictation stopped a server")))
    else:
        starts = _metal_takes_over(monkeypatch, rig)
    if stage == "first start waits for the lock":
        monkeypatch.setattr(cls, "_server_lock", _ResetWhileWaiting(cls))
    elif stage == "after the restart":
        start = rig.twin._start_server

        def start_then_reset(**kwargs):
            start(**kwargs)
            cls.abandon_requests()

        rig.twin._start_server = start_then_reset
    no_salvage = lambda: pytest.fail("a retired dictation salvaged on whisper-cli")
    rig.backend._cli_fallback = rig.twin._cli_fallback = no_salvage

    def post(req, timeout):
        posts.append(req.full_url.split("/")[2])
        if ":8178/" in req.full_url:
            if stage == "recovery waits for the lock":
                monkeypatch.setattr(cls, "_server_lock", _ResetWhileWaiting(cls))
            raise ConnectionResetError("Core ML inference failed")
        if stage == "during the retry":
            cls.abandon_requests()
            raise TimeoutError("the retry timed out after the reset")
        return _Words("words nobody will type")

    monkeypatch.setattr(transcriber, "urlopen_loopback", post)
    with pytest.raises(transcriber.RequestAbandoned):
        rig.backend.transcribe(rig.audio)
    assert rig.scheduled == [], "a retired dictation scheduled the Core ML hand-over"
    assert posts == {"first start waits for the lock": [],
                     "recovery waits for the lock": ["127.0.0.1:8178"],
                     "after the restart": ["127.0.0.1:8178"],
                     "during the retry": ["127.0.0.1:8178", "127.0.0.1:8179"]}[stage]


def test_a_reset_before_the_metal_fallback_runs_nothing(monkeypatch, tmp_path):
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=False)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)

    def refused(**_kwargs):
        cls.abandon_requests()
        raise transcriber.CoreMLNotLoaded("Core ML server not loaded")

    rig.backend._start_server = refused
    with pytest.raises(transcriber.RequestAbandoned):
        rig.backend.transcribe(rig.audio)
    assert rig.served == [] and rig.scheduled == []


def test_a_reset_while_metal_serves_schedules_no_hand_over(monkeypatch, tmp_path):
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=False)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)

    def reset_mid_dictation(audio, context=""):
        cls.abandon_requests()
        raise transcriber.RequestAbandoned("dictation abandoned")

    rig.twin.transcribe = reset_mid_dictation
    with pytest.raises(transcriber.RequestAbandoned):
        rig.backend.transcribe(rig.audio)
    assert rig.scheduled == []


def test_a_preparation_failure_after_an_idle_death_counts_once(comparison_rig):
    # Sol mac r5: get_backend scheduled the preparation before Metal's start
    # recorded the death; the admitted preparation's own failure made it 2.
    rig = comparison_rig
    key = (str(rig.cli), str(rig.model))
    assert macos_coreml.record_runtime_failure(key, "server exited (9)") == 1
    assert macos_coreml.record_comparison_failure(*key, "RuntimeError: exited") == 1
    assert macos_coreml.comparison_failures(*key) == 1


def test_a_preparation_admitted_before_a_failure_stops_at_its_next_wait(comparison_rig, monkeypatch):
    rig = comparison_rig
    key = (str(rig.cli), str(rig.model))
    monkeypatch.setattr(macos_coreml, "comparison", lambda c, m: {"preferred": True})
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda c, m: "/twin/whisper-cli")
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda *a: False)
    seen = []

    def hand_off(backend, cli, twin, model, cancelled):
        seen.append(cancelled())
        macos_coreml.record_runtime_failure(key, "server exited (9)")   # Metal replaced it meanwhile
        seen.append(cancelled())
        return False

    monkeypatch.setattr(macos_coreml, "_hand_off", hand_off)
    macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False)
    assert seen == [False, True]


@pytest.mark.parametrize("late", ["dictation", "comparison"])
def test_a_late_success_does_not_undo_this_launchs_failure(comparison_rig, late):
    # Sol mac r5: one chunk failed (count 1, Metal for the launch); another,
    # sent before the failure, succeeded and cleared both.
    rig = comparison_rig
    key = (str(rig.cli), str(rig.model))
    macos_coreml.record_runtime_failure(key, "request failed (TimeoutError)")
    if late == "dictation":
        macos_coreml.record_runtime_success(key)
    else:   # how record_comparison ends
        assert macos_coreml.clear_comparison_failures(*key) is False
    assert macos_coreml.comparison_failures(*key) == 1
    assert "earlier this session" in macos_coreml.comparison_blocked(*key)
    macos_coreml.reset_comparison_failures()   # the user's retry: GPU on
    macos_coreml.record_runtime_failure(key, "request failed (TimeoutError)")
    macos_coreml._session_failures.clear()      # the next launch
    macos_coreml.record_runtime_success(key)
    assert macos_coreml.comparison_failures(*key) == 0


def test_two_chunks_failing_on_one_incident_count_once(comparison_rig):
    # Sol mac r4: both passed served_by before either recovery replaced the
    # process, and counted 1, 2: the whole allowance in one launch.
    import threading
    rig = comparison_rig
    key = (str(rig.cli), str(rig.model))
    start = threading.Barrier(2)
    counts = []

    def fail():
        start.wait(2)
        counts.append(macos_coreml.record_runtime_failure(key, "request failed (TimeoutError)"))

    threads = [threading.Thread(target=fail) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert sorted(counts) == [1, 1]
    assert macos_coreml.comparison_failures(*key) == 1


def test_a_core_ml_server_that_loads_but_fails_dictation_backs_off(monkeypatch, tmp_path):
    # Sol mac r3: each failed request was served on Metal, scheduled another
    # load and the hand-over cleared the memory: three failures, three
    # hand-overs, nothing remembered.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    cli, model = tmp_path / "whisper-cli", tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    cli.write_bytes(b"cli")
    model.write_bytes(b"model")
    monkeypatch.setattr(macos_coreml, "runtime_key", lambda config: (str(cli), str(model)))
    monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "coreml-failures.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1"})
    monkeypatch.setattr(macos_coreml, "_session_failures", set())
    monkeypatch.setattr(macos_coreml, "_runtime_proven", set())
    core_ml = cls._server_process

    def post(req, timeout):
        if ":8178/" in req.full_url:
            raise ConnectionResetError("Core ML inference failed")
        return _Words("metal words")

    monkeypatch.setattr(transcriber, "urlopen_loopback", post)
    _metal_takes_over(monkeypatch, rig)
    for _launch in range(macos_coreml.MAX_COMPARISON_FAILURES):
        macos_coreml._session_failures.clear()   # a relaunch
        assert macos_coreml.comparison_blocked(str(cli), str(model)) is None
        # The hand-over published the loaded Core ML server.
        monkeypatch.setattr(cls, "_server_process", core_ml)
        monkeypatch.setattr(cls, "_server_port", 8178)
        monkeypatch.setattr(cls, "_server_binary", rig.backend.whisper_server_binary)
        assert rig.backend.transcribe(rig.audio) == "metal words"
        assert "earlier this session" in macos_coreml.comparison_blocked(str(cli), str(model)), \
            "the next hand-over would load the failing Core ML server again"
    macos_coreml._session_failures.clear()
    assert "next try within 24 h" in macos_coreml.comparison_blocked(str(cli), str(model))


def test_a_core_ml_warm_up_after_its_server_died_warms_metal(monkeypatch, tmp_path):
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=False)
    assert rig.backend.warm_up() is True
    assert rig.served == ["warm-up"]
    assert rig.scheduled == [(rig.config, rig.twin)]


def test_a_resident_core_ml_backend_still_serves_while_loaded(monkeypatch, tmp_path):
    # Control: the ready-only rule changes nothing while Core ML is loaded,
    # and its success ends any failure streak.
    from wayfinder.core import transcriber
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    seen = _runtime_memory(monkeypatch)
    monkeypatch.setattr(transcriber, "urlopen_loopback", lambda *a, **k: _Words("core ml words"))
    assert rig.backend.transcribe(rig.audio) == "core ml words"
    assert rig.served == [] and rig.scheduled == []
    assert seen.successes == 1 and seen.failures == []


@pytest.mark.parametrize("picked", [True, False])
def test_a_late_metal_fallback_after_another_model_is_picked_schedules_nothing(
        monkeypatch, tmp_path, picked):
    # Sol mac r3/r4: A's chunk is still in flight (a force reset retired it,
    # or it is the Welcome test); the user picks B, which loads; A's request
    # fails, and its Metal twin replaced B and re-requested A's Core ML.
    import threading
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    monkeypatch.setattr(cls, "_epoch", cls._epoch)
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    seen = _runtime_memory(monkeypatch)
    model_a = tmp_path / "turbo-a.bin"
    model_a.write_bytes(b"a")
    rig.config["model_path"] = str(model_a)
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda cli: True)
    monkeypatch.setattr(macos_coreml, "_worker", SimpleNamespace(is_alive=lambda: True))
    monkeypatch.setattr(macos_coreml, "_preparation_requested", threading.Event())
    monkeypatch.setattr(macos_coreml, "_job_running", threading.Event())
    monkeypatch.setattr(macos_coreml, "_latest_config", None)
    monkeypatch.setattr(macos_coreml, "_latest_epoch", None)
    monkeypatch.setattr(macos_coreml, "schedule_preparation", _REAL_SCHEDULE_PREPARATION)
    model_b = {"model_path": str(tmp_path / "turbo-b.bin"), "use_gpu": True}
    b_server = SimpleNamespace(poll=lambda: None)
    if picked:
        # The twin's real start: superseded, it spawns and stops nothing.
        monkeypatch.setattr(cls, "_server_lock", threading.Lock())
        monkeypatch.setattr(transcriber.subprocess, "Popen",
                            lambda *a, **k: pytest.fail("A's twin spawned a server over B's"))
        cli = SimpleNamespace(transcribe=lambda audio, context="", deadline=None: "a's words")
        rig.twin._cli_fallback = lambda: cli
    else:
        _metal_takes_over(monkeypatch, rig)

    def post(req, timeout):
        if ":8178/" in req.full_url:
            if picked:
                _pick_another_model(monkeypatch, model_b)
                monkeypatch.setattr(cls, "_server_process", b_server)   # B's warm-up loaded B
                monkeypatch.setattr(cls, "_server_coreml_key", None)
            raise ConnectionResetError("B's warm-up stopped A's server")
        return _Words("a's words")

    monkeypatch.setattr(transcriber, "urlopen_loopback", post)
    assert rig.backend.transcribe(rig.audio) == "a's words"
    if picked:
        assert cls._server_process is b_server, "A's Metal twin replaced B's server"
        assert macos_coreml._latest_config == model_b, "A's late fallback re-requested A's Core ML"
        assert seen.failures == [], "a model change counted as a Core ML failure"
    else:
        assert macos_coreml._latest_config is rig.config   # control: the hand-over is requested
        assert seen.failures == ["request failed (ConnectionResetError)"]


def test_a_model_change_supersedes_backends_built_for_the_old_model(monkeypatch):
    # Even with no preparation job running: an old backend starts nothing.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    monkeypatch.setattr(cls, "_epoch", cls._epoch)
    monkeypatch.setattr(macos_coreml, "_worker", None)
    old = transcriber.WhisperServerBackend(whisper_server_binary="/bin/whisper-server",
                                           model_path="/models/a.bin")
    _pick_another_model(monkeypatch, {"model_path": "/models/b.bin"})
    with pytest.raises(transcriber.ServerStartSuperseded):
        old._start_server()


@pytest.mark.parametrize("path", ["tray", "save and apply", "a download finishing after the pick"])
def test_a_model_picked_mid_dictation_waits_for_idle(monkeypatch, tmp_path, path):
    # Sol mac r3: loading it mid-dictation reloaded the server under the
    # running request, whose recovery then brought the old model back.
    # Sol mac r4: a download started earlier that finished after the pick
    # replaced the pick in the queue.
    import wayfinder.license as license_mod
    import wayfinder_main
    from wayfinder.state import AppState
    model_b = tmp_path / "ggml-large-v3-turbo-q8_0.bin"
    model_b.write_bytes(b"b")
    events = []
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", True)
    monkeypatch.setattr(wayfinder_main, "save_config", lambda cfg: None)
    monkeypatch.setattr(wayfinder_main, "_resolve_whisper_model", lambda name: model_b)
    monkeypatch.setattr(license_mod, "transcription_model_allowed", lambda path, gate: True)
    monkeypatch.setattr(wayfinder_main.WayfinderApp, "_refresh_basic_settings", lambda self: None)
    monkeypatch.setattr(wayfinder_main.WayfinderApp, "_retire_coreml_preparation",
                        lambda self: events.append("retire"))
    config = {"model_path": "~/models/turbo-a.bin"}
    app = SimpleNamespace(
        config=config, app_state=AppState.RECORDING, log=lambda *a: None,
        get_model_display=lambda: "Turbo B", _maybe_show_setup_cue=lambda: None,
        feature_gate=SimpleNamespace(has_feature=lambda feature: True),
        _warm_up_transcription=lambda *a, **k: events.append("warm-up"),
    )
    app._set_active_whisper_model = lambda p: wayfinder_main.WayfinderApp._set_active_whisper_model(app, p)
    app._apply_downloaded_model = lambda role, p, info=None: (
        wayfinder_main.WayfinderApp._apply_downloaded_model(app, role, p, info))
    if path == "tray":
        wayfinder_main.WayfinderApp._apply_model_from_tray(app, "large-v3-turbo-q8_0")
    else:
        wayfinder_main.WayfinderApp._set_active_whisper_model(app, str(model_b))
    if path.startswith("a download"):
        downloaded = tmp_path / "ggml-small.en.bin"
        downloaded.write_bytes(b"c")
        assert wayfinder_main.WayfinderApp._activate_downloaded_model(
            app, "whisper", str(downloaded)) == "skipped"
    assert config["model_path"] == "~/models/turbo-a.bin" and events == []
    app.app_state = AppState.IDLE
    wayfinder_main.WayfinderApp._flush_pending_model_activations(app)
    assert os.path.expanduser(config["model_path"]) == str(model_b)
    assert events == ["retire", "warm-up"]
    assert app._pending_model_pick is None   # the next dictation's downloads apply again


@pytest.mark.parametrize("cleanup_queued", [True, False])
def test_a_pick_at_idle_before_the_flush_runs_is_not_replaced_by_the_queued_one(
        monkeypatch, tmp_path, cleanup_queued):
    # Sol mac r5: B picked mid-dictation; IDLE queues the flush; C is picked
    # before it runs and applies at once; the flush then applied B over C.
    import wayfinder.license as license_mod
    import wayfinder_main
    from wayfinder.state import AppState
    model_b, model_c = tmp_path / "ggml-b.bin", tmp_path / "ggml-c.bin"
    model_b.write_bytes(b"b")
    model_c.write_bytes(b"c")
    monkeypatch.setattr(wayfinder_main, "IS_MACOS", False)
    monkeypatch.setattr(wayfinder_main, "save_config", lambda cfg: None)
    monkeypatch.setattr(license_mod, "transcription_model_allowed", lambda path, gate: True)
    monkeypatch.setattr(wayfinder_main.WayfinderApp, "_refresh_basic_settings", lambda self: None)
    config = {"model_path": "~/models/a.bin"}
    llm = (str(tmp_path / "cleanup.gguf"), None)
    app = SimpleNamespace(
        config=config, app_state=AppState.PROCESSING, log=lambda *a: None,
        get_model_display=lambda: "C", _maybe_show_setup_cue=lambda: None,
        feature_gate=SimpleNamespace(has_feature=lambda feature: True),
        _warm_up_transcription=lambda *a, **k: None,
        _pending_model_activations={"llm": llm} if cleanup_queued else {},
    )
    app._set_active_whisper_model = lambda p: wayfinder_main.WayfinderApp._set_active_whisper_model(app, p)
    applied = []
    app._apply_downloaded_model = lambda role, p, info=None: applied.append(role) or (
        wayfinder_main.WayfinderApp._apply_downloaded_model(app, role, p, info) if role == "whisper" else True)
    wayfinder_main.WayfinderApp._set_active_whisper_model(app, str(model_b))
    app.app_state = AppState.IDLE   # the flush is queued, not yet run
    wayfinder_main.WayfinderApp._set_active_whisper_model(app, str(model_c))
    assert os.path.expanduser(config["model_path"]) == str(model_c)
    wayfinder_main.WayfinderApp._flush_pending_model_activations(app)
    assert os.path.expanduser(config["model_path"]) == str(model_c), "the queued pick replaced the newer one"
    assert applied == (["llm"] if cleanup_queued else [])   # a queued cleanup model still applies
    # With nothing left to flush, the next dictation's download is not
    # mistaken for one racing a pick.
    app.app_state = AppState.PROCESSING
    later = tmp_path / "ggml-d.bin"
    later.write_bytes(b"d")
    assert wayfinder_main.WayfinderApp._activate_downloaded_model(app, "whisper", str(later)) == "deferred"


def test_a_core_ml_backend_salvages_through_metal_cli(monkeypatch):
    backend, _config, _scheduled = _darwin_selection(monkeypatch, resident=True)
    assert backend._cli_fallback() is backend._ready_only_fallback._cli_fallback()


def test_a_hand_over_that_finds_this_models_core_ml_dead_publishes_nothing(comparison_rig, monkeypatch):
    # Sol mac r6: the publish counted the retiring Core ML record's death for
    # the same model, then published that model's Core ML anyway.
    from contextlib import contextmanager
    rig = comparison_rig
    key = (str(rig.cli), str(rig.model))
    cls, old, probe = _published_server_rig(monkeypatch)
    old.poll.return_value = 9
    monkeypatch.setattr(cls, "_server_coreml_key", key)
    monkeypatch.setattr(cls, "_closed", False)
    monkeypatch.setattr(macos_coreml, "comparison", lambda c, m: {"preferred": True})
    monkeypatch.setattr(macos_coreml, "coreml_twin", lambda c, m: "/twin/whisper-cli")
    monkeypatch.setattr(macos_coreml, "coreml_resident", lambda *a: False)
    monkeypatch.setattr(macos_coreml, "_stop_probe", lambda proc: pytest.fail("a server was replaced"))

    @contextmanager
    def probe_server(*_a, **_k):
        yield probe

    monkeypatch.setattr(macos_coreml, "_probe_server", probe_server)
    with pytest.raises(InterruptedError):
        macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: False)
    assert cls._server_process is old and not probe.adopted
    assert macos_coreml.comparison_failures(*key) == 1


def test_a_reset_while_the_backend_is_built_schedules_no_preparation(monkeypatch):
    # Sol mac r7: get_backend scheduled the Core ML preparation for a
    # dictation retired while it ran, and a reset leaves that job eligible.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
    token = cls.request_token()
    cls.abandon_requests()
    with transcriber.dictation_requests(token):
        _backend, _config, scheduled = _darwin_selection(monkeypatch, resident=False)
    assert scheduled == []


def test_a_core_ml_answer_after_a_reset_proves_nothing(monkeypatch, tmp_path):
    # Sol mac r7 (minor): the retired dictation's answer recorded a runtime
    # success, which clears failure history and marks the launch proven.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
    rig = _resident_core_ml(monkeypatch, tmp_path, alive=True)
    seen = _runtime_memory(monkeypatch)

    def answer(*_a, **_k):
        cls.abandon_requests()   # Escape as the answer came back
        return _Words("late words")

    monkeypatch.setattr(transcriber, "urlopen_loopback", answer)
    rig.backend.transcribe(rig.audio)
    assert seen.successes == 0 and seen.failures == []


def _failure_rig(monkeypatch, tmp_path):
    """Real failure accounting over a scratch failure file; returns the
    runtime key."""
    cli = tmp_path / "whisper-cli"; cli.write_text("cli")
    model = tmp_path / "m.bin"; model.write_bytes(b"\x00")
    monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "failures.json")
    monkeypatch.setattr(macos_coreml, "_session_failures", set())
    monkeypatch.setattr(macos_coreml, "_runtime_proven", set())
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"chip": "test"})
    return (str(cli), str(model))


def _seed_a_streak(tmp_path, key):
    import json
    row = {macos_coreml._failure_key(*key): {"count": 1, "reason": "earlier", "at": 0}}
    (tmp_path / "failures.json").write_text(json.dumps(row), encoding="utf-8")


@pytest.mark.parametrize("retired", [True, False])
@pytest.mark.parametrize("at", ["lock-wait", "key", "read"])
@pytest.mark.parametrize("outcome", ["failure", "success"])
def test_core_ml_accounting_asks_the_retirement_under_its_lock(monkeypatch, tmp_path, outcome, at, retired):
    # Sol mac r11: a retirement while the accounting waited for its lock
    # still wrote the failure (Core ML blocked for the session) or cleared
    # the streak. *at*: the lock wait, the key's computation, or the read of
    # the failure file under the lock (Sol mac r12).
    import threading
    key = _failure_rig(monkeypatch, tmp_path)
    if outcome == "success":
        _seed_a_streak(tmp_path, key)
    else:
        macos_coreml._runtime_proven.add(key)
    flag = [False]
    if at == "key":
        failure_key = macos_coreml._failure_key

        def computed(*args):
            flag[0] = retired
            return failure_key(*args)

        monkeypatch.setattr(macos_coreml, "_failure_key", computed)
    if at == "read":
        read_failures = macos_coreml._read_failures

        def read():
            flag[0] = retired
            return read_failures()

        monkeypatch.setattr(macos_coreml, "_read_failures", read)
    if outcome == "failure":
        def record():
            macos_coreml.record_runtime_failure(key, "request failed", cancelled=lambda: flag[0])
    else:
        def record():
            macos_coreml.record_runtime_success(key, cancelled=lambda: flag[0])
    with macos_coreml._failure_lock:   # another record holds it
        worker = threading.Thread(target=record, daemon=True)
        worker.start()
        worker.join(0.2)
        assert worker.is_alive()
        if at == "lock-wait":
            flag[0] = retired
    worker.join(5)
    flag[0] = False
    rows = macos_coreml._read_failures()
    if outcome == "failure":
        assert (rows == {} and macos_coreml._session_failures == set()) is retired
        assert (key in macos_coreml._runtime_proven) is retired
    else:
        assert (len(rows) == 1 and macos_coreml._runtime_proven == set()) is retired


@pytest.mark.parametrize("outcome", ["failure", "success"])
def test_a_retired_dictations_core_ml_outcome_is_not_counted(monkeypatch, tmp_path, outcome):
    # Through the backend: its record passes the dictation's retirement on.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    key = _failure_rig(monkeypatch, tmp_path)
    if outcome == "success":
        _seed_a_streak(tmp_path, key)
    monkeypatch.setattr(macos_coreml, "runtime_key", lambda config: key)
    monkeypatch.setattr(cls, "_abandoned", cls._abandoned)
    monkeypatch.setattr(cls, "_closed", False)
    backend = cls(use_gpu=False, timeout=5)
    backend._ready_only_config = {}
    backend._load_epoch = cls._epoch
    with transcriber.dictation_requests(cls.request_token()):
        cls.abandon_requests()
        if outcome == "failure":
            backend._record_coreml_failure("request failed")
        else:
            backend._record_coreml_success()
    assert len(macos_coreml._read_failures()) == (0 if outcome == "failure" else 1)


@pytest.mark.parametrize("change", ["mode-switch", "quit"])
@pytest.mark.parametrize("outcome", ["failure", "success"])
def test_a_superseded_servers_core_ml_outcome_is_not_counted(monkeypatch, tmp_path, outcome, change):
    # Sol mac r12: the backend asked the epoch only before the accounting; a
    # mode switch or Quit during its read still counted. The whole predicate
    # is now asked under the lock, after the read.
    from wayfinder.core import transcriber
    cls = transcriber.WhisperServerBackend
    key = _failure_rig(monkeypatch, tmp_path)
    if outcome == "success":
        _seed_a_streak(tmp_path, key)
    monkeypatch.setattr(macos_coreml, "runtime_key", lambda config: key)
    monkeypatch.setattr(cls, "_closed", False)
    monkeypatch.setattr(cls, "_epoch", cls._epoch)
    backend = cls(use_gpu=False, timeout=5)
    backend._ready_only_config = {}
    backend._load_epoch = cls._epoch
    read_failures = macos_coreml._read_failures

    def read():
        if change == "quit":
            cls._closed = True
        else:
            cls._epoch += 1
        return read_failures()

    monkeypatch.setattr(macos_coreml, "_read_failures", read)
    if outcome == "failure":
        backend._record_coreml_failure("request failed")
    else:
        backend._record_coreml_success()
    assert len(read_failures()) == (0 if outcome == "failure" else 1)
    assert macos_coreml._session_failures == set()


def test_a_preparation_superseded_in_its_accounting_counts_nothing(comparison_rig, monkeypatch):
    # Sol mac r12: the preparation asked its cancellation before counting a
    # failure, not under the accounting lock: a switch while it waited there
    # still counted one.
    rig = comparison_rig
    flag = [False]

    failed = [False]

    def probes(*_a):
        failed[0] = True
        raise TimeoutError("Encoder device preparation timed out")

    read_failures = macos_coreml._read_failures

    def read():
        flag[0] = failed[0]   # superseded while the accounting read the file
        return read_failures()

    monkeypatch.setattr(macos_coreml, "_compare_with_probes", probes)
    monkeypatch.setattr(macos_coreml, "_read_failures", read)
    with pytest.raises(TimeoutError):
        macos_coreml._prepare_comparison(rig.config, rig.backend, cancelled=lambda: flag[0])
    assert macos_coreml.comparison_failures(str(rig.cli), str(rig.model)) == 0


# Sol mac r19: a switch that cancels Core ML preparation while a helper's
# watcher gets ready passed dictation's spawn check (no dictation owns this
# work), so the superseded helper started and was only stopped afterwards.

def _cancelled_while_the_watcher_gets_ready(monkeypatch, cancel):
    """Every spawn's command, recorded: the watcher's, then the helper's."""
    import subprocess
    from wayfinder.utils import child_supervisor
    spawned = []

    class Recording(subprocess.Popen):
        def __init__(self, args, **kwargs):
            spawned.append(list(args))
            super().__init__(args, **kwargs)

    monkeypatch.setattr(child_supervisor, "_POPEN", Recording)
    monkeypatch.setattr(child_supervisor.subprocess, "Popen", Recording)
    original = child_supervisor.spawn_supervised
    monkeypatch.setattr(child_supervisor, "spawn_supervised",
                        lambda command, **kw: original(command, platform_name="darwin", **kw))
    ready = child_supervisor._watcher_ready

    def meanwhile(watcher, timeout):
        cancel.set()
        return ready(watcher, timeout)

    monkeypatch.setattr(child_supervisor, "_watcher_ready", meanwhile)
    return spawned


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_probe_cancelled_while_its_watcher_gets_ready_never_starts(tmp_path, monkeypatch):
    import sys
    import threading
    import time
    from wayfinder.utils import child_supervisor
    marker = tmp_path / "ran"
    cancel = threading.Event()
    spawned = _cancelled_while_the_watcher_gets_ready(monkeypatch, cancel)
    backend = _probe_backend([sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])
    with pytest.raises(InterruptedError):
        with macos_coreml._probe_server(backend, "/bin/whisper-server", "/m.bin",
                                        coreml=True, cancelled=cancel.is_set):
            pass
    assert cancel.is_set()
    assert len(spawned) == 1                              # the watcher only
    assert not child_supervisor._tracked
    time.sleep(0.5)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="the compiler stand-in is a shell script")
def test_a_compile_cancelled_while_its_watcher_gets_ready_never_starts(tmp_path, monkeypatch):
    import threading
    import time
    from wayfinder.utils import child_supervisor, macos_coreml_convert
    marker = tmp_path / "ran"
    cli = tmp_path / "bin" / "whisper-cli"
    cli.parent.mkdir()
    cli.write_bytes(b"cli")
    compiler = cli.with_name("coreml-compile")
    compiler.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    compiler.chmod(0o700)
    model = tmp_path / "ggml-large-v3-turbo-q5_0.bin"
    model.write_bytes(b"model")
    monkeypatch.setattr(macos_coreml, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(macos_coreml, "_conversion_root", lambda: tmp_path / "derived")
    monkeypatch.setattr(macos_coreml, "_failure_path", lambda: tmp_path / "coreml-failures.json")
    monkeypatch.setattr(macos_coreml, "_hardware_identity", lambda: {"hardware": "MacBookAir10,1"})
    monkeypatch.setattr(macos_coreml, "_binary_twin", lambda c: cli)
    monkeypatch.setattr(macos_coreml, "prepared_twin", lambda c, m: None)
    monkeypatch.setattr(macos_coreml_convert, "template_path", lambda: None)
    monkeypatch.setattr(macos_coreml_convert, "load_bindings", lambda path: {"weights_size": 1})
    monkeypatch.setattr(macos_coreml_convert, "populate_package", lambda m, p, cancelled: {"format": 2})
    cancel = threading.Event()
    spawned = _cancelled_while_the_watcher_gets_ready(monkeypatch, cancel)
    with pytest.raises(InterruptedError):
        macos_coreml.prepare_encoder(str(cli), str(model), cancelled=cancel.is_set)
    assert cancel.is_set()
    assert len(spawned) == 1                              # the watcher only
    assert not child_supervisor._tracked
    time.sleep(0.5)
    assert not marker.exists()
    assert macos_coreml.comparison_failures(str(cli), str(model)) == 0

"""Record-orchestration tests — the single most-used path in the product.

The orchestration layer lives on ``WayfinderApp`` in ``wayfinder_main.py`` (a
~13.3K-line module whose ``WayfinderApp`` needs a real Tk root, so it is never
instantiated here). Instead we drive the *real* unbound methods against a
controlled stub ``self`` (``FakeApp``): the methods under test are attached to
``FakeApp`` verbatim from ``WayfinderApp`` — so this is the production code, not
a reimplementation — while the infrastructure they lean on (Tk ``after``/UI
paint via ``update_state``, thread pools, the recorder/transcriber/injector) is
replaced by deterministic fakes.

What is asserted (behaviour, not just call counts):
  * the record → transcribe → paste happy path walks
    IDLE→RECORDING→PROCESSING→PASTING→IDLE and the final text reaches the
    injector unchanged;
  * a transcription failure recovers to IDLE and injects NOTHING;
  * empty / too-short / silent recordings error out and never inject;
  * chunked finalize combines chunks *in order*, post-processes the combined
    text exactly once, and emits a single terminal event;
  * session-generation staleness: a superseded session's late result is dropped.

``wayfinder.state.get_next_state`` is the pure core (covered by test_state.py);
these tests verify the orchestration transitions are consistent with it rather
than re-testing it.
"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

wayfinder_main = pytest.importorskip("wayfinder_main")

# IMPORTANT: wayfinder_main.py defines its OWN AppState enum (line ~306), which
# is a DIFFERENT class object from wayfinder.state.AppState even though the two
# are structurally identical. The orchestration code hard-codes the former, so
# tests must drive/assert with wayfinder_main.AppState or enum-identity (Enum
# __eq__) comparisons silently fail. CoreState is the pure state machine's enum,
# used only for the get_next_state coherence check.
from wayfinder.state import AppState as CoreState, get_next_state  # noqa: E402

AppState = wayfinder_main.AppState
WApp = wayfinder_main.WayfinderApp


# ---------------------------------------------------------------------------
# Deterministic fakes
# ---------------------------------------------------------------------------

class SyncExecutor:
    """ThreadPoolExecutor stand-in: submit() runs the work synchronously.

    This collapses the executor→worker→event-queue hop into the calling thread
    so the whole pipeline is deterministic and single-threaded in tests.
    """

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        fn(*args, **kwargs)
        return SimpleNamespace(result=lambda timeout=None: None)


class FakeOverlay:
    """Overlay controller stub — records the command sequence, always succeeds."""

    def __init__(self):
        self.commands = []

    def show(self, state):
        self.commands.append(("show", state))
        return True

    def update(self, state):
        self.commands.append(("update", state))
        return True


class FakeRecorder:
    """Simple (non-chunked) recorder stub."""

    def __init__(self, duration=3.0, peak=0.5, audio_path="/tmp/fake-wayfinder-rec.wav"):
        self._duration = duration
        self._peak = peak
        self._audio_path = audio_path
        self.started = False
        self.stopped = False
        self.cleaned = False
        self.device = 7

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return self._audio_path

    def cleanup(self):
        self.cleaned = True

    def get_duration(self):
        return self._duration

    def get_peak_amplitude(self):
        return self._peak

    def is_recording(self):
        return self.started and not self.stopped


class FakeChunkedRecorder:
    """Chunked recorder stub. stop() returns (final_path, all_paths)."""

    def __init__(self, duration=45.0, peak=0.5, chunk_count=2,
                 final_path="/tmp/fake-final.wav", **kwargs):
        self._duration = duration
        self._peak = peak
        self._chunk_count = chunk_count
        self._final_path = final_path
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.cleaned = False
        self.device = 7

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return self._final_path, ["/tmp/c0.wav", "/tmp/c1.wav"]

    def cleanup(self):
        self.cleaned = True

    def get_duration(self):
        return self._duration

    def get_peak_amplitude(self):
        return self._peak

    def get_chunk_count(self):
        return self._chunk_count

    def is_recording(self):
        return self.started and not self.stopped


class FakeApp:
    """A stub ``self`` carrying exactly the surface the record methods touch.

    The orchestration methods are the *real* WayfinderApp functions (attached
    below); everything else here is a deterministic fake. Tk affordances are
    stubbed: ``update_state`` records the transition and mutates ``app_state``
    the way the real one does (minus all UI/tray/ducking side effects), and
    ``after`` records callbacks WITHOUT auto-running them (so the recording-
    duration self-reschedule can never recurse). Tests that need a scheduled
    callback to fire call :meth:`run_after` explicitly.
    """

    # --- real production methods under test (bound to this fake instance) ---
    on_record_button = WApp.on_record_button
    on_hotkey = WApp.on_hotkey
    start_recording = WApp.start_recording
    _start_chunked_recording = WApp._start_chunked_recording
    _update_recording_duration = WApp._update_recording_duration
    _transcribe_chunk = WApp._transcribe_chunk
    stop_recording_and_process = WApp.stop_recording_and_process
    _stop_simple_recording = WApp._stop_simple_recording
    _stop_chunked_recording = WApp._stop_chunked_recording
    _finalize_chunked_transcription = WApp._finalize_chunked_transcription
    _deduplicate_overlap_text = WApp._deduplicate_overlap_text
    _find_text_overlap = WApp._find_text_overlap
    _silence_error_message = WApp._silence_error_message
    _maybe_show_gpu_nudge = WApp._maybe_show_gpu_nudge
    transcribe_and_inject = WApp.transcribe_and_inject
    on_transcription_done = WApp.on_transcription_done
    do_inject = WApp.do_inject
    on_injection_done = WApp.on_injection_done
    _finish_injection = WApp._finish_injection
    on_error = WApp.on_error
    handle_event = WApp.handle_event
    _split_gen = staticmethod(WApp._split_gen)  # real code: it's a @staticmethod

    def __init__(self, config):
        self.config = config
        self.app_state = AppState.IDLE
        self.session_generation = 0

        # transition + log capture
        self.states = []           # list[AppState] passed to update_state
        self.logs = []

        # timers / jobs
        self._finish_injection_job = None
        self._duration_update_job = None
        self._recording_start_time = None
        self._processing_start_time = None
        self._gpu_nudge_shown = False  # GPU-nudge gate reads this; no feature_gate → no-op
        self._after_calls = []     # list[(delay, fn)] recorded, not auto-run
        self._after_cancelled = set()
        self._after_token = 0

        # overlay / indicator
        self._use_pyqt_overlay = True
        self.overlay_controller = FakeOverlay()
        self.indicator = None

        # status label (the recording-duration ticker writes to it)
        self.status_label = SimpleNamespace(configure=lambda **k: None)

        # recorders / audio
        self.recorder = FakeRecorder()
        self.chunked_recorder = None
        self.warm_mic = SimpleNamespace(device=7)
        self._resolved_audio_device = 7
        self._inject_target_window = None

        # chunk plumbing
        self.chunk_transcriptions = []
        self.chunk_transcription_lock = threading.Lock()

        # executors + event queue
        self.executor = SyncExecutor()
        self.transcription_executor = SyncExecutor()
        self.event_queue = queue.Queue()

        # misc flags read via getattr
        self._welcome_active = False
        self._welcome_pane = None
        self._game_mode = False
        self.last_transcription = ""

        # voice-learning side channel (personal tone only)
        self.voice_learned = []

    # --- stubbed Tk / infra ------------------------------------------------
    def update_state(self, new_state):
        # Mirror the real method's core mutation (old→new) without any UI work.
        self.app_state = new_state
        self.states.append(new_state)

    def log(self, msg):
        self.logs.append(msg)

    def after(self, delay, fn=None):
        self._after_token += 1
        token = self._after_token
        self._after_calls.append((token, delay, fn))
        return token

    def after_cancel(self, token):
        self._after_cancelled.add(token)

    def _add_to_voice_learning(self, text):
        self.voice_learned.append(text)

    # --- test helpers ------------------------------------------------------
    def run_after(self):
        """Fire recorded, non-cancelled ``after`` callbacks once (FIFO).

        Snapshots the pending list first so callbacks that schedule *new*
        after() work don't get run in the same drain (prevents runaway
        self-rescheduling loops like the recording-duration timer).
        """
        pending = self._after_calls
        self._after_calls = []
        for token, _delay, fn in pending:
            if token in self._after_cancelled or fn is None:
                continue
            fn()

    def pump_events(self, limit=20):
        """Drain the event queue through the real handle_event dispatcher."""
        n = 0
        while n < limit:
            try:
                et, data = self.event_queue.get_nowait()
            except queue.Empty:
                break
            self.handle_event(et, data)
            n += 1
        return n


@pytest.fixture
def config():
    from wayfinder.config import DEFAULT_CONFIG
    cfg = DEFAULT_CONFIG.copy()
    cfg["chunked_mode"] = False        # simple path by default; chunked tests override
    cfg["post_processing_enabled"] = False
    cfg["audio_ducking_enabled"] = False
    cfg["min_recording_duration"] = 0.5
    return cfg


@pytest.fixture
def app(config, monkeypatch):
    # Neutralise the X11 focus probe and the real injector for every test.
    monkeypatch.setattr(wayfinder_main, "inject_text", lambda *a, **k: None)
    import wayfinder.core.injector as _inj
    monkeypatch.setattr(_inj, "get_active_window", lambda: None, raising=False)
    return FakeApp(config)


# ===========================================================================
# on_record_button / on_hotkey dispatch
# ===========================================================================

class TestRecordButtonDispatch:
    def test_button_from_idle_starts_recording(self, app, monkeypatch):
        called = {}
        monkeypatch.setattr(app, "start_recording", lambda: called.setdefault("start", True))
        monkeypatch.setattr(app, "stop_recording_and_process", lambda: called.setdefault("stop", True))
        app.app_state = AppState.IDLE
        app.on_record_button()
        assert called == {"start": True}

    def test_button_from_recording_stops_and_processes(self, app, monkeypatch):
        called = {}
        monkeypatch.setattr(app, "start_recording", lambda: called.setdefault("start", True))
        monkeypatch.setattr(app, "stop_recording_and_process", lambda: called.setdefault("stop", True))
        app.app_state = AppState.RECORDING
        app.on_record_button()
        assert called == {"stop": True}

    def test_button_during_processing_is_ignored(self, app, monkeypatch):
        called = {}
        monkeypatch.setattr(app, "start_recording", lambda: called.setdefault("start", True))
        monkeypatch.setattr(app, "stop_recording_and_process", lambda: called.setdefault("stop", True))
        # A press mid-transcription must NOT start a new recording or double-stop.
        app.app_state = AppState.PROCESSING
        app.on_record_button()
        assert called == {}
        app.app_state = AppState.PASTING
        app.on_record_button()
        assert called == {}


# ===========================================================================
# start_recording
# ===========================================================================

class TestStartRecording:
    def test_simple_mode_transitions_to_recording_and_starts_recorder(self, app):
        app.config["chunked_mode"] = False
        app.on_hotkey()  # from IDLE
        assert app.app_state == AppState.RECORDING
        assert app.states == [AppState.RECORDING]
        assert app.recorder.started is True
        # This matches the pure state machine's IDLE --hotkey--> RECORDING edge.
        assert get_next_state(CoreState.IDLE, "hotkey") == CoreState.RECORDING

    def test_start_bumps_session_generation(self, app):
        before = app.session_generation
        app.start_recording()
        assert app.session_generation == before + 1

    def test_start_shows_listening_overlay(self, app):
        app.start_recording()
        assert ("show", "listening") in app.overlay_controller.commands

    def test_chunked_mode_creates_chunked_recorder(self, app, monkeypatch):
        app.config["chunked_mode"] = True
        created = {}

        def fake_chunked(**kwargs):
            rec = FakeChunkedRecorder(**{k: v for k, v in kwargs.items()
                                          if k in ("duration", "peak", "chunk_count")})
            created["rec"] = rec
            created["kwargs"] = kwargs
            return rec

        monkeypatch.setattr(wayfinder_main, "ChunkedRecorder", fake_chunked)
        app.start_recording()
        assert app.app_state == AppState.RECORDING
        assert created["rec"].started is True
        assert app.chunked_recorder is created["rec"]
        # Chunk params come straight from config (single source of truth).
        assert created["kwargs"]["chunk_duration"] == app.config["chunk_duration"]
        assert created["kwargs"]["sample_rate"] == app.config["sample_rate"]

    def test_remote_backend_skips_chunked_mode(self, app):
        # Cloud whisper backends handle long audio natively → simple recorder even
        # with chunked_mode on.
        app.config["chunked_mode"] = True
        app.config["transcription_backend"] = "groq_whisper"
        app.start_recording()
        assert app.recorder.started is True
        assert app.chunked_recorder is None

    def test_recorder_start_failure_routes_to_error(self, app, monkeypatch):
        app.config["chunked_mode"] = False

        def boom():
            raise RuntimeError("no mic")

        monkeypatch.setattr(app.recorder, "start", boom)
        app.start_recording()
        # on_error logged; state never advanced past the RECORDING it optimistically set,
        # but the error path funnels through _finish_injection → IDLE.
        app.run_after()
        assert any("no mic" in m for m in app.logs)


# ===========================================================================
# stop_recording_and_process — simple path
# ===========================================================================

class TestStopSimplePath:
    def _record(self, app):
        app.config["chunked_mode"] = False
        app.start_recording()
        return app.session_generation

    def test_processing_state_and_transcription_submitted(self, app, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "transcribe_with_config",
                            lambda path, cfg, **k: "hello world")
        self._record(app)
        app.stop_recording_and_process()
        assert AppState.PROCESSING in app.states
        # transcribe_and_inject ran (sync executor) and emitted a terminal event.
        et, data = app.event_queue.get_nowait()
        assert et == wayfinder_main.EventType.TRANSCRIPTION_DONE
        text, gen = app._split_gen(data)
        assert text == "hello world"

    def test_too_short_recording_errors_and_does_not_transcribe(self, app, monkeypatch):
        transcribed = {"called": False}
        monkeypatch.setattr(wayfinder_main, "transcribe_with_config",
                            lambda *a, **k: transcribed.update(called=True) or "x")
        app.recorder = FakeRecorder(duration=0.1)  # below min_recording_duration
        self._record(app)
        app.stop_recording_and_process()
        assert transcribed["called"] is False
        assert app.recorder.cleaned is True
        assert any("Too short" in m for m in app.logs)

    def test_silent_recording_errors_and_does_not_transcribe(self, app, monkeypatch):
        transcribed = {"called": False}
        monkeypatch.setattr(wayfinder_main, "transcribe_with_config",
                            lambda *a, **k: transcribed.update(called=True) or "x")
        # Long enough, but peak below the silence guard → whisper would hallucinate.
        app.recorder = FakeRecorder(duration=3.0, peak=wayfinder_main.SILENCE_PEAK_THRESHOLD / 2)
        self._record(app)
        app.stop_recording_and_process()
        assert transcribed["called"] is False
        assert app.recorder.cleaned is True
        # The silence message names the device / points at Settings → Audio.
        assert any("audio" in m.lower() or "mic" in m.lower() for m in app.logs)


# ===========================================================================
# Full happy path: record → transcribe → paste
# ===========================================================================

class TestHappyPath:
    def test_full_pipeline_states_and_injection_handoff(self, app, monkeypatch):
        injected = []
        monkeypatch.setattr(wayfinder_main, "transcribe_with_config",
                            lambda path, cfg, **k: "Hello there, friend.")
        monkeypatch.setattr(wayfinder_main, "inject_text",
                            lambda text, **k: injected.append(text))

        app.config["chunked_mode"] = False

        # 1) hotkey from IDLE → RECORDING
        app.on_record_button()
        assert app.app_state == AppState.RECORDING

        # 2) hotkey again → PROCESSING, transcription runs, TRANSCRIPTION_DONE queued
        app.on_record_button()
        assert app.app_state == AppState.PROCESSING

        # 3) dispatch the transcription event → PASTING, injection runs → INJECTION_DONE
        app.pump_events()
        # 4) the min-display-time reset was scheduled via after(); fire it → IDLE
        app.run_after()
        app.pump_events()
        app.run_after()

        assert app.app_state == AppState.IDLE
        # The exact record→paste transition ladder, in order.
        assert app.states == [
            AppState.RECORDING,
            AppState.PROCESSING,
            AppState.PASTING,
            AppState.IDLE,
        ]
        # The injector received the final text unchanged (single injection).
        assert injected == ["Hello there, friend."]

    def test_multiline_transcript_flattened_before_injection(self, app, monkeypatch):
        # do_inject collapses newlines/whitespace so ydotool never sends Enter.
        injected = []
        monkeypatch.setattr(wayfinder_main, "inject_text",
                            lambda text, **k: injected.append(text))
        app.update_state(AppState.PASTING)
        app.do_inject("line one\nline two   \r\n  end", app.session_generation)
        app.pump_events()
        assert injected == ["line one line two end"]

    def test_personal_tone_feeds_voice_learning(self, app, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "inject_text", lambda *a, **k: None)
        app.config["output_tone"] = "personal"
        app.config["caricature_mode"] = False
        app.on_transcription_done("remember this phrasing", app.session_generation)
        assert app.voice_learned == ["remember this phrasing"]

    def test_caricature_does_not_poison_voice_learning(self, app, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "inject_text", lambda *a, **k: None)
        app.config["output_tone"] = "personal"
        app.config["caricature_mode"] = True
        app.on_transcription_done("parody output", app.session_generation)
        assert app.voice_learned == []


# ===========================================================================
# Failure / empty paths — must recover state and NOT inject
# ===========================================================================

class TestFailurePaths:
    def test_transcription_exception_recovers_without_injecting(self, app, monkeypatch):
        injected = []
        monkeypatch.setattr(wayfinder_main, "inject_text",
                            lambda text, **k: injected.append(text))

        def boom(path, cfg, **k):
            raise RuntimeError("whisper crashed")

        monkeypatch.setattr(wayfinder_main, "transcribe_with_config", boom)

        app.config["chunked_mode"] = False
        app.on_record_button()   # RECORDING
        app.on_record_button()   # PROCESSING → transcribe (raises) → TRANSCRIPTION_ERROR
        app.pump_events()
        app.run_after()
        app.pump_events()
        app.run_after()

        assert injected == []                     # nothing typed
        assert app.app_state == AppState.IDLE      # recovered
        assert AppState.PASTING not in app.states  # never reached the paste state
        assert any("whisper crashed" in m for m in app.logs)

    def test_empty_transcription_errors_and_skips_injection(self, app, monkeypatch):
        injected = []
        monkeypatch.setattr(wayfinder_main, "inject_text",
                            lambda text, **k: injected.append(text))
        app.on_transcription_done("   ", app.session_generation)  # whitespace only
        app.run_after()
        assert injected == []
        assert app.app_state == AppState.IDLE
        assert AppState.PASTING not in app.states

    def test_stale_transcription_is_dropped(self, app, monkeypatch):
        injected = []
        monkeypatch.setattr(wayfinder_main, "inject_text",
                            lambda text, **k: injected.append(text))
        stale_gen = app.session_generation
        app.session_generation += 1  # a newer recording superseded this one
        app.on_transcription_done("late result", stale_gen)
        assert injected == []
        assert app.states == []  # no transition at all for a superseded session

    def test_stale_injection_is_dropped(self, app, monkeypatch):
        injected = []
        monkeypatch.setattr(wayfinder_main, "inject_text",
                            lambda text, **k: injected.append(text))
        stale_gen = app.session_generation
        app.session_generation += 1
        app.do_inject("late text", stale_gen)
        assert injected == []


# ===========================================================================
# Chunked finalize
# ===========================================================================

class TestChunkedFinalize:
    def test_combines_chunks_in_order_and_emits_once(self, app, monkeypatch):
        app.config["post_processing_enabled"] = False
        store = ["First chunk.", "Second chunk.", "Third chunk."]
        app.chunk_transcriptions = store
        rec = FakeChunkedRecorder()
        gen = app.session_generation

        app._finalize_chunked_transcription(len(store), gen, store, rec)

        # Exactly one terminal event, carrying the ordered combination.
        events = []
        while True:
            try:
                events.append(app.event_queue.get_nowait())
            except queue.Empty:
                break
        assert len(events) == 1
        et, data = events[0]
        assert et == wayfinder_main.EventType.CHUNKED_TRANSCRIPTION_DONE
        text, out_gen = app._split_gen(data)
        assert text == "First chunk. Second chunk. Third chunk."
        assert out_gen == gen
        assert rec.cleaned is True

    def test_post_processing_applied_once_to_combined_text(self, app, monkeypatch):
        app.config["post_processing_enabled"] = True
        calls = []

        def fake_pp(text, cfg):
            calls.append(text)
            return text.upper()

        import wayfinder.core.postprocessor as _pp
        monkeypatch.setattr(_pp, "process_with_config", fake_pp)

        store = ["alpha", "beta"]
        app.chunk_transcriptions = store
        gen = app.session_generation
        app._finalize_chunked_transcription(len(store), gen, store, FakeChunkedRecorder())

        # Post-processed exactly once, on the COMBINED text (not per-chunk).
        assert calls == ["alpha beta"]
        et, data = app.event_queue.get_nowait()
        text, _ = app._split_gen(data)
        assert text == "ALPHA BETA"

    def test_overlap_between_chunks_is_deduplicated(self, app):
        app.config["post_processing_enabled"] = False
        # Boundary repeats "over the lazy" — must appear once in the result.
        store = [
            "the quick brown fox jumps over the lazy",
            "over the lazy dog and keeps running",
        ]
        app.chunk_transcriptions = store
        gen = app.session_generation
        app._finalize_chunked_transcription(len(store), gen, store, FakeChunkedRecorder())
        et, data = app.event_queue.get_nowait()
        text, _ = app._split_gen(data)
        assert text.lower().count("over the lazy") == 1
        assert text.endswith("dog and keeps running")

    def test_all_chunks_empty_emits_error(self, app):
        app.config["post_processing_enabled"] = False
        store = ["[empty]", "[error]"]
        app.chunk_transcriptions = store
        gen = app.session_generation
        app._finalize_chunked_transcription(len(store), gen, store, FakeChunkedRecorder())
        et, data = app.event_queue.get_nowait()
        assert et == wayfinder_main.EventType.TRANSCRIPTION_ERROR

    def test_superseded_session_bails_without_emitting(self, app):
        store = ["chunk a", "chunk b"]
        app.chunk_transcriptions = store
        stale_gen = app.session_generation
        app.session_generation += 1  # a newer recording started
        rec = FakeChunkedRecorder()
        app._finalize_chunked_transcription(len(store), stale_gen, store, rec)
        # No terminal event for the dead session...
        assert app.event_queue.empty()
        # ...but this session's own recorder is still cleaned up (no leak).
        assert rec.cleaned is True

    def test_timeout_with_missing_chunks_still_finalizes(self, app, monkeypatch):
        import itertools
        app.config["post_processing_enabled"] = False
        # Only 1 of 3 chunks ever arrived. Drive time forward so the wait loop
        # trips its 120s timeout immediately (no real sleeping).
        monkeypatch.setattr(wayfinder_main.time, "sleep", lambda *a: None)
        monkeypatch.setattr(wayfinder_main.time, "time",
                            lambda _c=itertools.chain([0.0], itertools.repeat(9999.0)): next(_c))
        store = ["only chunk", "", ""]
        app.chunk_transcriptions = store
        gen = app.session_generation
        app._finalize_chunked_transcription(3, gen, store, FakeChunkedRecorder())
        et, data = app.event_queue.get_nowait()
        text, _ = app._split_gen(data)
        assert et == wayfinder_main.EventType.CHUNKED_TRANSCRIPTION_DONE
        assert text == "only chunk"
        assert any("Timeout" in m for m in app.logs)


# ===========================================================================
# stop_recording_and_process — chunked path wiring
# ===========================================================================

class TestStopChunkedPath:
    def test_stop_chunked_submits_final_chunk_and_finalizer(self, app, monkeypatch):
        monkeypatch.setattr(wayfinder_main, "transcribe_with_config",
                            lambda *a, **k: "final text")
        app.config["chunked_mode"] = True
        # chunk_count=0 → the only chunk is the final one, so the finalizer's
        # expected count (0 + 1) is met immediately (no 120s wait for phantom chunks).
        rec = FakeChunkedRecorder(duration=45.0, peak=0.5, chunk_count=0)
        app.chunked_recorder = rec
        rec.started = True
        app.app_state = AppState.RECORDING
        app.session_generation = 3

        app.stop_recording_and_process()

        assert AppState.PROCESSING in app.states
        assert rec.stopped is True
        # Sync executors ran: the final chunk was transcribed AND the finalizer ran,
        # producing a terminal event tagged with this session.
        seen = []
        while True:
            try:
                seen.append(app.event_queue.get_nowait())
            except queue.Empty:
                break
        types = {et for et, _ in seen}
        assert wayfinder_main.EventType.CHUNKED_TRANSCRIPTION_DONE in types

    def test_stop_chunked_too_short_errors(self, app):
        app.config["chunked_mode"] = True
        rec = FakeChunkedRecorder(duration=0.1)
        app.chunked_recorder = rec
        rec.started = True
        app.app_state = AppState.RECORDING
        app.stop_recording_and_process()
        assert rec.cleaned is True
        assert app.chunked_recorder is None
        assert any("Too short" in m for m in app.logs)


class TestGpuNudge:
    """Free-tier GPU upsell nudge gating (WayfinderApp._maybe_show_gpu_nudge).

    Real unbound methods driven against a minimal stub self — same approach as the
    orchestration tests above. Verifies the nudge fires ONLY on a long free-tier
    dictation, stays silent for premium/dev-unlock users, short clips, an already-
    shown session, and a persisted dismissal; and that Dismiss persists 'never again'.
    """

    class _FakeBanner:
        def __init__(self):
            self._packed = False

        def winfo_manager(self):
            return "pack" if self._packed else ""

        def pack(self, **_k):
            self._packed = True

        def pack_forget(self):
            self._packed = False

    def _make_self(self, *, shown=False, dismissed=False, has_gpu=False):
        ns = SimpleNamespace(
            _gpu_nudge_shown=shown,
            config={"gpu_nudge_dismissed": dismissed},
            feature_gate=SimpleNamespace(has_feature=lambda _fid: has_gpu),
            gpu_nudge_banner=self._FakeBanner(),
            _dictate_banner_anchor=None,
            log=lambda _m: None,
        )
        # Bind the real _hide_gpu_nudge so _dismiss_gpu_nudge exercises production code.
        ns._hide_gpu_nudge = wayfinder_main.WayfinderApp._hide_gpu_nudge.__get__(ns)
        return ns

    def _show(self, fake, dur):
        wayfinder_main.WayfinderApp._maybe_show_gpu_nudge(fake, dur)

    def test_long_free_dictation_shows_nudge(self):
        fake = self._make_self(has_gpu=False)
        self._show(fake, 60.0)
        assert fake.gpu_nudge_banner._packed
        assert fake._gpu_nudge_shown is True

    def test_short_dictation_stays_silent(self):
        fake = self._make_self(has_gpu=False)
        self._show(fake, 20.0)  # < _GPU_NUDGE_MIN_DURATION_S
        assert fake.gpu_nudge_banner._packed is False

    def test_premium_user_never_nudged(self):
        fake = self._make_self(has_gpu=True)  # already has GPU acceleration
        self._show(fake, 120.0)
        assert fake.gpu_nudge_banner._packed is False

    def test_dismissed_for_good_stays_silent(self):
        fake = self._make_self(has_gpu=False, dismissed=True)
        self._show(fake, 120.0)
        assert fake.gpu_nudge_banner._packed is False

    def test_at_most_once_per_session(self):
        fake = self._make_self(has_gpu=False, shown=True)
        self._show(fake, 120.0)
        assert fake.gpu_nudge_banner._packed is False

    def test_dismiss_persists_never_again(self, monkeypatch):
        saved = {}
        monkeypatch.setattr(wayfinder_main, "save_config", lambda c: saved.update(c))
        fake = self._make_self(has_gpu=False)
        self._show(fake, 60.0)
        assert fake.gpu_nudge_banner._packed is True
        wayfinder_main.WayfinderApp._dismiss_gpu_nudge(fake)
        assert fake.gpu_nudge_banner._packed is False
        assert fake.config["gpu_nudge_dismissed"] is True
        assert saved.get("gpu_nudge_dismissed") is True

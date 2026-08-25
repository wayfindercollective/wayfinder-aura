"""
Tests for the first-run welcome tour.

Covers the pure ``WelcomeFlow`` state machine and confirms the module imports
headlessly (WelcomeFlow available without touching Tk / creating windows).
"""


class TestWelcomeFlow:
    """State-machine transitions for the 3-step tour."""

    def test_starts_on_mic(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        assert flow.current == "mic"
        assert flow.index == 0
        assert flow.is_complete is False

    def test_advance_through_all_steps_completes(self):
        from wayfinder.ui.welcome import WelcomeFlow

        seen = []
        flow = WelcomeFlow(on_complete=lambda: seen.append("done"))

        flow.pass_mic_test()
        assert flow.advance() is True
        assert flow.current == "hotkey"
        flow.advance()
        assert flow.current == "dictate"
        assert flow.is_complete is False
        flow.advance()  # past last step -> complete
        assert flow.is_complete is True
        assert flow.current is None
        assert seen == ["done"]

    def test_skip_from_any_step_completes(self):
        from wayfinder.ui.welcome import WelcomeFlow

        for pre_advances in (0, 1, 2):
            seen = []
            flow = WelcomeFlow(on_complete=lambda seen=seen: seen.append(1))
            if pre_advances:
                flow.pass_mic_test()
            for _ in range(pre_advances):
                flow.advance()
            flow.skip()
            assert flow.is_complete is True
            assert seen == [1], f"on_complete not fired once after {pre_advances} advances"

    def test_on_complete_fires_exactly_once(self):
        from wayfinder.ui.welcome import WelcomeFlow

        calls = []
        flow = WelcomeFlow(on_complete=lambda: calls.append(1))
        flow.skip()
        flow.skip()  # idempotent
        flow.advance()  # no-op after complete
        assert calls == [1]

    def test_complete_is_idempotent(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        flow.pass_mic_test()
        flow.advance()
        flow.advance()
        flow.advance()  # complete
        assert flow.is_complete is True
        flow.advance()  # should not raise or reopen
        assert flow.is_complete is True
        assert flow.current is None

    def test_goto_jumps_to_named_step(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        flow.goto("dictate")
        assert flow.current == "dictate"
        assert flow.index == 2

    def test_goto_after_complete_is_noop(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        flow.skip()
        flow.goto("mic")
        assert flow.current is None
        assert flow.is_complete is True


class TestWelcomeModuleImport:
    """The module must be importable and WelcomeFlow usable even when
    customtkinter is unavailable — ctk is imported lazily inside WelcomePane."""

    def test_flow_works_without_customtkinter(self, monkeypatch):
        import importlib
        import sys

        # Force `import customtkinter` to fail anywhere it's attempted, then
        # (re)import the welcome module and exercise WelcomeFlow headlessly.
        monkeypatch.setitem(sys.modules, "customtkinter", None)
        sys.modules.pop("wayfinder.ui.welcome", None)
        mod = importlib.import_module("wayfinder.ui.welcome")

        flow = mod.WelcomeFlow()
        assert flow.current == "mic"
        flow.pass_mic_test()
        flow.advance()
        flow.advance()
        flow.advance()
        assert flow.is_complete is True


class TestWelcomeMicrophoneGate:
    def test_mic_step_cannot_continue_until_capture_passes(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        assert flow.can_advance is False
        assert flow.advance() is False
        assert flow.current == "mic"

        token = flow.begin_mic_test()
        assert flow.mark_mic_recording(token) is True
        assert flow.mark_mic_checking(token) is True
        assert flow.pass_mic_test(token) is True
        assert flow.can_advance is True
        assert flow.advance() is True
        assert flow.current == "hotkey"

    def test_retry_token_rejects_stale_worker_callbacks(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        first = flow.begin_mic_test()
        flow.invalidate_mic_test()
        second = flow.begin_mic_test()

        assert second != first
        assert flow.mark_mic_recording(first) is False
        assert flow.mic_test_state == "starting"
        assert flow.mark_mic_recording(second) is True

    def test_real_tutorial_dictation_can_satisfy_mic_gate(self):
        from wayfinder.ui.welcome import WelcomeFlow

        flow = WelcomeFlow()
        assert flow.pass_mic_test() is True
        assert flow.mic_test_state == "passed"
        assert flow.can_advance is True


class _FakeRecorder:
    def __init__(self, *, duration=5.0, peak=0.1, speech=True, speech_error=None):
        self.duration = duration
        self.peak = peak
        self.speech = speech
        self.speech_error = speech_error

    def get_duration(self):
        return self.duration

    def get_peak_amplitude(self):
        return self.peak

    def has_speech_activity(self):
        if self.speech_error is not None:
            raise self.speech_error
        return self.speech


class TestWelcomeMicrophoneAssessment:
    def test_voice_capture_passes(self):
        from wayfinder.ui.welcome import assess_microphone_capture

        result = assess_microphone_capture(_FakeRecorder())
        assert result.passed is True
        assert result.duration == 5.0

    def test_short_capture_fails_with_retry_guidance(self):
        from wayfinder.ui.welcome import assess_microphone_capture

        result = assess_microphone_capture(_FakeRecorder(duration=0.2))
        assert result.passed is False
        assert "too short" in result.message.lower()

    def test_silent_capture_fails_before_dictation(self):
        from wayfinder.ui.welcome import assess_microphone_capture

        result = assess_microphone_capture(_FakeRecorder(peak=0.0, speech=False))
        assert result.passed is False
        assert "no voice-level audio" in result.message.lower()

    def test_peak_fallback_supports_older_recorder(self):
        from wayfinder.ui.welcome import assess_microphone_capture

        result = assess_microphone_capture(_FakeRecorder(peak=0.05, speech_error=RuntimeError("not supported")))
        assert result.passed is True


class TestWelcomeMicrophoneGuidance:
    def test_portaudio_host_error_explains_bluetooth_profile(self):
        from wayfinder.ui.welcome import microphone_error_guidance

        message = microphone_error_guidance("Unanticipated host error [PaErrorCode -9999]")
        assert "Hands-Free / Headset" in message
        assert "High Fidelity Playback has no mic" in message

    def test_permission_error_is_actionable(self):
        from wayfinder.ui.welcome import microphone_error_guidance

        message = microphone_error_guidance("Permission denied")
        assert "privacy settings" in message

    def test_timeout_suggests_reconnect_or_restart(self):
        from wayfinder.ui.welcome import microphone_error_guidance

        message = microphone_error_guidance("audio device open timed out")
        assert "Reconnect" in message
        assert "restart Aura" in message

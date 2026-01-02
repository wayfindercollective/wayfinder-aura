"""
Tests for the state machine module.
"""

import pytest


class TestAppState:
    """Test application state enumeration."""

    def test_all_states_exist(self):
        """Verify all expected states exist."""
        from wayfinder.state import AppState

        assert hasattr(AppState, "IDLE")
        assert hasattr(AppState, "RECORDING")
        assert hasattr(AppState, "PROCESSING")
        assert hasattr(AppState, "PASTING")

    def test_states_are_unique(self):
        """Verify all states have unique values."""
        from wayfinder.state import AppState

        states = list(AppState)
        values = [s.value for s in states]

        assert len(values) == len(set(values)), "State values must be unique"


class TestStateLabels:
    """Test state label functionality."""

    def test_all_states_have_labels(self):
        """Verify all states have display labels."""
        from wayfinder.state import AppState, STATE_LABELS

        for state in AppState:
            assert state in STATE_LABELS, f"Missing label for {state}"

    def test_get_state_label(self):
        """Test get_state_label function."""
        from wayfinder.state import AppState, get_state_label

        assert get_state_label(AppState.IDLE) == "Ready to Record"
        assert get_state_label(AppState.RECORDING) == "Listening..."
        assert get_state_label(AppState.PROCESSING) == "Processing..."
        assert get_state_label(AppState.PASTING) == "Typing..."


class TestStateTransitions:
    """Test state transition logic."""

    def test_idle_to_recording_on_hotkey(self):
        """Test transition from IDLE to RECORDING."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.IDLE, "hotkey")
        assert next_state == AppState.RECORDING

    def test_recording_to_processing_on_hotkey(self):
        """Test transition from RECORDING to PROCESSING."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.RECORDING, "hotkey")
        assert next_state == AppState.PROCESSING

    def test_recording_to_processing_on_stop(self):
        """Test transition from RECORDING to PROCESSING on stop."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.RECORDING, "stop")
        assert next_state == AppState.PROCESSING

    def test_processing_to_pasting_on_transcribed(self):
        """Test transition from PROCESSING to PASTING."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.PROCESSING, "transcribed")
        assert next_state == AppState.PASTING

    def test_processing_to_idle_on_error(self):
        """Test transition from PROCESSING to IDLE on error."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.PROCESSING, "error")
        assert next_state == AppState.IDLE

    def test_pasting_to_idle_on_pasted(self):
        """Test transition from PASTING to IDLE."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.PASTING, "pasted")
        assert next_state == AppState.IDLE

    def test_unknown_transition_stays_in_state(self):
        """Test that unknown events keep the current state."""
        from wayfinder.state import AppState, get_next_state

        next_state = get_next_state(AppState.IDLE, "unknown_event")
        assert next_state == AppState.IDLE

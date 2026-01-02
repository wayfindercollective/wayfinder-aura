"""
State machine for Wayfinder Aura.

Defines application states and transitions for the recording workflow.
"""

from enum import Enum, auto


class AppState(Enum):
    """
    Application states for the voice dictation workflow.
    
    State flow:
        IDLE -> RECORDING -> PROCESSING -> PASTING -> IDLE
        
    IDLE: Ready to record, waiting for hotkey
    RECORDING: Actively capturing audio
    PROCESSING: Transcribing audio with whisper
    PASTING: Injecting text at cursor position
    """
    IDLE = auto()
    RECORDING = auto()
    PROCESSING = auto()
    PASTING = auto()


# State display labels
STATE_LABELS: dict[AppState, str] = {
    AppState.IDLE: "Ready to Record",
    AppState.RECORDING: "Listening...",
    AppState.PROCESSING: "Processing...",
    AppState.PASTING: "Typing...",
}


def get_state_label(state: AppState) -> str:
    """Get the display label for a state."""
    return STATE_LABELS.get(state, "Unknown")


def get_next_state(current: AppState, event: str) -> AppState:
    """
    Get the next state based on current state and event.
    
    Args:
        current: Current application state
        event: Event that occurred (e.g., "hotkey", "transcribed", "pasted")
        
    Returns:
        Next state after the transition
    """
    transitions = {
        (AppState.IDLE, "hotkey"): AppState.RECORDING,
        (AppState.RECORDING, "hotkey"): AppState.PROCESSING,
        (AppState.RECORDING, "stop"): AppState.PROCESSING,
        (AppState.PROCESSING, "transcribed"): AppState.PASTING,
        (AppState.PROCESSING, "error"): AppState.IDLE,
        (AppState.PASTING, "pasted"): AppState.IDLE,
        (AppState.PASTING, "error"): AppState.IDLE,
    }
    
    return transitions.get((current, event), current)





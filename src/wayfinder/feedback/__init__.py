"""User-feedback channels for Wayfinder Aura.

Audio cues and optional controller rumble for SteamOS Game Mode, where the
PyQt overlay can't render over a fullscreen game. See ``audio.py`` and
``rumble.py``.
"""

from .audio import play_cue, prewarm
from .rumble import pulse_for_cue


def play_game_mode_cue(name: str) -> None:
    """Fire Game Mode lifecycle feedback: audio chime + optional rumble pulse.

    Both channels are fire-and-forget and silent on failure. Call only when
    Game Mode dictation is active (caller gates on ``_game_mode``).
    """
    play_cue(name)
    pulse_for_cue(name)


__all__ = ["play_cue", "prewarm", "pulse_for_cue", "play_game_mode_cue"]

"""User-feedback channels for Wayfinder Aura.

Currently just audio cues — used in SteamOS Game Mode where the PyQt overlay
can't render over a fullscreen game, so the only feedback the user can perceive
is sound. See ``audio.py``.
"""

from .audio import play_cue, prewarm

__all__ = ["play_cue", "prewarm"]

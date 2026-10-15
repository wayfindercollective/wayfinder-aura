"""Game reports: players tell Wayfinder how Aura worked in a game.

Design: docs/plans/game-reports-design-2026-10-15.md. Nothing in this module
touches licensing, and nothing is sent unless the player presses Send.

The game key names a game in every report and in Wayfinder's server-side
catalog, which ``scripts/export_game_catalog.py`` generates from the Games-tab
lists with this same function. Changing how a key is made orphans every report
already sent for that game.
"""

from __future__ import annotations

import re
import unicodedata

GAME_KEY_MAX = 64
GAME_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_APOSTROPHES = ("'", "’")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def game_key(name: str) -> str:
    """The report key for a Games-tab name: ``Garry's Mod`` -> ``garrys-mod``.

    NFKD, then accents dropped and ASCII kept; lower-case; apostrophes dropped;
    every other run of non-alphanumerics becomes ``-``; trimmed; at most 64
    characters (re-trimmed so a cut never ends on ``-``). A name with no ASCII
    letter or digit gives ``""``, which is not a valid key.
    """
    text = unicodedata.normalize("NFKD", name or "")
    for mark in _APOSTROPHES:
        text = text.replace(mark, "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _NON_ALNUM.sub("-", text).strip("-")
    return text[:GAME_KEY_MAX].rstrip("-")


def is_valid_game_key(key: object) -> bool:
    """A non-empty lower-case slug of at most 64 characters."""
    return (isinstance(key, str) and 0 < len(key) <= GAME_KEY_MAX
            and GAME_KEY_RE.match(key) is not None)

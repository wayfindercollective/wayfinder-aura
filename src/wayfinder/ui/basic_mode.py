"""The simple workspace is a view of the normal settings, not another config.

Keep these projections free of Tk so Free/Ultra, custom advanced preferences,
and the three indicator states can be checked on every platform.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from wayfinder.core.ultra_defaults import RECOMMENDED_SPEECH_MODEL_ID
from wayfinder.license import is_free_transcription_model

LANGUAGE_CHOICES = ("English", "Multilingual")
INDICATOR_CHOICES = ("Always on", "Disappearing", "Off")
BASIC_TABS = ("dictate", "settings")


def basic_mode_enabled(config: Mapping) -> bool:
    """Existing installs get the simple view without changing their settings."""
    return not bool(config.get("advanced_mode", False))


def basic_tabs(config: Mapping, *, style_unlocked: bool = False) -> tuple[str, ...]:
    """Style is an Ultra destination, and Raw deliberately bypasses it."""
    if style_unlocked and not config.get("raw_transcription", False):
        return (*BASIC_TABS, "style")
    return BASIC_TABS


def language_choice(config: Mapping) -> str:
    """An English-only model cannot recognise other languages, even on Auto."""
    path = str(config.get("model_path", "") or "").lower()
    if ".en." in path or config.get("language", "en") == "en":
        return "English"
    return "Multilingual"


def premium_model_selected(config: Mapping) -> bool:
    path = str(config.get("model_path", "") or "")
    return bool(path) and not is_free_transcription_model(path)


@dataclass(frozen=True)
class SpeechChoice:
    model_id: str
    language: str


def speech_choice(language: str, premium: bool) -> SpeechChoice:
    """Plan an explicit choice; merely opening either view never applies it."""
    if language not in LANGUAGE_CHOICES:
        raise ValueError(f"Unknown language choice: {language}")
    return SpeechChoice(
        RECOMMENDED_SPEECH_MODEL_ID if premium else (
            "base.en" if language == "English" else "base"
        ),
        "en" if language == "English" else "auto",
    )


def indicator_choice(config: Mapping) -> str:
    if not config.get("overlay_enabled", True):
        return "Off"
    return "Disappearing" if config.get("overlay_type") == "disappearing" else "Always on"


def indicator_updates(choice: str) -> dict:
    """Off preserves the last style, scale, position, and menu-bar preference."""
    if choice not in INDICATOR_CHOICES:
        raise ValueError(f"Unknown indicator choice: {choice}")
    if choice == "Off":
        return {"overlay_enabled": False}
    return {
        "overlay_enabled": True,
        "overlay_type": "disappearing" if choice == "Disappearing" else "always_on",
    }

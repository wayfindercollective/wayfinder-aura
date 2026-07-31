"""Steam Deck-specific controller guidance and detection guards."""

from wayfinder.ui.steamdeck_help import (
    STEAM_DECK_BUTTON_HELP_BODY,
    STEAM_DECK_GAME_MODE_HELP,
    STEAM_DECK_BUTTON_HELP_TOOLTIP,
    should_show_steam_deck_button_help,
)


def test_help_is_only_shown_on_identified_deck_hardware():
    assert should_show_steam_deck_button_help("deck") is True
    assert should_show_steam_deck_button_help("steamos") is False
    assert should_show_steam_deck_button_help("machine") is False
    assert should_show_steam_deck_button_help(None) is False


def test_help_explains_the_complete_supported_trigger_path():
    required_copy = (
        "L4",
        "L5",
        "R4",
        "R5",
        "Desktop Layout",
        "Back Grips",
        "Right Joystick Click (R3)",
        "Desktop Mode",
    )
    for phrase in required_copy:
        assert phrase in STEAM_DECK_BUTTON_HELP_BODY

    assert "Steam Input owns" in STEAM_DECK_BUTTON_HELP_TOOLTIP
    assert "does not edit" in STEAM_DECK_BUTTON_HELP_TOOLTIP
    assert "Game Mode" not in STEAM_DECK_BUTTON_HELP_BODY
    assert "separate layout for each game" in STEAM_DECK_GAME_MODE_HELP
    assert "normal R3 action" in STEAM_DECK_GAME_MODE_HELP

"""Steam Deck-specific controller setup copy for the Settings screen.

Keep this separate from the Tk widget construction so the instructions and
their hardware gate can be tested without starting a display server.
"""

STEAM_DECK_BUTTON_HELP_TITLE = "USE A BACK BUTTON IN DESKTOP MODE"

STEAM_DECK_BUTTON_HELP_BODY = (
    "Steam owns L4, L5, R4, and R5, so Aura's Detect button cannot see an "
    "unassigned back button. Set it up once in Steam:\n\n"
    "1. Desktop Mode: Steam > Settings > Controller > Desktop Layout > Edit > "
    "Back Grips.\n"
    "2. Assign R4, R5, L4, L5, or another custom button to Right Joystick "
    "Click (R3).\n\n"
    "Aura's host helper watches the virtual R3 button and toggles recording in "
    "Desktop Mode. Your normal Aura keyboard hotkey still works too."
)

STEAM_DECK_GAME_MODE_HELP = (
    "Optional Game Mode setup: Steam uses a separate layout for each game. "
    "Map the same back/custom button to Right Joystick Click (R3) in that "
    "game's Controller Settings, or apply a reusable layout template. The game "
    "may also receive its normal R3 action."
)

STEAM_DECK_BUTTON_HELP_TOOLTIP = (
    "Why this is needed: Steam Input owns the Deck's rear controls. Aura does "
    "not edit private Steam controller files; its host helper listens for the "
    "Right Joystick Click event that Steam emits after you make this assignment."
)


def should_show_steam_deck_button_help(platform: str | None) -> bool:
    """Only show Deck grip instructions on positively identified Deck hardware."""
    return platform == "deck"

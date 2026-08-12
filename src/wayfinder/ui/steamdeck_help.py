"""Steam Deck-specific controller setup copy for the Settings screen.

Keep this separate from the Tk widget construction so the instructions and
their hardware gate can be tested without starting a display server.
"""

STEAM_DECK_BUTTON_HELP_TITLE = "USE A BACK BUTTON IN DESKTOP MODE"

STEAM_DECK_BUTTON_HELP_BODY = (
    "Steam owns L4, L5, R4, and R5, so Aura cannot see an unassigned back "
    "button. Give it a keyboard key in Steam, then bind that key here:\n\n"
    "1. Desktop Mode: Steam > Settings > Controller > Desktop Layout > Edit > "
    "Back Grips.\n"
    "2. Assign R4, R5, L4, L5, or another custom button to a keyboard key. "
    "Insert is a good pick — the Deck's own keyboard has no Insert key, so "
    "nothing else sends it.\n"
    "3. Back here, choose that key in the Hotkey list, or click Detect and "
    "press the back button.\n\n"
    "Pick a keyboard key, not a controller button: Aura cannot see Right "
    "Joystick Click (R3) or any other pad button, because those are not "
    "keyboard events."
)

STEAM_DECK_GAME_MODE_HELP = (
    "Optional Game Mode setup: Steam uses a separate layout for each game, and "
    "in Game Mode Aura cannot listen for keyboard keys at all. Map the same "
    "back/custom button to Right Joystick Click (R3) in that game's Controller "
    "Settings, then install Aura's host helper, which watches the pad "
    "directly. Get it from the Steam Deck setup guide at "
    "https://github.com/wayfindercollective/wayfinder-aura — the helper lives "
    "outside the sandbox, so it cannot be installed from this window. "
    "The game may also receive its normal R3 action."
)

STEAM_DECK_BUTTON_HELP_TOOLTIP = (
    "Why this is needed: Steam Input owns the Deck's rear controls, and Aura "
    "does not edit private Steam controller files. Once Steam turns the button "
    "into a keyboard key, Aura's normal hotkey listener sees it like any key."
)


def should_show_steam_deck_button_help(platform: str | None) -> bool:
    """Only show Deck grip instructions on positively identified Deck hardware."""
    return platform == "deck"

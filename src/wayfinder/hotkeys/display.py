"""What to SHOW as the global hotkey — which is not always what config says.

Under the evdev and pynput backends the app owns the binding, so the local
config IS the hotkey and displaying it is correct.

Under the GlobalShortcuts portal it is not. There ``hotkey_key`` /
``hotkey_modifiers`` are only a *request* (``preferred_trigger``); the
compositor decides, the user can edit the chord in its picker, and a bind can
come back having registered nothing at all. Displaying config regardless is how
a Flatpak install ended up advertising "Ctrl+Alt+Space" on the Deck while the
portal had bound nothing and no key did anything — the report that produced
this module.

Pure and dependency-free so the rule is testable without Tk, D-Bus or a
compositor.
"""

from __future__ import annotations

from typing import Optional

# Shown in place of a chord when the portal owns the binding and has told us
# nothing is bound. It has to name the fix: the user cannot bind it from here.
UNBOUND_LABEL = "not set — System Settings → Shortcuts"

# The same fact for surfaces that render the hotkey as a big unwrapped token
# (the first-run card). The long form does not fit there — on a Steam Deck the
# card is only 383 px wide — so those callers pair this with the explanation in
# body copy instead of cramming it into the token.
UNBOUND_SHORT = "not set"


def hotkey_label(
    config_label: str,
    *,
    portal_active: bool,
    portal_known: bool,
    portal_trigger: Optional[str],
) -> str:
    """The label to display for one global shortcut.

    ``config_label``   what the local config encodes, e.g. "Ctrl+Alt+Space".
    ``portal_active``  the GlobalShortcuts portal owns this binding.
    ``portal_known``   the portal has actually reported on this shortcut yet.
    ``portal_trigger`` its ``trigger_description``: a chord when bound, ``""``
                       when bound without a label, ``None`` when NOT bound.

    Truth table:

    ==============  ============  ==============  ====================
    portal_active   portal_known  portal_trigger  result
    ==============  ============  ==============  ====================
    False           any           any             config_label
    True            False         any             config_label
    True            True          None            UNBOUND_LABEL
    True            True          ""              config_label
    True            True          "Meta+I"        "Meta+I"
    ==============  ============  ==============  ====================

    The unknown case deliberately falls back to config rather than to
    "not set": at cold start the portal has not answered yet, and flashing
    "not set" for a second on every launch would be its own lie.

    The empty-string case means bound-but-unlabelled. Something IS bound, and
    the chord we asked for is the likeliest thing it is, so config is the best
    available label — the one case where the old behaviour was right.
    """
    if not portal_active or not portal_known:
        return config_label
    if portal_trigger is None:
        return UNBOUND_LABEL
    return portal_trigger or config_label


def is_unbound(label: str) -> bool:
    """Whether a label came from ``UNBOUND_LABEL`` (no chord to press)."""
    return label == UNBOUND_LABEL

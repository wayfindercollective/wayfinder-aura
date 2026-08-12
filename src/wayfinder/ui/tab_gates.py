"""Licence gating for main-window tabs.

Single source of truth for which tab needs which premium feature, so the tab
switcher and the status breadcrumb cannot drift apart. Publishing the locked
set lets an external harness tell "the gate refused" from "tab switching is
broken" — previously indistinguishable, which made the live smoke test fail on
every unlicensed build.
"""

from typing import Callable, Optional

# tab id -> premium feature required to open it.
TAB_FEATURES = {
    "style": "tone_system",
}


def feature_for_tab(tab_id: str) -> Optional[str]:
    """Premium feature ``tab_id`` requires, or None when it is free."""
    return TAB_FEATURES.get(tab_id)


def locked_tabs(has_feature: Callable[[str], bool]) -> list[str]:
    """Sorted tab ids the current licence denies.

    A ``has_feature`` that raises counts as locked: a broken licence backend
    must never hand out premium tabs.
    """
    locked = []
    for tab_id, feature in TAB_FEATURES.items():
        try:
            unlocked = bool(has_feature(feature))
        except Exception:
            unlocked = False
        if not unlocked:
            locked.append(tab_id)
    return sorted(locked)

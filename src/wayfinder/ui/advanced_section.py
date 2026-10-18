"""Layout rules for the collapsed "Advanced" section of the Settings tab.

Settings shows only what most people need (microphone, speech model, GPU,
cleanup model, hotkey). The rest still exists, but lives under one collapsed
"Advanced" tile so a casual user is never asked to choose a backend or tune the
status overlay. The rows are built into per-group frames of that tile; this
module holds the two decisions that must not depend on Tk so they stay
headlessly testable: which frame a row is built into, and which groups show.
"""

from __future__ import annotations

from typing import Any, Mapping

# (group key, heading). Each group is its own collapsible section inside the
# Advanced tile, closed until the user opens it. Order is the order shown.
ADVANCED_GROUPS: tuple[tuple[str, str], ...] = (
    ("audio", "Audio"),
    ("processing", "Processing"),
    ("system", "System"),
    ("overlay", "Status overlay"),
    ("benchmark", "Benchmark"),
)


def advanced_slot(app: Any, key: str, fallback: Any) -> Any:
    """The frame rows of group ``key`` are built into, or ``fallback``.

    ``app._advanced_groups`` maps key -> (group frame, content frame) once the
    Advanced tile exists. Before that (an early build, or a test stub that never
    built one) the row is built where it used to live, so nothing is lost.
    """
    try:
        groups = getattr(app, "_advanced_groups", None) or {}
        pair = groups.get(key)
        frame = pair[1] if pair else None
        if frame is not None and frame.winfo_exists():
            return frame
    except Exception:
        pass
    return fallback


def groups_to_show(has_packed_rows: Mapping[str, bool]) -> list[str]:
    """Group keys to pack, in display order.

    A group with nothing packed in it stays hidden, so Remote mode (which has no
    backend, chunking or benchmark rows) does not leave empty headings behind.
    """
    return [key for key, _ in ADVANCED_GROUPS if has_packed_rows.get(key, False)]

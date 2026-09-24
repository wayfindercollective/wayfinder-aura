"""macOS: keep stacked text labels inside the width they actually get.

Many labels carry a fixed ``wraplength`` sized for the Linux 800 px window
(430-560 px). The Mac window's cards are narrower, so the label asks for more
width than its card has; Tk centres the overflow and the text is clipped on
both edges. Long single-line labels (status lines) overflow the same way.

One class-level ``<Map>`` hook covers every label, including ones created
later (benchmark results, panels): a label stacked in its container (pack
side top/bottom) wraps at ``min(its designed wraplength, the container's
width minus its padding)`` and follows container resizes. Labels in rows
(side left/right), grid/place layouts, and anything inside a container that
manages its own wrapping (``_wf_manages_wrap``) are left alone.
"""

from __future__ import annotations

MIN_WRAP = 120          # never squeeze text narrower than this (px)
SHORT_TEXT = 40         # unwrapped labels shorter than this can't overflow a card
EDGE_SLACK = 4          # keep glyph overhang off the container edge


def fitted_wraplength(text, designed: int, container_width: int, horizontal_pad: int) -> int | None:
    """The wraplength a stacked label should use, or None to leave it alone."""
    if not isinstance(text, str) or " " not in text:
        return None
    if container_width < 60:
        return None  # not laid out yet
    if designed <= 0 and len(text) < SHORT_TEXT:
        return None
    available = max(MIN_WRAP, container_width - horizontal_pad - EDGE_SLACK)
    return int(available if designed <= 0 else min(designed, available))


def _pad_total(value) -> int:
    if isinstance(value, (tuple, list)):
        return int(sum(int(v) for v in value))
    try:
        return 2 * int(value)
    except (TypeError, ValueError):
        return 0


def _manages_own_wrap(widget) -> bool:
    while widget is not None:
        if getattr(widget, "_wf_manages_wrap", False):
            return True
        widget = getattr(widget, "master", None)
    return False


def fit_label(label) -> None:
    """Apply the stacked-label wrap rule to one CTkLabel (safe to repeat)."""
    try:
        info = label.pack_info()
    except Exception:
        return  # grid/place/unmapped: layout is explicit, leave it
    if info.get("side", "top") not in ("top", "bottom"):
        return
    container = label.master
    designed = getattr(label, "_wf_designed_wrap", None)
    if designed is None:
        try:
            designed = int(float(label.cget("wraplength") or 0))
        except Exception:
            designed = 0
        label._wf_designed_wrap = designed
    target = fitted_wraplength(
        label.cget("text"), designed, container.winfo_width(),
        _pad_total(info.get("padx", 0)) + _pad_total(info.get("ipadx", 0)),
    )
    if target is None:
        return
    try:
        if int(float(label.cget("wraplength") or 0)) != target:
            label.configure(wraplength=target)
        # A left-anchored line that now wraps should read left-aligned.
        if designed <= 0 and str(info.get("anchor", "")) in ("w", "nw", "sw") \
                and str(label.cget("justify")) == "center":
            label.configure(justify="left")
    except Exception:
        pass


def install(root, label_class) -> None:
    """Hook every tk Label's <Map> once for the whole app."""

    def refit_children(container) -> None:
        try:
            children = container.winfo_children()
        except Exception:
            return
        for child in children:
            if isinstance(child, label_class) and not _manages_own_wrap(child):
                fit_label(child)

    def on_map(event) -> None:
        label = getattr(event.widget, "master", None)  # the CTkLabel around tk's Label
        if not isinstance(label, label_class) or _manages_own_wrap(label):
            return
        fit_label(label)
        container = label.master
        if container is not None and not getattr(container, "_wf_fit_bound", False):
            container._wf_fit_bound = True
            try:
                container.bind("<Configure>", lambda _e, c=container: refit_children(c), add="+")
            except Exception:
                pass

    root.bind_class("Label", "<Map>", on_map, add="+")

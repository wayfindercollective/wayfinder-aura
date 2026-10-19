"""Let nested text editors hand unusable wheel gestures to their parent page."""

from __future__ import annotations


def text_wheel_at_edge(view, *, delta=0, num=None, state=0) -> bool:
    """Whether vertical input cannot move this text viewport any further."""
    if state & 1:  # Shift-wheel belongs to the editor's horizontal viewport.
        return False
    if num == 4:
        direction = -1
    elif num == 5:
        direction = 1
    elif delta:
        direction = -1 if delta > 0 else 1
    else:
        return False
    start, end = view
    return start <= 1e-9 if direction < 0 else end >= 1 - 1e-9


def install_text_scroll_chaining(root, scroll_parent) -> None:
    """Route at edges BEFORE Tk's Text class can consume the event.

    Standard bindtags keep editing, selection, keyboard navigation, and normal
    text scrolling intact. The shared page-wheel pipeline still owns scrolling
    distance and trackpad normalization. No timers or widget subclasses.
    """
    tag = "WayfinderNestedTextWheel"

    def forward(event):
        try:
            at_edge = text_wheel_at_edge(
                event.widget.yview(), delta=getattr(event, "delta", 0),
                num=getattr(event, "num", None), state=getattr(event, "state", 0),
            )
        except Exception:
            return None
        if not at_edge:
            return None  # Leave the native Text class binding in charge.
        scroll_parent(event)
        return "break"  # Do not dispatch the same input to bind_all again.

    def attach(widget):
        if widget.winfo_class() != "Text":
            return
        tags = list(widget.bindtags())
        if tag not in tags and "Text" in tags:
            tags.insert(tags.index("Text"), tag)
            widget.bindtags(tuple(tags))

    def walk(widget):
        attach(widget)
        for child in widget.winfo_children():
            walk(child)

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_class(tag, sequence, forward)
    root.bind_class("Text", "<Map>", lambda event: attach(event.widget), add="+")
    walk(root)  # Existing editors and lazily constructed Settings both work.

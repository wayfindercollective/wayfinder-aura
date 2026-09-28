"""Headless UI inspection: dump a tab's live widget tree as JSON.

Screenshots need the display on (macOS stops compositing while it sleeps);
Tk's layout does not. This walks a widget tree and records what a reviewer
checks by eye - the text, whether a control is enabled, its size, and any
text asking for more width than its container has (clipped). Triggered by
the local control socket (``inspect:<tab>``); writes a private file in the
runtime directory. Entry/textbox *contents* are never recorded, only their
length, so keys and dictations don't end up in the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_TEXT_CLASSES = ("CTkLabel", "CTkButton", "CTkSwitch", "CTkCheckBox", "CTkRadioButton",
                 "CTkSegmentedButton", "CTkOptionMenu")
_CONTENT_CLASSES = ("CTkTextbox", "CTkEntry")


def _cget(widget, key):
    try:
        return widget.cget(key)
    except Exception:
        return None


def _inner_label(widget):
    """CTk widgets draw their text in an inner tk Label/Button."""
    for name in ("_label", "_text_label", "_text"):
        inner = getattr(widget, name, None)
        if inner is not None and hasattr(inner, "winfo_reqwidth"):
            return inner
    return None


def describe(widget, root) -> dict:
    cls = type(widget).__name__
    node = {"class": cls}
    try:
        node.update({
            "x": widget.winfo_rootx() - root.winfo_rootx(),
            "y": widget.winfo_rooty() - root.winfo_rooty(),
            "w": widget.winfo_width(), "h": widget.winfo_height(),
            "mapped": bool(widget.winfo_ismapped()),
        })
    except Exception:
        pass
    if cls in _TEXT_CLASSES:
        text = _cget(widget, "text")
        if isinstance(text, str) and text:
            node["text"] = text
        state = _cget(widget, "state")
        if state is not None:
            node["state"] = str(state)
        color = _cget(widget, "text_color")
        if color is not None:
            node["text_color"] = str(color)
        inner = _inner_label(widget)
        if inner is not None:
            try:
                req = inner.winfo_reqwidth()
                node["req_w"] = req
                parent = widget.master
                avail = parent.winfo_width() if parent is not None else None
                if avail and avail > 1 and req > avail + 1:
                    node["clipped"] = {"needs": req, "has": avail}
            except Exception:
                pass
    elif cls in _CONTENT_CLASSES:
        try:
            content = widget.get("1.0", "end") if cls == "CTkTextbox" else widget.get()
            node["content_chars"] = len(str(content).strip())
        except Exception:
            pass
        state = _cget(widget, "state")
        if state is not None:
            node["state"] = str(state)
    children = []
    try:
        kids = widget.winfo_children()
    except Exception:
        kids = []
    for child in kids:
        # CTk internals (canvas, inner tk widgets) add noise; keep CTk-level widgets
        # and plain frames that contain them.
        if type(child).__name__.startswith(("CTk",)) or child.winfo_children():
            children.append(describe(child, root))
    if children:
        node["children"] = children
    return node


def flatten(node, out=None) -> list:
    out = [] if out is None else out
    out.append(node)
    for child in node.get("children", []):
        flatten(child, out)
    return out


def write_inspection(root_widget, top_widget, path: Path) -> dict:
    tree = describe(root_widget, top_widget)
    nodes = flatten(tree)
    summary = {
        "texts": [n["text"] for n in nodes if n.get("text") and n.get("mapped", True)],
        "disabled": [n.get("text", n["class"]) for n in nodes if n.get("state") == "disabled"],
        "clipped": [{"text": n.get("text", "")[:80], **n["clipped"]} for n in nodes if n.get("clipped")],
    }
    doc = {"summary": summary, "tree": tree}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(doc, fh, indent=1)
    return summary

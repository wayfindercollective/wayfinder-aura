"""Clipboard support for credential entry fields.

CustomTkinter entries ship no context menu, so the licence and API-key fields
had no way to paste at all — a key copied from an email could not be entered
into the one field that needs it.

The paste maths lives here as pure functions so it can be tested without a
display; ``attach_secret_paste`` is a thin Tk binding over them.
"""

from typing import Optional, Tuple


def sanitize_pasted_secret(text: str) -> str:
    """Drop every whitespace character from a copied secret.

    Licence keys and API keys never contain whitespace, but copying one out of
    an email, a chat window, or a soft-wrapped page routinely carries a
    trailing newline or a line break mid-key. Pasting that verbatim fails
    activation with no clue why, so strip it.
    """
    if not text:
        return ""
    return "".join(text.split())


def text_after_paste(
    current: str,
    pasted: str,
    sel_start: Optional[int],
    sel_end: Optional[int],
    cursor: int,
) -> Tuple[str, int]:
    """Return ``(new_text, new_cursor)`` for pasting into an entry.

    Replaces the selection when there is one (select-all then paste is how a
    wrong key gets corrected), otherwise inserts at the cursor. The cursor is
    clamped, since Tk can report an index past the end.
    """
    if not pasted:
        return current, cursor

    if sel_start is not None and sel_end is not None and sel_end > sel_start:
        start, end = max(0, sel_start), min(len(current), sel_end)
        return current[:start] + pasted + current[end:], start + len(pasted)

    at = max(0, min(cursor, len(current)))
    return current[:at] + pasted + current[at:], at + len(pasted)


def attach_secret_paste(entry, tk_module, *, log=None) -> bool:
    """Give ``entry`` a right-click Paste menu. Returns True when attached.

    Best-effort by design: a missing context menu is an annoyance, but an
    exception here would break the settings screen it is attached to.
    """
    try:
        # CTkEntry wraps a real tk.Entry; bindings must land on the inner one
        # or the right-click never reaches us.
        target = getattr(entry, "_entry", entry)

        # Untie this field's selection from the X11 PRIMARY selection.
        #
        # Tk entries default to exportselection=1, which publishes the widget's
        # selection as PRIMARY -- and Tk *silently clears the widget's own
        # selection* the moment any other client claims PRIMARY. Selecting the
        # key in an email or password manager to copy it is exactly such a
        # claim, so by the time the user pastes over the highlighted old key
        # the entry no longer has a selection: `sel.first` raises, the paste
        # falls into the insert-at-cursor branch, and the new key lands
        # *beside* the old one ("WV-OLD...WV-NEW..."), which then fails
        # activation with a key the user can see is right.
        #
        # Not exporting also keeps a credential out of PRIMARY, where any
        # client can read it and a stray middle-click can splatter it into
        # another window.
        try:
            target.configure(exportselection=False)
        except Exception:
            pass  # ancient/odd Tk build -- paste still works without a selection

        def _do_paste(_event=None):
            try:
                pasted = sanitize_pasted_secret(target.clipboard_get())
            except Exception:
                return "break"  # empty or non-text clipboard
            if not pasted:
                return "break"
            try:
                sel = (target.index("sel.first"), target.index("sel.last"))
            except Exception:
                sel = (None, None)
            new_text, new_cursor = text_after_paste(
                target.get(), pasted, sel[0], sel[1], target.index("insert")
            )
            target.delete(0, "end")
            target.insert(0, new_text)
            target.icursor(new_cursor)
            return "break"

        def _do_select_all(_event=None):
            target.select_range(0, "end")
            target.icursor("end")
            return "break"

        menu = tk_module.Menu(target, tearoff=0)
        menu.add_command(label="Paste", command=_do_paste)
        menu.add_command(label="Select All", command=_do_select_all)

        def _popup(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        target.bind("<Button-3>", _popup)
        # Ctrl+V and Ctrl+A explicitly: Tk's defaults vary by build, and this
        # field is the one place a user cannot retype what they lost.
        target.bind("<Control-v>", _do_paste)
        target.bind("<Control-V>", _do_paste)
        target.bind("<Control-a>", _do_select_all)
        target.bind("<Control-A>", _do_select_all)
        return True
    except Exception as exc:  # pragma: no cover - depends on the Tk build
        if log:
            log(f"⚠️ Could not attach paste menu: {exc}")
        return False

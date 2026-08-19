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
        # CTkEntry wraps a real tk.Entry. Bindings, selection indices and the
        # cursor belong to the inner widget or the right-click never reaches
        # us; text I/O goes through the outer wrapper (see _read/_write).
        target = getattr(entry, "_entry", entry)
        outer = entry

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

        def _read() -> str:
            """Current text, as the *app* sees it.

            Read through the CTk wrapper: while the placeholder is showing, the
            inner tk.Entry literally contains "WV-XXXX-XXXX-XXXX-XXXX" but
            CTkEntry.get() correctly reports "". Reading the inner widget here
            would splice the pasted key onto the end of the hint text.
            """
            try:
                return outer.get()
            except Exception:
                return target.get()

        def _write(new_text: str, new_cursor: int) -> None:
            """Replace the field's contents, keeping CTk's placeholder honest.

            CTkEntry.insert() deactivates the placeholder; writing straight to
            the inner widget leaves _placeholder_text_active set, so get()
            keeps returning "" and Activate reports "Please enter a license
            key" while the key is plainly visible on screen.
            """
            # For a bare tk.Entry `outer` *is* `target`, so this is the same
            # call; no fallback, because writing the inner widget behind CTk's
            # back is the very defect this indirection exists to prevent.
            outer.delete(0, "end")
            outer.insert(0, new_text)
            try:
                target.icursor(new_cursor)
            except Exception:
                pass  # cursor placement is cosmetic

        def _closest_gap(x):
            """Tk's EntryClosestGap: the gap nearest the click, not the glyph's
            left edge. A bare "@x" index always names the character under the
            pointer, so clicking the right half of a glyph would paste one
            position early."""
            pos = target.index(f"@{x}")
            try:
                bbox = target.bbox(pos)
                char_x, char_w = bbox[0], bbox[2]
            except Exception:
                return pos
            return pos if (x - char_x) < (char_w / 2) else pos + 1

        def _paste_from(get_source, click_x=None):
            try:
                pasted = sanitize_pasted_secret(get_source())
            except Exception:
                return "break"  # empty, non-text, or no such selection
            if not pasted:
                return "break"

            current = _read()
            try:
                sel = (target.index("sel.first"), target.index("sel.last"))
            except Exception:
                sel = (None, None)
            try:
                cursor = target.index("insert")
            except Exception:
                cursor = len(current)
            # Placeholder showing: the field is empty as far as the app is
            # concerned, so any selection/cursor index points into hint text
            # that is about to be discarded.
            if not current:
                sel, cursor = (None, None), 0
            # Middle-click with nothing selected drops the text where the user
            # clicked, as tk::EntryPaste does via EntryClosestGap.
            elif click_x is not None and sel == (None, None):
                try:
                    cursor = _closest_gap(click_x)
                except Exception:
                    pass

            new_text, new_cursor = text_after_paste(
                current, pasted, sel[0], sel[1], cursor
            )
            _write(new_text, new_cursor)
            return "break"

        def _do_paste(_event=None):
            return _paste_from(target.clipboard_get)

        def _do_paste_primary(event=None):
            """Middle-click paste, routed through the same replace logic.

            Tk's stock binding is tk::EntryPaste, which does
            `icursor` to the click gap, a bare `insert` (never deleting the
            selection, so middle-clicking onto a highlighted key appends
            instead of replacing -- the same defect by a different route),
            then focuses the entry. We break that binding, so the two
            behaviours worth keeping -- drop-at-click and focus -- are
            reproduced here.
            """
            # Tk suppresses this paste when the middle button was dragged --
            # a middle-drag scrolls a long value horizontally (EntryScanDrag),
            # and ending that scroll must not dump PRIMARY into the field.
            # Our binding runs ahead of the class binding that holds the
            # guard, so we have to apply it ourselves.
            try:
                would_paste = bool(int(target.tk.eval(
                    "expr {$tk_strictMotif"
                    " || ![info exists tk::Priv(mouseMoved)]"
                    " || !$tk::Priv(mouseMoved)}"
                )))
            except Exception:
                would_paste = True
            if not would_paste:
                return "break"

            result = _paste_from(
                lambda: target.selection_get(selection="PRIMARY"),
                click_x=getattr(event, "x", None),
            )
            try:
                if str(target.cget("state")) != "disabled":
                    target.focus_set()
            except Exception:
                pass
            return result

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
        # Middle-click paste on X11; ours runs first and breaks the class
        # binding so tk::EntryPaste's append-at-click never fires.
        target.bind("<<PasteSelection>>", _do_paste_primary)
        return True
    except Exception as exc:  # pragma: no cover - depends on the Tk build
        if log:
            log(f"⚠️ Could not attach paste menu: {exc}")
        return False

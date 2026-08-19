"""Paste behaviour for credential fields.

The licence and API-key entries had no clipboard affordance at all: no
right-click menu anywhere in the app, so a key copied from an email could not
be pasted into the field that needs it. These cover the pure logic; the Tk
binding is a thin wrapper over it.
"""

import pytest

from wayfinder.ui.clipboard import (
    attach_secret_paste,
    sanitize_pasted_secret,
    text_after_paste,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("WV-AAAA-BBBB-CCCC-DDDD", "WV-AAAA-BBBB-CCCC-DDDD"),
        ("  WV-AAAA-BBBB-CCCC-DDDD  ", "WV-AAAA-BBBB-CCCC-DDDD"),
        ("WV-AAAA-BBBB-CCCC-DDDD\n", "WV-AAAA-BBBB-CCCC-DDDD"),
        ("WV-AAAA-BBBB\r\n-CCCC-DDDD", "WV-AAAA-BBBB-CCCC-DDDD"),
        ("WV-AAAA BBBB\tCCCC", "WV-AAAABBBBCCCC"),
    ],
)
def test_pasted_secrets_lose_stray_whitespace(raw, expected):
    """Copying a key out of an email or chat routinely brings a trailing
    newline or a soft-wrap break; a secret never contains whitespace, so
    dropping it turns a silent activation failure into a working paste."""
    assert sanitize_pasted_secret(raw) == expected


def test_sanitize_handles_empty_and_whitespace_only():
    assert sanitize_pasted_secret("") == ""
    assert sanitize_pasted_secret("   \n\t ") == ""


def test_paste_into_empty_field():
    assert text_after_paste("", "KEY", None, None, 0) == ("KEY", 3)


def test_paste_at_cursor_without_a_selection():
    assert text_after_paste("ABEF", "CD", None, None, 2) == ("ABCDEF", 4)


def test_paste_replaces_the_selection():
    """Select-all then paste is how anyone replaces a wrong key."""
    assert text_after_paste("OLDKEY", "NEW", 0, 6, 0) == ("NEW", 3)


def test_paste_replaces_a_partial_selection():
    assert text_after_paste("ABCDEF", "xy", 2, 4, 2) == ("ABxyEF", 4)


def test_cursor_past_the_end_is_clamped():
    assert text_after_paste("AB", "C", None, None, 99) == ("ABC", 3)


def test_pasting_nothing_leaves_the_field_alone():
    assert text_after_paste("ABC", "", None, None, 1) == ("ABC", 1)


# ---------------------------------------------------------------------------
# The Tk binding layer
#
# These fakes model the two real behaviours that produced the bug: Tk drops an
# entry's selection when another X11 client claims PRIMARY, and CTkEntry
# reports "" while its placeholder is showing even though the inner tk.Entry
# literally contains the hint text.
# ---------------------------------------------------------------------------


class _TclError(Exception):
    """Stands in for tkinter.TclError, which the handler catches broadly."""


class _FakeInterp:
    """Just enough Tcl to answer the scan-drag guard expression."""

    def __init__(self, entry):
        self._entry = entry

    def eval(self, script):
        assert "mouseMoved" in script
        return "0" if self._entry.mouse_moved else "1"


class _FakeInnerEntry:
    """The inner ``tk.Entry``: text, cursor, selection, and the PRIMARY rule."""

    def __init__(self):
        self.text = ""
        self.cursor = 0
        self.selection = None
        self.options = {"exportselection": 1}
        self.bindings = {}
        self.clipboard = ""
        self.primary = ""
        self.state = "normal"
        self.focused = False
        self.mouse_moved = False
        self.tk = _FakeInterp(self)

    CHAR_W = 10  # pixels per glyph, so "@x" and bbox() can disagree

    def configure(self, **kw):
        self.options.update(kw)

    def bind(self, sequence, func):
        self.bindings[sequence] = func

    def clipboard_get(self):
        if not self.clipboard:
            raise _TclError("CLIPBOARD selection doesn't exist")
        return self.clipboard

    def selection_get(self, selection="PRIMARY"):
        if not self.primary:
            raise _TclError(f"{selection} selection doesn't exist")
        return self.primary

    def index(self, what):
        if what == "insert":
            return self.cursor
        if what == "end":
            return len(self.text)
        if isinstance(what, str) and what.startswith("@"):
            # Tk's "@<pixel>" names the character *under* the pointer -- not
            # the nearest gap. Keeping that distinction is what makes the
            # closest-gap test meaningful.
            return max(0, min(int(what[1:]) // self.CHAR_W, len(self.text)))
        if what in ("sel.first", "sel.last"):
            if self.selection is None:
                raise _TclError("selection isn't in widget")
            return self.selection[0 if what == "sel.first" else 1]
        raise _TclError(what)

    def get(self):
        return self.text

    def delete(self, first, last=None):
        end = len(self.text) if last in ("end", None) else last
        self.text = self.text[:first] + self.text[end:]
        self.selection = None
        self.cursor = min(self.cursor, len(self.text))

    def insert(self, index, string):
        at = len(self.text) if index == "end" else index
        self.text = self.text[:at] + string + self.text[at:]
        self.cursor = at + len(string)

    def icursor(self, index):
        self.cursor = len(self.text) if index == "end" else index

    def select_range(self, start, end):
        self.selection = (start, len(self.text) if end == "end" else end)

    def cget(self, option):
        return {"state": self.state}.get(option, "")

    def bbox(self, index):
        return (index * self.CHAR_W, 0, self.CHAR_W, 16)

    def focus_set(self):
        self.focused = True

    def foreign_primary_claim(self, text):
        """Another client takes PRIMARY -- e.g. selecting the key in an email.

        Tk drops this widget's own selection when it loses ownership, unless
        the widget opted out of exporting it. That drop is the whole bug.
        """
        self.primary = text
        if self.options.get("exportselection"):
            self.selection = None


class _FakeCTkEntry:
    """CTkEntry's placeholder contract over an inner entry."""

    def __init__(self, placeholder="WV-XXXX-XXXX-XXXX-XXXX"):
        self._entry = _FakeInnerEntry()
        self._entry.text = placeholder
        self._placeholder_active = True

    def get(self):
        return "" if self._placeholder_active else self._entry.get()

    def insert(self, index, string):
        if self._placeholder_active:
            self._placeholder_active = False
            self._entry.text = ""
        self._entry.insert(index, string)

    def delete(self, first, last=None):
        if self._placeholder_active:
            return
        self._entry.delete(first, last)


class _FakeMenu:
    def __init__(self, *_a, **_kw):
        self.commands = {}

    def add_command(self, label=None, command=None):
        self.commands[label] = command


class _FakeTk:
    Menu = _FakeMenu


class _Event:
    def __init__(self, x=0):
        self.x = x


def _fire(widget, sequence, event=None):
    """Invoke the handler bound to ``sequence``, as Tk would."""
    inner = getattr(widget, "_entry", widget)
    handler = inner.bindings[sequence]
    return handler() if event is None else handler(event)


def _dispatch(widget, sequence, event=None):
    """Run the widget binding, then Tk's class binding unless it broke.

    Tk walks bind tags widget-first and stops only on "break". Modelling that
    is what makes the middle-click test meaningful: without the "break", the
    stock tk::EntryPaste would still run and append a second copy.
    """
    inner = getattr(widget, "_entry", widget)
    result = _fire(widget, sequence, event)
    if result == "break":
        return result
    if sequence == "<<PasteSelection>>":
        if inner.mouse_moved:
            return result  # Tk skips the paste after a middle-drag scroll
        # tk::EntryPaste: cursor to the click gap, insert, never delete the
        # selection, then focus.
        inner.icursor(inner.index(f"@{getattr(event, 'x', 0)}"))
        inner.insert(inner.cursor, inner.primary)
        inner.focused = True
    return result


def test_secret_field_stops_exporting_its_selection():
    entry = _FakeInnerEntry()
    assert attach_secret_paste(entry, _FakeTk()) is True
    assert entry.options["exportselection"] is False


def test_paste_replaces_the_key_after_another_app_claims_primary():
    """The reported bug, end to end through the real handler.

    Selecting the key in another window to copy it takes PRIMARY away, which
    used to wipe this entry's selection: the paste then appended, leaving
    "WV-OLD...WV-NEW..." in the field and failing activation.
    """
    entry = _FakeInnerEntry()
    entry.text = "WV-2BQG-YYB2-U4QD-WSB5"
    attach_secret_paste(entry, _FakeTk())

    entry.select_range(0, "end")
    entry.clipboard = "WV-T9XR-HWS4-VQ95-DD8X"
    entry.foreign_primary_claim("WV-T9XR-HWS4-VQ95-DD8X")

    _fire(entry, "<Control-v>")
    assert entry.text == "WV-T9XR-HWS4-VQ95-DD8X"


def test_middle_click_paste_also_replaces_the_selection():
    """Tk's own tk::EntryPaste inserts at the click without deleting the
    selection, so middle-click appended by a different route."""
    entry = _FakeInnerEntry()
    entry.text = "WV-2BQG-YYB2-U4QD-WSB5"
    attach_secret_paste(entry, _FakeTk())

    entry.select_range(0, "end")
    entry.foreign_primary_claim("WV-T9XR-HWS4-VQ95-DD8X")

    # Dispatched through the bind-tag chain: if our handler stopped returning
    # "break", tk::EntryPaste would run too and append a second copy.
    assert _dispatch(entry, "<<PasteSelection>>", _Event(x=0)) == "break"
    assert entry.text == "WV-T9XR-HWS4-VQ95-DD8X"


def test_middle_click_without_a_selection_drops_text_at_the_click():
    """tk::EntryPaste pastes where you clicked; breaking it must not lose that."""
    entry = _FakeInnerEntry()
    entry.text = "ABCDEF"
    attach_secret_paste(entry, _FakeTk())
    entry.primary = "xy"
    entry.cursor = 0

    # x=30 is the left edge of "D" (10px glyphs) -> gap before it.
    _dispatch(entry, "<<PasteSelection>>", _Event(x=30))
    assert entry.text == "ABCxyDEF"


def test_middle_click_past_a_glyph_midpoint_pastes_after_it():
    """tk::EntryClosestGap rounds to the nearer gap; a bare "@x" index does
    not, and would drop the text one position early."""
    entry = _FakeInnerEntry()
    entry.text = "ABCDEF"
    attach_secret_paste(entry, _FakeTk())
    entry.primary = "xy"
    entry.cursor = 0

    # x=35 is the right half of "D" -> the gap *after* it.
    _dispatch(entry, "<<PasteSelection>>", _Event(x=35))
    assert entry.text == "ABCDxyEF"


def test_middle_drag_scroll_does_not_paste():
    """Middle-drag scrolls a long key horizontally (EntryScanDrag). Tk declines
    to paste on that release, and so must we -- otherwise ending a scroll
    dumps PRIMARY into the credential field."""
    entry = _FakeInnerEntry()
    entry.text = "WV-2BQG-YYB2-U4QD-WSB5"
    attach_secret_paste(entry, _FakeTk())
    entry.primary = "junk-from-somewhere-else"
    entry.mouse_moved = True

    _dispatch(entry, "<<PasteSelection>>", _Event(x=0))
    assert entry.text == "WV-2BQG-YYB2-U4QD-WSB5"


def test_middle_click_focuses_the_field_like_tk_does():
    entry = _FakeInnerEntry()
    entry.text = "ABC"
    attach_secret_paste(entry, _FakeTk())
    entry.primary = "z"

    _dispatch(entry, "<<PasteSelection>>", _Event(x=0))
    assert entry.focused is True


def test_paste_into_a_placeholder_field_is_visible_to_the_app():
    """Right-clicking Paste into the empty field is the documented affordance.

    Writing straight to the inner tk.Entry spliced the key onto the end of the
    "WV-XXXX-..." hint and left CTk's placeholder flag set, so get() still
    returned "" and Activate answered "Please enter a license key" while the
    key sat visible on screen.
    """
    entry = _FakeCTkEntry()
    attach_secret_paste(entry, _FakeTk())
    entry._entry.clipboard = "WV-T9XR-HWS4-VQ95-DD8X"

    _fire(entry, "<Control-v>")

    assert entry.get() == "WV-T9XR-HWS4-VQ95-DD8X"
    assert entry._entry.text == "WV-T9XR-HWS4-VQ95-DD8X"
    assert entry._placeholder_active is False


def test_right_click_menu_paste_uses_the_same_path():
    entry = _FakeCTkEntry()
    tk_module = _FakeTk()
    menus = []
    tk_module.Menu = lambda *a, **k: menus.append(_FakeMenu()) or menus[-1]
    attach_secret_paste(entry, tk_module)
    entry._entry.clipboard = "WV-T9XR-HWS4-VQ95-DD8X"

    menus[0].commands["Paste"]()

    assert entry.get() == "WV-T9XR-HWS4-VQ95-DD8X"


def test_paste_menu_still_attaches_when_exportselection_is_rejected():
    """Best-effort: an odd Tk build must not cost the field its paste menu."""

    class _Stubborn(_FakeInnerEntry):
        def configure(self, **kw):
            raise RuntimeError("this Tk build has no such option")

    entry = _Stubborn()
    assert attach_secret_paste(entry, _FakeTk()) is True
    for seq in ("<Button-3>", "<Control-v>", "<Control-a>", "<<PasteSelection>>"):
        assert seq in entry.bindings

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
# ---------------------------------------------------------------------------


class _FakeMenu:
    def __init__(self, *_a, **_kw):
        self.commands = []

    def add_command(self, label=None, command=None):
        self.commands.append((label, command))


class _FakeTk:
    Menu = _FakeMenu


class _FakeEntry:
    """Enough of a tk.Entry to exercise ``attach_secret_paste``."""

    def __init__(self, configure_raises: bool = False):
        self.options = {"exportselection": 1}
        self.bindings = {}
        self._configure_raises = configure_raises

    def configure(self, **kw):
        if self._configure_raises:
            raise RuntimeError("this Tk build has no such option")
        self.options.update(kw)

    def bind(self, sequence, func):
        self.bindings[sequence] = func


def test_secret_field_stops_exporting_its_selection():
    """The selection must survive another app claiming the X11 PRIMARY selection.

    Tk clears an entry's own selection when it loses PRIMARY, and copying a key
    out of an email or password manager is exactly what takes PRIMARY away. With
    the selection silently gone, pasting over a highlighted wrong key appends the
    new one beside it instead of replacing it, and activation fails on a key the
    user can see is correct.
    """
    entry = _FakeEntry()
    assert attach_secret_paste(entry, _FakeTk()) is True
    assert entry.options["exportselection"] is False


def test_paste_menu_still_attaches_when_exportselection_is_rejected():
    """Best-effort: an odd Tk build must not cost the field its paste menu."""
    entry = _FakeEntry(configure_raises=True)
    assert attach_secret_paste(entry, _FakeTk()) is True
    for seq in ("<Button-3>", "<Control-v>", "<Control-a>"):
        assert seq in entry.bindings

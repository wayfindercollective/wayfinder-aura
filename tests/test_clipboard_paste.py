"""Paste behaviour for credential fields.

The licence and API-key entries had no clipboard affordance at all: no
right-click menu anywhere in the app, so a key copied from an email could not
be pasted into the field that needs it. These cover the pure logic; the Tk
binding is a thin wrapper over it.
"""

import pytest

from wayfinder.ui.clipboard import sanitize_pasted_secret, text_after_paste


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

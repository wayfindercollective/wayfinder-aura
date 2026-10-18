"""The Advanced tile of Settings with real Tk widgets: sections open on their own.

The layout rules are covered headlessly in test_advanced_section.py. This drives
the real builder and handlers against a real (offscreen) window, because a layout
or packing mistake only shows up with widgets. Excluded from the gating run by the
``ui`` marker; skipped when Tk cannot open a window (headless CI).
"""

import types

import pytest

pytestmark = pytest.mark.ui

ctk = pytest.importorskip("customtkinter")


@pytest.fixture
def advanced():
    import tkinter as tk

    import wayfinder_main as wm
    from wayfinder.ui.advanced_section import advanced_slot

    try:
        root = ctk.CTk()
    except tk.TclError as exc:  # no display
        pytest.skip(f"Tk cannot open a window here: {exc}")
    root.geometry("460x520+3000+3000")  # offscreen: never flashes on a real screen
    app = types.SimpleNamespace(
        font_header=("Helvetica",),
        font_body=("Helvetica",),
        font_sizes={"caption": 11, "small": 12, "body": 13},
    )
    for name in ("toggle_advanced_group", "toggle_advanced_settings", "_refresh_advanced_groups"):
        setattr(app, name, getattr(wm.WayfinderApp, name).__get__(app))
    scroll = ctk.CTkFrame(root)
    scroll.pack(fill="both", expand=True)
    wm.WayfinderApp._create_advanced_tile(app, scroll)
    app._advanced_tile.pack(fill="x")
    root.update()
    try:
        yield app, root, advanced_slot
    finally:
        root.destroy()


def _packed(widget) -> bool:
    return bool(widget.winfo_manager())


def _add_row(app, advanced_slot, key, text="row"):
    label = ctk.CTkLabel(advanced_slot(app, key, None), text=text)
    label.pack()
    return label


def test_tile_starts_collapsed_with_every_section_closed(advanced):
    app, root, _slot = advanced
    root.update()
    assert app._advanced_open is False
    assert not _packed(app._advanced_body)
    assert app._advanced_toggle_btn.cget("text") == "Show"
    assert set(app._advanced_group_open.values()) == {False}


def test_tile_toggle_shows_and_hides_the_body(advanced):
    app, root, slot = advanced
    _add_row(app, slot, "processing")
    app.toggle_advanced_settings()
    root.update()
    assert _packed(app._advanced_body)
    assert app._advanced_toggle_btn.cget("text") == "Hide"
    app.toggle_advanced_settings()
    root.update()
    assert not _packed(app._advanced_body)
    assert app._advanced_toggle_btn.cget("text") == "Show"


def test_only_sections_that_hold_rows_are_shown_in_order(advanced):
    app, root, slot = advanced
    _add_row(app, slot, "overlay")
    _add_row(app, slot, "processing")
    app._refresh_advanced_groups()
    root.update()
    shown = [key for key, (group, _c) in app._advanced_groups.items() if _packed(group)]
    assert shown == ["processing", "overlay"]


def test_each_section_opens_and_closes_on_its_own(advanced):
    app, root, slot = advanced
    row_a = _add_row(app, slot, "processing", "backend")
    row_b = _add_row(app, slot, "overlay", "scale")
    app._refresh_advanced_groups()
    app.toggle_advanced_settings()
    root.update()
    processing = app._advanced_groups["processing"][1]
    overlay = app._advanced_groups["overlay"][1]
    # Opening Advanced does not dump every row: sections are closed.
    assert not _packed(processing) and not _packed(overlay)
    assert not row_a.winfo_ismapped() and not row_b.winfo_ismapped()

    app.toggle_advanced_group("processing")
    root.update()
    assert _packed(processing) and not _packed(overlay)
    assert row_a.winfo_ismapped() and not row_b.winfo_ismapped()
    assert app._advanced_group_buttons["processing"].cget("text") == "Hide"
    assert app._advanced_group_buttons["overlay"].cget("text") == "Show"

    app.toggle_advanced_group("processing")
    root.update()
    assert not _packed(processing)
    assert app._advanced_group_buttons["processing"].cget("text") == "Show"


def test_a_section_that_loses_its_rows_hides_itself(advanced):
    app, root, slot = advanced
    _add_row(app, slot, "processing")
    app._refresh_advanced_groups()
    assert _packed(app._advanced_groups["processing"][0])
    # Switching to Remote clears the processing rows (see _build_mode_settings).
    for child in app._advanced_groups["processing"][1].winfo_children():
        child.destroy()
    app._refresh_advanced_groups()
    root.update()
    assert not _packed(app._advanced_groups["processing"][0])


def test_unknown_section_is_ignored(advanced):
    app, _root, _slot = advanced
    app.toggle_advanced_group("nope")  # must not raise
    assert "nope" not in app._advanced_group_open

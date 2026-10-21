"""Nested vocabulary-style editors must not trap the Settings page wheel."""

import pytest

pytestmark = pytest.mark.ui
ctk = pytest.importorskip("customtkinter")


@pytest.fixture(params=["existing", "created_later"])
def editor(request):
    import tkinter as tk

    import wayfinder_main as wm
    from wayfinder.ui import components

    before_wheel_active = components._WHEEL_SCROLL_ACTIVE
    try:
        root = ctk.CTk()
    except tk.TclError as exc:
        pytest.skip(f"Tk cannot open here: {exc}")
    root.geometry("520x420+3000+3000")
    if request.param == "created_later":
        wm.WayfinderApp._enable_linux_mousewheel(root)
    page = ctk.CTkScrollableFrame(root)
    page.pack(fill="both", expand=True)
    box = ctk.CTkTextbox(page, height=100, wrap="none")
    box.pack(fill="x", pady=24)
    ctk.CTkFrame(page, height=1000).pack(fill="x")
    root.update()
    if request.param == "existing":
        wm.WayfinderApp._enable_linux_mousewheel(root)
    try:
        yield root, page._parent_canvas, box._textbox
    finally:
        root.destroy()
        components.set_wheel_scroll_active(before_wheel_active)


def _wheel(root, text, delta, *, shift=False):
    text.event_generate(
        "<MouseWheel>", delta=delta, state=1 if shift else 0,
        x=10, y=10, rootx=text.winfo_rootx() + 10, rooty=text.winfo_rooty() + 10,
        when="now",
    )
    root.update()


@pytest.mark.parametrize("delta", [-120, -1])
def test_a_short_vocabulary_list_scrolls_the_page_without_changing_editing_state(editor, delta):
    root, page, text = editor
    text.insert("1.0", "Wayfinder\nAura")
    text.mark_set("insert", "2.2")
    text.tag_add("sel", "1.0", "1.4")
    root.update()
    assert text.yview() == (0.0, 1.0)
    before = text.get("1.0", "end"), text.index("insert"), text.tag_ranges("sel"), root.focus_get()
    _wheel(root, text, delta)
    assert page.yview()[0] > 0
    assert text.yview() == (0.0, 1.0)
    assert (text.get("1.0", "end"), text.index("insert"), text.tag_ranges("sel"), root.focus_get()) == before


def test_a_long_list_scrolls_inside_then_hands_off_at_both_edges(editor):
    root, page, text = editor
    text.insert("1.0", "\n".join(f"Word {n}" for n in range(120)))
    root.update()
    _wheel(root, text, -120)
    assert text.yview()[0] > 0
    assert page.yview()[0] == 0  # Never scroll both containers for one event.
    text.yview_moveto(1)
    root.update()
    _wheel(root, text, -120)
    assert page.yview()[0] > 0
    assert text.yview()[1] == 1
    # Put the editor back in view with room for the page to move upward.
    page.yview_moveto(0.01)
    text.yview_moveto(0)
    root.update()
    before = page.yview()[0]
    _wheel(root, text, 120)
    assert page.yview()[0] < before
    assert text.yview()[0] == 0


def test_horizontal_scrolling_still_belongs_to_the_editor(editor):
    root, page, text = editor
    text.insert("1.0", "A long vocabulary term " * 40)
    root.update()
    _wheel(root, text, -120, shift=True)
    assert text.xview()[0] > 0
    assert page.yview()[0] == 0


def test_legacy_wheel_buttons_also_chain_to_the_page(editor):
    root, page, text = editor
    root.update()
    for sequence in ("<Button-5>", "<Button-4>"):
        before = page.yview()[0]
        # The first scroll partly moves the editor above the viewport. Aim at
        # its still-visible lower portion, not a point outside the window.
        y = text.winfo_height() - 10
        text.event_generate(
            sequence, x=10, y=y, rootx=text.winfo_rootx() + 10,
            rooty=text.winfo_rooty() + y, when="now",
        )
        root.update()
        if sequence == "<Button-5>":
            assert page.yview()[0] > before
        else:
            assert page.yview()[0] < before

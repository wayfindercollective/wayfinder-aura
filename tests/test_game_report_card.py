"""The game-report card and Your reports: copy as pure functions (design
docs/plans/game-reports-design-2026-10-15.md, sections 4.2 and 4.3). The widget
construction test needs a display and is marked ``ui``.
"""

from __future__ import annotations

import pytest

from wayfinder.core.game_reports import ReportView
from wayfinder.ui import game_report_card as card


def test_the_question_and_answers():
    assert card.prompt_title("World of Warcraft") == "How did Aura work in World of Warcraft?"
    assert list(card.ANSWER_LABELS.items()) == [
        ("works", "Works"), ("some_issues", "Some issues"), ("doesnt_work", "Doesn't work")]


def test_note_placeholders():
    assert card.note_placeholder("works") == (
        "Anything worth knowing? No names or contact details, please.")
    for answer in ("some_issues", "doesnt_work"):
        assert card.note_placeholder(answer) == (
            "What went wrong? For example: chat didn't open, or the text didn't paste.")


@pytest.mark.parametrize("os_name,version,shown", [
    ("macos", "15", "macOS 15"), ("windows", "11", "Windows 11"),
    ("linux", "steamos 3", "SteamOS 3"), ("linux", "bazzite 42", "Bazzite 42"),
    ("linux", "arch", "Arch Linux"), ("linux", "fedora 41", "Fedora 41"),
    ("linux", "linux", "Linux"),
])
def test_the_operating_system_as_shown(os_name, version, shown):
    assert card.os_display(os_name, version) == shown


def test_sends_only_names_what_is_sent():
    assert card.sends_only_line("World of Warcraft", "1.2.1", "macos", "15") == (
        "Sends only: World of Warcraft, Aura 1.2.1, macOS 15, your answer and note.")


def test_the_note_count_shows_past_240_and_send_needs_a_valid_note():
    assert card.note_count_text("x" * 240) == ""
    assert card.note_count_text("x" * 241) == "241/280"
    assert card.send_allowed("x" * 280) and not card.send_allowed("x" * 281)
    assert card.note_problem_text("see www.example.com") == "Links can't be sent"
    assert not card.send_allowed("see https://example.com")
    assert card.note_problem_text("fine") == "" and card.send_allowed("")


def test_result_messages():
    assert card.result_text("sent") == (
        "Thanks. It helps other players pick games. You can see or withdraw it in Games.")
    assert card.result_text("gone") == card.result_text("sent")
    assert card.result_text("saved") == "Saved. Aura sends it when you're online."
    assert card.result_text("limit").startswith("You've sent 5 reports today.")
    assert card.DONT_ASK_TEXT == "Aura won't ask. You can still report a game from the Games tab."
    assert card.CLOSE_AFTER_MS == 4000


def test_games_tab_copy():
    assert card.GAMES_HINT == "Played one of these? Click it to tell us how Aura worked."
    assert card.you_said("works") == "You said: Works"
    assert card.you_said("doesnt_work") == "You said: Doesn't work"
    assert card.your_reports_line(2) == "Your reports: 2. See or withdraw."
    assert card.your_reports_line(1) == "Your reports: 1. See or withdraw."
    assert card.your_reports_line(0) == "Your reports: none yet."


@pytest.mark.parametrize("status,text", [
    ("sent", "Sent"), ("queued", "Waiting to send"), ("not_confirmed", "Not confirmed"),
    ("failed", "Couldn't send"), ("withdrawing", "Withdrawing"),
])
def test_status_names(status, text):
    assert card.status_text(status) == text


def test_a_report_row_names_everything_that_was_sent():
    view = ReportView("world-of-warcraft", "World of Warcraft", "some_issues",
                      "Chat lagged.", "1.2.1", 1_760_000_000.0, "sent")
    text = card.report_row_text(view)
    for part in ("Some issues", "Chat lagged.", "Aura 1.2.1", "Sent"):
        assert part in text
    assert card.report_row_text(ReportView(None, "All your reports", None, None, None,
                                           None, "withdrawing")) == "Withdrawing"


@pytest.mark.ui
def test_the_card_builds_and_walks_through_an_answer(monkeypatch):
    import customtkinter as ctk

    from wayfinder.core import game_reports
    from wayfinder.ui import icons

    monkeypatch.setattr(icons, "_CACHE", {})  # icons belong to one Tk root
    sent = []
    monkeypatch.setattr(game_reports, "submit",
                        lambda name, answer, note, **k: sent.append((name, answer, note)) or "queued")
    root = ctk.CTk()
    try:
        app = type("App", (), {"font_sizes": dict.fromkeys(("display", "title", "heading", "body", "small", "caption"), 12),
            "font_body": ("DejaVu Sans",), "font_header": ("DejaVu Sans",),
            "after": root.after, "config": {}})()
        widget = card.GameReportCard(root, app, "world-of-warcraft", "World of Warcraft",
                                     prompt=True, post=lambda fn: fn(), on_close=lambda: None,
                                     on_dont_ask=lambda: None)
        widget.pack()
        widget.choose("some_issues")
        widget.note_entry.insert(0, "Chat lagged.")
        widget.send()
        assert sent == [("World of Warcraft", "some_issues", "Chat lagged.")]
    finally:
        root.destroy()

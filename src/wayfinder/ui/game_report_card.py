"""Game reports in Aura's window: the inline card and the Your reports panel.

Design: docs/plans/game-reports-design-2026-10-15.md, sections 4.2 and 4.3.

* ``GameReportCard`` asks "How did Aura work in <game>?" with three answer
  pills and, once one is chosen, an optional note and Send. It is a plain
  ``CTkFrame`` packed into the main window: on the Dictate tab when Aura asks,
  or under a Games-tab row when the player clicks one. It never opens a
  window, takes focus, notifies or plays a sound.
* ``YourReportsPanel`` is the quiet "Your reports" line at the bottom of the
  Games tile. It expands into what this computer sent, with Withdraw per game
  and Withdraw all. It works whether or not Wayfinder is accepting reports,
  and offline (withdrawals then wait).

All copy comes from the pure functions below (tested without a display);
customtkinter is imported only when a widget is built. Network work happens on
background threads in ``core.game_reports``; ``post`` hands results back to
the Tk thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date

from wayfinder.core import game_labels, game_reports
from wayfinder.ui.theme import COLORS, RADIUS, SPACING

CLOSE_AFTER_MS = 4000
COUNT_FROM = 240

ANSWER_LABELS = {
    "works": "Works",
    "some_issues": "Some issues",
    "doesnt_work": "Doesn't work",
}
# The overlay's state colours: mint (pasting), gold (processing), rose (recording).
ANSWER_COLORS = {
    "works": "state_typing",
    "some_issues": "state_processing",
    "doesnt_work": "state_recording",
}
STATUS_TEXT = {
    game_reports.SENT: "Sent",
    game_reports.QUEUED: "Waiting to send",
    game_reports.NOT_CONFIRMED: "Not confirmed",
    game_reports.FAILED: "Couldn't send",
    game_reports.WITHDRAWING: "Withdrawing",
}
GAMES_HINT = "Played one of these? Click it to tell us how Aura worked."
DONT_ASK_TEXT = "Aura won't ask. You can still report a game from the Games tab."
LINKS_TEXT = "Links can't be sent"
PAUSED_TEXT = "Reports are paused right now. You can still see or withdraw yours in Games."
_RESULTS = {
    "sent": "Thanks. It helps other players pick games. You can see or withdraw it in Games.",
    "saved": "Saved. Aura sends it as soon as Wayfinder can take it.",
    "failed": "Aura couldn't send this report.",
    "limit": "You've sent 5 reports today. Aura can send more tomorrow.",
    "error": "Aura couldn't save this report.",
    "closed": PAUSED_TEXT,
}
_LINUX_NAMES = {
    "steamos": "SteamOS", "bazzite": "Bazzite", "ubuntu": "Ubuntu", "fedora": "Fedora",
    "arch": "Arch Linux", "debian": "Debian", "linuxmint": "Linux Mint", "pop": "Pop!_OS",
    "manjaro": "Manjaro", "endeavouros": "EndeavourOS", "nobara": "Nobara",
    "cachyos": "CachyOS", "linux": "Linux",
}


# --- copy (pure) ----------------------------------------------------------------------

def prompt_title(name: str) -> str:
    return f"How did Aura work in {name}?"


def note_placeholder(answer: str) -> str:
    if answer == "works":
        return "Anything worth knowing? No names or contact details, please."
    return "What went wrong? For example: chat didn't open, or the text didn't paste."


def os_display(os_name: str, os_version: str) -> str:
    if os_name == "macos":
        return f"macOS {os_version}"
    if os_name == "windows":
        return f"Windows {os_version}"
    ident, _, major = (os_version or "linux").partition(" ")
    name = _LINUX_NAMES.get(ident, ident[:1].upper() + ident[1:])
    return f"{name} {major}".strip()


def sends_only_line(name: str, app_version: str, os_name: str, os_version: str) -> str:
    return (f"Sends only: {name}, Aura {app_version}, {os_display(os_name, os_version)}, "
            "your answer and note.")


def note_count_text(text: str) -> str:
    count = len(text or "")
    return f"{count}/{game_reports.NOTE_MAX}" if count > COUNT_FROM else ""


def note_problem_text(text: str) -> str:
    return LINKS_TEXT if game_reports.clean_note(text)[1] == "links" else ""


def send_allowed(text: str) -> bool:
    return len(text or "") <= game_reports.NOTE_MAX and not note_problem_text(text)


def result_text(outcome: str) -> str:
    """The card's last line after Send: sent (or dropped as already
    withdrawn), saved for later, failed, the daily limit or an error."""
    return _RESULTS.get("sent" if outcome == "gone" else outcome, _RESULTS["error"])


def you_said(answer: str) -> str:
    return f"You said: {ANSWER_LABELS.get(answer, answer)}"


def your_reports_line(count: int) -> str:
    return f"Your reports: {count}. See or withdraw." if count else "Your reports: none yet."


def status_text(status: str) -> str:
    return STATUS_TEXT.get(status, status)


def report_row_text(view: game_reports.ReportView) -> str:
    """One line of Your reports: answer, note, version, date and status."""
    if view.status == game_reports.WITHDRAWING:
        return status_text(view.status)
    parts = [ANSWER_LABELS.get(view.answer or "", "")]
    if view.note:
        parts.append(f'"{view.note}"')
    if view.app_version:
        parts.append(f"Aura {view.app_version}")
    if view.created_at:
        try:
            parts.append(date.fromtimestamp(view.created_at).isoformat())
        except (OverflowError, OSError, ValueError):
            pass
    parts.append(status_text(view.status))
    return " · ".join(p for p in parts if p)


# --- widgets ------------------------------------------------------------------------------

def _fonts(app):
    family = app.font_body[0]
    sizes = app.font_sizes
    return family, sizes


class GameReportCard:
    """The inline question card (composition around a standard CTkFrame).

    ``prompt=True`` is Aura asking on the Dictate tab: closing it or answering
    counts as asked, and it offers Don't ask again. ``prompt=False`` is the
    Games-tab variant under a row."""

    def __init__(self, parent, app, key: str, name: str, *, prompt: bool,
                 post: Callable[[Callable[[], None]], None],
                 on_close: Callable[[], None],
                 on_dont_ask: Callable[[], None] | None = None):
        import customtkinter as ctk

        from wayfinder.ui.icons import get_icon

        self._ctk = ctk
        self.app, self.key, self.name, self.prompt = app, key, name, prompt
        self.post, self.on_close, self.on_dont_ask = post, on_close, on_dont_ask
        self.answer: str | None = None
        self.note_entry = None
        self._closed = False
        self._finished = False
        family, sizes = _fonts(app)
        self._family, self._sizes = family, sizes

        self.frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=RADIUS["md"],
                                  border_width=1, border_color=COLORS["border_rim"])
        head = ctk.CTkFrame(self.frame, fg_color="transparent")
        head.pack(fill="x", padx=SPACING["md"], pady=(SPACING["sm"], SPACING["xs"]))
        ctk.CTkLabel(head, text=prompt_title(name), anchor="w", justify="left", wraplength=400,
                     font=(family, sizes["body"], "bold"),
                     text_color=COLORS["text_primary"]).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(head, text="", image=get_icon("x", 14, COLORS["text_muted"]), width=24,
                      height=24, fg_color="transparent", hover_color=COLORS["bg_hover"],
                      corner_radius=RADIUS["sm"], command=self.close).pack(side="right")

        pills = ctk.CTkFrame(self.frame, fg_color="transparent")
        pills.pack(fill="x", padx=SPACING["md"], pady=(0, SPACING["xs"]))
        self.pills = {}
        for answer, label in ANSWER_LABELS.items():
            color = COLORS[ANSWER_COLORS[answer]]
            button = ctk.CTkButton(
                pills, text=label, height=28, width=96, corner_radius=RADIUS["md"],
                border_width=1, border_color=color, fg_color="transparent",
                hover_color=COLORS["bg_hover"], text_color=color,
                font=(family, sizes["small"]),
                command=lambda a=answer: self.choose(a))
            button.pack(side="left", padx=(0, SPACING["sm"]))
            self.pills[answer] = button

        self.body = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.body.pack(fill="x", padx=SPACING["md"], pady=(0, SPACING["sm"]))
        if prompt:
            ctk.CTkButton(self.body, text="Don't ask again", height=22, width=0,
                          fg_color="transparent", hover_color=COLORS["bg_hover"],
                          text_color=COLORS["text_muted"], anchor="w",
                          font=(family, sizes["caption"]),
                          command=self.dont_ask).pack(anchor="w")
        # One one-shot callback at a time, for when report permission lapses.
        self._schedule_expiry(game_labels.permission_left(time.time()))

    # The card is used like a widget by its host.
    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def destroy(self) -> None:
        self._closed = True
        try:
            self.frame.destroy()
        except Exception:
            pass

    def winfo_exists(self) -> bool:
        try:
            return not self._closed and bool(self.frame.winfo_exists())
        except Exception:
            return False

    def _clear_body(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def choose(self, answer: str) -> None:
        """A pill was pressed: light it and show the note, the disclosure and Send."""
        ctk = self._ctk
        first = self.answer is None
        self.answer = answer
        for key, button in self.pills.items():
            color = COLORS[ANSWER_COLORS[key]]
            chosen = key == answer
            button.configure(fg_color=color if chosen else "transparent",
                             text_color=COLORS["bg_base"] if chosen else color)
        if not first and self.note_entry is not None:
            self.note_entry.configure(placeholder_text=note_placeholder(answer))
            return
        self._clear_body()
        family, sizes = self._family, self._sizes
        self.note_entry = ctk.CTkEntry(
            self.body, placeholder_text=note_placeholder(answer), height=30,
            corner_radius=RADIUS["md"], fg_color=COLORS["bg_input"],
            border_color=COLORS["border_subtle"], text_color=COLORS["text_primary"],
            font=(family, sizes["small"]))
        self.note_entry.pack(fill="x", pady=(0, SPACING["xs"]))
        self.note_entry.bind("<KeyRelease>", lambda _e: self._note_changed(), add="+")
        self.note_status = ctk.CTkLabel(self.body, text="", anchor="w", height=16,
                                        font=(family, sizes["caption"]),
                                        text_color=COLORS["accent_red"])
        self.note_status.pack(fill="x")
        os_name, os_version = game_reports.coarse_os()
        ctk.CTkLabel(self.body, anchor="w", justify="left", wraplength=420,
                     text=sends_only_line(self.name, game_reports._app_version(), os_name,
                                          os_version),
                     font=(family, sizes["caption"]),
                     text_color=COLORS["text_muted"]).pack(fill="x", pady=(0, SPACING["xs"]))
        buttons = ctk.CTkFrame(self.body, fg_color="transparent")
        buttons.pack(fill="x")
        self.send_button = ctk.CTkButton(
            buttons, text="Send", width=72, height=28, corner_radius=RADIUS["md"],
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"], font=(family, sizes["small"]), command=self.send)
        self.send_button.pack(side="right")
        ctk.CTkButton(buttons, text="Cancel", width=72, height=28, corner_radius=RADIUS["md"],
                      fg_color="transparent", hover_color=COLORS["bg_hover"],
                      border_width=1, border_color=COLORS["border_subtle"],
                      text_color=COLORS["text_secondary"], font=(family, sizes["small"]),
                      command=self.close).pack(side="right", padx=(0, SPACING["sm"]))

    def note_text(self) -> str:
        return self.note_entry.get() if self.note_entry is not None else ""

    def _note_changed(self) -> None:
        text = self.note_text()
        problem = note_problem_text(text)
        self.note_status.configure(text=problem or note_count_text(text),
                                   text_color=COLORS["accent_red"] if problem or len(text) >
                                   game_reports.NOTE_MAX else COLORS["text_muted"])
        self.send_button.configure(state="normal" if send_allowed(text) else "disabled")

    def _schedule_expiry(self, seconds: float) -> None:
        try:
            self.app.after(int(seconds * 1000) + 100, self._permission_lapsed)
        except Exception:
            pass

    def _permission_lapsed(self) -> None:
        """The deadline this was scheduled for has passed. Ask again: a refresh
        may have renewed permission since, and then the draft stays."""
        if not self.winfo_exists() or self._finished:
            return
        left = game_reports.report_window(self.key)
        if left > 0:
            self._schedule_expiry(left)
        else:
            self._finish(PAUSED_TEXT)

    def send(self) -> None:
        text = self.note_text()
        if self.answer is None or not send_allowed(text):
            return
        if not game_reports.may_report(self.key):
            self._finish(PAUSED_TEXT)  # permission lapsed or intake closed meanwhile
            return
        result = game_reports.submit(self.name, self.answer, text,
                                     on_sent=lambda: self.post(self._after_send))
        if result == "queued":
            self._show_message("Sending...")
        else:
            self._finish(result_text("error" if result in ("invalid", "link") else result))

    def _after_send(self) -> None:
        if self.winfo_exists():
            self._finish(result_text(game_reports.outcome(self.key)))

    def _show_message(self, text: str) -> None:
        self._clear_body()
        for button in self.pills.values():
            button.configure(state="disabled")
        self._ctk.CTkLabel(self.body, text=text, anchor="w", justify="left", wraplength=420,
                           font=(self._family, self._sizes["small"]),
                           text_color=COLORS["text_secondary"]).pack(fill="x")

    def _finish(self, text: str) -> None:
        """Show the last line, then close once, a few seconds later."""
        self._finished = True
        self._show_message(text)
        try:
            self.app.after(CLOSE_AFTER_MS, self._closed_by_timer)
        except Exception:
            self._closed_by_timer()

    def _closed_by_timer(self) -> None:
        if not self._closed:
            self._closed = True
            self.on_close()

    def close(self) -> None:
        """[x] or Cancel. When Aura asked, that counts as asked."""
        if self.prompt:
            game_reports.mark_asked(self.key)
        self._closed_by_timer()

    def dont_ask(self) -> None:
        game_reports.mark_asked(self.key)
        if self.on_dont_ask is not None:
            self.on_dont_ask()
        self._finish(DONT_ASK_TEXT)


class YourReportsPanel:
    """"Your reports: N. See or withdraw." and, expanded, the list with
    Withdraw per game and Withdraw all. Always available."""

    def __init__(self, parent, app, *, on_change: Callable[[], None] | None = None):
        import customtkinter as ctk

        self._ctk, self.app, self.on_change = ctk, app, on_change
        self._family, self._sizes = _fonts(app)
        self.expanded = False
        self._confirm_all = False
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.line = ctk.CTkButton(
            self.frame, text="", height=22, width=0, anchor="w", fg_color="transparent",
            hover_color=COLORS["bg_hover"], text_color=COLORS["text_muted"],
            font=(self._family, self._sizes["small"]), command=self.toggle)
        self.line.pack(anchor="w")
        self.body = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.refresh()

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self._confirm_all = False
        self.refresh()

    def refresh(self) -> None:
        try:
            if not self.frame.winfo_exists():
                return
        except Exception:
            return
        rows = game_reports.your_reports()
        reports = [r for r in rows if r.status != game_reports.WITHDRAWING]
        self.line.configure(text=your_reports_line(len(reports)))
        for child in self.body.winfo_children():
            child.destroy()
        if not self.expanded or not rows:
            self.body.pack_forget()
            return
        self.body.pack(fill="x", pady=(SPACING["xs"], 0))
        self._render(rows)

    def _render(self, rows: list[game_reports.ReportView]) -> None:
        ctk, family, sizes = self._ctk, self._family, self._sizes
        groups: dict[str | None, list[game_reports.ReportView]] = {}
        for row in rows:
            groups.setdefault(row.game_key, []).append(row)
        for key, views in groups.items():
            head = ctk.CTkFrame(self.body, fg_color="transparent")
            head.pack(fill="x", pady=(SPACING["xs"], 0))
            ctk.CTkLabel(head, text=views[0].game_name, anchor="w",
                         font=(family, sizes["small"], "bold"),
                         text_color=COLORS["text_primary"]).pack(side="left")
            if key is not None and any(v.status != game_reports.WITHDRAWING for v in views):
                ctk.CTkButton(head, text="Withdraw", width=80, height=22,
                              corner_radius=RADIUS["sm"], fg_color="transparent",
                              hover_color=COLORS["bg_hover"], border_width=1,
                              border_color=COLORS["border_subtle"],
                              text_color=COLORS["text_secondary"],
                              font=(family, sizes["caption"]),
                              command=lambda k=key: self.withdraw(k)).pack(side="right")
            for view in views:
                ctk.CTkLabel(self.body, text=report_row_text(view), anchor="w", justify="left",
                             wraplength=440, font=(family, sizes["caption"]),
                             text_color=COLORS["text_muted"]).pack(fill="x")
        if any(r.status != game_reports.WITHDRAWING for r in rows):
            foot = ctk.CTkFrame(self.body, fg_color="transparent")
            foot.pack(fill="x", pady=(SPACING["sm"], 0))
            text = "Withdraw all: are you sure?" if self._confirm_all else "Withdraw all"
            ctk.CTkButton(foot, text=text, height=24, width=0, corner_radius=RADIUS["sm"],
                          fg_color="transparent", hover_color=COLORS["bg_hover"],
                          border_width=1, border_color=COLORS["accent_red"] if self._confirm_all
                          else COLORS["border_subtle"],
                          text_color=COLORS["accent_red"] if self._confirm_all
                          else COLORS["text_secondary"],
                          font=(family, sizes["caption"]),
                          command=self.withdraw_all).pack(side="left")
            if self._confirm_all:
                ctk.CTkButton(foot, text="Keep them", height=24, width=0,
                              corner_radius=RADIUS["sm"], fg_color="transparent",
                              hover_color=COLORS["bg_hover"], text_color=COLORS["text_muted"],
                              font=(family, sizes["caption"]),
                              command=self._cancel_all).pack(side="left", padx=(SPACING["sm"], 0))

    def _changed(self) -> None:
        self.refresh()
        if self.on_change is not None:
            self.on_change()

    def withdraw(self, key: str) -> None:
        game_reports.withdraw(key)
        self._changed()

    def withdraw_all(self) -> None:
        if not self._confirm_all:
            self._confirm_all = True
            self.refresh()
            return
        self._confirm_all = False
        game_reports.withdraw_all()
        self._changed()

    def _cancel_all(self) -> None:
        self._confirm_all = False
        self.refresh()

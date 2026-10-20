"""A transient foreground view over the existing staged Settings builder."""

from __future__ import annotations

import customtkinter as ctk

from wayfinder.ui.theme import COLORS, RADIUS, SPACING


class SettingsLoadingView:
    def __init__(self, app, parent, *, surface: dict, retry):
        self.frame = ctk.CTkFrame(parent, **surface)
        self.advanced_switch = app._workspace.create_mode_switch(self.frame)
        self.advanced_switch.pack(anchor="w", padx=SPACING["md"], pady=(SPACING["sm"], SPACING["lg"]))
        content = ctk.CTkFrame(self.frame, fg_color="transparent")
        content.pack(expand=True, padx=SPACING["lg"], pady=SPACING["lg"])
        self.label = ctk.CTkLabel(
            content, text="Loading advanced settings…",
            font=(app.font_body[0], app.font_sizes["body"]),
            text_color=COLORS["text_secondary"],
        )
        self.label.pack()
        self.retry = ctk.CTkButton(
            content, text="Retry", command=retry, width=100,
            font=(app.font_body[0], app.font_sizes["body"]),
            fg_color=COLORS["bg_input"], hover_color=COLORS["bg_elevated"],
            corner_radius=RADIUS["md"],
        )
        self.callbacks = []

    def busy(self) -> None:
        self.label.configure(text="Loading advanced settings…")
        self.retry.pack_forget()

    def failed(self) -> None:
        self.label.configure(text="Couldn’t load advanced settings.")
        self.retry.pack(pady=(SPACING["md"], 0))

    def when_ready(self, callback) -> None:
        if callback not in self.callbacks:
            self.callbacks.append(callback)

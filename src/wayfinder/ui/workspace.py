"""One surface, core tabs; the existing workspace remains one switch away.

Only layout changes here. In particular, switching views never restarts an
indicator, reloads a model, changes the current dictation, or resets a setting.
"""

from __future__ import annotations

import customtkinter as ctk

from wayfinder import __version__
from wayfinder.ui.basic_mode import basic_mode_enabled, basic_tabs
from wayfinder.ui.macos_window import CONTENT_PANE_INSET
from wayfinder.ui.surfaces import panel_style
from wayfinder.ui.theme import COLORS, RADIUS, SPACING


class Workspace:
    def __init__(self, app, parent):
        self.app = app
        self.frame = ctk.CTkFrame(
            parent, **panel_style("workspace"),
        )
        self.frame.pack(fill="both", expand=True)
        self.advanced_var = ctk.BooleanVar(master=self.frame, value=not basic_mode_enabled(app.config))
        self.nav = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.nav.pack(fill="x", padx=SPACING["2xl"], pady=(0, SPACING["md"]))
        self.tabs = ctk.CTkSegmentedButton(
            self.nav, values=["Dictate", "Settings"],
            command=lambda value: app._switch_tab(value.lower()),
            font=(app.font_body[0], app.font_sizes["body"]),
            height=36, corner_radius=RADIUS["md"], border_width=SPACING["xs"],
            fg_color=COLORS["bg_input"], selected_color=COLORS["bg_elevated"],
            selected_hover_color=COLORS["bg_elevated"], unselected_color=COLORS["bg_input"],
            unselected_hover_color=COLORS["bg_hover"], text_color=COLORS["text_primary"],
        )
        self.tabs.pack()
        self.body = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.body.pack(fill="both", expand=True)
        self.body.grid_columnconfigure(0, weight=0, minsize=180)
        self.body.grid_columnconfigure(1, weight=1)
        self.body.grid_rowconfigure(0, weight=1)
        self.footer = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.advanced_switch = self.create_mode_switch(self.footer)
        self.advanced_switch.pack(side="left")
        self.version = ctk.CTkLabel(
            self.footer, text="", height=20,
            font=(app.font_mono[0], app.font_sizes["caption"]),
            text_color=COLORS["text_muted"],
        )
        self.version.pack(side="right")
        self._sidebar = None
        self._pane = None
        self._pane_style = None
        self._page_host = None
        self._mode = None

    def attach(self, sidebar, pane) -> None:
        self._sidebar = sidebar
        self._pane = pane
        page_host = getattr(self.app, "tab_content_container", pane)
        self._page_host = page_host if page_host is not pane else None
        self._pane_style = {
            key: pane.cget(key)
            for key in ("fg_color", "bg_color", "border_width", "corner_radius")
        }
        self.refresh()

    def available_tabs(self) -> tuple[str, ...]:
        return basic_tabs(
            self.app.config,
            style_unlocked=self.app.feature_gate.has_feature("tone_system"),
        )

    def create_mode_switch(self, parent):
        """The simple footer and advanced Settings share one preference."""
        return ctk.CTkSwitch(
            parent, text="Show advanced settings", variable=self.advanced_var,
            command=lambda: self.app._set_advanced_mode(bool(self.advanced_var.get())),
            font=(self.app.font_body[0], self.app.font_sizes["small"]),
            text_color=COLORS["text_secondary"],
            fg_color=COLORS["bg_elevated"], progress_color=COLORS["accent"],
            button_color=COLORS["text_primary"], button_hover_color=COLORS["text_bright"],
            switch_width=32, switch_height=18, corner_radius=RADIUS["md"],
        )

    def refresh(self) -> None:
        basic = basic_mode_enabled(self.app.config)
        self.advanced_var.set(not basic)
        tier = "Ultra" if self.app.feature_gate.is_premium else "Free"
        self.version.configure(text=f"v{__version__} · {tier}")
        if self._mode != basic and self._pane is not None:
            self._mode = basic
            if basic:
                self.frame.configure(fg_color=COLORS["bg_card"], border_width=0)
                self.nav.pack(fill="x", padx=SPACING["2xl"], pady=(0, SPACING["md"]), before=self.body)
                self._sidebar.grid_remove()
                self.body.grid_columnconfigure(0, minsize=0)
                self._pane.grid_configure(column=0, columnspan=2, padx=0)
                self._pane.configure(
                    fg_color=COLORS["bg_card"], bg_color=COLORS["bg_card"], border_width=0,
                )
                backdrop = self.frame.cget("bg_color")
                self.app.hero_frame.configure(
                    border_width=0, bg_color=backdrop,
                    # Joined panels need transparent top corners, but solid
                    # lower corners where they meet the rest of the surface.
                    background_corner_colors=(backdrop, backdrop, COLORS["bg_card"], COLORS["bg_card"]),
                )
                self.app.hero_frame.pack_configure(pady=0)
                self.body.pack_configure(pady=0)
                self.footer.pack(
                    side="bottom", fill="x", padx=SPACING["lg"],
                    pady=(SPACING["sm"], SPACING["lg"]), before=self.body,
                )
            else:
                self.frame.configure(fg_color="transparent", border_width=0)
                self.nav.pack_forget()
                self.footer.pack_forget()
                self._sidebar.grid()
                self.body.grid_columnconfigure(0, minsize=180)
                self._pane.grid_configure(column=1, columnspan=1, padx=(SPACING["md"], 0))
                self._pane.configure(**self._pane_style)
                self.app.hero_frame.configure(border_width=1, background_corner_colors=None)
                self.app.hero_frame.pack_configure(pady=(0, SPACING["md"]))
                self.body.pack_configure(pady=0)
            if self._page_host is not None:
                self._page_host.pack_configure(
                    padx=0 if basic else CONTENT_PANE_INSET,
                    pady=0 if basic else RADIUS["lg"] // 2,
                )
            scale_controls = getattr(self.app, "_header_scale_controls", None)
            if scale_controls is not None:
                if basic:
                    scale_controls.pack_forget()
                else:
                    scale_controls.pack(side="right", padx=(0, SPACING["md"]))
            right_controls = getattr(self.app, "_header_right_controls", None)
            if right_controls is not None:
                # An empty CTkFrame still requests its default 200px height.
                # Removing the last toolbar control must remove its shell too.
                if any(child.winfo_manager() for child in right_controls.winfo_children()):
                    right_controls.pack(side="right")
                else:
                    right_controls.pack_forget()
        tab_ids = self.available_tabs()
        values = [tab.capitalize() for tab in tab_ids]
        if self.tabs.cget("values") != values:
            self.tabs.configure(values=values)
        active = getattr(self.app, "active_tab", "dictate")
        if active in tab_ids:
            self.tabs.set(active.capitalize())

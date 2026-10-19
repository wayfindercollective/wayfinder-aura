"""A quiet, single-column settings view over Aura's existing handlers.

No recording, licensing, or transcription implementation lives here. The
controller translates the few plain-language choices to the normal settings
and uses the same authenticated downloader and model activation path.
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from wayfinder.hotkeys.types import EventType
from wayfinder.state import AppState
from wayfinder.ui.basic_mode import (
    INDICATOR_CHOICES,
    LANGUAGE_CHOICES,
    indicator_choice,
    language_choice,
    premium_model_selected,
    speech_choice,
)
from wayfinder.ui.icons import get_icon
from wayfinder.ui.theme import COLORS, RADIUS, SPACING


class BasicSettingsController:
    """UI actions, including bounded, non-blocking model downloads."""

    def __init__(self, app, downloader_factory: Callable, catalog: dict):
        self.app = app
        self.downloader_factory = downloader_factory
        self.catalog = catalog
        self.on_change: Callable = lambda: None
        self.status = ""
        self.downloading = False
        self._request = 0
        self._pending = None
        self._progress = -1

    def idle(self) -> bool:
        return getattr(self.app, "app_state", AppState.IDLE) == AppState.IDLE

    def refresh(self) -> None:
        self.on_change()

    def _on_ui(self, callback: Callable) -> None:
        self.app.event_queue.put((EventType.UI_CALLBACK, callback))

    def choose_model(self, language: str, premium: bool) -> None:
        if not self.idle() or self.downloading or getattr(self.app, "_ultra_model_downloading", False):
            self.refresh()
            return
        if premium and not self.app.feature_gate.has_feature("large_models"):
            self.refresh()
            self.app._show_premium_prompt("large_models")
            return
        if self.app.config.get("processing_mode", "local") != "local":
            self.status = "Remote transcription is selected in advanced settings."
            self.refresh()
            return
        choice = speech_choice(language, premium)
        self._request += 1
        request = self._request
        original = (
            self.app.config.get("model_path"), self.app.config.get("language", "en")
        )
        downloader = self.downloader_factory()
        existing = downloader.get_model_path(choice.model_id)

        def ready(path):
            if request != self._request:
                return
            self.downloading = False
            self._pending = (str(path), choice, original)
            self.apply_pending()

        if existing is not None:
            ready(existing)
            return
        info = self.catalog.get(choice.model_id, {})
        name = info.get("name", choice.model_id)
        self.downloading = True
        self._progress = -1
        self.status = f"Downloading {name}…"
        self.refresh()

        def progress(fraction, _done, _total):
            percent = max(0, min(100, int(fraction * 100)))
            if percent == self._progress:
                return
            self._progress = percent

            def update():
                if request == self._request and self.downloading:
                    self.status = f"Downloading {name} · {percent}%"
                    self.refresh()

            self._on_ui(update)

        def error(message):
            def update():
                if request != self._request:
                    return
                self.downloading = False
                self.status = f"Download failed: {message}. Your current model is unchanged."
                self.refresh()

            self._on_ui(update)

        try:
            downloader.download_model(
                choice.model_id,
                progress_callback=progress,
                complete_callback=lambda path: self._on_ui(lambda: ready(path)),
                error_callback=error,
            )
        except Exception as exc:
            error(str(exc))

    def apply_pending(self) -> None:
        """Never change a model/language under an active dictation."""
        if self._pending is None:
            self.refresh()
            return
        if not self.idle():
            self.status = "Model ready. It will switch when this dictation finishes."
            self.refresh()
            return
        path, choice, original = self._pending
        self._pending = None
        current = (self.app.config.get("model_path"), self.app.config.get("language", "en"))
        if current != original:
            self.status = "Model downloaded. Keeping the selection you made in advanced settings."
        elif self.app._activate_downloaded_model(
            "whisper", path, self.catalog.get(choice.model_id)
        ) == "active":
            self.app.on_language_changed(choice.language)
            if getattr(self.app, "_pending_welcome_after_model", False):
                self.app._on_whisper_model_ready(path)
            self.status = ""
        else:
            self.status = "Could not switch models. Your current selection is unchanged."
        self.refresh()


class BasicSettingsView:
    """Composes standard CTk widgets; no custom widget/event implementation."""

    def __init__(self, app, parent, controller: BasicSettingsController, *, option_menu=ctk.CTkOptionMenu, tooltip=None):
        self.app = app
        self.controller = controller
        self.frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=RADIUS["none"])
        # This responsive grid owns wrapping. The app-wide legacy label fitter
        # must not compete with it (two geometry owners can oscillate forever).
        self.frame._wf_manages_wrap = True
        self.frame.pack(fill="both", expand=True)
        self._rows = []
        self._stacked = None
        self._last_width = None
        self._mic_signature = None
        self._body_font = (app.font_body[0], app.font_sizes["body"])
        self._hint_font = (app.font_body[0], app.font_sizes["small"])
        self._control_states = {}
        self._option_menu = option_menu
        self._tooltip = tooltip

        content = ctk.CTkFrame(self.frame, fg_color="transparent")
        content.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])

        slot = self._row(content, "Hotkey")
        self.hotkey_button = ctk.CTkButton(
            slot, text=app.get_hotkey_display(), width=210, height=36,
            command=self._detect_hotkey, **self._button_style(),
        )
        self.hotkey_button.pack(anchor="e")

        slot = self._row(content, "Microphone")
        self.mic_var = ctk.StringVar(master=self.frame)
        self.microphone = self._menu(slot, [], self.mic_var, self._choose_microphone)
        self.microphone.pack(side="left")
        self.refresh_mic_button = ctk.CTkButton(
            slot, text="", width=32, height=36,
            image=get_icon("rotate-ccw", 14, COLORS["text_secondary"]),
            command=self._refresh_devices, **self._button_style(),
        )
        self.refresh_mic_button.pack(side="left", padx=(SPACING["sm"], 0))

        slot = self._row(content, "Speech model")
        self.language_var = ctk.StringVar(master=self.frame)
        self.language = self._segments(slot, LANGUAGE_CHOICES, self.language_var, self._choose_language)

        slot = self._row(content, "Premium model", description="Better recognition with Whisper Turbo.", ultra=True)
        self.premium_var = ctk.BooleanVar(master=self.frame)
        self.premium_switch = self._switch(slot, self.premium_var, self._choose_premium)

        slot = self._row(content, "GPU acceleration", ultra=True)
        self.gpu_var = ctk.BooleanVar(master=self.frame)
        self.gpu_switch = self._switch(slot, self.gpu_var, self._toggle_gpu)

        slot = self._row(
            content, "Raw transcript",
            description="Keeps ums and ahs; no cleanup or AI rewriting.",
            tooltip="When on, keep filler words and punctuation as recognized. Skip cleanup, corrections, and AI rewriting.",
        )
        self.raw_var = ctk.BooleanVar(master=self.frame)
        self.raw_switch = self._switch(slot, self.raw_var, self._toggle_raw)

        slot = self._row(content, "Recording indicator", last=True)
        self.indicator_var = ctk.StringVar(master=self.frame)
        self.indicator = self._segments(slot, INDICATOR_CHOICES, self.indicator_var, self._choose_indicator)
        self.status_label = ctk.CTkLabel(
            self.frame, text="", anchor="w", justify="left", wraplength=500,
            font=self._hint_font, text_color=COLORS["text_secondary"],
        )

        self.frame.bind("<Configure>", self._resize, add="+")
        controller.on_change = self.refresh
        self.refresh_microphones()
        self.refresh()

    def _button_style(self) -> dict:
        return {
            "fg_color": COLORS["bg_input"], "hover_color": COLORS["bg_elevated"],
            "text_color": COLORS["text_primary"], "corner_radius": RADIUS["md"],
            "font": self._body_font, "border_width": 0,
        }

    def _row(self, parent, title: str, *, description=None, ultra=False, last=False, tooltip=None):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, SPACING["xs"]))
        row.grid_columnconfigure(0, weight=1)
        labels = ctk.CTkFrame(row, fg_color="transparent")
        labels.grid(row=0, column=0, sticky="ew", pady=SPACING["sm"], padx=(0, SPACING["xl"]))
        heading = ctk.CTkFrame(labels, fg_color="transparent")
        heading.pack(anchor="w")
        label = ctk.CTkLabel(
            heading, text=title, font=self._body_font, height=20,
            text_color=COLORS["text_primary"], anchor="w",
        )
        label.pack(side="left")
        if tooltip and self._tooltip is not None:
            self._tooltip(label, tooltip)
        if ultra:
            ctk.CTkLabel(
                heading, text="ULTRA", height=20, corner_radius=RADIUS["xs"],
                font=(self.app.font_body[0], self.app.font_sizes["caption"]),
                text_color=COLORS["accent_yellow"],
            ).pack(side="left", padx=(SPACING["sm"], 0))
        hint = None
        if description:
            hint = ctk.CTkLabel(
                labels, text=description, font=self._hint_font, justify="left", anchor="w", height=16,
                wraplength=330, text_color=COLORS["text_secondary"],
            )
            hint.pack(anchor="w", pady=(SPACING["xs"], 0))
        control = ctk.CTkFrame(row, fg_color="transparent")
        control.grid(row=0, column=1, sticky="e", pady=SPACING["sm"])
        self._rows.append((row, labels, hint, control))
        if not last:
            ctk.CTkFrame(parent, fg_color=COLORS["border_subtle"], height=1).pack(fill="x")
        return control

    def _menu(self, parent, values, variable, command):
        menu = self._option_menu(
            parent, values=values or ["System default"], variable=variable,
            command=command, width=210, height=36, corner_radius=RADIUS["md"], dynamic_resizing=False,
            fg_color=COLORS["bg_input"], button_color=COLORS["bg_input"],
            button_hover_color=COLORS["bg_elevated"], text_color=COLORS["text_primary"],
            dropdown_fg_color=COLORS["bg_surface"], dropdown_hover_color=COLORS["bg_hover_strong"],
            font=self._body_font, dropdown_font=self._body_font,
        )
        menu.pack(anchor="e")
        return menu

    def _segments(self, parent, values, variable, command):
        control = ctk.CTkSegmentedButton(
            parent, values=list(values), variable=variable, command=command,
            height=36, corner_radius=RADIUS["sm"], border_width=SPACING["xs"],
            fg_color=COLORS["bg_input"], selected_color=COLORS["bg_elevated"],
            selected_hover_color=COLORS["bg_elevated"],
            unselected_color=COLORS["bg_input"], unselected_hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"], font=self._hint_font,
        )
        control.pack(anchor="e")
        return control

    def _switch(self, parent, variable, command):
        switch = ctk.CTkSwitch(
            parent, text="", variable=variable, command=command, width=46,
            switch_width=40, switch_height=22, corner_radius=RADIUS["md"],
            fg_color=COLORS["bg_elevated"], progress_color=COLORS["accent"],
            button_color=COLORS["text_primary"], button_hover_color=COLORS["text_bright"],
        )
        switch.pack(anchor="e")
        return switch

    def _resize(self, event) -> None:
        scaling = self.frame._get_widget_scaling()
        signature = (event.width, scaling)
        if event.width < 60 or signature == self._last_width:
            return
        self._last_width = signature
        available = event.width / scaling - 2 * SPACING["lg"]
        stacked = available < 520
        if stacked != self._stacked:
            self._stacked = stacked
            for _row, labels, _hint, control in self._rows:
                labels.grid_configure(padx=(0, 0 if stacked else SPACING["xl"]))
                control.grid_configure(
                    row=1 if stacked else 0, column=0 if stacked else 1,
                    sticky="w" if stacked else "e",
                    pady=(0, SPACING["md"]) if stacked else SPACING["sm"],
                )
        for _row, _labels, hint, control in self._rows:
            if hint is not None:
                width = available if stacked else available - control.winfo_reqwidth() / scaling - SPACING["xl"]
                hint.configure(wraplength=max(140, int(width)))
        self.microphone.configure(width=min(210, max(120, int(available - 40))))
        self.status_label.configure(wraplength=max(140, int(available)))

    def refresh_microphones(self) -> None:
        options, current = self.app._get_microphone_dropdown_options()
        self.microphone.configure(values=options)
        self.mic_var.set(current)

    def refresh(self) -> None:
        if not self.frame.winfo_exists():
            return
        config = self.app.config
        idle = self.controller.idle()
        local = config.get("processing_mode", "local") == "local"
        ultra_download = bool(getattr(self.app, "_ultra_model_downloading", False))
        model_state = "normal" if idle and local and not (self.controller.downloading or ultra_download) else "disabled"
        self.language_var.set(language_choice(config))
        self.premium_var.set(self.app.feature_gate.has_feature("large_models") and (premium_model_selected(config) or ultra_download))
        self.gpu_var.set(self.app.feature_gate.has_feature("gpu_acceleration") and bool(config.get("use_gpu")))
        self.raw_var.set(bool(config.get("raw_transcription", False)))
        self.indicator_var.set(indicator_choice(config))
        for control, state in (
            (self.language, model_state),
            (self.premium_switch, model_state),
            (self.gpu_switch, "normal" if idle and local else "disabled"),
            (self.microphone, "normal" if idle else "disabled"),
            (self.refresh_mic_button, "normal" if idle else "disabled"),
        ):
            # CTk 5.2 can configure a segmented control's state but cannot
            # cget it. Track only our last paint, not a second preference.
            if self._control_states.get(control, "normal") != state:
                control.configure(state=state)
                self._control_states[control] = state
        armed = getattr(self.app, "_hotkey_capture_target", None) == "record"
        self.hotkey_button.configure(
            text="Press a shortcut…" if armed else self.app.get_hotkey_display(),
            state="disabled" if armed else "normal",
        )
        status = self.controller.status
        if ultra_download:
            percent = getattr(self.app, "_ultra_model_pct", -1)
            progress = f" · {percent}%" if percent >= 0 else ""
            status = f"Downloading the Premium model{progress}"
        if not status and not local:
            status = "Remote transcription is selected. Manage its provider and model in advanced settings."
        self.status_label.configure(text=status)
        if status:
            if not self.status_label.winfo_manager():
                self.status_label.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["lg"]))
        else:
            self.status_label.pack_forget()

    def _shared_bool(self, name: str, value: bool) -> None:
        variable = getattr(self.app, name, None)
        if variable is None:
            variable = ctk.BooleanVar(master=self.frame, value=value)
            setattr(self.app, name, variable)
        else:
            variable.set(value)

    def _toggle_gpu(self) -> None:
        self._shared_bool("gpu_var", self.gpu_var.get())
        self.app.toggle_gpu()
        self.refresh()

    def _toggle_raw(self) -> None:
        self._shared_bool("raw_transcription_var", self.raw_var.get())
        self.app._on_raw_transcription_changed()
        self.refresh()

    def _choose_language(self, value: str) -> None:
        self.controller.choose_model(value, bool(self.premium_var.get()))

    def _choose_premium(self) -> None:
        self.controller.choose_model(self.language_var.get(), bool(self.premium_var.get()))

    def _choose_indicator(self, value: str) -> None:
        self.app.set_indicator_mode(value)
        self.refresh()

    def _choose_microphone(self, value: str) -> None:
        self.app._on_microphone_selected(value)
        self.refresh_microphones()

    def _refresh_devices(self) -> None:
        self.app._refresh_mic_devices()
        self.refresh_microphones()

    def _detect_hotkey(self) -> None:
        self.app._detect_btn_record = self.hotkey_button
        self.app._start_hotkey_detect("record")
        self.refresh()

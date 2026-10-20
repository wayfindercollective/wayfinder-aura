"""A quiet, single-column settings view over Aura's existing handlers.

No recording, licensing, or transcription implementation lives here. The
controller translates the few plain-language choices to the normal settings
and uses the same authenticated downloader and model activation path.
When "Show advanced settings" is enabled, a clearly differentiated Advanced
section unfolds below in the same scrollable page and style.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import sys
import threading

import customtkinter as ctk

from wayfinder import __version__
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

    _LANGUAGE_CODES = ("en", "auto", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "zh", "ja", "ko")

    def __init__(self, app, parent, controller: BasicSettingsController, *, option_menu=ctk.CTkOptionMenu, tooltip=None):
        self.app = app
        self.parent = parent
        self.controller = controller
        self.frame = ctk.CTkFrame(parent, fg_color=COLORS["bg_card"], corner_radius=RADIUS["none"])
        # This responsive grid owns wrapping. The app-wide legacy label fitter
        # must not compete with it (two geometry owners can oscillate forever).
        self.frame._wf_manages_wrap = True
        self.frame.pack(fill="both", expand=True)
        self._rows = []
        self._advanced_rows = []
        self._stacked = None
        self._last_width = None
        self._mic_signature = None
        self._body_font = (app.font_body[0], app.font_sizes["body"])
        self._hint_font = (app.font_body[0], app.font_sizes["small"])
        self._caption_font = (app.font_header[0], app.font_sizes["caption"], "bold")
        self._control_states = {}
        self._option_menu = option_menu
        self._tooltip = tooltip
        self.advanced_section = None
        self._advanced_built = False

        content = ctk.CTkFrame(self.frame, fg_color="transparent")
        content.pack(fill="x", padx=SPACING["lg"], pady=SPACING["sm"])
        self._basic_content = content

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
            content, "Cleanup",
            description="Removes ums and ahs; applies corrections and writing style.",
            tooltip="When on, removes filler words ('um', 'uh'), fixes punctuation, and applies vocabulary corrections or AI style rewriting. Turn off for raw speech-to-text output.",
        )
        self.cleanup_var = ctk.BooleanVar(master=self.frame)
        self.raw_var = ctk.BooleanVar(master=self.frame)
        self.cleanup_switch = self._switch(slot, self.cleanup_var, self._toggle_cleanup)
        self.raw_switch = self.cleanup_switch

        slot = self._row(content, "Recording indicator", last=True)
        self.indicator_var = ctk.StringVar(master=self.frame)
        self.indicator = self._segments(slot, INDICATOR_CHOICES, self.indicator_var, self._choose_indicator)
        self.status_label = ctk.CTkLabel(
            self.frame, text="", anchor="w", justify="left", wraplength=500,
            font=self._hint_font, text_color=COLORS["text_secondary"],
        )

        self.frame.bind("<Configure>", self._resize, add="+")
        parent_canvas = getattr(parent, "_parent_canvas", None)
        if parent_canvas is not None:
            parent_canvas.bind("<Configure>", self._resize, add="+")
        controller.on_change = self.refresh
        self.refresh_microphones()
        self.refresh()

    def _button_style(self) -> dict:
        return {
            "fg_color": COLORS["bg_input"], "hover_color": COLORS["bg_elevated"],
            "text_color": COLORS["text_primary"], "corner_radius": RADIUS["md"],
            "font": self._body_font, "border_width": 0,
        }

    def _row(self, parent, title: str, *, description=None, ultra=False, last=False, tooltip=None, advanced=False):
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
        target_list = self._advanced_rows if advanced else self._rows
        target_list.append((row, labels, hint, control))
        if not last:
            ctk.CTkFrame(parent, fg_color=COLORS["border_subtle"], height=1).pack(fill="x")
        return control

    def _section(self, parent, title: str, *, subtitle: str | None = None, ultra: bool = False):
        card = ctk.CTkFrame(parent, fg_color="transparent", border_width=0)
        card.pack(fill="x", pady=(SPACING["sm"], SPACING["md"]))
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", pady=(SPACING["xs"], SPACING["xs"]))
        ctk.CTkLabel(
            header, text=title.upper(),
            font=self._caption_font,
            text_color=COLORS["accent"],
            anchor="w",
        ).pack(side="left")
        if ultra:
            ctk.CTkLabel(
                header, text="ULTRA",
                font=(self.app.font_body[0], self.app.font_sizes["caption"], "bold"),
                text_color=COLORS["accent_yellow"],
            ).pack(side="left", padx=(SPACING["sm"], 0))
        ctk.CTkFrame(
            header, fg_color=COLORS["border_subtle"], height=1,
        ).pack(side="left", fill="x", expand=True, padx=(SPACING["md"], 0))
        if subtitle:
            ctk.CTkLabel(
                card, text=subtitle,
                font=self._hint_font,
                text_color=COLORS["text_muted"],
                anchor="w", justify="left", wraplength=540,
            ).pack(fill="x", pady=(0, SPACING["xs"]))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x")
        return card, body

    def _menu(self, parent, values, variable, command, *, width=210):
        menu = self._option_menu(
            parent, values=values or ["System default"], variable=variable,
            command=command, width=width, height=36, corner_radius=RADIUS["md"], dynamic_resizing=False,
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

    def _slider(self, parent, from_: float, to: float, variable, command, value_text: str):
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(anchor="e")
        slider = ctk.CTkSlider(
            wrap, from_=from_, to=to, variable=variable, command=command,
            width=150, height=18,
            progress_color=COLORS["accent"],
            button_color=COLORS["text_bright"],
            button_hover_color=COLORS["text_primary"],
            fg_color=COLORS["bg_input"],
        )
        slider.pack(side="left")
        label = ctk.CTkLabel(
            wrap, text=value_text, width=52, anchor="e",
            font=self._body_font, text_color=COLORS["accent"],
        )
        label.pack(side="left", padx=(SPACING["sm"], 0))
        return slider, label

    def _all_responsive_rows(self):
        return [*self._rows, *self._advanced_rows]

    def _resize(self, event) -> None:
        scaling = self.frame._get_widget_scaling()
        signature = (event.width, scaling, len(self._advanced_rows))
        if event.width < 60 or signature == self._last_width:
            return
        self._last_width = signature
        available = event.width / scaling - 2 * SPACING["lg"]
        stacked = available < 520
        all_rows = self._all_responsive_rows()
        if stacked != self._stacked:
            self._stacked = stacked
        for _row, labels, _hint, control in all_rows:
            if not _row.winfo_exists():
                continue
            labels.grid_configure(padx=(0, 0 if stacked else SPACING["xl"]))
            control.grid_configure(
                row=1 if stacked else 0, column=0 if stacked else 1,
                sticky="w" if stacked else "e",
                pady=(0, SPACING["md"]) if stacked else SPACING["sm"],
            )
        for _row, _labels, hint, control in all_rows:
            if hint is not None and hint.winfo_exists():
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
        raw = bool(config.get("raw_transcription", False))
        self.raw_var.set(raw)
        self.cleanup_var.set(not raw)
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
                if self.advanced_section is not None and self.advanced_section.winfo_manager():
                    self.status_label.pack(
                        fill="x", padx=SPACING["lg"], pady=(0, SPACING["lg"]),
                        before=self.advanced_section,
                    )
                else:
                    self.status_label.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["lg"]))
        else:
            self.status_label.pack_forget()

        show_advanced = bool(config.get("advanced_mode", False))
        if show_advanced:
            self._ensure_advanced_section()
            if self.advanced_section is not None and not self.advanced_section.winfo_manager():
                self.advanced_section.pack(fill="x", padx=SPACING["lg"], pady=(SPACING["sm"], SPACING["lg"]))
            self._refresh_advanced_controls()
        elif self.advanced_section is not None and self.advanced_section.winfo_manager():
            self.advanced_section.pack_forget()

    def scroll_to_advanced(self) -> None:
        """Scroll down slightly so the Advanced Settings differentiator is in view."""
        canvas = getattr(self.parent, "_parent_canvas", None)
        if canvas is None:
            return
        try:
            self.frame.update_idletasks()
            canvas.yview_moveto(0.14)
        except Exception:
            pass

    def scroll_to_top(self) -> None:
        canvas = getattr(self.parent, "_parent_canvas", None)
        if canvas is None:
            return
        try:
            canvas.yview_moveto(0.0)
        except Exception:
            pass

    # ── Advanced Settings Section (same clean row style, clearly differentiated) ──

    def _ensure_advanced_section(self) -> None:
        if self._advanced_built and self.advanced_section is not None and self.advanced_section.winfo_exists():
            return
        self._advanced_built = True
        self._advanced_rows.clear()
        adv = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.advanced_section = adv

        # Clear visual differentiator between Basic Settings and Advanced Settings
        divider_wrap = ctk.CTkFrame(adv, fg_color="transparent")
        divider_wrap.pack(fill="x", pady=(SPACING["md"], SPACING["md"]))
        ctk.CTkFrame(divider_wrap, fg_color=COLORS["accent_dim"], height=1).pack(fill="x", pady=(0, SPACING["md"]))

        banner = ctk.CTkFrame(
            divider_wrap,
            fg_color=COLORS["bg_surface"],
            corner_radius=RADIUS["md"],
            border_width=1,
            border_color=COLORS["accent_dim"],
        )
        banner.pack(fill="x")
        banner_inner = ctk.CTkFrame(banner, fg_color="transparent")
        banner_inner.pack(fill="x", padx=SPACING["lg"], pady=SPACING["md"])
        ctk.CTkLabel(
            banner_inner,
            text="ADVANCED SETTINGS",
            font=self._caption_font,
            text_color=COLORS["accent"],
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            banner_inner,
            text="Additional controls for audio, models, vocabulary, hotkeys, indicator placement, benchmark, and updates.",
            font=self._hint_font,
            text_color=COLORS["text_secondary"],
            anchor="w",
            justify="left",
            wraplength=520,
        ).pack(anchor="w", pady=(SPACING["xs"], 0))

        self._build_advanced_audio_section(adv)
        self._build_advanced_models_section(adv)
        self._build_advanced_vocabulary_section(adv)
        self._build_advanced_system_section(adv)
        self._build_advanced_indicator_section(adv)
        self._build_advanced_gamer_section(adv)
        self._build_advanced_benchmark_section(adv)
        self._build_advanced_updates_and_license_section(adv)
        self._last_width = None

    def _build_advanced_audio_section(self, parent) -> None:
        _card, body = self._section(parent, "Audio")

        slot = self._row(
            body, "Audio processing",
            description="Volume leveling and noise filtering before transcription.",
            advanced=True,
        )
        current_prep = str(self.app.config.get("audio_preprocessing", "light")).capitalize()
        self.preprocess_var = ctk.StringVar(master=self.frame, value=current_prep)
        self.app.preprocess_var = self.preprocess_var
        self.preprocess_menu = self._menu(
            slot, ["Off", "Light", "Medium", "Heavy"],
            self.preprocess_var, self._choose_audio_processing,
        )

        slot = self._row(
            body, "Audio ducking",
            description="Lower music and other audio already playing while recording.",
            advanced=True,
        )
        self.ducking_var = ctk.BooleanVar(
            master=self.frame, value=bool(self.app.config.get("audio_ducking_enabled", True)),
        )
        self.app.audio_ducking_var = self.ducking_var
        self.ducking_switch = self._switch(slot, self.ducking_var, self._toggle_audio_ducking)

        slot = self._row(
            body, "Duck amount",
            description="How much to lower other audio while recording (100% mutes it).",
            last=True, advanced=True,
        )
        duck_pct = int(self.app.config.get("audio_ducking_percent", 30))
        self.duck_percent_var = ctk.DoubleVar(master=self.frame, value=duck_pct)
        self.app.duck_percent_slider_var = self.duck_percent_var
        self.duck_slider, self.duck_value_label = self._slider(
            slot, 0, 100, self.duck_percent_var, self._on_duck_slider, f"{duck_pct}%",
        )
        self.app.duck_percent_slider = self.duck_slider
        self.app.duck_percent_value_label = self.duck_value_label

    def _build_advanced_models_section(self, parent) -> None:
        _card, body = self._section(parent, "Models & Processing")

        slot = self._row(
            body, "Processing mode",
            description="100% private on-device processing or cloud API transcription.",
            ultra=True, advanced=True,
        )
        mode_val = "Remote" if self.app.config.get("processing_mode", "local") == "remote" else "Local"
        self.proc_mode_var = ctk.StringVar(master=self.frame, value=mode_val)
        self.proc_mode_segments = self._segments(
            slot, ["Local", "Remote"], self.proc_mode_var, self._choose_processing_mode,
        )

        self.mode_container = ctk.CTkFrame(body, fg_color="transparent")
        self.mode_container.pack(fill="x")
        self.app.mode_settings_container = self.mode_container
        self.rebuild_processing_mode_rows(self.app.config.get("processing_mode", "local"))

    def rebuild_processing_mode_rows(self, mode: str) -> None:
        container = getattr(self, "mode_container", None)
        if container is None or not container.winfo_exists():
            return
        self._advanced_rows = [
            item for item in self._advanced_rows
            if item[0].winfo_exists() and not str(item[0]).startswith(str(container))
        ]
        for child in container.winfo_children():
            child.destroy()

        if mode == "remote":
            self._build_remote_processing_rows(container)
        else:
            self._build_local_processing_rows(container)
        self._last_width = None

    def _build_local_processing_rows(self, parent) -> None:
        slot = self._row(
            parent, "Whisper model catalog",
            description="Browse, download, or select any local Whisper model.",
            advanced=True,
        )
        model_text = self.app.get_model_display() if hasattr(self.app, "get_model_display") else "Choose model…"
        self.whisper_model_button = ctk.CTkButton(
            slot, text=model_text, width=210, height=36,
            command=self._open_whisper_models, **self._button_style(),
        )
        self.whisper_model_button.pack(anchor="e")
        self.app.model_btn = self.whisper_model_button

        slot = self._row(
            parent, "Language",
            description="Force a specific spoken language or auto-detect.",
            advanced=True,
        )
        lang_val = str(self.app.config.get("language", "en"))
        self.adv_language_var = ctk.StringVar(master=self.frame, value=lang_val)
        self.app.language_var = self.adv_language_var
        self.adv_language_menu = self._menu(
            slot, list(self._LANGUAGE_CODES), self.adv_language_var, self._choose_specific_language,
        )

        slot = self._row(
            parent, "Transcription backend",
            description="Local speech recognition engine.",
            advanced=True,
        )
        backend = self.app.config.get("transcription_backend", "whisper_cpp")
        if backend in ("openai_whisper", "groq_whisper"):
            backend = "whisper_cpp"
        backend_map = {
            "Auto (whisper.cpp)": "auto",
            "whisper.cpp": "whisper_cpp",
        }
        try:
            from wayfinder.utils.gpu import get_gpu_info
            if get_gpu_info().is_nvidia or backend == "faster_whisper":
                backend_map["Faster-Whisper (experimental)"] = "faster_whisper"
        except Exception:
            if backend == "faster_whisper":
                backend_map["Faster-Whisper (experimental)"] = "faster_whisper"
        self.app._backend_display_map = backend_map
        self.app._backend_id_to_display = {v: k for k, v in backend_map.items()}
        backend_display = (
            "Auto (whisper.cpp)"
            if self.app.config.get("transcription_backend_auto", True)
            else self.app._backend_id_to_display.get(backend, "whisper.cpp")
        )
        self.backend_var = ctk.StringVar(master=self.frame, value=backend_display)
        self.app.backend_var = self.backend_var
        self.backend_menu = self._menu(
            slot, list(backend_map.keys()), self.backend_var, self.app.on_backend_changed,
        )

        slot = self._row(
            parent, "Chunk processing",
            description="Splits long dictations into segments so text appears faster.",
            ultra=True, advanced=True,
        )
        chunked_unlocked = self.app.feature_gate.has_feature("chunked_recording")
        raw_chunk = str(self.app.config.get("chunked_mode", "off")).lower()
        if raw_chunk not in ("off", "auto", "on") or not chunked_unlocked:
            raw_chunk = "off"
        chunk_map = {"Off": "off", "Auto (Recommended)": "auto", "On": "on"}
        chunk_rev = {v: k for k, v in chunk_map.items()}
        self.app._chunked_display_to_mode = chunk_map
        self.app._chunked_mode_to_display = chunk_rev
        self.chunked_var = ctk.StringVar(master=self.frame, value=chunk_rev.get(raw_chunk, "Off"))
        self.app.chunked_mode_var = self.chunked_var
        self.chunked_menu = self._menu(
            slot, list(chunk_map.keys()), self.chunked_var, self.app.on_chunked_mode_changed,
        )

        slot = self._row(
            parent, "Remove repetition loops",
            description="Removes near-duplicate phrases if speech recognition loops.",
            advanced=True,
        )
        self.repetition_var = ctk.BooleanVar(
            master=self.frame, value=bool(self.app.config.get("collapse_whisper_repetitions", True)),
        )
        self.app.repetition_cleanup_var = self.repetition_var
        self.repetition_switch = self._switch(slot, self.repetition_var, self._toggle_repetition)

        postproc_on = bool(self.app.config.get("post_processing_enabled", False))
        tone_unlocked = bool(
            hasattr(self.app, "feature_gate") and self.app.feature_gate.has_feature("tone_system")
        )
        slot = self._row(
            parent, "AI post-processing",
            description="Use a local LLM to rewrite dictated text for writing styles.",
            last=not (postproc_on and tone_unlocked), advanced=True,
        )
        self.postproc_var = ctk.BooleanVar(master=self.frame, value=postproc_on)
        self.app.postproc_enabled_var = self.postproc_var
        self.postproc_switch = self._switch(slot, self.postproc_var, self._toggle_postproc)

        if postproc_on and tone_unlocked:
            if hasattr(self.app, "_build_inline_model_section"):
                llm_wrap = ctk.CTkFrame(parent, fg_color="transparent")
                llm_wrap.pack(fill="x", pady=(SPACING["xs"], SPACING["xs"]))
                self.app._build_inline_model_section(llm_wrap, "llama_cpp")
                ctk.CTkFrame(parent, fg_color=COLORS["border_subtle"], height=1).pack(fill="x")

            residency_map = {
                "Instant (keeps model loaded)": "auto",
                "Save memory (loads per use)": "save_memory",
            }
            residency_rev = {
                "auto": "Instant (keeps model loaded)",
                "instant": "Instant (keeps model loaded)",
                "save_memory": "Save memory (loads per use)",
            }
            self.app._residency_map = residency_map
            self.app._residency_reverse = residency_rev
            cur_res = str(self.app.config.get("llama_cpp_residency", "auto"))
            slot = self._row(
                parent, "Cleanup speed",
                description="Keep the cleanup model in memory for instant rewrites, or unload after each use.",
                last=True, advanced=True,
            )
            self.residency_var = ctk.StringVar(
                master=self.frame,
                value=residency_rev.get(cur_res, "Instant (keeps model loaded)"),
            )
            self.app.residency_var = self.residency_var
            self.residency_menu = self._menu(
                slot, list(residency_map.keys()), self.residency_var, self.app._on_residency_changed,
                width=230,
            )

    def _persist_config(self) -> None:
        wm = sys.modules.get("wayfinder_main")
        saver = getattr(wm, "save_config", None) if wm is not None else None
        if callable(saver):
            saver(self.app.config)
            return
        from wayfinder.config import save_config

        save_config(self.app.config)

    def _build_remote_processing_rows(self, parent) -> None:
        provider_map = {"Groq (Fast)": "groq_whisper", "OpenAI": "openai_whisper"}
        provider_rev = {v: k for k, v in provider_map.items()}
        self.app._remote_provider_map = provider_map
        self.app._remote_provider_reverse = provider_rev
        cur_backend = self.app.config.get("transcription_backend", "groq_whisper")
        if cur_backend not in provider_rev:
            cur_backend = "groq_whisper"
        is_groq_asr = cur_backend == "groq_whisper"

        slot = self._row(
            parent, "Speech-to-text provider",
            description="Cloud service that transcribes your voice.",
            advanced=True,
        )
        self.remote_provider_var = ctk.StringVar(
            master=self.frame, value=provider_rev.get(cur_backend, "Groq (Fast)"),
        )
        self.app.remote_provider_var = self.remote_provider_var
        self.remote_provider_menu = self._menu(
            slot, list(provider_map.keys()), self.remote_provider_var, self._choose_remote_provider,
        )

        if is_groq_asr:
            groq_asr_map = {
                "Whisper Large v3": "whisper-large-v3",
                "Whisper Large v3 Turbo": "whisper-large-v3-turbo",
            }
            groq_asr_rev = {v: k for k, v in groq_asr_map.items()}
            cur_groq_asr = self.app.config.get("groq_whisper_model", "whisper-large-v3")
            slot = self._row(
                parent, "Groq speech model",
                description="Whisper model running on Groq's LPU hardware.",
                advanced=True,
            )
            self.groq_asr_model_var = ctk.StringVar(
                master=self.frame,
                value=groq_asr_rev.get(cur_groq_asr, "Whisper Large v3"),
            )

            def _pick_groq_asr(label: str):
                model_id = groq_asr_map.get(label, "whisper-large-v3")
                self.app.config["groq_whisper_model"] = model_id
                self._persist_config()
                if hasattr(self.app, "log"):
                    self.app.log(f"⚙ Groq speech model set to: {model_id}")

            self._menu(slot, list(groq_asr_map.keys()), self.groq_asr_model_var, _pick_groq_asr, width=220)
        else:
            slot = self._row(
                parent, "OpenAI speech model",
                description="OpenAI's hosted Whisper transcription model.",
                advanced=True,
            )
            ctk.CTkLabel(
                slot, text="whisper-1", font=self._body_font, text_color=COLORS["text_secondary"],
            ).pack(anchor="e")

        slot = self._row(
            parent, "Specific language",
            description="Force a specific spoken language or auto-detect.",
            advanced=True,
        )
        lang_val = str(self.app.config.get("language", "en"))
        self.adv_language_var = ctk.StringVar(master=self.frame, value=lang_val)
        self.app.language_var = self.adv_language_var
        self.adv_language_menu = self._menu(
            slot, list(self._LANGUAGE_CODES), self.adv_language_var, self._choose_specific_language,
        )

        groq_key = self.app.config.get("groq_api_key", "") or os.environ.get("GROQ_API_KEY", "")
        openai_key = self.app.config.get("openai_api_key", "") or os.environ.get("OPENAI_API_KEY", "")
        api_key = groq_key if is_groq_asr else openai_key
        status = "Configured ✓" if api_key else "Configure key…"
        key_title = "Groq API key" if is_groq_asr else "OpenAI API key"

        slot = self._row(
            parent, key_title,
            description=f"API key used for {'Groq' if is_groq_asr else 'OpenAI'} cloud requests.",
            advanced=True,
        )
        self.remote_api_btn = ctk.CTkButton(
            slot, text=status, width=210, height=36,
            command=self.app.open_remote_api_settings, **self._button_style(),
        )
        self.remote_api_btn.pack(anchor="e")
        self.app.remote_api_btn = self.remote_api_btn

        postproc_on = bool(self.app.config.get("post_processing_enabled", False))
        slot = self._row(
            parent, "AI post-processing",
            description="Use a cloud LLM to rewrite transcriptions for cleanup and styles.",
            last=not postproc_on, advanced=True,
        )
        self.postproc_var = ctk.BooleanVar(master=self.frame, value=postproc_on)
        self.app.postproc_enabled_var = self.postproc_var
        self.postproc_switch = self._switch(slot, self.postproc_var, self._toggle_postproc)

        if postproc_on:
            pp_backend = self.app.config.get("post_processing_backend", "")
            # Default to Groq when Groq is active or has a key configured and OpenAI does not.
            # Never call save_config during UI construction.
            if pp_backend not in ("groq", "openai", "anthropic"):
                pp_backend = "groq" if (is_groq_asr or (groq_key and not openai_key)) else "openai"
                self.app.config["post_processing_backend"] = pp_backend
            elif pp_backend == "openai" and is_groq_asr and groq_key and not openai_key:
                pp_backend = "groq"
                self.app.config["post_processing_backend"] = "groq"

            llm_provider_map = {
                "Groq (uses Groq key)": "groq",
                "OpenAI": "openai",
                "Anthropic": "anthropic",
            }
            llm_provider_rev = {v: k for k, v in llm_provider_map.items()}

            slot = self._row(
                parent, "Cleanup LLM provider",
                description="Which cloud service runs text cleanup and writing styles.",
                advanced=True,
            )
            self.postproc_backend_var = ctk.StringVar(
                master=self.frame,
                value=llm_provider_rev.get(pp_backend, "Groq (uses Groq key)"),
            )
            self.app.postproc_backend_var = self.postproc_backend_var

            def _pick_llm_provider(label: str):
                backend_id = llm_provider_map.get(label, "groq")
                self.app.config["post_processing_backend"] = backend_id
                self._persist_config()
                if hasattr(self.app, "on_postproc_backend_changed"):
                    try:
                        self.app.on_postproc_backend_changed(backend_id)
                    except Exception:
                        pass
                self.rebuild_processing_mode_rows("remote")

            self._menu(
                slot, list(llm_provider_map.keys()),
                self.postproc_backend_var, _pick_llm_provider, width=220,
            )

            if pp_backend == "groq":
                groq_llm_map = {
                    "Llama 3.3 70B Versatile": "llama-3.3-70b-versatile",
                    "Llama 3.1 8B Instant": "llama-3.1-8b-instant",
                }
                groq_llm_rev = {v: k for k, v in groq_llm_map.items()}
                cur_groq_llm = self.app.config.get("groq_llm_model", "llama-3.3-70b-versatile")
                slot = self._row(
                    parent, "Groq cleanup model",
                    description="Shares your Groq API key above — no extra key needed.",
                    last=True, advanced=True,
                )
                self.groq_llm_model_var = ctk.StringVar(
                    master=self.frame,
                    value=groq_llm_rev.get(cur_groq_llm, "Llama 3.3 70B Versatile"),
                )

                def _pick_groq_llm(label: str):
                    model_id = groq_llm_map.get(label, "llama-3.3-70b-versatile")
                    self.app.config["groq_llm_model"] = model_id
                    self._persist_config()
                    if hasattr(self.app, "log"):
                        self.app.log(f"⚙ Groq cleanup model set to: {model_id}")

                self._menu(slot, list(groq_llm_map.keys()), self.groq_llm_model_var, _pick_groq_llm, width=220)
            else:
                from wayfinder.core.cloud_keys import ANTHROPIC_CLEANUP_MODELS, OPENAI_CLEANUP_MODELS

                is_oai = pp_backend == "openai"
                models = OPENAI_CLEANUP_MODELS if is_oai else ANTHROPIC_CLEANUP_MODELS
                cfg_key = "openai_model" if is_oai else "anthropic_model"
                cur_m = self.app.config.get(cfg_key, models[0])
                if cur_m not in models:
                    cur_m = models[0]
                slot = self._row(
                    parent, "OpenAI cleanup model" if is_oai else "Anthropic cleanup model",
                    description="Cloud LLM used for cleanup and styles.",
                    advanced=True,
                )
                self.cloud_llm_model_var = ctk.StringVar(master=self.frame, value=cur_m)

                def _pick_cloud_llm(model_id: str, k=cfg_key):
                    self.app.config[k] = model_id
                    self._persist_config()

                self._menu(slot, list(models), self.cloud_llm_model_var, _pick_cloud_llm, width=220)

                llm_key = openai_key if is_oai else (
                    self.app.config.get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
                )
                llm_status = "Configured ✓" if llm_key else "Configure key…"
                slot = self._row(
                    parent, "OpenAI API key" if is_oai else "Anthropic API key",
                    description="Configure API key for cloud LLM cleanup.",
                    last=True, advanced=True,
                )
                self.postproc_config_btn = ctk.CTkButton(
                    slot, text=llm_status, width=210, height=36,
                    command=self.app.open_postproc_settings, **self._button_style(),
                )
                self.postproc_config_btn.pack(anchor="e")
                self.app.postproc_config_btn = self.postproc_config_btn

    def _build_advanced_vocabulary_section(self, parent) -> None:
        vocab_card = ctk.CTkFrame(parent, fg_color="transparent", border_width=0)
        vocab_card.pack(fill="x", pady=(0, SPACING["md"]))
        self.app._vocabulary_tile = vocab_card
        if hasattr(self.app, "_render_vocabulary_tile"):
            self.app._render_vocabulary_tile()

    def _build_advanced_system_section(self, parent) -> None:
        _card, body = self._section(parent, "Hotkey & System")

        if hasattr(self.app, "_hotkey_key_codes"):
            key_codes = self.app._hotkey_key_codes
        else:
            try:
                from wayfinder_main import hotkey_key_options
                key_codes = hotkey_key_options()
            except Exception:
                key_codes = {"F9": 67}
            self.app._hotkey_key_codes = key_codes
            self.app._hotkey_code_to_name = {v: k for k, v in key_codes.items()}

        code_to_name = getattr(self.app, "_hotkey_code_to_name", {v: k for k, v in key_codes.items()})
        cur_code = self.app.config.get("hotkey_key", 67)
        cur_key_name = code_to_name.get(cur_code, next(iter(key_codes.keys()), "F9"))

        slot = self._row(
            body, "Hotkey key",
            description="Choose a key from the list, or use the Hotkey button above to detect any key or mouse button.",
            advanced=True,
        )
        self.hotkey_key_var = ctk.StringVar(master=self.frame, value=cur_key_name)
        self.app._hotkey_key_var = self.hotkey_key_var
        self.hotkey_key_menu = self._menu(
            slot, list(key_codes.keys()), self.hotkey_key_var, self._choose_hotkey_key,
        )

        slot = self._row(
            body, "Hotkey modifiers",
            description="Require modifier keys to be held with your hotkey.",
            advanced=True,
        )
        mod_wrap = ctk.CTkFrame(slot, fg_color="transparent")
        mod_wrap.pack(anchor="e")
        cur_mods = self.app.config.get("hotkey_modifiers", [])
        mod_options = ["fn", "super", "ctrl", "alt", "shift"] if sys.platform == "darwin" else ["ctrl", "alt", "shift"]
        self.app._hotkey_mod_vars = {}
        self.app._hotkey_mod_checks = {}
        for mod in mod_options:
            var = ctk.BooleanVar(master=self.frame, value=mod in cur_mods)
            self.app._hotkey_mod_vars[mod] = var
            cb = ctk.CTkCheckBox(
                mod_wrap, text=mod.capitalize(), variable=var,
                font=self._hint_font, text_color=COLORS["text_primary"],
                fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
                checkmark_color="#000000", width=24,
                command=self._on_hotkey_mod_toggled,
            )
            cb.pack(side="left", padx=(SPACING["sm"], 0))
            self.app._hotkey_mod_checks[mod] = cb

        # Hotkey input device
        slot = self._row(
            body, "Hotkey input device",
            description="Listen on all keyboards and mice, or restrict to one device.",
            advanced=True,
        )
        device_options, cur_device = self._get_hotkey_device_options()
        self.device_var = ctk.StringVar(master=self.frame, value=cur_device)
        self.app._device_var = self.device_var
        self.device_menu = self._menu(
            slot, device_options, self.device_var, self.app._on_device_selected,
        )

        slot = self._row(
            body, "Auto press Enter",
            description="Press Enter automatically after dictated text is typed so chat messages send hands-free.",
            advanced=True,
        )
        self.auto_enter_var = ctk.BooleanVar(
            master=self.frame, value=bool(self.app.config.get("press_enter_after_dictation", False)),
        )
        self.app._auto_enter_var = self.auto_enter_var
        self.auto_enter_switch = self._switch(slot, self.auto_enter_var, self.app._on_auto_enter_toggled)

        if sys.platform.startswith("linux"):
            try:
                from wayfinder.core.portal_keyboard import host_is_wayland_desktop
                is_wayland = host_is_wayland_desktop()
            except Exception:
                is_wayland = False
            if is_wayland:
                slot = self._row(
                    body, "Type into every app",
                    description="Use the desktop portal so dictation reaches native Wayland and X11 apps.",
                    advanced=True,
                )
                self.portal_typing_var = ctk.BooleanVar(
                    master=self.frame, value=bool(self.app.config.get("linux_portal_typing", True)),
                )
                self.app._portal_typing_var = self.portal_typing_var
                self.portal_typing_switch = self._switch(
                    slot, self.portal_typing_var, self.app._on_portal_typing_toggled,
                )

        slot = self._row(
            body, "UI scale",
            description="Scale the Settings window text and controls.",
            last=True, advanced=True,
        )
        ui_scale = float(getattr(self.app, "ui_scale", self.app.config.get("ui_scale", 1.0)) or 1.0)
        self.ui_scale_var = ctk.DoubleVar(master=self.frame, value=ui_scale)
        self.app.scale_slider_var = self.ui_scale_var
        self.ui_scale_slider, self.ui_scale_label = self._slider(
            slot, 0.7, 2.5, self.ui_scale_var, self._on_ui_scale_slider, f"{int(round(ui_scale * 100))}%",
        )
        self.app.scale_slider = self.ui_scale_slider
        self.app.scale_value_label = self.ui_scale_label

    def _build_advanced_indicator_section(self, parent) -> None:
        _card, body = self._section(parent, "Recording Indicator")

        slot = self._row(
            body, "Indicator size",
            description="Scale the floating recording indicator pill.",
            advanced=True,
        )
        ov_scale = float(self.app.config.get("overlay_scale", 1.0) or 1.0)
        self.ov_scale_var = ctk.DoubleVar(master=self.frame, value=ov_scale)
        self.app.overlay_scale_slider_var = self.ov_scale_var
        self.ov_scale_slider, self.ov_scale_label = self._slider(
            slot, 0.5, 2.0, self.ov_scale_var, self._on_ov_scale_slider, f"{int(round(ov_scale * 100))}%",
        )
        self.app.overlay_scale_slider = self.ov_scale_slider
        self.app.overlay_scale_value_label = self.ov_scale_label

        slot = self._row(
            body, "Indicator placement",
            description="Choose which screen corner or edge the indicator sits at.",
            advanced=True,
        )
        grid = ctk.CTkFrame(slot, fg_color="transparent")
        grid.pack(anchor="e")
        self.app._anchor_buttons = {}
        current_anchor = self.app.config.get("overlay_anchor", "bottom-center")
        layout = [
            [("top-left", "↖"), ("top-center", "↑"), ("top-right", "↗")],
            [("bottom-left", "↙"), ("bottom-center", "↓"), ("bottom-right", "↘")],
        ]
        for r, rowdef in enumerate(layout):
            for c, (anchor, glyph) in enumerate(rowdef):
                btn = ctk.CTkButton(
                    grid, text=glyph, width=32, height=28,
                    fg_color=COLORS["accent"] if anchor == current_anchor else COLORS["bg_input"],
                    hover_color=COLORS["accent"],
                    text_color=COLORS["text_bright"],
                    corner_radius=RADIUS["sm"],
                    command=lambda a=anchor: self.app._on_overlay_anchor_selected(a),
                )
                btn.grid(row=r, column=c, padx=2, pady=2)
                self.app._anchor_buttons[anchor] = btn

        slot = self._row(
            body, "Indicator vertical offset",
            description="Fine-tune the indicator's vertical position from its anchor.",
            advanced=True,
        )
        ov_offset = int(self.app.config.get("overlay_vertical_offset", 0))
        self.ov_offset_var = ctk.DoubleVar(master=self.frame, value=ov_offset)
        self.app.overlay_position_slider_var = self.ov_offset_var
        self.ov_offset_slider, self.ov_offset_label = self._slider(
            slot, -900, 120, self.ov_offset_var, self._on_ov_offset_slider, f"{ov_offset}px",
        )
        self.app.overlay_position_slider = self.ov_offset_slider
        self.app.overlay_position_value_label = self.ov_offset_label

        try:
            from wayfinder.utils.platform import supports_steam_game_mode
            has_game_mode = supports_steam_game_mode()
        except Exception:
            has_game_mode = False

        slot = self._row(
            body, "Animate idle waveform",
            description="Animate the ambient wave while idle; turn off to save battery.",
            last=not has_game_mode, advanced=True,
        )
        self.ov_quality_var = ctk.BooleanVar(
            master=self.frame, value=self.app.config.get("overlay_quality", "high") != "performance",
        )
        self.app.overlay_quality_var = self.ov_quality_var
        self.ov_quality_switch = self._switch(slot, self.ov_quality_var, self.app._on_overlay_quality_toggled)

        if has_game_mode:
            slot = self._row(
                body, "Game Mode dictation",
                description="Keep voice dictation active in SteamOS Game Mode with audio cues.",
                last=True, advanced=True,
            )
            self.game_mode_var = ctk.BooleanVar(
                master=self.frame, value=bool(self.app.config.get("game_mode_dictation", False)),
            )
            self.app.game_mode_dictation_var = self.game_mode_var
            self._switch(slot, self.game_mode_var, self.app._on_game_mode_dictation_toggled)

    def _build_advanced_gamer_section(self, parent) -> None:
        _card, body = self._section(parent, "Gamer Mode")

        slot = self._row(
            body, "Gamer mode",
            description="Optimize dictation for in-game chat (recognizes gamer terms and pastes cleanly).",
            advanced=True,
        )
        self.gamer_mode_var = ctk.BooleanVar(
            master=self.frame, value=bool(self.app.config.get("gamer_mode", True)),
        )
        self.app.game_chat_var = self.gamer_mode_var
        self._switch(slot, self.gamer_mode_var, self._toggle_gamer_mode)

        slot = self._row(
            body, "Send game messages automatically",
            description="Press Enter automatically in game chat, or leave messages in the chat box.",
            advanced=True,
        )
        self.game_send_var = ctk.BooleanVar(
            master=self.frame, value=bool(self.app.config.get("game_chat_send", True)),
        )
        self.app.game_chat_send_var = self.game_send_var
        self._switch(slot, self.game_send_var, self._toggle_game_send)

        slot = self._row(
            body, "Compatible games",
            description="Search tested games and see how Aura works with each one.",
            last=True, advanced=True,
        )
        ctk.CTkButton(
            slot, text="Browse games…", width=160, height=36,
            command=lambda: self.app._switch_tab("games"), **self._button_style(),
        ).pack(anchor="e")

    def _build_advanced_benchmark_section(self, parent) -> None:
        _card, body = self._section(
            parent, "Benchmark",
            subtitle="Times speech-to-text and cleanup warmed up on a 10-second clip across CPU and GPU.",
        )

        hw_frame = ctk.CTkFrame(body, fg_color=COLORS["bg_input"], corner_radius=RADIUS["sm"])
        hw_frame.pack(fill="x", pady=(SPACING["xs"], SPACING["sm"]))
        hw_label = ctk.CTkLabel(
            hw_frame,
            text="CPU: Detecting…\nGPU: Detecting…\nRAM: Detecting…",
            font=self._hint_font,
            text_color=COLORS["text_secondary"],
            justify="left", anchor="w",
        )
        hw_label.pack(anchor="w", padx=SPACING["md"], pady=SPACING["sm"])

        def _probe_hw():
            try:
                from wayfinder.core.benchmark import BenchmarkRunner
                detected = BenchmarkRunner.get_system_info()
            except Exception:
                return

            def _apply():
                if not hw_label.winfo_exists():
                    return
                cpu = detected.get("cpu", "Unknown")
                gpu = detected.get("gpu", "Unknown")
                ram = detected.get("ram", "Unknown")
                text = f"CPU: {cpu[:55]}{'…' if len(cpu) > 55 else ''}\n"
                text += f"GPU: {gpu[:55]}{'…' if len(gpu) > 55 else ''}\n"
                text += f"RAM: {ram}"
                hw_label.configure(text=text)

            self.app.event_queue.put((EventType.UI_CALLBACK, _apply))

        threading.Thread(target=_probe_hw, daemon=True, name="settings-hw-probe").start()

        self.app.benchmark_results_frame = ctk.CTkFrame(body, fg_color="transparent")
        self.app.benchmark_results_frame.pack(fill="x", pady=(0, SPACING["sm"]))
        if hasattr(self.app, "_update_benchmark_results_display"):
            self.app._update_benchmark_results_display()

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, SPACING["xs"]))
        self.app.benchmark_test_btn = ctk.CTkButton(
            btn_row,
            text="Compare CPU vs GPU",
            font=(self.app.font_body[0], self.app.font_sizes["body"], "bold"),
            height=36, width=180,
            corner_radius=RADIUS["md"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color="#000000",
            command=getattr(self.app, "_run_inline_benchmark", None),
        )
        self.app.benchmark_test_btn.pack(side="left")

        self.app.benchmark_status_label = ctk.CTkLabel(
            btn_row, text="", font=self._hint_font, text_color=COLORS["text_muted"],
        )
        self.app.benchmark_status_label.pack(side="left", padx=(SPACING["md"], 0))

        self.app.benchmark_progress = ctk.CTkProgressBar(
            body, height=4, mode="indeterminate",
            progress_color=COLORS["accent"], fg_color=COLORS["bg_input"],
        )

    def _build_advanced_updates_and_license_section(self, parent) -> None:
        _card, body = self._section(parent, "Updates & Diagnostics")

        try:
            from wayfinder.core.app_updates import channel_label
            cur_channel = channel_label(__version__, self.app.config.get("update_channel"))
        except Exception:
            cur_channel = "Beta" if "beta" in __version__ else "Stable"

        slot = self._row(
            body, "Update channel",
            description="Receive stable releases or early beta previews.",
            advanced=True,
        )
        self.update_channel_var = ctk.StringVar(master=self.frame, value=cur_channel)
        self.app._update_channel_var = self.update_channel_var
        self._segments(
            slot, ["Stable", "Beta"], self.update_channel_var, self.app._on_update_channel_changed,
        )

        slot = self._row(
            body, f"Installed version (v{__version__})",
            description="Check for a newer version of Wayfinder Aura and install in place.",
            advanced=True,
        )
        ctk.CTkButton(
            slot, text="Check for updates", width=170, height=36,
            command=getattr(self.app, "_check_app_update_now", None), **self._button_style(),
        ).pack(anchor="e")

        try:
            from wayfinder.core.crash_reports import reports_enabled
            crash_on = reports_enabled(self.app.config, __version__)
        except Exception:
            crash_on = False

        slot = self._row(
            body, "Send crash reports",
            description="Automatically send anonymous crash reports to help improve Aura.",
            advanced=True,
        )
        self.crash_reports_var = ctk.BooleanVar(master=self.frame, value=crash_on)
        self.app._crash_reports_var = self.crash_reports_var
        self._switch(slot, self.crash_reports_var, self.app._on_crash_reports_toggled)

        slot = self._row(
            body, "Diagnostics & feedback",
            description="Inspect the activity log or send feedback directly to the team.",
            last=True, advanced=True,
        )
        actions = ctk.CTkFrame(slot, fg_color="transparent")
        actions.pack(anchor="e")
        ctk.CTkButton(
            actions, text="Activity log", width=120, height=36,
            command=lambda: self.app._switch_tab("history"), **self._button_style(),
        ).pack(side="left", padx=(0, SPACING["sm"]))
        if hasattr(self.app, "_toggle_feedback_panel"):
            ctk.CTkButton(
                actions, text="Send feedback…", width=140, height=36,
                command=self.app._toggle_feedback_panel, **self._button_style(),
            ).pack(side="left")

        # License card right below Updates & Diagnostics
        license_card = ctk.CTkFrame(
            parent,
            fg_color=COLORS["bg_surface"],
            corner_radius=RADIUS["md"],
            border_width=1,
            border_color=COLORS["border_subtle"],
        )
        license_card.pack(fill="x", pady=(0, SPACING["md"]))
        self.app._license_tile = license_card
        if hasattr(self.app, "_render_license_tile"):
            self.app._render_license_tile()

    def _refresh_advanced_controls(self) -> None:
        if not self._advanced_built:
            return
        config = self.app.config
        if hasattr(self, "preprocess_var"):
            self.preprocess_var.set(str(config.get("audio_preprocessing", "light")).capitalize())
        if hasattr(self, "ducking_var"):
            self.ducking_var.set(bool(config.get("audio_ducking_enabled", True)))
        mode_rebuilt = False
        if hasattr(self, "proc_mode_var"):
            want_mode = "Remote" if config.get("processing_mode", "local") == "remote" else "Local"
            if self.proc_mode_var.get() != want_mode:
                self.proc_mode_var.set(want_mode)
                self.rebuild_processing_mode_rows(config.get("processing_mode", "local"))
                mode_rebuilt = True
        if hasattr(self, "postproc_var") and not mode_rebuilt:
            want_postproc = bool(config.get("post_processing_enabled", False))
            if self.postproc_var.get() != want_postproc:
                self.postproc_var.set(want_postproc)
                self.rebuild_processing_mode_rows(config.get("processing_mode", "local"))
        if hasattr(self, "chunked_var"):
            chunk_label = {"off": "Off", "auto": "Auto", "on": "On"}.get(
                str(config.get("chunked_mode", "auto")).lower(), "Auto"
            )
            self.chunked_var.set(chunk_label)
        if hasattr(self, "whisper_model_button") and self.whisper_model_button.winfo_exists():
            if hasattr(self.app, "get_model_display"):
                self.whisper_model_button.configure(text=self.app.get_model_display())
        if hasattr(self, "adv_language_var"):
            self.adv_language_var.set(str(config.get("language", "en")))
        if hasattr(self, "repetition_var"):
            self.repetition_var.set(bool(config.get("collapse_whisper_repetitions", True)))
        if hasattr(self, "auto_enter_var"):
            self.auto_enter_var.set(bool(config.get("press_enter_after_dictation", False)))
        if hasattr(self, "gamer_mode_var"):
            self.gamer_mode_var.set(bool(config.get("gamer_mode", True)))
        if hasattr(self, "game_send_var"):
            self.game_send_var.set(bool(config.get("game_chat_send", True)))
        if hasattr(self, "hotkey_key_var"):
            code_to_name = getattr(self.app, "_hotkey_code_to_name", {})
            cur_code = config.get("hotkey_key", 67)
            if cur_code in code_to_name:
                self.hotkey_key_var.set(code_to_name[cur_code])
        for mod, var in getattr(self.app, "_hotkey_mod_vars", {}).items():
            var.set(mod in config.get("hotkey_modifiers", []))

    def _get_hotkey_device_options(self) -> tuple[list[str], str]:
        if sys.platform in ("darwin", "win32"):
            return ["All Devices (Global)"], "All Devices (Global)"
        cached = getattr(self.app, "_hotkey_device_names_snapshot", None)
        names = list(cached) if cached else []
        is_flatpak = bool(os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists())
        all_label = "All Devices (Portal)" if is_flatpak else "All Devices"
        options = [all_label] + names
        enabled = self.app.config.get("enabled_input_devices", [])
        if is_flatpak or not enabled or len(enabled) != 1 or enabled[0] not in names:
            return options, all_label
        return options, enabled[0]

    # ── Basic & Advanced Callbacks ──────────────────────────────────────────

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

    def _toggle_cleanup(self) -> None:
        cleanup_on = bool(self.cleanup_var.get())
        raw = not cleanup_on
        self.raw_var.set(raw)
        self._shared_bool("raw_transcription_var", raw)
        self.app._on_raw_transcription_changed()
        if hasattr(self.app, "_update_benchmark_results_display"):
            try:
                self.app._update_benchmark_results_display()
            except Exception:
                pass
        self.refresh()

    def _toggle_raw(self) -> None:
        raw = bool(self.raw_var.get())
        self.cleanup_var.set(not raw)
        self._shared_bool("raw_transcription_var", raw)
        self.app._on_raw_transcription_changed()
        if hasattr(self.app, "_update_benchmark_results_display"):
            try:
                self.app._update_benchmark_results_display()
            except Exception:
                pass
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

    def _choose_audio_processing(self, value: str) -> None:
        if hasattr(self.app, "_on_audio_processing_selected"):
            self.app._on_audio_processing_selected(value)

    def _toggle_audio_ducking(self) -> None:
        if hasattr(self.app, "_on_audio_ducking_toggled"):
            self.app._on_audio_ducking_toggled()

    def _on_duck_slider(self, value: float) -> None:
        snapped = round(int(float(value)) / 5) * 5
        self.duck_value_label.configure(text=f"{snapped}%")
        if hasattr(self.app, "_apply_duck_percent"):
            self.app._apply_duck_percent(snapped)

    def _choose_processing_mode(self, value: str) -> None:
        mode = value.lower()
        if hasattr(self.app, "_on_processing_mode_changed"):
            self.app._on_processing_mode_changed(mode)
        self.refresh()

    def _open_whisper_models(self) -> None:
        if hasattr(self.app, "open_model_settings"):
            self.app.open_model_settings()

    def _choose_specific_language(self, value: str) -> None:
        if hasattr(self.app, "on_language_changed"):
            self.app.on_language_changed(value)
        self.refresh()

    def _toggle_repetition(self) -> None:
        if hasattr(self.app, "_on_repetition_cleanup_changed"):
            self.app._on_repetition_cleanup_changed()

    def _choose_remote_provider(self, value: str) -> None:
        if hasattr(self.app, "_on_remote_provider_changed"):
            self.app._on_remote_provider_changed(value)
        self.rebuild_processing_mode_rows("remote")

    def _toggle_postproc(self) -> None:
        if hasattr(self.app, "toggle_post_processing"):
            self.app.toggle_post_processing()
        if hasattr(self.app, "_update_benchmark_results_display"):
            try:
                self.app._update_benchmark_results_display()
            except Exception:
                pass
        self.rebuild_processing_mode_rows(self.app.config.get("processing_mode", "local"))

    def _choose_hotkey_key(self, value: str) -> None:
        if hasattr(self.app, "_on_hotkey_key_changed"):
            self.app._on_hotkey_key_changed(value)
        self.refresh()

    def _on_hotkey_mod_toggled(self) -> None:
        if hasattr(self.app, "_on_hotkey_mod_changed"):
            self.app._on_hotkey_mod_changed()
        self.refresh()

    def _on_ui_scale_slider(self, value: float) -> None:
        snapped = round(float(value) * 20) / 20
        self.ui_scale_label.configure(text=f"{int(round(snapped * 100))}%")
        timer = getattr(self, "_ui_scale_timer", None)
        if timer is not None:
            try:
                self.app.after_cancel(timer)
            except Exception:
                pass
        self._ui_scale_timer = self.app.after(150, lambda: self.app._apply_live_scale(snapped))

    def _on_ov_scale_slider(self, value: float) -> None:
        snapped = round(float(value) * 10) / 10
        self.ov_scale_label.configure(text=f"{int(round(snapped * 100))}%")
        timer = getattr(self, "_ov_scale_timer", None)
        if timer is not None:
            try:
                self.app.after_cancel(timer)
            except Exception:
                pass
        self._ov_scale_timer = self.app.after(150, lambda: self.app._apply_overlay_scale(snapped))

    def _on_ov_offset_slider(self, value: float) -> None:
        px = int(round(float(value)))
        self.ov_offset_label.configure(text=f"{px}px")
        timer = getattr(self, "_ov_offset_timer", None)
        if timer is not None:
            try:
                self.app.after_cancel(timer)
            except Exception:
                pass
        self._ov_offset_timer = self.app.after(150, lambda: self.app._apply_overlay_position(px))

    def _toggle_gamer_mode(self) -> None:
        self.app.config["gamer_mode"] = bool(self.gamer_mode_var.get())
        self._persist_config()
        if hasattr(self.app, "log"):
            self.app.log("🎮 " + ("Gamer mode on" if self.gamer_mode_var.get() else "Gamer mode off"))

    def _toggle_game_send(self) -> None:
        self.app.config["game_chat_send"] = bool(self.game_send_var.get())
        self._persist_config()
        if hasattr(self.app, "log"):
            self.app.log("🎮 " + ("Game messages send automatically" if self.game_send_var.get() else "Game messages left in chat"))

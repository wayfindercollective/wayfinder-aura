"""
First-run welcome tour for Wayfinder Aura.

Two layers:

- ``WelcomeFlow`` — a tiny, pure state machine (optional free model download,
  then mic -> hotkey -> dictate).
  It has NO Tk/customtkinter dependency and is headless-testable.
- ``WelcomePane`` — the in-window card that renders the flow over the tab content
  area. It imports customtkinter lazily (inside the builder), so importing this
  module and using ``WelcomeFlow`` works without a display.

Design brief: the first five minutes feel guided and calm. One card, a short flow,
dot progress, always skippable, max restraint — matched to the app's design
language (theme tokens, caps + divider aesthetic, blue brand accent).

Injection safety: while the pane is active the app suppresses text injection and
routes each dictation transcript here via ``receive_transcript`` — the product
demos itself without typing into whatever window happens to be focused.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from wayfinder.config import save_config
from wayfinder.ui.theme import COLORS, FONT_SIZES, FONTS, RADIUS, SPACING


class WelcomeFlow:
    """Pure state machine for the first-run welcome tour.

    No Tk imports — importable and testable headlessly. ``on_complete`` fires
    exactly once, whether the flow is completed (advance past the last step) or
    skipped.
    """

    STEPS = ("mic", "hotkey", "dictate")

    def __init__(self, on_complete=None, *, include_model: bool = False,
                 include_permissions: bool = False, steps=None):
        self.steps = list(steps) if steps else list(self.STEPS)
        if include_model and not steps:
            self.steps.insert(1, "model")
        if include_permissions and not steps:
            # macOS: grant mic/Accessibility/Input Monitoring before the mic test.
            self.steps.insert(0, "permissions")
        self._index = 0
        self.is_complete = False
        self.on_complete = on_complete
        self._fired = False
        self.model_download_state = "idle"
        self.model_download_error = ""
        self.mic_test_state = "idle"
        self.mic_test_error = ""
        self._mic_test_generation = 0

    def detour_to(self, step) -> None:
        """Show ``step`` now; advancing past it resumes the current step
        (instead of replaying the steps between them)."""
        if self.is_complete or self.current == step:
            return
        if step in self.steps:
            at = self.steps.index(step)
            self.steps.pop(at)
            if at < self._index:
                self._index -= 1
        self.steps.insert(self._index, step)

    @property
    def current(self):
        """The current step id, or ``None`` once complete."""
        if self.is_complete or self._index >= len(self.steps):
            return None
        return self.steps[self._index]

    @property
    def index(self) -> int:
        return self._index

    @property
    def can_advance(self) -> bool:
        """Whether Continue is allowed on the current step.

        The tour remains globally skippable, but its first Continue must not let
        users walk past an untested microphone and discover the failure only
        after learning the dictation hotkey.
        """
        if self.current == "model":
            return self.model_download_state == "ready"
        return self.current != "mic" or self.mic_test_state == "passed"

    def begin_model_download(self) -> bool:
        if self.is_complete or self.current != "model":
            return False
        self.model_download_state = "downloading"
        self.model_download_error = ""
        return True

    def complete_model_download(self) -> bool:
        if self.is_complete or self.current != "model":
            return False
        self.model_download_state = "ready"
        self.model_download_error = ""
        return True

    def fail_model_download(self, message: str) -> bool:
        if self.is_complete or self.current != "model":
            return False
        self.model_download_state = "failed"
        self.model_download_error = str(message)
        return True

    @property
    def mic_test_generation(self) -> int:
        return self._mic_test_generation

    def begin_mic_test(self) -> int | None:
        """Begin a new microphone test and return its stale-callback token."""
        if self.is_complete or self.current != "mic":
            return None
        self._mic_test_generation += 1
        self.mic_test_state = "starting"
        self.mic_test_error = ""
        return self._mic_test_generation

    def mark_mic_recording(self, generation: int) -> bool:
        if generation != self._mic_test_generation or self.mic_test_state != "starting":
            return False
        self.mic_test_state = "recording"
        return True

    def mark_mic_checking(self, generation: int) -> bool:
        if generation != self._mic_test_generation or self.mic_test_state != "recording":
            return False
        self.mic_test_state = "checking"
        return True

    def pass_mic_test(self, generation: int | None = None) -> bool:
        """Mark audio ready. ``None`` accepts a real tutorial dictation result."""
        if self.is_complete or self.current != "mic":
            return False
        if generation is not None and generation != self._mic_test_generation:
            return False
        self.mic_test_state = "passed"
        self.mic_test_error = ""
        return True

    def fail_mic_test(self, message: str, generation: int | None = None) -> bool:
        if self.is_complete or self.current != "mic":
            return False
        if generation is not None and generation != self._mic_test_generation:
            return False
        self.mic_test_state = "failed"
        self.mic_test_error = message
        return True

    def invalidate_mic_test(self) -> None:
        """Invalidate worker callbacks when retrying, skipping, or tearing down."""
        self._mic_test_generation += 1
        if self.mic_test_state != "passed":
            self.mic_test_state = "idle"
            self.mic_test_error = ""

    def advance(self) -> bool:
        """Move to the next step; return False when the mic gate blocks it."""
        if self.is_complete:
            return False
        if not self.can_advance:
            return False
        if self._index < len(self.steps) - 1:
            self._index += 1
        else:
            self._complete()
        return True

    def goto(self, step: str) -> None:
        """Jump to a named step (no-op if unknown or already complete)."""
        if not self.is_complete and step in self.steps:
            self._index = self.steps.index(step)

    def skip(self) -> None:
        """Skip the rest of the tour — completes immediately."""
        self._complete()

    def _complete(self) -> None:
        if self.is_complete:
            return
        self.is_complete = True
        if self.on_complete is not None and not self._fired:
            self._fired = True
            self.on_complete()


# Max characters of a transcript we echo back inside the card.
_TRANSCRIPT_MAX = 140

# How long the final "try it now" step waits for a first transcript before it
# surfaces a troubleshooting line + skip button. Single-shot (see rule #1).
_HELP_DELAY_MS = 13000

# A short, capture-only test is long enough for the speech-activity detector to
# distinguish a voice from steady room noise without making first run feel slow.
_MIC_TEST_DURATION_S = 5.0


@dataclass(frozen=True)
class MicrophoneTestResult:
    passed: bool
    message: str
    duration: float
    peak: float


def assess_microphone_capture(recorder) -> MicrophoneTestResult:
    """Assess a stopped production ``AudioRecorder`` without retaining audio."""
    duration = max(0.0, float(recorder.get_duration()))
    peak = max(0.0, float(recorder.get_peak_amplitude()))
    if duration < 0.5:
        return MicrophoneTestResult(
            False,
            "That sample was too short. Retry and speak for a few seconds.",
            duration,
            peak,
        )

    try:
        has_voice = bool(recorder.has_speech_activity())
    except Exception:
        # Fail sensibly with older/custom recorder implementations that expose
        # only peak amplitude. The production recorder takes the stronger path.
        has_voice = peak >= 0.003
    if not has_voice:
        return MicrophoneTestResult(
            False,
            "No voice-level audio was detected. Check mute and input level, then retry.",
            duration,
            peak,
        )
    return MicrophoneTestResult(
        True,
        "Voice audio detected — your microphone is ready.",
        duration,
        peak,
    )


def microphone_error_guidance(error: object) -> str:
    """Turn capture-open failures into useful first-run guidance."""
    detail = str(error or "").lower()
    if any(token in detail for token in ("no audio", "no input", "host api", "-9999")):
        return (
            "Aura couldn't open a microphone. For a Bluetooth headset, choose "
            "Hands-Free / Headset mode — High Fidelity Playback has no mic — then retry."
        )
    if any(token in detail for token in ("permission", "denied", "not permitted")):
        return "Microphone access was denied. Allow it in system privacy settings, then retry."
    if any(token in detail for token in ("timed out", "timeout", "wedged")):
        return "The microphone did not respond. Reconnect it or restart Aura, then retry."
    return (
        "Aura couldn't start the microphone. Check mute and the selected input; "
        "Bluetooth headsets may need Hands-Free / Headset mode."
    )


def fit_card_size(avail_w, avail_h, preferred, minimum, margin=2 * SPACING["md"]):
    """Largest card size up to ``preferred`` that fits the area less ``margin``.

    Never smaller than ``minimum``; the caller keeps an overflowing card's
    top-left edge in view.
    """
    width = max(minimum[0], min(preferred[0], int(avail_w - margin)))
    height = max(minimum[1], min(preferred[1], int(avail_h - margin)))
    return width, height


class WelcomePane:
    """Renders ``WelcomeFlow`` as a centered card over the tab content area.

    ``parent`` is the container to cover (the app's ``tab_content_container``);
    ``app`` is the ``WayfinderApp`` instance (used for the live hotkey display,
    the link-hover helper, and config persistence).
    """

    # Per-step card titles (rendered in the fixed header, above the divider).
    _STEP_TITLES = {
        "permissions": "welcome to wayfinder aura",
        "model": "set up local dictation",
        "mic": "welcome to wayfinder aura",
        "hotkey": "your hotkey",
        "dictate": "try it now",
    }

    # macOS permission rows: (id, title, why, icon).
    _PERMISSION_ROWS = (
        ("microphone", "Microphone", "to hear you", "mic"),
        ("accessibility", "Accessibility", "to type your words into any app", "pen-line"),
        ("input_monitoring", "Input Monitoring", "to hear your hotkey anywhere", "lock"),
    )
    _PERMISSION_POLL_MS = 1000  # live grant pickup while the step is up (rule 1: >= 100 ms)

    # Preferred card size. In a compact window the tab area can be smaller, so
    # the card shrinks toward the minimum instead of overflowing every edge
    # (which hid the title and clipped the start of each line).
    _CARD_SIZE = (520, 360)
    _CARD_MIN_SIZE = (320, 300)

    def __init__(self, parent, app, *, only_permissions: bool = False):
        import customtkinter as ctk  # lazy: keep the module headless-importable

        self._ctk = ctk
        self.parent = parent
        self.app = app
        # Permissions-only pane (e.g. after Ultra activation): one step, and it
        # never marks the first-run tour as done.
        self._only_permissions = bool(only_permissions)
        self._perm_poll_id = None
        self._perm_snapshot = None
        self._perm_done_scheduled = False
        self._perm_requested: set[str] = set()  # rows whose "allow" was clicked
        # macOS Welcome owns the free Base-model download ("runs entirely on this
        # Mac"). Linux keeps its separate setup flow and cue banner.
        needs_model = False
        if sys.platform == "darwin":
            try:
                needs_model = not bool(app._has_usable_whisper_model())
            except Exception:
                needs_model = False
        needs_permissions = False
        if sys.platform == "darwin":
            try:
                from wayfinder.utils.macos_permissions import permission_snapshot

                needs_permissions = any(v is not True for v in permission_snapshot().values())
            except Exception:
                needs_permissions = False
        if self._only_permissions:
            self.flow = WelcomeFlow(on_complete=self._complete_flow, steps=["permissions"])
        else:
            self.flow = WelcomeFlow(
                on_complete=self._complete_flow,
                include_model=needs_model,
                include_permissions=needs_permissions,
            )
        self._transcript = None
        self._dictation_error = ""
        self._destroyed = False
        self._mic_test_recorder = None
        self._mic_test_after_id = None
        self._mic_test_started_at = None
        self._mic_level_bar = None
        self._mic_level_label = None
        self._model_progress_bar = None
        self._model_progress_label = None
        self._model_downloader = None
        self._model_choice = "base.en"
        # Final-step recovery: if the first dictation never lands, a single-shot
        # timer swaps the calm "listening…" line for a troubleshooting affordance.
        self._help_after_id = None
        self._help_shown = False
        self._card_size = self._CARD_SIZE
        self._wrap_labels = []

        # Full-size dim underlay. CTk has no real alpha; a plain bg_base frame
        # covering the tab content reads as a focused/modal state.
        self.underlay = ctk.CTkFrame(parent, fg_color=COLORS["bg_base"], corner_radius=0)
        if sys.platform == "darwin":
            from wayfinder.ui.macos_window import place_in_content_pane

            # Stay inside the rounded content pane on the macOS glass.
            place_in_content_pane(self.underlay)
        else:
            self.underlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Single centered card.
        self.card = ctk.CTkFrame(
            self.underlay,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_rim"],
            width=self._CARD_SIZE[0],
            height=self._CARD_SIZE[1],
        )
        self.card.place(relx=0.5, rely=0.5, anchor="center")
        self.card.pack_propagate(False)
        self.card._wf_manages_wrap = True  # its labels rewrap on card resize
        if sys.platform == "darwin":
            # macOS opens content-sized, so the tab area can be smaller than the
            # card. Linux keeps its fixed card (its default window fits it).
            self.underlay.bind("<Configure>", self._fit_card, add="+")

        self._render_step()

    # --- helpers -------------------------------------------------------------

    def _fit_card(self, _event=None) -> None:
        """Size the card to the tab area and keep its top-left edge in view."""
        if self._destroyed:
            return
        try:
            scale = self._ctk.ScalingTracker.get_widget_scaling(self.card)
            avail_w = self.underlay.winfo_width() / scale
            avail_h = self.underlay.winfo_height() / scale
        except Exception:
            return
        if avail_w <= 1 or avail_h <= 1:
            return
        width, height = fit_card_size(avail_w, avail_h, self._CARD_SIZE, self._CARD_MIN_SIZE)
        if (width, height) != self._card_size:
            self._card_size = (width, height)
            self.card.configure(width=width, height=height)
            wrap = self._wraplength()
            for label in self._wrap_labels:
                try:
                    label.configure(wraplength=wrap)
                except Exception:
                    pass
        # Below the minimum the card still overflows; pin it to the top/left so
        # the title and the start of every line stay readable.
        relx, h_anchor = (0.5, "") if avail_w >= width else (0.0, "w")
        rely, v_anchor = (0.5, "") if avail_h >= height else (0.0, "n")
        try:
            self.card.place_configure(relx=relx, rely=rely, anchor=(v_anchor + h_anchor) or "center")
        except Exception:
            pass

    def _wraplength(self) -> int:
        # 440 at the preferred 520 width: card minus the xl side pads and a 2xl gutter.
        return self._card_size[0] - 2 * SPACING["xl"] - SPACING["2xl"]

    def _hotkey_text(self) -> str:
        try:
            if hasattr(self.app, "get_hotkey_display"):
                return self.app.get_hotkey_display()
        except Exception:
            pass
        return "your hotkey"

    def _continue_button(self, parent, text, gold=False, command=None):
        # Primary CTAs use brand blue. ``gold`` kept for call-site compatibility
        # but no longer switches palette — Ultra gold stays on the paid badge only.
        ctk = self._ctk
        btn = ctk.CTkButton(
            parent,
            text=text,
            width=150,
            height=38,
            corner_radius=RADIUS["md"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"],
            font=(FONTS["body"][0], FONT_SIZES["body"], "bold"),
            command=command or self._on_continue,
        )
        btn.pack(anchor="w", pady=(SPACING["xl"], 0))
        return btn

    def _title(self, parent, text):
        ctk = self._ctk
        lbl = ctk.CTkLabel(
            parent,
            text=text,
            font=(FONTS["header"][0], FONT_SIZES["title"], "bold"),
            text_color=COLORS["text_bright"],
        )
        lbl.pack(anchor="w")
        div = ctk.CTkFrame(parent, height=1, fg_color=COLORS["border_subtle"])
        div.pack(fill="x", pady=(SPACING["sm"], 0))
        return lbl

    def _body_label(self, parent, text, muted=False, pady=(0, 0)):
        ctk = self._ctk
        lbl = ctk.CTkLabel(
            parent,
            text=text,
            font=(FONTS["body"][0], FONT_SIZES["small"] if muted else FONT_SIZES["body"]),
            text_color=COLORS["text_muted"] if muted else COLORS["text_secondary"],
            wraplength=self._wraplength(),
            justify="left",
        )
        lbl.pack(anchor="w", pady=pady)
        self._wrap_labels.append(lbl)
        return lbl

    def _status_label(self, parent, text, color, pady=(SPACING["sm"], 0)):
        ctk = self._ctk
        lbl = ctk.CTkLabel(
            parent,
            text=text,
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            text_color=color,
            wraplength=self._wraplength(),
            justify="left",
        )
        lbl.pack(anchor="w", pady=pady)
        self._wrap_labels.append(lbl)
        return lbl

    # --- rendering -----------------------------------------------------------

    def _render_step(self) -> None:
        ctk = self._ctk
        if self.flow.is_complete:
            return
        self._cancel_mic_meter_poll()
        self._cancel_permission_poll()
        for child in self.card.winfo_children():
            child.destroy()
        self._wrap_labels = []

        pad = SPACING["xl"]  # one horizontal margin for header, body AND footer (they used to disagree: 28 vs 22)

        # Title + divider: a fixed header pinned to the top of the card.
        header = ctk.CTkFrame(self.card, fg_color="transparent")
        header.pack(side="top", fill="x", padx=pad, pady=(pad, 0))
        title = self._STEP_TITLES.get(self.flow.current, "")
        if self.flow.current == "permissions" and self._only_permissions:
            title = "finish setting up"
        elif self.flow.current == "mic" and "permissions" in self.flow.steps:
            title = "test your microphone"
        self._title(header, title)

        # Footer (dots + skip) pinned to the bottom — packed BEFORE the body so the
        # body's expand fills only the band between header and footer.
        self._render_footer(pad)

        # Step body: a left-aligned copy+button cluster, VERTICALLY CENTERED in the
        # band between header and footer. expand + fill="x" hands the inner frame the
        # leftover height and centres it within that cavity — this kills the old
        # top-cluster-with-dead-space-below look while keeping the copy's left edge
        # aligned to the title.
        body_wrap = ctk.CTkFrame(self.card, fg_color="transparent")
        body_wrap.pack(side="top", fill="both", expand=True, padx=pad)
        body = ctk.CTkFrame(body_wrap, fg_color="transparent")
        body.pack(expand=True, fill="x")

        step = self.flow.current
        if step == "permissions":
            self._render_permissions(body)
        elif step == "model":
            self._render_model(body)
        elif step == "mic":
            self._render_mic(body)
        elif step == "hotkey":
            self._render_hotkey(body)
        elif step == "dictate":
            self._render_dictate(body)

    def _render_permissions(self, body) -> None:
        """Three live rows; Allow/Repair trigger macOS's own prompts."""
        ctk = self._ctk
        from wayfinder.utils.macos_permissions import permission_snapshot

        snapshot = permission_snapshot()
        self._perm_snapshot = snapshot
        self._remember_granted_permissions(snapshot)
        all_on = all(v is True for v in snapshot.values())
        self._body_label(
            body,
            "all set — Aura can hear you and type for you."
            if all_on
            else "turn these on once. each opens a mac prompt, and aura picks it up right away.",
        )
        seen = set(self.app.config.get("macos_permissions_seen", []) or [])
        try:
            from wayfinder.ui.icons import get_icon
        except Exception:
            get_icon = None
        rows = ctk.CTkFrame(body, fg_color="transparent")
        rows.pack(fill="x", pady=(SPACING["md"], 0))
        for name, title, why, icon in self._PERMISSION_ROWS:
            granted = snapshot.get(name) is True
            # macOS hands an Input Monitoring grant to new processes only (its
            # own Settings offers "Quit & Reopen"): once asked, offer that here.
            relaunch = (not granted and name == "input_monitoring"
                        and name in self._perm_requested)
            if relaunch:
                why = "switched it on? relaunch aura to finish"
            row = ctk.CTkFrame(rows, fg_color="transparent")
            row.pack(fill="x", pady=(0, SPACING["sm"]))
            if get_icon is not None:
                image = get_icon("check" if granted else icon, 16,
                                 COLORS["accent_green"] if granted else COLORS["text_secondary"])
                ctk.CTkLabel(row, text="", image=image, width=20).pack(side="left", padx=(0, SPACING["sm"]))
            text = ctk.CTkFrame(row, fg_color="transparent")
            text.pack(side="left", fill="x", expand=True)
            # Line-height labels (CTkLabel's default 28 px min height doubled
            # each row, pushing "not now" under the footer).
            ctk.CTkLabel(text, text=title, anchor="w", height=FONT_SIZES["body"] + 6,
                         font=(FONTS["body"][0], FONT_SIZES["body"], "bold"),
                         text_color=COLORS["text_primary"]).pack(anchor="w")
            ctk.CTkLabel(text, text=why, anchor="w", height=FONT_SIZES["small"] + 5,
                         font=(FONTS["body"][0], FONT_SIZES["small"]),
                         text_color=COLORS["text_muted"]).pack(anchor="w")
            if granted:
                ctk.CTkLabel(row, text="on", font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
                             text_color=COLORS["accent_green"]).pack(side="right")
            elif relaunch:
                ctk.CTkButton(
                    row, text="relaunch", width=86, height=30,
                    corner_radius=RADIUS["sm"],
                    fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
                    text_color=COLORS["bg_base"],
                    font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
                    command=self._relaunch_for_permissions,
                ).pack(side="right")
            else:
                repair = name in seen and name != "microphone"
                ctk.CTkButton(
                    row, text="repair" if repair else "allow", width=86, height=30,
                    corner_radius=RADIUS["sm"],
                    fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
                    text_color=COLORS["bg_base"],
                    font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
                    command=lambda n=name, r=repair: self._request_permission(n, r),
                ).pack(side="right")
        if all_on:
            if not self._perm_done_scheduled:
                # Let the green checks register, then move on by themselves.
                self._perm_done_scheduled = True
                self.card.after(900, self._permissions_complete)
            return
        later = ctk.CTkLabel(body, text="not now", height=FONT_SIZES["small"] + 5,
                             font=(FONTS["body"][0], FONT_SIZES["small"]),
                             text_color=COLORS["text_muted"], cursor="hand2")
        later.pack(anchor="w", pady=(SPACING["xs"], 0))
        later.bind("<Button-1>", lambda _e: self._permissions_complete())
        self._perm_poll_id = self.card.after(self._PERMISSION_POLL_MS, self._poll_permissions)

    def _relaunch_for_permissions(self) -> None:
        try:
            if self.app.relaunch_app():
                return
        except Exception:
            pass
        # No bundle to reopen (source run): the Settings pane is the fallback.
        self._request_permission("input_monitoring")

    def _request_permission(self, name: str, repair: bool = False) -> None:
        first_ask = name not in self._perm_requested
        self._perm_requested.add(name)
        if first_ask and name == "input_monitoring":
            self._render_step()  # show the relaunch hint beside "allow"
        try:
            from wayfinder.utils import macos_permissions as perms

            if repair:
                perms.repair_permission(name)
            else:
                perms.ask_for_permission(name)
        except Exception:
            pass

    def _remember_granted_permissions(self, snapshot) -> None:
        """Remember grants once seen, so a later loss shows Repair, not Allow."""
        try:
            seen = list(self.app.config.get("macos_permissions_seen", []) or [])
            added = [n for n, v in snapshot.items() if v is True and n not in seen]
            if added:
                self.app.config["macos_permissions_seen"] = seen + added
                save_config(self.app.config)
        except Exception:
            pass

    def _poll_permissions(self) -> None:
        self._perm_poll_id = None
        if self._destroyed or self.flow.current != "permissions":
            return
        try:
            from wayfinder.utils.macos_permissions import permission_snapshot

            snapshot = permission_snapshot()
        except Exception:
            snapshot = self._perm_snapshot
        before = self._perm_snapshot or {}
        if snapshot != before:
            newly = [n for n in ("accessibility", "input_monitoring")
                     if snapshot.get(n) is True and before.get(n) is not True]
            if newly:
                try:
                    self.app._macos_permissions_granted(newly)
                except Exception:
                    pass
            self._render_step()  # redraws rows (and re-arms the poll)
            return
        self._perm_poll_id = self.card.after(self._PERMISSION_POLL_MS, self._poll_permissions)

    def _cancel_permission_poll(self) -> None:
        if self._perm_poll_id is not None:
            try:
                self.card.after_cancel(self._perm_poll_id)
            except Exception:
                pass
            self._perm_poll_id = None

    def _permissions_complete(self) -> None:
        if self._destroyed or self.flow.current != "permissions":
            return
        self._cancel_permission_poll()
        self._perm_done_scheduled = False
        self._on_continue()

    def _render_model(self, body) -> None:
        ctk = self._ctk
        state = self.flow.model_download_state
        model_label = (
            "Base (English)"
            if self._model_choice == "base.en"
            else "Base (Multilingual)"
        )
        if state == "downloading":
            self._body_label(body, f"downloading {model_label}…")
            self._body_label(
                body,
                "the free model runs entirely on this Mac after download.",
                muted=True,
                pady=(SPACING["sm"], SPACING["md"]),
            )
            self._model_progress_bar = ctk.CTkProgressBar(
                body,
                height=10,
                corner_radius=RADIUS["xs"],
                progress_color=COLORS["accent"],
                fg_color=COLORS["bg_input"],
            )
            self._model_progress_bar.pack(fill="x")
            self._model_progress_bar.set(0)
            self._model_progress_label = self._status_label(
                body,
                "starting…",
                COLORS["text_muted"],
                pady=(SPACING["sm"], 0),
            )
            return

        if state == "ready":
            self._status_label(
                body,
                "✓ Base model ready",
                COLORS["accent_green"],
                pady=(0, SPACING["sm"]),
            )
            self._body_label(body, "Local dictation is installed and ready to test.")
            self._continue_button(body, "continue")
            return

        self._body_label(body, "Choose a free speech model for private local dictation.")
        self._body_label(
            body,
            "Both run entirely on this Mac after download.",
            muted=True,
            pady=(SPACING["sm"], 0),
        )
        choices = ctk.CTkFrame(body, fg_color="transparent")
        choices.pack(fill="x", pady=(SPACING["md"], 0))
        for model_id, label in (
            ("base.en", "English"),
            ("base", "Multilingual"),
        ):
            selected = model_id == self._model_choice
            ctk.CTkButton(
                choices,
                text=label,
                height=34,
                corner_radius=RADIUS["sm"],
                fg_color=COLORS["accent"] if selected else COLORS["bg_input"],
                hover_color=COLORS["accent_dim"] if selected else COLORS["bg_hover"],
                text_color=COLORS["bg_base"] if selected else COLORS["text_secondary"],
                font=(FONTS["body"][0], FONT_SIZES["small"], "bold" if selected else "normal"),
                command=lambda value=model_id: self._select_model_choice(value),
            ).pack(side="left", fill="x", expand=True, padx=(0, SPACING["sm"]))
        if state == "failed":
            self._status_label(
                body,
                self.flow.model_download_error or "The download did not finish.",
                COLORS["accent_red"],
                pady=(SPACING["md"], 0),
            )
            label = "retry download"
        else:
            label = f"download {model_label}"
        self._continue_button(body, label, command=self._start_model_download)

    def _select_model_choice(self, model_id: str) -> None:
        if model_id not in ("base.en", "base"):
            return
        self._model_choice = model_id
        self._render_step()

    def _render_mic(self, body) -> None:
        ctk = self._ctk
        state = self.flow.mic_test_state

        if state == "starting":
            self._body_label(body, "opening your microphone…")
            self._body_label(
                body,
                "Aura is checking the same local audio path used for dictation.",
                muted=True,
                pady=(SPACING["sm"], 0),
            )
            return

        if state == "recording":
            self._body_label(body, "say a few words while the meter moves.")
            self._body_label(
                body,
                "the sample stays on this device and is discarded after the test.",
                muted=True,
                pady=(SPACING["sm"], SPACING["md"]),
            )
            self._mic_level_bar = ctk.CTkProgressBar(
                body,
                height=10,
                corner_radius=RADIUS["xs"],
                progress_color=COLORS["accent_green"],
                fg_color=COLORS["bg_input"],
            )
            self._mic_level_bar.pack(fill="x")
            self._mic_level_bar.set(0)
            self._mic_level_label = self._status_label(
                body,
                "listening…",
                COLORS["accent"],
                pady=(SPACING["sm"], 0),
            )
            self._continue_button(body, "stop and check", command=self._stop_mic_test)
            self._schedule_mic_meter_poll()
            return

        if state == "checking":
            self._body_label(body, "checking the sample…")
            self._body_label(
                body,
                "looking for voice-level audio before we introduce dictation.",
                muted=True,
                pady=(SPACING["sm"], 0),
            )
            return

        if state == "passed":
            self._status_label(
                body,
                "✓ microphone ready",
                COLORS["accent_green"],
                pady=(0, SPACING["sm"]),
            )
            if self._transcript:
                quote = self._transcript.strip()
                if len(quote) > _TRANSCRIPT_MAX:
                    quote = quote[: _TRANSCRIPT_MAX - 1].rstrip() + "…"
                self._body_label(body, f'we heard: "{quote}"')
            else:
                self._body_label(body, "Aura detected voice audio through the dictation input.")
            self._continue_button(body, "continue")
            return

        self._body_label(body, "let's test your microphone before dictation.")
        self._body_label(
            body,
            "speak for five seconds. nothing will be typed or saved.",
            muted=True,
            pady=(SPACING["sm"], 0),
        )
        if state == "failed":
            self._status_label(
                body,
                self.flow.mic_test_error,
                COLORS["accent_red"],
                pady=(SPACING["md"], 0),
            )
            self._continue_button(body, "retry microphone", command=self._start_mic_test)
        else:
            self._continue_button(body, "test microphone", command=self._start_mic_test)

    def _render_hotkey(self, body) -> None:
        ctk = self._ctk
        token = ctk.CTkLabel(
            body,
            text=self._hotkey_text(),
            font=(FONTS["mono"][0], FONT_SIZES["display"] + 8, "bold"),
            text_color=COLORS["accent"],
        )
        token.pack(anchor="w", pady=(0, SPACING["md"]))
        tap_hold = False
        try:
            tap_hold = sys.platform == "darwin" and bool(self.app._record_hotkey_is_tap_hold())
        except Exception:
            pass
        self._body_label(
            body,
            "tap it to start a dictation and tap again to stop — or hold it while you talk."
            if tap_hold
            else "press it once to start a dictation — press it again to stop.",
        )
        if sys.platform == "darwin":
            try:
                from wayfinder.utils.macos_permissions import macos_install_location_ready

                install_ready = macos_install_location_ready()
            except Exception:
                install_ready = True
            try:
                accessibility, input_monitoring = self.app._macos_permission_state()
            except Exception:
                accessibility = input_monitoring = None
            missing = (
                "install_location" if not install_ready
                else "accessibility" if accessibility is not True
                else "input_monitoring" if input_monitoring is not True
                else None
            )
            if missing is not None:
                self.app._missing_macos_permission = missing
                pane = (
                    "Applications"
                    if missing == "install_location"
                    else "Accessibility"
                    if missing == "accessibility"
                    else "Input Monitoring"
                )
                self._status_label(
                    body,
                    (
                        "Before granting permissions, move Wayfinder Aura into Applications "
                        "and relaunch that stable copy."
                        if missing == "install_location"
                        else f"Before trying the hotkey, enable Wayfinder Aura in {pane}. "
                        "Input Monitoring may require using + to add the Applications copy."
                    ),
                    COLORS["accent_yellow"],
                    pady=(SPACING["md"], 0),
                )
                if missing == "install_location":
                    self._body_label(
                        body,
                        "After moving it, open Aura from Applications; this tour resumes here.",
                        muted=True,
                        pady=(SPACING["sm"], 0),
                    )
                    self._continue_button(
                        body, "open applications",
                        command=self.app._open_missing_macos_permission,
                    )
                    return
                # Aura picks new grants up live - no quit and reopen.
                self._continue_button(
                    body,
                    "turn on permissions",
                    command=self._show_permission_checklist,
                )
                return
        self._continue_button(body, "continue")

    def show_permissions_step(self) -> None:
        """Show the permissions checklist inside this pane (app entry point)."""
        if self._destroyed or self.flow.is_complete:
            return
        self.flow.detour_to("permissions")
        self._render_step()

    def _show_permission_checklist(self) -> None:
        self.show_permissions_step()

    def _render_dictate(self, body) -> None:
        ctk = self._ctk
        if self._transcript:
            self._body_label(body, "nice — that's dictation.")
            quote = self._transcript.strip()
            if len(quote) > _TRANSCRIPT_MAX:
                quote = quote[: _TRANSCRIPT_MAX - 1].rstrip() + "…"
            heard = ctk.CTkLabel(
                body,
                text=f'we heard: "{quote}"',
                font=(FONTS["body"][0], FONT_SIZES["body"]),
                text_color=COLORS["text_primary"],
                wraplength=self._wraplength(),
                justify="left",
            )
            heard.pack(anchor="w", pady=(SPACING["sm"], 0))
            self._wrap_labels.append(heard)
            self._login_item_choice(body)
            self._continue_button(body, "done", gold=True)
        elif self._help_shown:
            self._render_dictate_help(body)
        else:
            self._body_label(body, "try it now — dictate a sentence.")
            self._body_label(
                body,
                "listening for your first dictation…",
                muted=True,
                pady=(SPACING["sm"], 0),
            )
            self._schedule_help()

    def _login_item_choice(self, body) -> None:
        """macOS: offer "open at login" where it matters - right after the first
        dictation works. A visible, pre-ticked box the user can untick; applied
        on "done" (SMAppService, listed in System Settings ▸ Login Items)."""
        self._login_item_var = None
        if sys.platform != "darwin":
            return
        try:
            from wayfinder.utils import macos_login_item

            if not macos_login_item.available() or macos_login_item.is_enabled():
                return
        except Exception:
            return
        ctk = self._ctk
        self._login_item_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            body, text="open aura when I log in", variable=self._login_item_var,
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            text_color=COLORS["text_secondary"], fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"], border_color=COLORS["border_rim"],
            corner_radius=RADIUS["xs"], checkbox_width=18, checkbox_height=18,
        ).pack(anchor="w", pady=(SPACING["md"], 0))

    def _apply_login_item_choice(self) -> None:
        var = getattr(self, "_login_item_var", None)
        if var is None or not var.get():
            return
        try:
            from wayfinder.utils import macos_login_item

            ok, message = macos_login_item.set_enabled(True)
            self.app.log(("✓ " if ok else "⚠ ") + (message or "Aura will open when you log in"))
        except Exception:
            pass

    def _render_dictate_help(self, body) -> None:
        """The recovery variant of the dictate step, shown once the wait elapses
        without a transcript. Re-shows the hotkey token and offers a clear exit."""
        ctk = self._ctk
        self._body_label(body, "try it now — dictate a sentence.")
        token = ctk.CTkLabel(
            body,
            text=self._hotkey_text(),
            font=(FONTS["mono"][0], FONT_SIZES["title"], "bold"),
            text_color=COLORS["accent"],
        )
        token.pack(anchor="w", pady=(SPACING["sm"], 0))
        self._body_label(
            body,
            self._dictation_error or "having trouble? check your mic and press your hotkey — or skip for now.",
            muted=True,
            pady=(SPACING["sm"], 0),
        )
        self._continue_button(body, "skip for now", command=self._on_skip)

    def _schedule_help(self) -> None:
        """Arm the single-shot recovery timer (idempotent)."""
        if self._help_after_id is not None or self._help_shown:
            return
        try:
            self._help_after_id = self.card.after(_HELP_DELAY_MS, self._on_help_timeout)
        except Exception:
            self._help_after_id = None

    def _cancel_help(self) -> None:
        if self._help_after_id is not None:
            try:
                self.card.after_cancel(self._help_after_id)
            except Exception:
                pass
            self._help_after_id = None

    def _on_help_timeout(self) -> None:
        self._help_after_id = None
        if self._transcript or self.flow.is_complete:
            return
        self._help_shown = True
        self._render_step()

    def _render_footer(self, pad) -> None:
        ctk = self._ctk
        footer = ctk.CTkFrame(self.card, fg_color="transparent")
        footer.pack(fill="x", side="bottom", padx=pad, pady=(0, SPACING["xl"]))

        # Dot progress.
        dots = ctk.CTkFrame(footer, fg_color="transparent")
        dots.pack(side="left")
        for i in range(len(self.flow.steps)):
            color = COLORS["accent"] if i == self.flow.index else COLORS["border"]
            dot = ctk.CTkFrame(dots, width=8, height=8, corner_radius=4, fg_color=color)
            dot.pack(side="left", padx=(0, SPACING["sm"]))
            dot.pack_propagate(False)

        # Skip link (bottom-right), always visible.
        skip = ctk.CTkLabel(
            footer,
            text="skip",
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            text_color=COLORS["accent"],
        )
        skip.pack(side="right")
        skip.bind("<Button-1>", lambda _e: self._on_skip())
        if hasattr(self.app, "_bind_link_hover"):
            try:
                self.app._bind_link_hover(skip, FONT_SIZES["small"])
            except Exception:
                pass

    # --- events --------------------------------------------------------------

    @property
    def is_microphone_test_running(self) -> bool:
        return self.flow.current == "mic" and self.flow.mic_test_state in {"starting", "recording", "checking"}

    def _start_model_download(self) -> None:
        if self._destroyed or not self.flow.begin_model_download():
            return
        self._render_step()

        def progress(pct, done, total):
            self._post_to_ui(
                lambda: self._update_model_progress(pct, done, total)
            )

        def complete(path):
            self._post_to_ui(lambda: self._finish_model_download(path))

        def error(message):
            self._post_to_ui(lambda: self._fail_model_download(message))

        try:
            self._model_downloader = self.app._start_welcome_model_download(
                self._model_choice,
                progress,
                complete,
                error,
            )
        except Exception as exc:
            self._fail_model_download(str(exc))

    def _update_model_progress(self, pct, done, total) -> None:
        if self._destroyed or self.flow.model_download_state != "downloading":
            return
        try:
            pct = max(0.0, min(1.0, float(pct)))
            if self._model_progress_bar is not None:
                self._model_progress_bar.set(pct)
            if self._model_progress_label is not None:
                done_mb = float(done) / (1024 * 1024)
                total_mb = float(total) / (1024 * 1024) if total else 0.0
                text = (
                    f"{done_mb:.0f} / {total_mb:.0f} MB  ·  {pct * 100:.0f}%"
                    if total_mb
                    else f"{done_mb:.0f} MB"
                )
                self._model_progress_label.configure(text=text)
        except Exception:
            pass

    def _finish_model_download(self, path) -> None:
        if self._destroyed or not self.flow.complete_model_download():
            return
        try:
            self.app._on_whisper_model_ready(path)
        except Exception as exc:
            self.flow.fail_model_download(str(exc))
        self._render_step()

    def _fail_model_download(self, message) -> None:
        if self._destroyed:
            return
        self.flow.fail_model_download(str(message))
        self._render_step()

    def _start_mic_test(self) -> None:
        if self._destroyed or self.is_microphone_test_running:
            return
        token = self.flow.begin_mic_test()
        if token is None:
            return
        self._transcript = None
        self._render_step()

        try:
            from wayfinder.core.recorder import AudioRecorder

            recorder = AudioRecorder(
                sample_rate=int(self.app.config.get("sample_rate", 16000)),
                channels=1,
                device=getattr(
                    self.app,
                    "_resolved_audio_device",
                    self.app.config.get("audio_device"),
                ),
                preprocessing=self.app.config.get("audio_preprocessing", "light"),
                # This is the exact stream Aura will use for dictation, including
                # packaged PipeWire capture and stale-device healing.
                warm_mic=getattr(self.app, "warm_mic", None),
            )
        except Exception as exc:
            self._finish_mic_start_error(token, exc)
            return

        self._mic_test_recorder = recorder

        def open_stream():
            try:
                recorder.start()
            except Exception as exc:
                error = exc
                self._dispose_mic_recorder(recorder)
                self._post_to_ui(lambda error=error: self._finish_mic_start_error(token, error))
                return
            if self._destroyed or token != self.flow.mic_test_generation:
                self._dispose_mic_recorder(recorder)
                return
            self._post_to_ui(lambda: self._finish_mic_start(token, recorder))

        import threading

        threading.Thread(
            target=open_stream,
            daemon=True,
            name="welcome-microphone-open",
        ).start()

    def _finish_mic_start(self, token: int, recorder) -> None:
        if self._destroyed or recorder is not self._mic_test_recorder or not self.flow.mark_mic_recording(token):
            self._dispose_mic_recorder(recorder)
            return
        import time

        self._mic_test_started_at = time.monotonic()
        self._render_step()

    def _finish_mic_start_error(self, token: int, error: object) -> None:
        if self._destroyed or token != self.flow.mic_test_generation:
            return
        self._mic_test_recorder = None
        guidance = microphone_error_guidance(error)
        self.flow.fail_mic_test(guidance, token)
        try:
            self.app.log(f"⚠ Welcome microphone test: {error}")
        except Exception:
            pass
        self._render_step()

    def _schedule_mic_meter_poll(self) -> None:
        if self._mic_test_after_id is not None or self._destroyed:
            return
        try:
            self._mic_test_after_id = self.card.after(50, self._poll_mic_meter)
        except Exception:
            self._mic_test_after_id = None

    def _cancel_mic_meter_poll(self) -> None:
        if self._mic_test_after_id is None:
            return
        try:
            self.card.after_cancel(self._mic_test_after_id)
        except Exception:
            pass
        self._mic_test_after_id = None

    def _poll_mic_meter(self) -> None:
        self._mic_test_after_id = None
        recorder = self._mic_test_recorder
        if self._destroyed or self.flow.mic_test_state != "recording" or recorder is None:
            return
        try:
            level = max(0.0, min(1.0, float(recorder.get_audio_level())))
            if self._mic_level_bar is not None:
                self._mic_level_bar.set(level)
        except Exception:
            level = 0.0

        import time

        elapsed = max(0.0, time.monotonic() - (self._mic_test_started_at or time.monotonic()))
        if self._mic_level_label is not None:
            try:
                remaining = max(0, int(_MIC_TEST_DURATION_S - elapsed + 0.999))
                self._mic_level_label.configure(text=f"listening… {remaining}s")
            except Exception:
                pass
        if elapsed >= _MIC_TEST_DURATION_S:
            self._stop_mic_test()
        else:
            self._schedule_mic_meter_poll()

    def _stop_mic_test(self) -> None:
        recorder = self._mic_test_recorder
        token = self.flow.mic_test_generation
        if recorder is None or not self.flow.mark_mic_checking(token):
            return
        self._cancel_mic_meter_poll()
        self._mic_test_recorder = None
        self._render_step()

        def stop_and_assess():
            try:
                recorder.stop()
                result = assess_microphone_capture(recorder)
            except Exception as exc:
                result = MicrophoneTestResult(
                    False,
                    microphone_error_guidance(exc),
                    0.0,
                    0.0,
                )
                try:
                    self.app.log(f"⚠ Welcome microphone test: {exc}")
                except Exception:
                    pass
            finally:
                try:
                    recorder.cleanup()
                except Exception:
                    pass
            self._post_to_ui(lambda: self._finish_mic_assessment(token, result))

        import threading

        threading.Thread(
            target=stop_and_assess,
            daemon=True,
            name="welcome-microphone-check",
        ).start()

    def _finish_mic_assessment(self, token: int, result: MicrophoneTestResult) -> None:
        if self._destroyed or token != self.flow.mic_test_generation:
            return
        if result.passed:
            self.flow.pass_mic_test(token)
        else:
            self.flow.fail_mic_test(result.message, token)
        self._render_step()

    def _post_to_ui(self, callback) -> None:
        if self._destroyed:
            return
        try:
            self.app.after(0, callback)
        except Exception:
            pass

    @staticmethod
    def _dispose_mic_recorder(recorder) -> None:
        # ``WarmMic.acquire`` attaches its sink before opening the stream. Even
        # a failed open therefore needs stop() to release that sink.
        try:
            recorder.stop()
        except Exception:
            pass
        try:
            recorder.cleanup()
        except Exception:
            pass

    def _cancel_mic_test(self) -> None:
        state = self.flow.mic_test_state
        self.flow.invalidate_mic_test()
        self._cancel_mic_meter_poll()
        recorder = self._mic_test_recorder
        self._mic_test_recorder = None
        if recorder is None or state == "starting":
            # The open worker owns a starting recorder; its now-stale callback
            # disposes it after the blocking open returns.
            return
        import threading

        threading.Thread(
            target=self._dispose_mic_recorder,
            args=(recorder,),
            daemon=True,
            name="welcome-microphone-cancel",
        ).start()

    def _on_continue(self) -> None:
        if not self.flow.advance():
            return
        if not self.flow.is_complete:
            self._render_step()
        # If completing, _complete_flow() already tore the pane down.

    def _on_skip(self) -> None:
        self.flow.skip()  # fires _complete_flow -> teardown

    def receive_transcript(self, text: str) -> None:
        """The app hands a suppressed dictation transcript to the pane.

        A real dictation on the mic step proves the entire audio path and satisfies
        its gate. On the hotkey step it fast-forwards to the final demonstration.
        """
        if self.flow.is_complete:
            return
        self._cancel_help()
        self._transcript = text or ""
        self._dictation_error = ""
        if self.flow.current == "mic":
            self._cancel_mic_test()
            self.flow.pass_mic_test()
        elif self.flow.current != "dictate":
            self.flow.goto("dictate")
        self._render_step()

    def receive_error(self, message: str) -> None:
        """Surface production dictation errors inside the covering welcome pane."""
        if self.flow.is_complete:
            return
        guidance = microphone_error_guidance(message) if self.flow.current == "mic" else message
        if self.flow.current == "mic":
            self._cancel_mic_test()
            self.flow.fail_mic_test(guidance)
        else:
            self._dictation_error = guidance
            self._help_shown = True
        self._render_step()

    # --- completion ----------------------------------------------------------

    def _complete_flow(self) -> None:
        """Called once by the flow on complete/skip: persist + tear down."""
        if self._only_permissions:
            self._teardown()
            return
        try:
            self.app.config["welcome_completed"] = True
            save_config(self.app.config)
        except Exception:
            pass
        self._apply_login_item_choice()
        # Collapse first-session Dictate tips once the tour is done.
        try:
            if hasattr(self.app, "_hide_dictate_tips"):
                self.app._hide_dictate_tips()
        except Exception:
            pass
        self._teardown()

    def _teardown(self) -> None:
        self._destroyed = True
        downloader = self._model_downloader
        self._model_downloader = None
        if downloader is not None:
            try:
                downloader.cancel_download()
            except Exception:
                pass
        self._cancel_mic_test()
        self._cancel_help()
        self._cancel_permission_poll()
        if self._only_permissions:
            try:
                self.app._permissions_pane = None
                self.app._refresh_macos_permission_banner()
            except Exception:
                pass
        else:
            # Clear the app-side flag so normal injection resumes.
            try:
                self.app._welcome_active = False
                self.app._welcome_pane = None
            except Exception:
                pass
        try:
            self.underlay.destroy()
        except Exception:
            pass

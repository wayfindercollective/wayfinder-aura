"""
First-run welcome tour for Wayfinder Aura.

Two layers:

- ``WelcomeFlow`` — a tiny, pure state machine (steps: mic -> hotkey -> dictate).
  It has NO Tk/customtkinter dependency and is headless-testable.
- ``WelcomePane`` — the in-window card that renders the flow over the tab content
  area. It imports customtkinter lazily (inside the builder), so importing this
  module and using ``WelcomeFlow`` works without a display.

Design brief: the first five minutes feel guided and calm. One card, three steps,
dot progress, always skippable, max restraint — matched to the app's design
language (theme tokens, caps + divider aesthetic, blue brand accent).

Injection safety: while the pane is active the app suppresses text injection and
routes each dictation transcript here via ``receive_transcript`` — the product
demos itself without typing into whatever window happens to be focused.
"""
from __future__ import annotations

from wayfinder.config import save_config
from wayfinder.hotkeys.display import UNBOUND_SHORT, is_unbound
from wayfinder.ui.theme import COLORS, FONT_SIZES, FONTS, RADIUS, SPACING


class WelcomeFlow:
    """Pure state machine for the 3-step welcome tour.

    No Tk imports — importable and testable headlessly. ``on_complete`` fires
    exactly once, whether the flow is completed (advance past the last step) or
    skipped.
    """

    STEPS = ("mic", "hotkey", "dictate")

    def __init__(self, on_complete=None):
        self.steps = list(self.STEPS)
        self._index = 0
        self.is_complete = False
        self.on_complete = on_complete
        self._fired = False

    @property
    def current(self):
        """The current step id, or ``None`` once complete."""
        if self.is_complete or self._index >= len(self.steps):
            return None
        return self.steps[self._index]

    @property
    def index(self) -> int:
        return self._index

    def advance(self) -> None:
        """Move to the next step; completing the flow if past the last step."""
        if self.is_complete:
            return
        if self._index < len(self.steps) - 1:
            self._index += 1
        else:
            self._complete()

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

# Design size of the card. These are a CAP, not a fixed size: the card shrinks
# to whatever the tab-content area actually offers. On a Steam Deck the cavity
# is 407x430 px (639-wide window - 40 content padding - 180 sidebar - 12 gap),
# so a hard 520x360 card overflowed by 113 px and lost ~56 px off each side.
_CARD_MAX_W = 520
_CARD_MAX_H = 360
# Breathing room between the card and the edge of the pane it sits in.
_CARD_MARGIN = 12
# Horizontal padding inside the card (header/body/footer share one margin, see
# ``_render_step``), doubled for both sides plus a little slack for the border.
_CARD_TEXT_INSET = 80


def fit_card_size(host_w, host_h, *, max_w=_CARD_MAX_W, max_h=_CARD_MAX_H,
                  margin=_CARD_MARGIN):
    """Card size for a host of ``host_w`` x ``host_h``: capped, never overflowing.

    Pure and Tk-free so the sizing rule is testable headlessly (same split as
    ``WelcomeFlow`` vs ``WelcomePane``).

    There is deliberately NO minimum size. A floor such as
    ``max(MIN_W, min(MAX_W, available))`` re-introduces the very bug this fixes
    whenever the host is narrower than the floor — the card would again be wider
    than the cavity it lives in. Shrinking past comfortable is the correct
    failure mode; overflowing off-screen is not.

    A non-positive host means "not laid out yet": return the design size and let
    the ``<Configure>`` refit correct it once real geometry arrives.
    """
    def _axis(host, cap):
        if host <= 0:
            return cap
        return max(1, min(cap, host - 2 * margin))

    return _axis(host_w, max_w), _axis(host_h, max_h)

# How long the final "try it now" step waits for a first transcript before it
# surfaces a troubleshooting line + skip button. Single-shot (see rule #1).
_HELP_DELAY_MS = 13000


class WelcomePane:
    """Renders ``WelcomeFlow`` as a centered card over the tab content area.

    ``parent`` is the container to cover (the app's ``tab_content_container``);
    ``app`` is the ``WayfinderApp`` instance (used for the live hotkey display,
    the link-hover helper, and config persistence).
    """

    # Per-step card titles (rendered in the fixed header, above the divider).
    _STEP_TITLES = {
        "mic": "welcome to wayfinder aura",
        "hotkey": "your hotkey",
        "dictate": "try it now",
    }

    def __init__(self, parent, app):
        import customtkinter as ctk  # lazy: keep the module headless-importable

        self._ctk = ctk
        self.parent = parent
        self.app = app
        self.flow = WelcomeFlow(on_complete=self._complete_flow)
        self._transcript = None
        # Final-step recovery: if the first dictation never lands, a single-shot
        # timer swaps the calm "listening…" line for a troubleshooting affordance.
        self._help_after_id = None
        self._help_shown = False
        # Labels whose wraplength tracks the card width. Rebuilt by every
        # _render_step; re-measured (without a re-render) by every refit.
        self._wrapped = []

        # Full-size dim underlay. CTk has no real alpha; a plain bg_base frame
        # covering the tab content reads as a focused/modal state.
        self.underlay = ctk.CTkFrame(parent, fg_color=COLORS["bg_base"], corner_radius=0)
        self.underlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Single centered card, sized to the host rather than to a constant —
        # see fit_card_size. Start at the design cap; the <Configure> refit
        # below shrinks it as soon as the underlay has real geometry.
        self._card_size = (_CARD_MAX_W, _CARD_MAX_H)
        self.card = ctk.CTkFrame(
            self.underlay,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_rim"],
            width=_CARD_MAX_W,
            height=_CARD_MAX_H,
        )
        self.card.place(relx=0.5, rely=0.5, anchor="center")
        self.card.pack_propagate(False)

        # add="+": CTkFrame binds <Configure> itself for corner/border redraw;
        # replacing that binding would break the frame's own rendering.
        self.underlay.bind("<Configure>", self._on_host_resize, add="+")

        self._render_step()
        self._refit_card()

    # --- responsive sizing ---------------------------------------------------

    def _on_host_resize(self, _event=None) -> None:
        self._refit_card()

    def _refit_card(self) -> None:
        """Resize the card to the underlay, capped at the design size.

        Idempotent: a no-op when the computed size is unchanged, so the
        <Configure> storm CTk frames generate costs one comparison. Never
        re-renders the step — destroying and rebuilding children here would
        restart the dictate step's single-shot help timer on every resize.
        """
        try:
            w, h = fit_card_size(self.underlay.winfo_width(), self.underlay.winfo_height())
        except Exception:
            return
        if (w, h) == self._card_size:
            return
        self._card_size = (w, h)
        try:
            self.card.configure(width=w, height=h)
        except Exception:
            return
        self._apply_wraplength()

    def _wrap_width(self) -> int:
        """Wraplength for text inside the card, derived from its live width."""
        return max(120, self._card_size[0] - _CARD_TEXT_INSET)

    def _track_wrapped(self, label):
        """Register a label so refits can re-wrap it without a re-render."""
        self._wrapped.append(label)
        return label

    def _apply_wraplength(self) -> None:
        wrap = self._wrap_width()
        for label in list(self._wrapped):
            try:
                label.configure(wraplength=wrap)
            except Exception:
                # Destroyed by a re-render between refits — drop it.
                try:
                    self._wrapped.remove(label)
                except ValueError:
                    pass

    # --- helpers -------------------------------------------------------------

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
            wraplength=self._wrap_width(),
            justify="left",
        )
        lbl.pack(anchor="w", pady=pady)
        return self._track_wrapped(lbl)

    # --- rendering -----------------------------------------------------------

    def _render_step(self) -> None:
        ctk = self._ctk
        if self.flow.is_complete:
            return
        # Children are about to be destroyed; the wrap registry must not
        # outlive them (refits would then configure dead widgets).
        self._wrapped = []
        for child in self.card.winfo_children():
            child.destroy()

        pad = SPACING["xl"]  # one horizontal margin for header, body AND footer (they used to disagree: 28 vs 22)

        # Title + divider: a fixed header pinned to the top of the card.
        header = ctk.CTkFrame(self.card, fg_color="transparent")
        header.pack(side="top", fill="x", padx=pad, pady=(pad, 0))
        self._title(header, self._STEP_TITLES.get(self.flow.current, ""))

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
        if step == "mic":
            self._render_mic(body)
        elif step == "hotkey":
            self._render_hotkey(body)
        elif step == "dictate":
            self._render_dictate(body)

    def _render_mic(self, body) -> None:
        self._body_label(body, "let's make sure your mic is live.")
        # Level-meter choice: there is no cheap always-on level source when we
        # aren't recording (the mic level only exists during an active recorder
        # loop), so rather than spin up a recorder just for a readout we point the
        # user at the live hero waveform, which already reacts to input.
        self._body_label(
            body,
            "speak and watch the hero waveform respond above.",
            muted=True,
            pady=(SPACING["sm"], 0),
        )
        self._continue_button(body, "continue")

    def _render_hotkey(self, body) -> None:
        ctk = self._ctk
        live = self._hotkey_text()
        unbound = is_unbound(live)
        token = ctk.CTkLabel(
            body,
            # The unbound label names a fix ("System Settings → Shortcuts")
            # that cannot fit a display-size token on a 383 px Deck card, so
            # the token carries the short form and the body copy explains.
            text=UNBOUND_SHORT if unbound else live,
            font=(FONTS["mono"][0], FONT_SIZES["display"] + 8, "bold"),
            text_color=COLORS["text_muted"] if unbound else COLORS["accent"],
            wraplength=self._wrap_width(),
            justify="left",
        )
        self._track_wrapped(token)
        token.pack(anchor="w", pady=(0, SPACING["md"]))
        if unbound:
            self._body_label(
                body,
                "your desktop hasn't bound a shortcut yet — set one in "
                "System Settings → Shortcuts, under Wayfinder Aura.",
            )
        else:
            self._body_label(
                body,
                "press it once to start a dictation — press it again to stop.",
            )
        self._continue_button(body, "continue")

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
                wraplength=self._wrap_width(),
                justify="left",
            )
            self._track_wrapped(heard)
            heard.pack(anchor="w", pady=(SPACING["sm"], 0))
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

    def _render_dictate_help(self, body) -> None:
        """The recovery variant of the dictate step, shown once the wait elapses
        without a transcript. Re-shows the hotkey token and offers a clear exit."""
        ctk = self._ctk
        live = self._hotkey_text()
        unbound = is_unbound(live)
        self._body_label(body, "try it now — dictate a sentence.")
        token = ctk.CTkLabel(
            body,
            # Same treatment as _render_hotkey: the long unbound label cannot
            # fit a title-size token on a 383 px Deck card (Codex review).
            text=UNBOUND_SHORT if unbound else live,
            font=(FONTS["mono"][0], FONT_SIZES["title"], "bold"),
            text_color=COLORS["text_muted"] if unbound else COLORS["accent"],
            wraplength=self._wrap_width(),
            justify="left",
        )
        self._track_wrapped(token)
        token.pack(anchor="w", pady=(SPACING["sm"], 0))
        self._body_label(
            body,
            "no shortcut is bound yet — set one in System Settings → Shortcuts."
            if unbound else
            "having trouble? check your mic and press your hotkey — or skip for now.",
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

    def _on_continue(self) -> None:
        self.flow.advance()
        if not self.flow.is_complete:
            self._render_step()
        # If completing, _complete_flow() already tore the pane down.

    def _on_skip(self) -> None:
        self.flow.skip()  # fires _complete_flow -> teardown

    def refresh_hotkey_display(self) -> None:
        """Re-render if the live hotkey label changed under us.

        The portal reports what is actually bound asynchronously, and that can
        land while the card is already showing the hotkey — which would
        otherwise keep displaying the stale config chord (Codex review).

        Two steps render the token: "hotkey", and "dictate" once its
        troubleshooting variant is up. Re-rendering the latter is safe — the
        help state does not re-arm the single-shot timer, and the
        transcript variant shows no token at all, so it is skipped. A no-op
        everywhere else, and cheap enough to call unconditionally.
        """
        if self.flow.is_complete:
            return
        step = self.flow.current
        shows_token = step == "hotkey" or (
            step == "dictate" and self._help_shown and not self._transcript
        )
        if not shows_token:
            return
        try:
            self._render_step()
        except Exception:
            pass

    def receive_transcript(self, text: str) -> None:
        """The app hands a suppressed dictation transcript to the pane.

        On steps 1-2 a dictation means the user explored the hotkey early — we
        fast-forward to step 3 and show what we heard.
        """
        if self.flow.is_complete:
            return
        self._cancel_help()
        self._transcript = text or ""
        if self.flow.current != "dictate":
            self.flow.goto("dictate")
        self._render_step()

    # --- completion ----------------------------------------------------------

    def _complete_flow(self) -> None:
        """Called once by the flow on complete/skip: persist + tear down."""
        try:
            self.app.config["welcome_completed"] = True
            save_config(self.app.config)
        except Exception:
            pass
        # Collapse first-session Dictate tips once the tour is done.
        try:
            if hasattr(self.app, "_hide_dictate_tips"):
                self.app._hide_dictate_tips()
        except Exception:
            pass
        self._teardown()

    def _teardown(self) -> None:
        self._cancel_help()
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

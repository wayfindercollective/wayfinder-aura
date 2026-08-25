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

from dataclasses import dataclass

from wayfinder.config import save_config
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
        self.mic_test_state = "idle"
        self.mic_test_error = ""
        self._mic_test_generation = 0

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
        return self.current != "mic" or self.mic_test_state == "passed"

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
        self._dictation_error = ""
        self._destroyed = False
        self._mic_test_recorder = None
        self._mic_test_after_id = None
        self._mic_test_started_at = None
        self._mic_level_bar = None
        self._mic_level_label = None
        # Final-step recovery: if the first dictation never lands, a single-shot
        # timer swaps the calm "listening…" line for a troubleshooting affordance.
        self._help_after_id = None
        self._help_shown = False

        # Full-size dim underlay. CTk has no real alpha; a plain bg_base frame
        # covering the tab content reads as a focused/modal state.
        self.underlay = ctk.CTkFrame(parent, fg_color=COLORS["bg_base"], corner_radius=0)
        self.underlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        # Single centered card.
        self.card = ctk.CTkFrame(
            self.underlay,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_rim"],
            width=520,
            height=360,
        )
        self.card.place(relx=0.5, rely=0.5, anchor="center")
        self.card.pack_propagate(False)

        self._render_step()

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
            wraplength=440,
            justify="left",
        )
        lbl.pack(anchor="w", pady=pady)
        return lbl

    def _status_label(self, parent, text, color, pady=(SPACING["sm"], 0)):
        ctk = self._ctk
        lbl = ctk.CTkLabel(
            parent,
            text=text,
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            text_color=color,
            wraplength=440,
            justify="left",
        )
        lbl.pack(anchor="w", pady=pady)
        return lbl

    # --- rendering -----------------------------------------------------------

    def _render_step(self) -> None:
        ctk = self._ctk
        if self.flow.is_complete:
            return
        self._cancel_mic_meter_poll()
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
                wraplength=440,
                justify="left",
            )
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
        self._destroyed = True
        self._cancel_mic_test()
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

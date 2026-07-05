"""First-run dependency setup, rendered as an in-window pane.

This replaces the old modal ``SetupWizard`` (a ``CTkToplevel`` with
``transient`` + ``grab_set``). Everything now lives INSIDE the main window: a
full-cover pane placed over the tab content — exactly the mechanic
``welcome.py`` uses — so there is no popup dialog, no Wayland focus/sizing bug,
and no ``grab_set``.

Two layers, matching ``welcome.py``:

- ``SetupFlow`` — a tiny, pure state machine (complete / skip -> fire
  ``on_complete`` exactly once, tracking ``result``: True = completed,
  False = skipped). NO Tk/customtkinter dependency, headless-testable.
- ``SetupPane`` — the in-window pane that renders the dependency checklist and
  drives the SAME setup engine in ``core.setup`` (checks, install-all,
  whisper.cpp build, model download) via background threads marshalled back to
  the Tk main loop through a queue polled with ``app.after`` (>=100ms cadence,
  per CLAUDE.md rule 1). customtkinter is imported lazily inside the pane, so
  importing this module and using ``SetupFlow`` works without a display.

Engine ownership: ``core/setup.py`` is untouched — this module only calls its
public functions (``get_dependencies``, ``install_system_packages``,
``build_whisper_cpp``, ``download_whisper_model`` …), the same ones the old
wizard used.
"""
from __future__ import annotations

import queue
import sys
import threading
from typing import Optional

from wayfinder.config import IS_APPIMAGE, IS_FLATPAK, save_config
from wayfinder.core.setup import (
    WHISPER_MODELS,
    Dependency,
    DependencyStatus,
    build_whisper_cpp,
    download_whisper_model,
    get_dependencies,
    get_missing_system_packages,
    get_recommended_model,
    install_system_packages,
)
from wayfinder.ui.theme import COLORS, FONT_SIZES, FONTS, RADIUS, SPACING


# ─── Pure flow state machine (headless-testable) ─────────────────────────────


class SetupFlow:
    """Pure completion state for the first-run setup.

    Unlike the 3-step ``WelcomeFlow`` there are no ordered steps — setup is a
    single checklist that ends one of two ways. ``on_complete`` fires exactly
    once (whether the user completes or skips). ``result`` records which:
    ``True`` = completed, ``False`` = skipped — preserving the old
    ``SetupWizard.result`` contract.
    """

    def __init__(self, on_complete=None):
        self.is_complete = False
        self.result = False
        self.on_complete = on_complete
        self._fired = False

    def complete(self) -> None:
        """User finished setup (all required deps met + Continue)."""
        self._finish(result=True)

    def skip(self) -> None:
        """User skipped the rest of setup."""
        self._finish(result=False)

    def _finish(self, result: bool) -> None:
        if self.is_complete:
            return
        self.is_complete = True
        self.result = result
        if self.on_complete is not None and not self._fired:
            self._fired = True
            self.on_complete()


class SetupSequence:
    """Pure, headless sequencing for the guided auto-install.

    The widget builds an ordered list of step ids (in dependency order) and hands
    it here. This class owns ONLY the state machine — which step is active, what's
    still queued / done / skipped, and the transitions between them (advance on
    success, pause on failure, retry, skip-step, cancel). It runs NO installs and
    touches NO Tk / core.setup: the widget asks it for the active step, runs the
    real install for that id off-thread, then reports the outcome back. Keeping
    the sequencing HERE (not in widget code) is what makes the new auto-advance
    flow headless-testable — mirroring how ``SetupFlow`` isolates completion.

    Lifecycle::

        seq = SetupSequence(["system_packages", "whisper_cpp", "whisper_model"])
        sid = seq.start()                 # -> "system_packages" (now ACTIVE)
        # ... run it ...
        sid = seq.on_success(sid)         # -> "whisper_cpp"
        # ... it fails / needs the user ...
        seq.on_failure(sid)               # pauses HERE; not finished
        sid = seq.retry()                 # re-activate the paused step
        # ... or give up on just this step and move on ...
        sid = seq.skip_step()             # -> "whisper_model"
        sid = seq.on_success(sid)         # -> None, seq.finished is True
    """

    QUEUED = "queued"
    ACTIVE = "active"
    DONE = "done"
    PAUSED = "paused"
    SKIPPED = "skipped"

    def __init__(self, step_ids):
        self.steps = list(step_ids)
        self.status = {sid: self.QUEUED for sid in self.steps}
        self.active = None
        self.cancelled = False

    # ── queries ──
    @property
    def finished(self) -> bool:
        """True once nothing is active or queued (all done/skipped), or cancelled.

        A PAUSED step is NOT finished — it keeps ``active`` set and awaits
        ``retry()`` / ``skip_step()``.
        """
        if self.cancelled:
            return True
        return self.active is None and not any(
            s == self.QUEUED for s in self.status.values()
        )

    def position(self, sid) -> tuple:
        """1-based ``(index, total)`` of ``sid`` — for a "step 2 of 3" label."""
        try:
            return (self.steps.index(sid) + 1, len(self.steps))
        except ValueError:
            return (0, len(self.steps))

    def remaining(self) -> list:
        """Step ids still queued (not yet started) — the visual "up next" list."""
        return [s for s in self.steps if self.status[s] == self.QUEUED]

    # ── transitions ──
    def start(self):
        """Activate the first queued step. Returns its id, or None if there's
        nothing to run (→ immediately ``finished``)."""
        return self._activate_next()

    def _activate_next(self):
        if self.cancelled:
            self.active = None
            return None
        for sid in self.steps:
            if self.status[sid] == self.QUEUED:
                self.status[sid] = self.ACTIVE
                self.active = sid
                return sid
        self.active = None
        return None

    def on_success(self, sid):
        """Mark the active step DONE and activate the next. Returns next id/None."""
        if self.status.get(sid) == self.ACTIVE:
            self.status[sid] = self.DONE
        return self._activate_next()

    def on_failure(self, sid):
        """Pause on a failed step (or one needing user interaction).

        Leaves ``active`` pointing at the paused step so ``retry()`` re-runs it;
        the queue does NOT advance. Returns None.
        """
        if self.status.get(sid) == self.ACTIVE:
            self.status[sid] = self.PAUSED
        self.active = sid
        return None

    def retry(self, sid=None):
        """Re-activate the paused step. Returns its id (now ACTIVE again)."""
        sid = sid or self.active
        if self.status.get(sid) == self.PAUSED:
            self.status[sid] = self.ACTIVE
            self.active = sid
        return self.active

    def skip_step(self, sid=None):
        """Give up on just the paused/active step and advance the queue.

        Returns the next step id, or None if that was the last one.
        """
        sid = sid or self.active
        if self.status.get(sid) in (self.PAUSED, self.ACTIVE):
            self.status[sid] = self.SKIPPED
        return self._activate_next()

    def cancel(self):
        """Skip Setup — cancel the whole queue. Every still-queued step becomes
        SKIPPED and no further step will start. Any in-flight (active/paused) step
        is abandoned by the widget between steps via the cancelled flag."""
        self.cancelled = True
        for sid in self.steps:
            if self.status[sid] == self.QUEUED:
                self.status[sid] = self.SKIPPED
        self.active = None


# ─── Pure footer/status logic (headless-testable) ────────────────────────────


def all_required_ok(deps) -> bool:
    """True when every REQUIRED dependency is satisfied.

    A required dep whose status carries a *blocking* warning (e.g. the ydotool
    binary is installed but its daemon is down, so text injection will actually
    fail) does NOT count as OK — otherwise Setup would declare "All set!" while
    typing is silently broken (Codex review F11). The row still shows its gold
    warning badge; only the top-level gate treats it as not-ok.
    """
    return all(
        d.is_ok and not (d.status is not None and d.status.blocking_warning)
        for d in deps if d.required
    )


def any_missing(deps) -> bool:
    """True when any dependency (required or optional) is not yet satisfied."""
    return any(not d.is_ok for d in deps)


def footer_decision(deps, installing: bool) -> dict:
    """Decide footer control visibility from the current dependency state.

    Pure — mirrors the old wizard's ``_update_footer_state`` so it can be unit
    tested without Tk. Returns a dict:

    * ``show_continue`` — reveal the Continue button (all required deps met and
      not mid-install);
    * ``show_install`` — the "Install All Missing" button is still relevant
      (something is missing);
    * ``install_enabled`` — the install button should be clickable.
    """
    req_ok = all_required_ok(deps)
    missing = any_missing(deps)

    if req_ok and not installing:
        return {
            "show_continue": True,
            # Everything installed -> hide install button entirely.
            "show_install": missing,
            "install_enabled": missing,
        }
    return {
        "show_continue": False,
        "show_install": True,
        "install_enabled": not installing,
    }


def should_show_model_selector(deps) -> bool:
    """True while the Whisper model dependency is still unmet (offer a picker)."""
    model_dep = next((d for d in deps if d.id == "whisper_model"), None)
    return model_dep is not None and not model_dep.is_ok


# ─── Pure first-run orchestration (headless-testable) ────────────────────────


def first_run_plan(setup_completed: bool, welcome_completed: bool, frozen: bool) -> dict:
    """Decide what the app schedules at startup for the first-run flow.

    Pure — this is the branching main.py used to spell out inline, lifted here so
    it can be unit tested without launching Tk. Frozen (.app) builds bundle every
    dependency AND crash on the Linux package-manager probes, so setup is treated
    as already complete there.

    Returns a dict:

    * ``show_setup`` — schedule the inline setup pane (its completion chains to
      the welcome tour via ``should_chain_welcome``);
    * ``show_welcome`` — schedule the welcome tour DIRECTLY (setup already done,
      welcome still pending).

    At most one is True; both False means the first-run flow is finished.
    """
    setup_done = setup_completed or frozen
    if not setup_done:
        return {"show_setup": True, "show_welcome": False}
    if not welcome_completed:
        return {"show_setup": False, "show_welcome": True}
    return {"show_setup": False, "show_welcome": False}


def should_chain_welcome(welcome_completed: bool) -> bool:
    """After setup completes/skips, hand off to the welcome tour iff it's pending."""
    return not welcome_completed


# ─── In-window pane ──────────────────────────────────────────────────────────

# Status glyph + color per dependency-row state (module-scope: pure token dicts,
# no Tk). Mirrors the old wizard's checklist affordances.
_STATUS_ICONS = {
    "checking": ("...", COLORS["text_muted"]),
    "ok": ("OK", COLORS["state_typing"]),            # Mint
    "warning": ("OK", COLORS["state_processing"]),   # Gold
    "missing": ("--", COLORS["state_recording"]),    # Rose
    "missing_opt": ("--", COLORS["text_muted"]),     # Dim for optional
    "installing": ("...", COLORS["accent"]),          # Violet
    "queued": ("··", COLORS["text_muted"]),          # Dim: waiting in the auto-install queue
    "failed": ("!!", COLORS["state_recording"]),     # Rose: step paused on a failure
}


class _DepRow:
    """One dependency row (status glyph + name/detail + progress + action).

    Composition, not a ``CTkFrame`` subclass — keeps this module importable
    without customtkinter (``ctk`` is passed in by the pane).
    """

    def __init__(self, ctk, parent, dep: Dependency, on_install=None):
        self._ctk = ctk
        self._dep = dep
        self._on_install = on_install

        sz_name = FONT_SIZES["small"]      # 11
        sz_detail = FONT_SIZES["caption"]  # 10
        sz_status = FONT_SIZES["small"]    # 11

        self.frame = ctk.CTkFrame(
            parent, fg_color=COLORS["bg_card"], corner_radius=RADIUS["sm"]
        )

        self._status_label = ctk.CTkLabel(
            self.frame,
            text="...",
            width=28,
            font=(FONTS["mono"][0], sz_status, "bold"),
            text_color=COLORS["text_muted"],
        )
        self._status_label.pack(side="left", padx=(8, 0), pady=6)

        text_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=6)

        req_tag = "" if dep.required else "  (optional)"
        self._name_label = ctk.CTkLabel(
            text_frame,
            text=dep.name + req_tag,
            font=(FONTS["body"][0], sz_name, "bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
        )
        self._name_label.pack(anchor="w")

        self._detail_label = ctk.CTkLabel(
            text_frame,
            text=dep.description,
            font=(FONTS["body"][0], sz_detail),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        self._detail_label.pack(anchor="w")

        # Progress bar (hidden until a download reports progress). Driven only
        # via .set() from the worker marshaling — never .start() (its 20ms
        # internal loop is banned by CLAUDE.md rule 1).
        self._progress = ctk.CTkProgressBar(
            text_frame,
            height=3,
            fg_color=COLORS["bg_input"],
            progress_color=COLORS["accent"],
            corner_radius=RADIUS["xs"],
        )

        self._action_btn = ctk.CTkButton(
            self.frame,
            text="Install",
            width=70,
            height=26,
            font=(FONTS["body"][0], sz_detail),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["sm"],
            command=self._on_action,
        )

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)

    def update_status(self, status: Optional[DependencyStatus]):
        if status is None:
            self._set_state("checking")
            return

        if status.installed:
            if status.warning:
                self._set_state("warning")
                self._detail_label.configure(
                    text=status.warning, text_color=COLORS["state_processing"]
                )
            else:
                self._set_state("ok")
                if status.detail:
                    self._detail_label.configure(
                        text=status.detail, text_color=COLORS["text_muted"]
                    )
            self._action_btn.pack_forget()
        else:
            state = "missing" if self._dep.required else "missing_opt"
            self._set_state(state)
            error_text = status.error or self._dep.description
            self._detail_label.configure(
                text=error_text, text_color=COLORS["state_recording"]
            )
            # Per-row Install/Download buttons only appear when an ``on_install``
            # was wired (the old manual mode). The guided auto-install passes
            # ``on_install=None`` — the queue drives every step, and a paused step
            # is retried from the footer, not per row — so no button here.
            if self._on_install is not None and (self._dep.can_install or self._dep.id in (
                "ydotool", "build_tools", "cuda", "whisper_cpp", "whisper_model",
            )):
                btn_text = "Download" if self._dep.id == "whisper_model" else "Install"
                self._action_btn.configure(text=btn_text)
                self._action_btn.pack(side="right", padx=8, pady=6)

    def set_queued(self):
        """Waiting in the auto-install queue (not yet its turn)."""
        self._set_state("queued")
        self._action_btn.pack_forget()
        self._detail_label.configure(text="Queued…", text_color=COLORS["text_muted"])

    def set_failed(self, msg: str):
        """The step for this row paused on a failure."""
        self._set_state("failed")
        self._action_btn.pack_forget()
        self._detail_label.configure(
            text=msg or "Couldn't finish — see the log below.",
            text_color=COLORS["state_recording"],
        )

    def set_installing(self):
        self._set_state("installing")
        self._action_btn.configure(state="disabled", text="...")
        self._detail_label.configure(text="Installing...", text_color=COLORS["accent"])

    def update_progress(self, downloaded: int, total: int):
        if total > 0:
            pct = downloaded / total
            if not self._progress.winfo_ismapped():
                self._progress.pack(anchor="w", fill="x", pady=(4, 0))
            self._progress.set(pct)
            mb_dl = downloaded / 1_000_000
            mb_tot = total / 1_000_000
            self._detail_label.configure(
                text=f"Downloading... {mb_dl:.0f} / {mb_tot:.0f} MB ({pct * 100:.0f}%)",
                text_color=COLORS["accent"],
            )

    def _set_state(self, state: str):
        icon, color = _STATUS_ICONS.get(state, ("?", COLORS["text_muted"]))
        self._status_label.configure(text=icon, text_color=color)

    def _on_action(self):
        if self._on_install:
            self._on_install()


class SetupPane:
    """Renders ``SetupFlow`` as a full-cover pane over the tab content area.

    ``parent`` is the container to cover (the app's ``tab_content_container``);
    ``app`` is the ``WayfinderApp`` instance (used for ``after`` marshaling, the
    in-window dropdown host, and config persistence). ``on_done(result)`` is
    invoked once when setup completes (``True``) or is skipped (``False``) —
    this is where main.py chains the welcome tour.
    """

    def __init__(self, parent, app, on_done=None):
        import customtkinter as ctk  # lazy: keep the module headless-importable

        self._ctk = ctk
        self.parent = parent
        self.app = app
        self._on_done = on_done
        self.flow = SetupFlow(on_complete=self._complete_flow)

        self._config = app.config
        self._msg_queue: queue.Queue = queue.Queue()
        self._alive = True
        self._dep_rows: dict[str, _DepRow] = {}
        self._deps: list[Dependency] = []

        # Guided auto-install state (the sequencing itself lives in the pure,
        # headless-testable SetupSequence — this widget just drives it).
        self._seq: Optional[SetupSequence] = None
        self._cancelled = False        # Skip Setup pressed → abandon the queue
        self._auto_started = False     # probe→auto-install fires exactly once
        self._phase = "checking"       # checking | installing | paused | done

        # Full-cover dim underlay (CTk has no real alpha; a bg_base frame over
        # the tab content reads as a focused/modal state) — same as WelcomePane.
        self.underlay = ctk.CTkFrame(parent, fg_color=COLORS["bg_base"], corner_radius=0)
        self.underlay.place(relx=0, rely=0, relwidth=1, relheight=1)

        # A single large card that adapts to the window size (the old wizard was
        # a fixed 800x860 Toplevel; relwidth/relheight keeps it sane inline).
        self.card = ctk.CTkFrame(
            self.underlay,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border_rim"],
        )
        self.card.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.96, relheight=0.97)

        self._build_ui()
        self._start_checks()
        self.app.after(100, self._poll_queue)

    # ─── UI construction ─────────────────────────────────────────

    def _build_ui(self):
        ctk = self._ctk
        sz_title = FONT_SIZES["title"]
        sz_detail = FONT_SIZES["caption"]
        pad = SPACING["lg"]

        # Header (caps + divider aesthetic, matching the welcome pane).
        header = ctk.CTkFrame(self.card, fg_color="transparent")
        header.pack(fill="x", padx=pad, pady=(pad, SPACING["sm"]))

        ctk.CTkLabel(
            header,
            text="welcome to wayfinder aura",
            font=(FONTS["header"][0], sz_title, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text="let's check your system and install what's needed.",
            font=(FONTS["body"][0], sz_detail),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(2, 0))

        # Live status line — the guided stepper's current phase / active step.
        self._status_label = ctk.CTkLabel(
            header,
            text="Checking your system…",
            font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
            text_color=COLORS["accent"],
            anchor="w",
        )
        self._status_label.pack(anchor="w", pady=(SPACING["xs"], 0))

        div = ctk.CTkFrame(self.card, height=1, fg_color=COLORS["border_subtle"])
        div.pack(fill="x", padx=pad, pady=(0, SPACING["xs"]))

        # Footer pinned to the bottom BEFORE the expanding list so the buttons
        # never get pushed off a short window.
        self._footer = ctk.CTkFrame(self.card, fg_color="transparent")
        self._footer.pack(side="bottom", fill="x", padx=pad, pady=(SPACING["xs"], pad))

        self._skip_btn = ctk.CTkButton(
            self._footer,
            text="Skip Setup",
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_secondary"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=RADIUS["md"],
            width=110,
            command=self._on_skip,
        )
        self._skip_btn.pack(side="left")

        # Retry — revealed only when the queue PAUSES on a failed step. Re-runs
        # that one step (auto-install replaced the old "Install All Missing").
        self._retry_btn = ctk.CTkButton(
            self._footer,
            text="Retry",
            font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["md"],
            width=110,
            command=self._retry_active,
        )
        # Not packed yet — revealed on a pause.

        # Continue — dual role, phase-driven (see _on_continue): skip the paused
        # step and resume the queue, OR proceed past a finished-but-incomplete
        # setup. Revealed on pause and on a required-still-missing finish.
        self._continue_btn = ctk.CTkButton(
            self._footer,
            text="Continue",
            font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
            fg_color=COLORS["state_typing"],
            hover_color=COLORS["accent_yellow"],
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["md"],
            width=120,
            command=self._on_continue,
        )
        # Not packed yet — revealed by phase transitions.

        # Log area (collapsible, shown when installation starts).
        self._log_frame = ctk.CTkFrame(
            self.card, fg_color=COLORS["bg_surface"], corner_radius=RADIUS["md"]
        )
        self._log_text = ctk.CTkTextbox(
            self._log_frame,
            height=120,
            font=(FONTS["mono"][0], sz_detail),
            fg_color=COLORS["bg_input"],
            text_color=COLORS["text_secondary"],
            corner_radius=RADIUS["sm"],
            activate_scrollbars=True,
            wrap="word",
        )
        self._log_text.pack(fill="both", expand=True, padx=6, pady=6)

        # Model selector (shown when the model dependency is missing).
        self._build_model_selector()

        # Dependency list (scrollable) fills the remaining space.
        self._dep_frame = ctk.CTkScrollableFrame(
            self.card,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_elevated"],
        )
        self._dep_frame.pack(fill="both", expand=True, padx=pad, pady=(0, SPACING["xs"]))

    def _build_model_selector(self):
        ctk = self._ctk
        # InlineOptionMenu renders its OPEN list as an in-window place()'d panel
        # on the app toplevel (never a native tk.Menu) — no CTkOptionMenu here
        # (its tk_popup list mis-stacks on KDE/Wayland). Import lazily so this
        # module stays headless-importable for the SetupFlow tests.
        from wayfinder_main import InlineOptionMenu

        self._model_frame = ctk.CTkFrame(
            self.card, fg_color=COLORS["bg_card"], corner_radius=RADIUS["md"]
        )
        ctk.CTkLabel(
            self._model_frame,
            text="Choose Whisper model:",
            font=(FONTS["body"][0], FONT_SIZES["caption"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left", padx=(10, 6), pady=6)

        recommended = get_recommended_model()
        model_options = [f"{v['label']} ({v['size']})" for v in WHISPER_MODELS.values()]
        model_keys = list(WHISPER_MODELS.keys())
        default_idx = model_keys.index(recommended) if recommended in model_keys else 0

        self._model_var = ctk.StringVar(value=model_options[default_idx])
        self._model_menu = InlineOptionMenu(
            self._model_frame,
            values=model_options,
            variable=self._model_var,
            font=(FONTS["body"][0], FONT_SIZES["caption"]),
            fg_color=COLORS["bg_elevated"],
            button_color=COLORS["bg_elevated"],
            button_hover_color=COLORS["accent_dim"],
            dropdown_fg_color=COLORS["bg_surface"],
            dropdown_hover_color=COLORS["bg_hover"],
            dropdown_text_color=COLORS["text_primary"],
            dropdown_font=(FONTS["body"][0], FONT_SIZES["body"]),
            text_color=COLORS["text_primary"],
            width=220,
            corner_radius=RADIUS["sm"],
        )
        self._model_menu.pack(side="left", padx=(0, 10), pady=6)

    # ─── Dependency checks ───────────────────────────────────────

    def _add_dep_row(self, dep: Dependency):
        # on_install=None → no per-row Install button; the guided queue installs
        # everything and a paused step is retried from the footer.
        row = _DepRow(self._ctk, self._dep_frame, dep, on_install=None)
        row.pack(fill="x", pady=(0, 4))
        self._dep_rows[dep.id] = row

        # One-line Wayland approval hint under the text-injection row: on
        # KDE/Wayland the compositor gates synthetic input behind a one-time
        # "allow input control" prompt that must be approved or typing fails.
        if dep.id == 'ydotool' and sys.platform != 'darwin':
            hint = self._ctk.CTkLabel(
                self._dep_frame,
                text="On Wayland, approve the one-time 'allow input control' "
                     "prompt so Aura can type for you.",
                font=(FONTS['body'][0], FONT_SIZES['caption']),
                text_color=COLORS['text_muted'],
                anchor='w',
                justify='left',
                wraplength=360,
            )
            hint.pack(fill='x', anchor='w', padx=(SPACING['md'], 0), pady=(0, SPACING['xs']))

    def _start_checks(self):
        """Run all dependency checks in a background thread."""
        self._deps = get_dependencies(self._config)

        for dep in self._deps:
            self._add_dep_row(dep)

        def _run_checks():
            for dep in self._deps:
                dep.check()
                self._msg_queue.put(("status", dep.id))
            self._msg_queue.put(("checks_done", None))

        threading.Thread(target=_run_checks, daemon=True).start()

    def _poll_queue(self):
        """Drain worker messages on the Tk main thread (100ms cadence)."""
        if not self._alive:
            return
        try:
            while True:
                msg_type, data = self._msg_queue.get_nowait()

                if msg_type == "status":
                    dep_id = data
                    if dep_id in self._dep_rows:
                        dep = next((d for d in self._deps if d.id == dep_id), None)
                        if dep:
                            self._dep_rows[dep_id].update_status(dep.status)

                elif msg_type == "checks_done":
                    self._on_checks_complete()

                elif msg_type == "log":
                    self._append_log(data)

                elif msg_type == "progress":
                    dep_id, downloaded, total = data
                    if dep_id in self._dep_rows:
                        self._dep_rows[dep_id].update_progress(downloaded, total)

                elif msg_type == "step_done":
                    sid, success, detail = data
                    self._on_step_done(sid, success, detail)

        except queue.Empty:
            pass

        if self._alive and self.app.winfo_exists():
            self.app.after(100, self._poll_queue)

    # ─── Guided auto-install ─────────────────────────────────────
    # The pane no longer waits for the user to click "Install": after the probe
    # it AUTO-STARTS installing everything missing, one step at a time, in
    # dependency order — driving the pure SetupSequence. Skip Setup stays visible
    # (cancels the queue), and any step that fails PAUSES the queue with Retry /
    # Continue rather than aborting the whole run.

    def _on_checks_complete(self):
        """A probe (or an in-flight recheck) landed. Refresh the model picker,
        then — exactly once — kick off the guided auto-install after a short beat
        so the user actually sees the checklist first."""
        self._refresh_dynamic_ui()
        if self._auto_started or self._cancelled:
            return
        self._auto_started = True
        self.app.after(700, self._maybe_autostart)  # brief "here's your system" pause

    def _maybe_autostart(self):
        """Build the ordered step list and start the queue (or finish straight to
        the welcome tour when nothing is missing)."""
        if not self._alive or self._cancelled:
            return
        steps = self._build_install_steps()
        if not steps:
            self._finish_sequence()   # system already ready → hand off to welcome
            return
        self._seq = SetupSequence(steps)
        self._phase = "installing"
        self._show_log()
        # Show the whole queue up front: every step's rows read "Queued…".
        for sid in steps:
            for rid in self._rows_for_step(sid):
                if rid in self._dep_rows:
                    self._dep_rows[rid].set_queued()
        self._seq.start()
        self._run_active_step()

    def _build_install_steps(self) -> list:
        """Ordered ids of every missing, installable step (dependency order):
        custom-installer deps (macOS pyautogui) → system packages → whisper.cpp →
        model. Mirrors the old Install-All sequence, minus the bundled skips."""
        is_bundled = IS_APPIMAGE or IS_FLATPAK
        steps: list = []
        for dep in self._deps:
            if dep._install is not None and not self._dep_ok(dep.id):
                steps.append(dep.id)
        if not is_bundled and get_missing_system_packages():
            steps.append("system_packages")
        if not self._dep_ok("whisper_cpp"):
            steps.append("whisper_cpp")
        if not self._dep_ok("whisper_model"):
            steps.append("whisper_model")
        return steps

    def _rows_for_step(self, sid: str) -> list:
        """Dep-row ids a step maps to (system_packages spans several rows)."""
        if sid == "system_packages":
            return [r for r in ("ydotool", "build_tools", "cuda") if r in self._dep_rows]
        return [sid]

    def _step_name(self, sid: str) -> str:
        friendly = {
            "system_packages": "system packages",
            "whisper_cpp": "the speech engine (whisper.cpp)",
            "whisper_model": "the Whisper model",
        }
        if sid in friendly:
            return friendly[sid]
        dep = next((d for d in self._deps if d.id == sid), None)
        return dep.name if dep else sid

    def _run_active_step(self):
        """Kick off the sequence's current active step (or finish if none)."""
        if not self._alive or self._cancelled or self._seq is None:
            return
        sid = self._seq.active
        if sid is None:
            self._finish_sequence()
            return
        self._phase = "installing"
        self._hide_pause_buttons()
        self._skip_btn.configure(state="normal")  # Skip stays live mid-install
        idx, total = self._seq.position(sid)
        verb = "Downloading" if sid == "whisper_model" else "Installing"
        self._set_status(
            f"{verb} {self._step_name(sid)}…  (step {idx} of {total})",
            COLORS["accent"],
        )
        for rid in self._rows_for_step(sid):
            if rid in self._dep_rows:
                self._dep_rows[rid].set_installing()
        self._dispatch_step(sid)

    def _dispatch_step(self, sid: str):
        """Run the REAL install for ``sid`` via the (untouched) core.setup engine.
        Each engine fn threads internally and reports back through ``done_cb`` →
        the ``step_done`` queue message, drained on the Tk main thread."""
        gpu_vendor = self._detect_vendor_from_deps()

        def log_cb(msg):
            self._msg_queue.put(("log", msg))

        def done_cb(success, detail):
            self._msg_queue.put(("step_done", (sid, success, detail)))

        try:
            if sid == "system_packages":
                pkgs = get_missing_system_packages()
                if not pkgs:
                    done_cb(True, "No system packages needed")
                    return
                # install_system_packages uses pkexec — a GUI polkit dialog, NOT a
                # hidden terminal sudo prompt, so auto-start can't hang on it. If
                # the user declines (or pkexec is unavailable) it returns
                # done(False) with copy-paste manual instructions in the log,
                # which pauses the queue on this step (Retry / Continue).
                install_system_packages(log_cb, done_cb, pkgs)
            elif sid == "whisper_cpp":
                build_whisper_cpp(
                    log_cb, done_cb,
                    use_cuda=(gpu_vendor == "nvidia"),
                    use_vulkan=(gpu_vendor == "amd"),
                )
            elif sid == "whisper_model":
                model_name = self._get_selected_model()

                def progress_cb(dl, tot):
                    self._msg_queue.put(("progress", ("whisper_model", dl, tot)))

                download_whisper_model(model_name, log_cb, done_cb, progress=progress_cb)
            else:
                dep = next((d for d in self._deps if d.id == sid), None)
                if dep is not None and dep._install is not None:
                    dep._install(log_cb, done_cb)
                else:
                    done_cb(False, "No installer available")
        except Exception as e:  # never leave the queue wedged on a dispatch error
            done_cb(False, str(e))

    def _on_step_done(self, sid: str, success: bool, detail: str):
        """A step finished (main thread). Persist paths, advance or pause."""
        if not self._alive:
            return
        if sid == "whisper_cpp" and success and detail:
            self._config["whisper_binary"] = detail
        if sid == "whisper_model" and success and detail:
            self._config["model_path"] = detail
        if success:
            for rid in self._rows_for_step(sid):
                self._recheck(rid)  # flips the row(s) to OK

        # Cancel is honored BETWEEN steps: a Skip during a running step lets the
        # in-flight worker finish, but no further step starts.
        if self._cancelled or self._seq is None:
            return

        if success:
            nxt = self._seq.on_success(sid)
            if nxt is not None:
                self._run_active_step()
            else:
                self._finish_sequence()
        else:
            self._seq.on_failure(sid)
            self._pause_on_failure(sid, detail)

    def _pause_on_failure(self, sid: str, detail: str):
        """A step failed / needs interaction — pause the queue on it."""
        self._phase = "paused"
        for rid in self._rows_for_step(sid):
            if rid in self._dep_rows:
                self._dep_rows[rid].set_failed(detail)
        self._set_status(
            f"Couldn't finish {self._step_name(sid)}. Check the log below, then "
            f"Retry — or Continue to skip it.",
            COLORS["state_recording"],
        )
        self._show_pause_buttons()

    def _retry_active(self):
        """Re-run the paused step."""
        if self._seq is None or self._cancelled:
            return
        self._seq.retry()
        self._run_active_step()

    def _continue_active(self):
        """Skip just the paused step and resume the queue."""
        if self._seq is None or self._cancelled:
            return
        nxt = self._seq.skip_step()
        if nxt is not None:
            self._run_active_step()
        else:
            self._finish_sequence()

    def _finish_sequence(self):
        """The queue drained. Auto-advance to the welcome tour when the required
        deps are satisfied; otherwise offer a manual Continue / Skip."""
        if not self._alive or self._cancelled:
            return
        self._phase = "done"
        self._hide_pause_buttons()
        if all_required_ok(self._deps):
            self._set_status("All set! Starting Wayfinder…", COLORS["state_typing"])
            self.app.after(1200, self._auto_advance)  # → on_done → welcome tour
        else:
            self._set_status(
                "Some required components still need attention. "
                "Continue to proceed anyway, or Skip Setup.",
                COLORS["state_processing"],
            )
            self._continue_btn.configure(text="Continue anyway")
            if not self._continue_btn.winfo_ismapped():
                self._continue_btn.pack(side="right", padx=(8, 0))

    def _auto_advance(self):
        if self._alive and not self._cancelled and not self.flow.is_complete:
            self.flow.complete()

    def _show_pause_buttons(self):
        self._continue_btn.configure(text="Continue")
        if not self._continue_btn.winfo_ismapped():
            self._continue_btn.pack(side="right", padx=(8, 0))
        if not self._retry_btn.winfo_ismapped():
            self._retry_btn.pack(side="right", padx=(8, 0))

    def _hide_pause_buttons(self):
        self._retry_btn.pack_forget()
        self._continue_btn.pack_forget()

    def _set_status(self, text: str, color=None):
        try:
            self._status_label.configure(text=text, text_color=color or COLORS["accent"])
        except Exception:
            pass

    # ─── Helpers ─────────────────────────────────────────────────

    def _dep_ok(self, dep_id: str) -> bool:
        dep = next((d for d in self._deps if d.id == dep_id), None)
        return dep is not None and dep.is_ok

    def _recheck(self, dep_id: str):
        dep = next((d for d in self._deps if d.id == dep_id), None)
        if dep:
            dep.check()
            self._msg_queue.put(("status", dep_id))
        # _recheck can run on the install background thread — never touch Tk
        # here. Marshal the footer refresh through the queue (drained on the
        # main thread by _poll_queue, reusing the "checks_done" handler).
        self._msg_queue.put(("checks_done", None))

    def _recheck_all(self):
        for dep in self._deps:
            dep.check()
            self._msg_queue.put(("status", dep.id))
        self._msg_queue.put(("checks_done", None))

    def _detect_vendor_from_deps(self) -> str:
        dep = next((d for d in self._deps if d.id == "gpu_driver"), None)
        if dep and dep.status and dep.status.installed:
            detail = dep.status.detail.lower()
            if "nvidia" in detail:
                return "nvidia"
            if "amd" in detail:
                return "amd"
        return "unknown"

    def _get_selected_model(self) -> str:
        selected_label = self._model_var.get()
        for key, info in WHISPER_MODELS.items():
            if selected_label.startswith(info["label"]):
                return key
        return get_recommended_model()

    def _refresh_dynamic_ui(self):
        """Keep the (fixed) Whisper-model picker visible while the model is still
        missing, with its sensible default preselected. The queue auto-downloads
        whatever's selected when the model step runs, so the user only has to
        touch it to override the default."""
        if should_show_model_selector(self._deps):
            if not self._model_frame.winfo_ismapped():
                self._model_frame.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["sm"]))
        else:
            self._model_frame.pack_forget()

    def _show_log(self):
        if not self._log_frame.winfo_ismapped():
            self._log_frame.pack(fill="x", padx=SPACING["lg"], pady=(0, SPACING["sm"]))

    def _append_log(self, text: str):
        self._log_text.configure(state="normal")
        self._log_text.insert("end", text + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    # ─── Events / completion ─────────────────────────────────────

    def _on_continue(self):
        # Phase-driven: while PAUSED the button skips just the stuck step and
        # resumes the queue; otherwise (a finished-but-incomplete run) it proceeds
        # past setup into the app.
        if self._phase == "paused" and self._seq is not None:
            self._continue_active()
        else:
            self.flow.complete()  # -> _complete_flow (persist + teardown + on_done)

    def _on_skip(self):
        # Skip Setup — cancel the queue cleanly. The cancelled flag is checked
        # between steps (workers already in flight finish harmlessly; no new step
        # starts), then complete the flow as "skipped".
        self._cancelled = True
        if self._seq is not None:
            self._seq.cancel()
        self.flow.skip()      # -> _complete_flow

    def _complete_flow(self):
        """Fired once by the flow. Persist setup_completed, tear down, hand off."""
        try:
            self._config["setup_completed"] = True
            save_config(self._config)
        except Exception:
            pass
        result = self.flow.result
        self._teardown()
        if self._on_done is not None:
            try:
                self._on_done(result)
            except Exception:
                pass

    def _teardown(self):
        self._alive = False
        try:
            self.app._setup_active = False
            self.app._setup_pane = None
        except Exception:
            pass
        try:
            self.underlay.destroy()
        except Exception:
            pass

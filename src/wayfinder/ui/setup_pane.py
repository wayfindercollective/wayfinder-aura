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


# ─── Pure footer/status logic (headless-testable) ────────────────────────────


def all_required_ok(deps) -> bool:
    """True when every REQUIRED dependency is satisfied."""
    return all(d.is_ok for d in deps if d.required)


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
            if self._dep.can_install or self._dep.id in (
                "ydotool", "build_tools", "cuda", "whisper_cpp", "whisper_model",
            ):
                btn_text = "Download" if self._dep.id == "whisper_model" else "Install"
                self._action_btn.configure(text=btn_text)
                self._action_btn.pack(side="right", padx=8, pady=6)

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
        self._installing = False
        self._alive = True
        self._dep_rows: dict[str, _DepRow] = {}
        self._deps: list[Dependency] = []

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

        self._install_btn = ctk.CTkButton(
            self._footer,
            text="Install All Missing",
            font=(FONTS["body"][0], FONT_SIZES["small"], "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["md"],
            width=180,
            command=self._on_install_all,
        )
        self._install_btn.pack(side="right")

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
        # Not packed yet — revealed when all required deps are met.

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
        row = _DepRow(
            self._ctk, self._dep_frame, dep,
            on_install=lambda d=dep: self._install_single(d),
        )
        row.pack(fill="x", pady=(0, 4))
        self._dep_rows[dep.id] = row

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
                    self._update_footer_state()

                elif msg_type == "log":
                    self._append_log(data)

                elif msg_type == "progress":
                    dep_id, downloaded, total = data
                    if dep_id in self._dep_rows:
                        self._dep_rows[dep_id].update_progress(downloaded, total)

                elif msg_type == "install_done":
                    dep_id, success, detail = data
                    self._on_install_done(dep_id, success, detail)

                elif msg_type == "install_sequence_done":
                    self._installing = False
                    self._install_btn.configure(text="Install All Missing")
                    self._skip_btn.configure(state="normal")
                    self._recheck_all()

        except queue.Empty:
            pass

        if self._alive and self.app.winfo_exists():
            self.app.after(100, self._poll_queue)

    # ─── Installation logic ──────────────────────────────────────

    def _on_install_all(self):
        """Full install sequence: system packages -> whisper.cpp -> model."""
        if self._installing:
            return
        self._installing = True
        self._install_btn.configure(state="disabled", text="Installing...")
        self._skip_btn.configure(state="disabled")
        self._show_log()

        is_bundled = IS_APPIMAGE or IS_FLATPAK
        missing_pkgs = [] if is_bundled else get_missing_system_packages()
        whisper_missing = not self._dep_ok("whisper_cpp")
        model_missing = not self._dep_ok("whisper_model")

        gpu_vendor = self._detect_vendor_from_deps()
        use_cuda = gpu_vendor == "nvidia"
        use_vulkan = gpu_vendor == "amd"

        def _sequence():
            # Step 0: deps with their own installer (e.g. pyautogui on macOS).
            for dep in self._deps:
                if dep._install is not None and not self._dep_ok(dep.id):
                    self._run_step_sync(dep.id, dep._install)
                    self._recheck(dep.id)

            # Step 1: system packages (skipped in bundled environments).
            if missing_pkgs:
                self._run_step_sync(
                    "system_packages",
                    lambda log_cb, done_cb: install_system_packages(log_cb, done_cb, missing_pkgs),
                )
                for dep_id in ("ydotool", "build_tools", "cuda"):
                    self._recheck(dep_id)

            # Step 2: build whisper.cpp.
            if whisper_missing:
                self._run_step_sync(
                    "whisper_cpp",
                    lambda log_cb, done_cb: build_whisper_cpp(
                        log_cb, done_cb, use_cuda=use_cuda, use_vulkan=use_vulkan
                    ),
                )

            # Step 3: download the model.
            if model_missing:
                model_name = self._get_selected_model()
                self._run_step_sync(
                    "whisper_model",
                    lambda log_cb, done_cb: download_whisper_model(
                        model_name, log_cb, done_cb,
                        progress=lambda dl, tot: self._msg_queue.put(
                            ("progress", ("whisper_model", dl, tot))
                        ),
                    ),
                )

            self._msg_queue.put(("install_sequence_done", None))

        threading.Thread(target=_sequence, daemon=True).start()

    def _run_step_sync(self, dep_id: str, install_fn):
        """Run an install step and BLOCK the calling (background) thread until it
        finishes. Must be called off the Tk main thread."""
        done_event = threading.Event()

        def log_cb(msg):
            self._msg_queue.put(("log", msg))

        def done_cb(success, detail):
            self._msg_queue.put(("install_done", (dep_id, success, detail)))
            done_event.set()

        if dep_id in self._dep_rows:
            self._msg_queue.put(("status", dep_id))

        install_fn(log_cb, done_cb)
        done_event.wait(timeout=600)  # 10 minute ceiling

    def _install_single(self, dep: Dependency):
        """Install a single dependency from its row's action button."""
        if self._installing:
            return

        self._show_log()
        dep_id = dep.id
        gpu_vendor = self._detect_vendor_from_deps()

        if dep_id in ("ydotool", "build_tools", "cuda"):
            if IS_APPIMAGE or IS_FLATPAK:
                self._append_log("Dependencies are bundled — no system install needed.")
                return

            if dep._install is not None:
                self._installing = True
                if dep_id in self._dep_rows:
                    self._dep_rows[dep_id].set_installing()

                def log_cb(msg):
                    self._msg_queue.put(("log", msg))

                def done_cb(success, detail):
                    self._msg_queue.put(("install_done", (dep_id, success, detail)))
                    self._installing = False
                    self._recheck(dep_id)

                dep._install(log_cb, done_cb)
                return

            pkgs = get_missing_system_packages()
            if pkgs:
                self._installing = True
                if dep_id in self._dep_rows:
                    self._dep_rows[dep_id].set_installing()

                def log_cb(msg):
                    self._msg_queue.put(("log", msg))

                def done_cb(success, detail):
                    self._msg_queue.put(("install_done", (dep_id, success, detail)))
                    self._installing = False
                    for did in ("ydotool", "build_tools", "cuda"):
                        self._recheck(did)

                install_system_packages(log_cb, done_cb, pkgs)

        elif dep_id == "whisper_cpp":
            self._installing = True
            if dep_id in self._dep_rows:
                self._dep_rows[dep_id].set_installing()

            def log_cb(msg):
                self._msg_queue.put(("log", msg))

            def done_cb(success, detail):
                self._msg_queue.put(("install_done", (dep_id, success, detail)))
                self._installing = False

            build_whisper_cpp(
                log_cb, done_cb,
                use_cuda=(gpu_vendor == "nvidia"),
                use_vulkan=(gpu_vendor == "amd"),
            )

        elif dep_id == "whisper_model":
            self._installing = True
            if dep_id in self._dep_rows:
                self._dep_rows[dep_id].set_installing()

            model_name = self._get_selected_model()

            def log_cb(msg):
                self._msg_queue.put(("log", msg))

            def done_cb(success, detail):
                self._msg_queue.put(("install_done", (dep_id, success, detail)))
                self._installing = False

            def progress_cb(dl, tot):
                self._msg_queue.put(("progress", ("whisper_model", dl, tot)))

            download_whisper_model(model_name, log_cb, done_cb, progress=progress_cb)

    def _on_install_done(self, dep_id: str, success: bool, detail: str):
        """Called on the main thread when an install step finishes."""
        self._recheck(dep_id)

        if dep_id == "whisper_cpp" and success and detail:
            self._config["whisper_binary"] = detail
        if dep_id == "whisper_model" and success and detail:
            self._config["model_path"] = detail

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

    def _update_footer_state(self):
        """Reveal Continue / toggle Install based on current dependency state."""
        decision = footer_decision(self._deps, self._installing)

        if decision["show_continue"]:
            self._continue_btn.pack(side="right", padx=(8, 0))
        else:
            self._continue_btn.pack_forget()

        if decision["show_install"]:
            if not self._install_btn.winfo_ismapped():
                self._install_btn.pack(side="right")
            self._install_btn.configure(
                state="normal" if decision["install_enabled"] else "disabled"
            )
        else:
            self._install_btn.pack_forget()

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
        self.flow.complete()  # -> _complete_flow (persist + teardown + on_done)

    def _on_skip(self):
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

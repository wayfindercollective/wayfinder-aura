"""
First-run Setup Wizard for Wayfinder Aura.

Shows a checklist of dependencies, auto-detects what's installed,
and guides the user through installing missing components.

Usage:
    from wayfinder.ui.dialogs.setup_wizard import SetupWizard

    wizard = SetupWizard(parent, config)
    parent.wait_window(wizard)  # blocks until wizard closes
    # wizard.result is True if setup completed, False if skipped
"""

import os
import queue
import threading
from pathlib import Path
from typing import Optional

import customtkinter as ctk

from ...config import save_config
from ...core.setup import (
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
from ..theme import COLORS, FONT_SIZES, FONTS, RADIUS, SPACING


class SetupWizard(ctk.CTkToplevel):
    """
    First-run setup wizard.

    Shows a dependency checklist and guides installation.
    Set self.result to True when the user completes setup,
    False if they skip.
    """

    def __init__(self, parent, config: dict):
        super().__init__(parent)

        self.config = config
        self.result = False
        self._msg_queue: queue.Queue = queue.Queue()
        self._installing = False
        self._dep_rows: dict[str, "_DependencyRow"] = {}

        # Window setup
        self.title("Wayfinder Aura — Setup")
        self.geometry("680x720")
        self.minsize(580, 600)
        self.configure(fg_color=COLORS["bg_base"])

        # Modal behavior
        self.transient(parent)
        self.grab_set()
        self.focus_force()
        self.protocol("WM_DELETE_WINDOW", self._on_skip)

        # Center on parent
        self.update_idletasks()
        px = parent.winfo_rootx() + (parent.winfo_width() - 680) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - 720) // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

        self._build_ui()
        self._start_checks()
        self._poll_queue()

    # ─── UI Construction ─────────────────────────────────────

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=SPACING["xl"], pady=(SPACING["xl"], SPACING["md"]))

        ctk.CTkLabel(
            header,
            text="Welcome to Wayfinder Aura",
            font=(FONTS["display"][0], FONT_SIZES["display"], "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text="Let's check your system and install what's needed.",
            font=(FONTS["body"][0], FONT_SIZES["body"]),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(4, 0))

        # Dependency list (scrollable)
        self._dep_frame = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS["bg_elevated"],
        )
        self._dep_frame.pack(fill="both", expand=True, padx=SPACING["xl"], pady=(0, SPACING["sm"]))

        # Log area (collapsible, starts hidden)
        self._log_frame = ctk.CTkFrame(self, fg_color=COLORS["bg_surface"], corner_radius=RADIUS["md"])
        # Don't pack yet — shown when installation starts

        self._log_text = ctk.CTkTextbox(
            self._log_frame,
            height=140,
            font=(FONTS["mono"][0], FONT_SIZES["caption"]),
            fg_color=COLORS["bg_input"],
            text_color=COLORS["text_secondary"],
            corner_radius=RADIUS["sm"],
            activate_scrollbars=True,
            wrap="word",
        )
        self._log_text.pack(fill="both", expand=True, padx=8, pady=8)

        # Model selector (shown when model is missing)
        self._model_frame = ctk.CTkFrame(self, fg_color=COLORS["bg_card"], corner_radius=RADIUS["md"])
        # Don't pack yet

        ctk.CTkLabel(
            self._model_frame,
            text="Choose Whisper model:",
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            text_color=COLORS["text_secondary"],
        ).pack(side="left", padx=(12, 8), pady=8)

        recommended = get_recommended_model()
        model_options = [
            f"{v['label']} ({v['size']})" for v in WHISPER_MODELS.values()
        ]
        model_keys = list(WHISPER_MODELS.keys())
        default_idx = model_keys.index(recommended) if recommended in model_keys else 0

        self._model_var = ctk.StringVar(value=model_options[default_idx])
        self._model_menu = ctk.CTkOptionMenu(
            self._model_frame,
            values=model_options,
            variable=self._model_var,
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            fg_color=COLORS["bg_elevated"],
            button_color=COLORS["accent_dim"],
            button_hover_color=COLORS["accent"],
            dropdown_fg_color=COLORS["bg_surface"],
            dropdown_hover_color=COLORS["bg_hover"],
            width=260,
        )
        self._model_menu.pack(side="left", padx=(0, 12), pady=8)

        # Footer buttons
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=SPACING["xl"], pady=(SPACING["sm"], SPACING["xl"]))

        self._skip_btn = ctk.CTkButton(
            footer,
            text="Skip Setup",
            font=(FONTS["body"][0], FONT_SIZES["body"]),
            fg_color="transparent",
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_secondary"],
            border_width=1,
            border_color=COLORS["border"],
            corner_radius=RADIUS["md"],
            width=120,
            command=self._on_skip,
        )
        self._skip_btn.pack(side="left")

        self._install_btn = ctk.CTkButton(
            footer,
            text="Install All Missing",
            font=(FONTS["body"][0], FONT_SIZES["body"], "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["md"],
            width=200,
            command=self._on_install_all,
        )
        self._install_btn.pack(side="right")

        self._continue_btn = ctk.CTkButton(
            footer,
            text="Continue",
            font=(FONTS["body"][0], FONT_SIZES["body"], "bold"),
            fg_color=COLORS["state_typing"],
            hover_color="#4DC498",
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["md"],
            width=140,
            command=self._on_continue,
        )
        # Don't pack yet — shown when all required deps are met

    # ─── Dependency Rows ─────────────────────────────────────

    def _add_dep_row(self, dep: Dependency):
        row = _DependencyRow(self._dep_frame, dep, on_install=lambda d=dep: self._install_single(d))
        row.pack(fill="x", pady=(0, 6))
        self._dep_rows[dep.id] = row

    # ─── Check Dependencies ──────────────────────────────────

    def _start_checks(self):
        """Run all dependency checks in a background thread."""
        self._deps = get_dependencies(self.config)

        # Add rows immediately with "checking" state
        for dep in self._deps:
            self._add_dep_row(dep)

        def _run_checks():
            for dep in self._deps:
                dep.check()
                self._msg_queue.put(("status", dep.id))
            self._msg_queue.put(("checks_done", None))

        threading.Thread(target=_run_checks, daemon=True).start()

    def _poll_queue(self):
        """Process messages from background threads."""
        try:
            while True:
                msg_type, data = self._msg_queue.get_nowait()

                if msg_type == "status":
                    dep_id = data
                    if dep_id in self._dep_rows:
                        dep = next(d for d in self._deps if d.id == dep_id)
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

        except queue.Empty:
            pass

        if self.winfo_exists():
            self.after(100, self._poll_queue)

    # ─── Installation Logic ──────────────────────────────────

    def _on_install_all(self):
        """Run full installation sequence: system packages -> whisper.cpp -> model."""
        if self._installing:
            return
        self._installing = True
        self._install_btn.configure(state="disabled", text="Installing...")
        self._skip_btn.configure(state="disabled")
        self._show_log()

        # Determine what needs installing
        missing_pkgs = get_missing_system_packages()
        whisper_missing = not self._dep_ok("whisper_cpp")
        model_missing = not self._dep_ok("whisper_model")

        gpu_vendor = self._detect_vendor_from_deps()
        use_cuda = gpu_vendor == "nvidia"
        use_vulkan = gpu_vendor == "amd"

        def _sequence():
            """Run installations in sequence."""
            # Step 1: System packages (if needed)
            if missing_pkgs:
                self._run_step_sync(
                    "system_packages",
                    lambda log_cb, done_cb: install_system_packages(log_cb, done_cb, missing_pkgs),
                )
                # Re-check affected dependencies
                for dep_id in ("ydotool", "build_tools", "cuda"):
                    self._recheck(dep_id)

            # Step 2: Build whisper.cpp (if needed)
            if whisper_missing:
                self._run_step_sync(
                    "whisper_cpp",
                    lambda log_cb, done_cb: build_whisper_cpp(log_cb, done_cb, use_cuda=use_cuda, use_vulkan=use_vulkan),
                )

            # Step 3: Download model (if needed)
            if model_missing:
                model_name = self._get_selected_model()
                self._run_step_sync(
                    "whisper_model",
                    lambda log_cb, done_cb: download_whisper_model(
                        model_name, log_cb, done_cb,
                        progress=lambda dl, tot: self._msg_queue.put(("progress", ("whisper_model", dl, tot))),
                    ),
                )

            # Done
            self._msg_queue.put(("install_sequence_done", None))

        # We need a different queue message for sequence done
        original_poll = self._poll_queue

        def _extended_poll():
            try:
                while True:
                    msg_type, data = self._msg_queue.get_nowait()
                    if msg_type == "install_sequence_done":
                        self._installing = False
                        self._install_btn.configure(text="Install All Missing")
                        self._skip_btn.configure(state="normal")
                        # Re-check everything
                        self._recheck_all()
                        return  # Let normal poll take over
                    # Handle normally
                    elif msg_type == "status":
                        dep_id = data
                        if dep_id in self._dep_rows:
                            dep = next((d for d in self._deps if d.id == dep_id), None)
                            if dep:
                                self._dep_rows[dep_id].update_status(dep.status)
                    elif msg_type == "log":
                        self._append_log(data)
                    elif msg_type == "progress":
                        dep_id, downloaded, total = data
                        if dep_id in self._dep_rows:
                            self._dep_rows[dep_id].update_progress(downloaded, total)
                    elif msg_type == "install_done":
                        dep_id, success, detail = data
                        self._on_install_done(dep_id, success, detail)
            except queue.Empty:
                pass

            if self.winfo_exists():
                self.after(100, _extended_poll)

        # Replace poll temporarily and start sequence
        self.after_cancel_all_pending = True  # Flag
        threading.Thread(target=_sequence, daemon=True).start()

    def _run_step_sync(self, dep_id: str, install_fn):
        """
        Run an install step and block the calling thread until it finishes.
        Must be called from a background thread, NOT the UI thread.
        """
        done_event = threading.Event()
        result = {"success": False, "detail": ""}

        def log_cb(msg):
            self._msg_queue.put(("log", msg))

        def done_cb(success, detail):
            result["success"] = success
            result["detail"] = detail
            self._msg_queue.put(("install_done", (dep_id, success, detail)))
            done_event.set()

        # Mark as in-progress
        if dep_id in self._dep_rows:
            self._msg_queue.put(("status", dep_id))

        install_fn(log_cb, done_cb)
        done_event.wait(timeout=600)  # 10 minute timeout

    def _install_single(self, dep: Dependency):
        """Install a single dependency when its individual button is clicked."""
        if self._installing:
            return

        self._show_log()
        dep_id = dep.id
        gpu_vendor = self._detect_vendor_from_deps()

        if dep_id in ("ydotool", "build_tools", "cuda"):
            # System package
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
                    # Re-check all system deps
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

            build_whisper_cpp(log_cb, done_cb,
                              use_cuda=(gpu_vendor == "nvidia"),
                              use_vulkan=(gpu_vendor == "amd"))

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
        """Called when an install step finishes."""
        self._recheck(dep_id)

        # Update config paths if whisper.cpp was built
        if dep_id == "whisper_cpp" and success and detail:
            self.config["whisper_binary"] = detail

        # Update config model path if model was downloaded
        if dep_id == "whisper_model" and success and detail:
            self.config["model_path"] = detail

    # ─── Helpers ─────────────────────────────────────────────

    def _dep_ok(self, dep_id: str) -> bool:
        dep = next((d for d in self._deps if d.id == dep_id), None)
        return dep is not None and dep.is_ok

    def _recheck(self, dep_id: str):
        dep = next((d for d in self._deps if d.id == dep_id), None)
        if dep:
            dep.check()
            self._msg_queue.put(("status", dep_id))
        self.after(200, self._update_footer_state)

    def _recheck_all(self):
        for dep in self._deps:
            dep.check()
            self._msg_queue.put(("status", dep.id))
        self.after(300, self._update_footer_state)

    def _detect_vendor_from_deps(self) -> str:
        """Get GPU vendor from the driver dep status."""
        dep = next((d for d in self._deps if d.id == "gpu_driver"), None)
        if dep and dep.status and dep.status.installed:
            detail = dep.status.detail.lower()
            if "nvidia" in detail:
                return "nvidia"
            if "amd" in detail:
                return "amd"
        return "unknown"

    def _get_selected_model(self) -> str:
        """Get the model key from the dropdown selection."""
        selected_label = self._model_var.get()
        for key, info in WHISPER_MODELS.items():
            if selected_label.startswith(info["label"]):
                return key
        return get_recommended_model()

    def _update_footer_state(self):
        """Show Continue button if all required deps are met."""
        all_required_ok = all(d.is_ok for d in self._deps if d.required)
        any_missing = any(not d.is_ok for d in self._deps)

        if all_required_ok and not self._installing:
            self._continue_btn.pack(side="right", padx=(8, 0))
            if not any_missing:
                # Everything installed — hide install button
                self._install_btn.pack_forget()
            else:
                self._install_btn.configure(state="normal")
        else:
            self._continue_btn.pack_forget()
            if not self._installing:
                self._install_btn.configure(state="normal")

        # Show model selector if model is missing
        model_dep = next((d for d in self._deps if d.id == "whisper_model"), None)
        if model_dep and not model_dep.is_ok:
            self._model_frame.pack(fill="x", padx=SPACING["xl"], pady=(0, SPACING["sm"]),
                                   before=self._log_frame if self._log_frame.winfo_ismapped() else None)
        else:
            self._model_frame.pack_forget()

    def _show_log(self):
        """Show the log output area."""
        if not self._log_frame.winfo_ismapped():
            self._log_frame.pack(fill="x", padx=SPACING["xl"], pady=(0, SPACING["sm"]))

    def _append_log(self, text: str):
        """Add a line to the log output."""
        self._log_text.configure(state="normal")
        self._log_text.insert("end", text + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _on_continue(self):
        """User completed setup."""
        self.config["setup_completed"] = True
        save_config(self.config)
        self.result = True
        self.grab_release()
        self.destroy()

    def _on_skip(self):
        """User skipped setup."""
        self.result = False
        self.grab_release()
        self.destroy()


# ─── Dependency Row Widget ───────────────────────────────────────

class _DependencyRow(ctk.CTkFrame):
    """A single row in the dependency checklist."""

    STATUS_ICONS = {
        "checking": ("...", COLORS["text_muted"]),
        "ok": ("OK", COLORS["state_typing"]),          # Mint
        "warning": ("OK", COLORS["state_processing"]),  # Gold
        "missing": ("--", COLORS["state_recording"]),   # Rose
        "missing_opt": ("--", COLORS["text_muted"]),    # Dim for optional
        "installing": ("...", COLORS["accent"]),         # Violet
    }

    def __init__(self, parent, dep: Dependency, on_install=None):
        super().__init__(parent, fg_color=COLORS["bg_card"], corner_radius=RADIUS["md"])
        self._dep = dep
        self._on_install = on_install

        # Status indicator
        self._status_label = ctk.CTkLabel(
            self,
            text="...",
            width=36,
            font=(FONTS["mono"][0], FONT_SIZES["body"], "bold"),
            text_color=COLORS["text_muted"],
        )
        self._status_label.pack(side="left", padx=(12, 0), pady=10)

        # Text area
        text_frame = ctk.CTkFrame(self, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True, padx=(8, 0), pady=10)

        req_tag = "" if dep.required else "  (optional)"
        self._name_label = ctk.CTkLabel(
            text_frame,
            text=dep.name + req_tag,
            font=(FONTS["body"][0], FONT_SIZES["body"], "bold"),
            text_color=COLORS["text_primary"],
            anchor="w",
        )
        self._name_label.pack(anchor="w")

        self._detail_label = ctk.CTkLabel(
            text_frame,
            text=dep.description,
            font=(FONTS["body"][0], FONT_SIZES["caption"]),
            text_color=COLORS["text_muted"],
            anchor="w",
        )
        self._detail_label.pack(anchor="w")

        # Progress bar (hidden by default)
        self._progress = ctk.CTkProgressBar(
            text_frame,
            height=4,
            fg_color=COLORS["bg_input"],
            progress_color=COLORS["accent"],
            corner_radius=2,
        )

        # Action button (hidden until status known)
        self._action_btn = ctk.CTkButton(
            self,
            text="Install",
            width=80,
            height=30,
            font=(FONTS["body"][0], FONT_SIZES["small"]),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["bg_base"],
            corner_radius=RADIUS["sm"],
            command=self._on_action,
        )

    def update_status(self, status: Optional[DependencyStatus]):
        if status is None:
            self._set_state("checking")
            return

        if status.installed:
            if status.warning:
                self._set_state("warning")
                self._detail_label.configure(text=status.warning, text_color=COLORS["state_processing"])
            else:
                self._set_state("ok")
                if status.detail:
                    self._detail_label.configure(text=status.detail, text_color=COLORS["text_muted"])
            self._action_btn.pack_forget()
        else:
            state = "missing" if self._dep.required else "missing_opt"
            self._set_state(state)
            error_text = status.error or self._dep.description
            self._detail_label.configure(text=error_text, text_color=COLORS["state_recording"])
            if self._dep.can_install or self._dep.id in ("ydotool", "build_tools", "cuda", "whisper_cpp", "whisper_model"):
                btn_text = "Download" if self._dep.id == "whisper_model" else "Install"
                self._action_btn.configure(text=btn_text)
                self._action_btn.pack(side="right", padx=12, pady=10)

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
        icon, color = self.STATUS_ICONS.get(state, ("?", COLORS["text_muted"]))
        self._status_label.configure(text=icon, text_color=color)

    def _on_action(self):
        if self._on_install:
            self._on_install()

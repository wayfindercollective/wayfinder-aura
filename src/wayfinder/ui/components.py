"""
Reusable UI components for Wayfinder Aura.

The one home for the app's shared widgets and setting hints (wayfinder_main
imports them from here):
- ToolTip: Hover tooltips for widgets, plus hide_all_tooltips()
- ModeSelector: Segmented control for mode selection
- SmoothScrollableFrame: Scrollable container
- CompatibilityBanner: Model compatibility warning
- SETTING_TOOLTIPS / get_dynamic_tooltip: Setting hints
"""

import os
import sys
import weakref
from pathlib import Path

import customtkinter as ctk

from wayfinder.core.whisper_timings import format_bench_seconds, warm_benchmark_results

from .theme import COLORS, RADIUS, FONT_SIZES
from .tooltip_geometry import anchor_in_area, tooltip_position, tooltip_wraplength
from .window_geometry import macos_visible_frame, windows_work_area

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"
_IS_LINUX = sys.platform.startswith("linux")


# Flipped True for the duration of a mouse-wheel gesture so hover ToolTips
# don't spawn CTkToplevel windows while content is sliding under the pointer.
_WHEEL_SCROLL_ACTIVE = False


def set_wheel_scroll_active(active: bool) -> None:
    """The app's wheel handler flips this; ToolTips stay hidden while it is True."""
    global _WHEEL_SCROLL_ACTIVE
    _WHEEL_SCROLL_ACTIVE = active


#: Every ToolTip that has been constructed and not yet destroyed.
#: A WeakSet so holding a reference here never keeps a dead widget alive.
#: Tk's event delivery through pack_forget() is the thing that failed here, so
#: hide_all_tooltips() does not rely on it — the tab switcher calls it directly.
_LIVE_TOOLTIPS: "weakref.WeakSet[ToolTip]" = weakref.WeakSet()


def hide_all_tooltips() -> None:
    """Dismiss every visible tooltip. Safe to call at any time."""
    for tip in list(_LIVE_TOOLTIPS):
        try:
            tip.on_leave()
        except Exception:
            # One stuck tooltip must never stop the others being cleared.
            pass


class ToolTip:
    """
    Modern hover tooltip for CustomTkinter widgets.
    Styled to match Nano Banana design system.
    """

    def __init__(self, widget, text, delay=300):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tooltip_window = None
        self.scheduled_id = None
        _LIVE_TOOLTIPS.add(self)

        widget.bind("<Enter>", self.on_enter)
        widget.bind("<Leave>", self.on_leave)
        widget.bind("<ButtonPress>", self.on_leave)
        # Switching tabs calls pack_forget() on the pane, which unmaps this
        # widget without the pointer ever leaving it — so <Leave> never fires
        # and the tooltip was left stranded. It is an overrideredirect topmost
        # Toplevel with no chrome, so there is then no way to dismiss it: it
        # floats over every other tab until the app restarts.
        widget.bind("<Unmap>", self.on_leave, add="+")
        widget.bind("<Destroy>", self.on_destroy, add="+")

    def on_enter(self, event=None):
        # Pointer is skating across rows during a wheel gesture — scheduling a
        # Toplevel tooltip mid-scroll is a common source of bottom-of-pane jank.
        if _WHEEL_SCROLL_ACTIVE:
            return
        self.scheduled_id = self.widget.after(self.delay, self.show_tooltip)

    def on_leave(self, event=None):
        if self.scheduled_id:
            try:
                self.widget.after_cancel(self.scheduled_id)
            except Exception:
                # The widget may already be gone; the timer dies with it.
                pass
            self.scheduled_id = None
        self.hide_tooltip()

    def on_destroy(self, event=None):
        self.on_leave()
        _LIVE_TOOLTIPS.discard(self)

    def show_tooltip(self):
        if self.tooltip_window:
            return
        if _WHEEL_SCROLL_ACTIVE:
            return
        # The 300ms timer can outlive the widget: hover a row, switch tab, and
        # this fires against something unmapped or destroyed, stranding a
        # tooltip from a pane the user already left.
        try:
            if not self.widget.winfo_exists() or not self.widget.winfo_ismapped():
                return
        except Exception:
            return

        # Create tooltip window
        self.tooltip_window = tw = ctk.CTkToplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)
        tw.configure(fg_color=COLORS["bg_surface"])
        
        # Modern tooltip frame with subtle styling
        frame = ctk.CTkFrame(
            tw,
            fg_color=COLORS["bg_surface"],
            corner_radius=RADIUS["sm"],
            border_width=1,
            border_color=COLORS["border"],
        )
        frame.pack(fill="both", expand=True)
        
        # Tooltip content
        label = ctk.CTkLabel(
            frame,
            text=self.text,
            font=("DejaVu Sans", 12),  # module-scope: no font_sizes access
            text_color=COLORS["text_primary"],
            wraplength=tooltip_wraplength(self.text),
            justify="left",
        )
        label.pack(padx=12, pady=10)

        # Above the widget if it fits, else below, else beside it - always
        # inside the usable screen (macOS: menu bar and Dock excluded).
        tw.update_idletasks()
        size = (tw.winfo_width(), tw.winfo_height())
        screen_w, screen_h = tw.winfo_screenwidth(), tw.winfo_screenheight()
        anchor = (self.widget.winfo_rootx(), self.widget.winfo_rooty(),
                  self.widget.winfo_width(), self.widget.winfo_height())
        area = (macos_visible_frame(screen_w, screen_h) if IS_MACOS
                else windows_work_area(screen_w, screen_h, physical=True) if IS_WINDOWS
                else (0, 0, screen_w, screen_h))
        if anchor_in_area(anchor, area):
            x, y = tooltip_position(anchor, size, area)
        else:
            # Another monitor: Tk only reports the primary screen, so don't
            # pull the tooltip onto it; keep it centred above (or below) the widget.
            x = anchor[0] + (anchor[2] - size[0]) // 2
            y = anchor[1] - size[1] - 6
            if y < 10:
                y = anchor[1] + anchor[3] + 6
        tw.wm_geometry(f"+{x}+{y}")
    
    def hide_tooltip(self):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

    def update_text(self, new_text: str):
        """Update the tooltip text dynamically."""
        self.text = new_text


class ModeSelector(ctk.CTkFrame):
    """
    Segmented control widget for selecting processing mode.
    Two mutually exclusive options: Local | Remote
    """
    
    def __init__(
        self,
        parent,
        values: list[str] = None,
        current_value: str = "local",
        command=None,
        **kwargs
    ):
        super().__init__(parent, fg_color="transparent", **kwargs)
        
        self.values = values or ["local", "remote"]
        self.current_value = current_value
        self.command = command
        self.buttons: dict[str, ctk.CTkButton] = {}
        
        # Display labels and icons for each mode
        self.display_info = {
            "local": {"label": "Local", "icon": "", "desc": "100% Private"},
            "remote": {"label": "Remote", "icon": "", "desc": "Cloud Processing"},
        }
        
        # Create container with pill background
        self.container = ctk.CTkFrame(
            self,
            fg_color=COLORS["bg_input"],
            corner_radius=RADIUS["md"],
            height=44,
        )
        self.container.pack(fill="x", padx=0, pady=0)
        self.container.pack_propagate(False)
        
        # Inner frame for buttons
        inner = ctk.CTkFrame(self.container, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=3, pady=3)
        
        # Configure columns
        for i in range(len(self.values)):
            inner.grid_columnconfigure(i, weight=1)
        inner.grid_rowconfigure(0, weight=1)
        
        # Create buttons
        for i, value in enumerate(self.values):
            info = self.display_info.get(value, {"label": value.title(), "icon": "", "desc": ""})
            text = f"{info['icon']}  {info['label']}".strip() if info.get("icon") else info["label"]
            
            is_selected = value == self.current_value
            btn = ctk.CTkButton(
                inner,
                text=text,
                font=("DejaVu Sans", 13, "bold" if is_selected else "normal"),  # module-scope: no font_sizes access
                fg_color=COLORS["bg_card"] if is_selected else "transparent",
                hover_color=COLORS["bg_hover"],
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                corner_radius=RADIUS["sm"],
                height=36,
                command=lambda v=value: self._on_select(v),
            )
            btn.grid(row=0, column=i, sticky="nsew", padx=1)
            self.buttons[value] = btn
    
    def _on_select(self, value: str) -> None:
        """Handle button selection."""
        if value == self.current_value:
            return
        
        old_value = self.current_value
        self.current_value = value
        
        # Update all button styles
        for v, btn in self.buttons.items():
            is_selected = v == value
            btn.configure(
                fg_color=COLORS["bg_card"] if is_selected else "transparent",
                text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                font=("DejaVu Sans", 13, "bold" if is_selected else "normal"),  # module-scope: no font_sizes access
            )
        
        # Call callback
        if self.command:
            self.command(value)
    
    def get(self) -> str:
        """Get current selected value."""
        return self.current_value
    
    def set(self, value: str) -> None:
        """Set current value without triggering callback."""
        if value in self.values and value != self.current_value:
            self.current_value = value
            for v, btn in self.buttons.items():
                is_selected = v == value
                btn.configure(
                    fg_color=COLORS["bg_card"] if is_selected else "transparent",
                    text_color=COLORS["text_bright"] if is_selected else COLORS["text_secondary"],
                    font=("DejaVu Sans", 13, "bold" if is_selected else "normal"),  # module-scope: no font_sizes access
                )


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """
    Scrollable frame with a few scroll-feel patches on top of CTk.

    CTk's stock <Configure>→scrollregion path rewrites the canvas region on
    every child geometry change (hover recolors, font reflow, etc.). Near the
    bottom that can shift yview by a fraction of a pixel and read as a glitch.
    We only push scrollregion when bbox("all") actually changes.

    The custom momentum/lerp scroller was removed earlier — it fought CTk.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sr_bbox = None
        # Replace CTk's Configure→scrollregion binding (bind without add
        # overwrites the widget-level handler installed in super().__init__).
        self.bind("<Configure>", self._on_inner_configure)

    def _on_inner_configure(self, _event=None):
        try:
            bbox = self._parent_canvas.bbox("all")
        except Exception:
            return
        if bbox is None or bbox == self._sr_bbox:
            return
        self._sr_bbox = bbox
        try:
            self._parent_canvas.configure(scrollregion=bbox)
        except Exception:
            pass
        # Just use standard CTk scrolling - works reliably


# Setting tooltip descriptions with latency indicators
# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY GUIDE: ⚡ = none, 🟢 = <10ms, 🟡 = 10-100ms, 🔴 = 100ms+, 🚀 = speedup
# ═══════════════════════════════════════════════════════════════════════════════

# Model recommendations for post-processing (based on testing)
MODEL_RECOMMENDATIONS = """
✅ Styles (Professional, Casual, Dev, Personal):
  • qwen3:4b — ⭐ Best overall (most reliable in every style we tested; Ultra)

✅ Normal needs no model — it removes um/uh instantly.
  • gemma3:1b — Fast; fine for Normal, but rewords technical terms in styles
  • qwen3.5:2b — Leaves text largely unchanged; Normal only

⚠️ Use with caution:
  • phi3:mini — Rewrites in standard mode (use strong/caricature only)
  • smollm2:360m — May hallucinate

💡 Tip: qwen3:4b for styles and Strong; Normal works with no model at all.
"""

# Setting hints (hover the (i) next to a setting). Plain text, no emoji
# (rule 11); keep claims to what the code does today. Benchmark-driven ones are
# rebuilt by get_dynamic_tooltip.
SETTING_TOOLTIPS = {
    "hotkey": "The shortcut that starts and stops dictation.",
    "microphone": "The microphone Aura listens to.",
    "hotkey_devices": "Which keyboards, mice or keypads can trigger the shortcut.",
    "enable_tray_icon": (
        "Show Aura in the menu bar, with its state and quick controls. "
        "Turn it off if your menu bar is crowded."
    ),
    "ui_scale": "Size of Aura's window and text.",
    "update_channel": (
        "Stable: tested releases, every few weeks.\n"
        "Beta: the newest build, as often as daily. New features sooner, "
        "and sometimes new bugs.\n"
        "Going back to Stable keeps your version until the next stable release."
    ),
    "crash_reports": (
        "When Aura crashes or hits an error, send Wayfinder the kind of error, "
        "where in Aura's code it happened and the app and system versions, so "
        "it gets fixed. Never the error message, audio, dictated text or your "
        "settings.\n"
        "On by default for Beta, off for Stable."
    ),
    "gamer_mode": (
        "When World of Warcraft is in front, Aura hears gamer talk (inc, pull, LFG, "
        "M+...), keeps your words as said, and sends them to chat: don't press "
        "Enter first, just tap your shortcut, speak, tap again. One message per "
        "dictation: past WoW's 255-character limit, the next part waits in chat "
        "for your Enter.\n"
        "Also set up for Final Fantasy XIV, Elder Scrolls Online, Lord of the Rings "
        "Online, Albion Online, RuneScape and EVE Online. In games Aura pastes "
        "rather than typing keys, so dictation can't trigger a keybind."
        + (
            "\nDark Age of Camelot (Linux) has no paste: press Enter to open chat "
            "first, then dictate, and Aura types it into the chat box."
            if _IS_LINUX and not (IS_MACOS or IS_WINDOWS) else ""
        )
    ),
    "game_chat_send": (
        "On: Aura presses Enter to send each message.\n"
        "Off: the text is left in the chat box for you to check and send."
    ),
    "overlay_type": (
        "Always On: the pill stays on screen and never takes focus.\n"
        "Disappearing: it shows only while you dictate.\n"
        "Applies immediately."
    ),
    "overlay_enabled": (
        "Show the floating status pill (Listening, Processing, Ready). "
        "With it off, dictation still works."
    ),
    "overlay_scale": "Size of the status pill, separate from the window size.",
    "overlay_position": "Move the status pill up or down the screen. Left is higher, right is lower.",
    "language": (
        "The language you dictate in. Other languages need a multilingual "
        "model (one without .en in its name)."
    ),
    "linux_portal_typing": (
        "Lets Aura type into every app on a Wayland desktop, native Wayland apps "
        "included, through your desktop's remote-control permission. Your desktop "
        "asks once; KDE shows a Remote Control icon in the tray while Aura runs.\n"
        "Off: Aura types with xdotool, which reaches X11 apps and games only."
    ),
    "press_enter_after_dictation": (
        "Press Enter after the text is typed. Off by default.\n"
        "This sends chat messages, runs terminal commands and submits AI prompts "
        "straight away, so leave it off if you want to check the text first."
    ),
    "audio_preprocessing": (
        "Sound processing on your mic before Whisper hears it. It never changes "
        "words; text cleanup is separate (Post-Processing).\n\n"
        "Off: the raw mic signal.\n"
        "Light (recommended): raises quiet recordings toward a normal level, at "
        "most 8x, so faint noise isn't blown up.\n"
        "Medium: Light plus an 80 Hz filter for hum, rumble and desk bumps. For a "
        "fan, air conditioning or a mic on the desk.\n"
        "Heavy: Medium plus turning steady noise between words down. For a noisy "
        "room when Medium isn't enough; it can swallow soft speech.\n\n"
        "None of them can fix clipping or a mic set far too loud."
    ),
    "chunked_mode": (
        "Transcribes long dictations in pieces while you speak, so the text is "
        "ready soon after you stop. Ultra.\n\n"
        "Off: one pass for the whole recording.\n"
        "Auto: one pass under 30 s; longer recordings go in 15 s pieces that "
        "overlap by 2 s. Recommended: in testing, 2-minute dictations on Large "
        "v3 Turbo Q5 lost words in one pass and kept them all on Auto.\n"
        "On: 15 s pieces from the start."
    ),
    "whisper_model": (
        "The speech model. It runs on your computer; nothing is sent to the cloud."
    ),
    "backend": (
        "Local transcription engine.\n\n"
        "• Auto / default — always whisper.cpp. Free uses Base on the CPU; Ultra\n"
        "  follows the GPU toggle. Never selects Faster-Whisper.\n\n"
        "• whisper.cpp — CPU for Free; with Ultra's GPU toggle, Metal on a Mac\n"
        "  and Vulkan on Linux and Windows.\n\n"
        "• Faster-Whisper (experimental) — Manual only, NVIDIA hosts only.\n"
        "  CTranslate2 CUDA path; not fully dogfooded. May fall back to slow CPU.\n"
        "  Ultra feature. Manual pick turns Auto off."
    ),
    "gpu_acceleration": (
        (
            "Use the Mac's GPU (Metal) for transcription and local cleanup. Ultra.\n"
            "Free runs Base on the CPU. Benchmark shows what the GPU would do."
        )
        if IS_MACOS
        else (
            "Use your graphics card for transcription. Ultra.\n"
            "Vulkan on AMD, NVIDIA and Intel GPUs; text cleanup stays on the CPU.\n"
            "Free runs Base on the CPU. Benchmark shows what the GPU would do."
        )
        if IS_WINDOWS
        else (
            "Use your GPU for transcription and local cleanup. Ultra.\n"
            "whisper.cpp uses Vulkan on AMD, NVIDIA and Intel GPUs. "
            "Faster-Whisper (experimental, manual): NVIDIA CUDA only.\n"
            "Free runs Base on the CPU. Benchmark shows what the GPU would do."
        )
    ),
    "gpu_device": (
        "Which graphics card transcribes. Automatic picks the dedicated GPU;\n"
        "choose the integrated one if the big card is busy with games or a local AI model."
    ),
}


def get_dynamic_tooltip(key: str, config: dict) -> str:
    """
    Generate dynamic tooltip text based on benchmark results.
    Falls back to static text with TBD for unbenchmarked values.
    """
    benchmark_results = warm_benchmark_results(config)
    fastest = config.get("benchmark_fastest_processor", None)
    
    # Model-specific tooltip with actual benchmarked speeds
    if key == "whisper_model":
        base_text = SETTING_TOOLTIPS["whisper_model"]
        selected_name = Path(os.path.expanduser(str(config.get("model_path", "") or ""))).name.lower()
        if selected_name in ("ggml-base.en.bin", "ggml-base.bin"):
            base_text += (
                "\n\nBase is the Free default: fast and light, but less accurate. "
                "Large v3 Turbo Q5 (Ultra) on a GPU made 43% fewer mistakes in our "
                "tests, at the same speed; on the CPU it is much slower."
            )
        
        if not benchmark_results:
            return f"{base_text}\n\nRun Benchmark to measure speeds on your hardware."
        
        # Build speed table from benchmarks
        speed_lines = []
        processor_label = "GPU" if fastest == "gpu" else "CPU" if fastest == "cpu" else "Best"
        
        model_order = ["tiny.en", "base.en", "small.en", "medium.en", "large-v3-turbo-q5", "large-v3-turbo", "large-v3"]
        model_names = {
            "tiny.en": "Tiny", "base.en": "Base", "small.en": "Small", 
            "medium.en": "Medium", "large-v3-turbo-q5": "Turbo Q5",
            "large-v3-turbo": "Turbo", "large-v3": "Large v3"
        }
        
        for model_id in model_order:
            if model_id in benchmark_results:
                result = benchmark_results[model_id]
                if fastest == "gpu" and result.get("gpu_10s") is not None:
                    time_str = format_bench_seconds(result["gpu_10s"])
                elif result.get("cpu_10s") is not None:
                    time_str = format_bench_seconds(result["cpu_10s"])
                else:
                    time_str = "TBD"
                name = model_names.get(model_id, model_id)
                speed_lines.append(f"{name}: ~{time_str}")
        
        if speed_lines:
            speeds = " | ".join(speed_lines)
            return f"{base_text}\n\n{processor_label} time for 10 s of audio:\n{speeds}"
        
        return f"{base_text}\n\nRun Benchmark to measure speeds on your hardware."
    
    # Accuracy mode tooltip with benchmarked impact
    
    # GPU acceleration tooltip with measured speedup
    if key == "gpu_acceleration":
        base_text = SETTING_TOOLTIPS.get("gpu_acceleration", "Use GPU for transcription.")
        if benchmark_results:
            # Calculate actual speedup from benchmark data
            speedups = []
            for model_id, result in benchmark_results.items():
                cpu_time = result.get("cpu_10s")
                gpu_time = result.get("gpu_10s")
                if cpu_time and gpu_time and gpu_time > 0:
                    speedups.append(cpu_time / gpu_time)
            if speedups:
                avg_speedup = sum(speedups) / len(speedups)
                return f"{base_text}\n\nOn your hardware the GPU was {avg_speedup:.1f}x faster than the CPU."
        return base_text

    # Backend: static rules + this machine's Auto recommendation
    if key == "backend":
        base = SETTING_TOOLTIPS.get("backend", "")
        try:
            from wayfinder.utils.gpu import recommend_local_transcription_backend
            # Tooltip cannot call feature_gate; pass allow from config-only hints
            # (Ultra is not stored as a simple flag — fail closed for “allowed”).
            fw_hint = False
            rec, reason = recommend_local_transcription_backend(
                allow_faster_whisper=fw_hint,
                use_gpu=bool(config.get("use_gpu", False)),
            )
        except Exception:
            rec, reason = "whisper_cpp", "whisper.cpp is the safe default."
        rec_label = {
            "whisper_cpp": "whisper.cpp",
            "faster_whisper": "Faster-Whisper (experimental)",
        }.get(rec, rec)
        auto_on = bool(config.get("transcription_backend_auto", True))
        active = config.get("transcription_backend", "whisper_cpp")
        active_label = {
            "whisper_cpp": "whisper.cpp",
            "faster_whisper": "Faster-Whisper (experimental)",
        }.get(active, active)
        mode = "Auto" if auto_on else "Manual (your choice)"
        return (
            f"{base}\n\n"
            f"This PC → Auto picks: {rec_label}\n"
            f"{reason}\n\n"
            f"Current: {active_label} · {mode}"
        )

    # Post-processing tooltip with measured cleanup times + pipeline total
    if key == "post_processing":
        base_text = (
            "Rewrites your dictation in the chosen style with a local model.\n"
            "Normal needs no model: it removes filler words instantly."
        )
        pp_results = config.get("postprocessing_benchmark_results", {}) or {}
        pipeline = config.get("pipeline_benchmark", {}) or {}
        if pipeline.get("method") != "warm":  # legacy cold pipeline: retire
            pipeline = {}
        lines = []
        if pipeline.get("total_time") is not None:
            asr = pipeline.get("asr_time")
            ppt = pipeline.get("pp_time")
            asr_s = format_bench_seconds(asr)
            pp_s = format_bench_seconds(ppt) if ppt is not None else "off"
            lines.append(
                f"Your setup total: {format_bench_seconds(pipeline['total_time'])} "
                f"(speech-to-text {asr_s} + cleanup {pp_s})"
            )
        if pp_results:
            ranked = sorted(
                (
                    (mid, r) for mid, r in pp_results.items()
                    if isinstance(r, dict) and r.get("avg_time") is not None and not r.get("error")
                ),
                key=lambda item: item[1]["avg_time"],
            )
            for mid, r in ranked[:6]:
                mark = " ← current" if r.get("is_current") else ""
                name = r.get("model_name", mid)
                lines.append(f"  {name}: {format_bench_seconds(r['avg_time'])}{mark}")
        if lines:
            return base_text + "\n\nMeasured on your hardware:\n" + "\n".join(lines)
        return base_text + "\n\nRun Benchmark for measured times on your hardware."
    
    # Default to static tooltip
    return SETTING_TOOLTIPS.get(key, "")


def create_section_header(parent, title: str, font_family: str = "DejaVu Sans") -> ctk.CTkLabel:
    """Create a styled section header label."""
    return ctk.CTkLabel(
        parent,
        text=title,
        font=(font_family, FONT_SIZES["heading"], "bold"),
        text_color=COLORS["text_primary"],
        anchor="w",
    )


def create_body_label(parent, text: str, font_family: str = "DejaVu Sans") -> ctk.CTkLabel:
    """Create a styled body text label."""
    return ctk.CTkLabel(
        parent,
        text=text,
        font=(font_family, FONT_SIZES["body"]),
        text_color=COLORS["text_secondary"],
        anchor="w",
    )


class CompatibilityBanner(ctk.CTkFrame):
    """
    A banner that displays model compatibility warnings and recommendations.
    
    Shows when the selected post-processing model can't handle the
    requested intensity level, with actionable upgrade suggestions.
    """
    
    # Severity colors
    SEVERITY_COLORS = {
        "ok": None,  # Hidden when ok
        "warning": "#3D3520",  # Muted amber background
        "incompatible": "#3D2020",  # Muted red background
    }
    
    SEVERITY_BORDER_COLORS = {
        "ok": COLORS["border"],
        "warning": "#E5AC2A",  # Muted gold
        "incompatible": "#E87070",  # Muted rose
    }
    
    SEVERITY_ICONS = {
        "ok": "✓",
        "warning": "⚠",
        "incompatible": "⚠",
    }
    
    def __init__(self, parent, **kwargs):
        """
        Create a compatibility banner.
        
        Args:
            parent: Parent widget
        """
        super().__init__(
            parent,
            fg_color=COLORS["bg_input"],
            corner_radius=RADIUS["sm"],
            border_width=1,
            border_color=COLORS["border"],
            **kwargs
        )
        
        self._visible = False
        
        # Main content container
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(fill="x", padx=12, pady=10)
        
        # Header row with icon and title
        self.header = ctk.CTkFrame(self.content, fg_color="transparent")
        self.header.pack(fill="x")
        
        self.icon_label = ctk.CTkLabel(
            self.header,
            text="⚠",
            font=("DejaVu Sans", 14),  # module-scope: no font_sizes access
            text_color=COLORS["accent_yellow"],
        )
        self.icon_label.pack(side="left", padx=(0, 8))
        
        self.title_label = ctk.CTkLabel(
            self.header,
            text="Model Compatibility",
            font=("DejaVu Sans", 12, "bold"),  # module-scope: no font_sizes access
            text_color=COLORS["text_bright"],
        )
        self.title_label.pack(side="left")
        
        # Issue description
        self.issue_label = ctk.CTkLabel(
            self.content,
            text="",
            font=("DejaVu Sans", 11),  # module-scope: no font_sizes access
            text_color=COLORS["text_secondary"],
            wraplength=320,
            justify="left",
        )
        self.issue_label.pack(anchor="w", pady=(6, 0))
        
        # Recommendations frame
        self.recommendations_frame = ctk.CTkFrame(self.content, fg_color="transparent")
        self.recommendations_frame.pack(fill="x", pady=(8, 0))
        
        # Model suggestion with copy button
        self.suggestion_frame = ctk.CTkFrame(
            self.content,
            fg_color=COLORS["bg_elevated"],
            corner_radius=RADIUS["sm"],
        )
        self.suggestion_frame.pack(fill="x", pady=(8, 0))
        
        self.suggestion_label = ctk.CTkLabel(
            self.suggestion_frame,
            text="",
            font=("DejaVu Sans", 11),  # module-scope: no font_sizes access
            text_color=COLORS["accent"],
        )
        self.suggestion_label.pack(side="left", padx=10, pady=8)
        
        # Initially hidden
        self.pack_forget()
    
    def update_status(self, compatibility: dict) -> None:
        """
        Update the banner based on compatibility check results.
        
        Args:
            compatibility: Dict from check_settings_compatibility() with:
                - is_compatible: bool
                - issues: list of strings
                - recommendations: list of strings
                - upgrade_message: str or None
                - severity: "ok" | "warning" | "incompatible"
        """
        severity = compatibility.get("severity", "ok")
        
        if severity == "ok" or not compatibility.get("issues"):
            # Hide banner when everything is compatible
            self._visible = False
            self.pack_forget()
            return
        
        # Show and update banner
        self._visible = True
        
        # Update colors based on severity
        bg_color = self.SEVERITY_COLORS.get(severity, COLORS["bg_input"])
        border_color = self.SEVERITY_BORDER_COLORS.get(severity, COLORS["border"])
        icon = self.SEVERITY_ICONS.get(severity, "⚠")
        
        self.configure(fg_color=bg_color or COLORS["bg_input"], border_color=border_color)
        self.icon_label.configure(
            text=icon,
            text_color=border_color,
        )
        
        # Update issue text
        issues = compatibility.get("issues", [])
        if issues:
            issue_text = issues[0]
            if len(issues) > 1:
                issue_text += f" (+{len(issues)-1} more)"
            self.issue_label.configure(text=issue_text)
        
        # Update recommendations
        for widget in self.recommendations_frame.winfo_children():
            widget.destroy()
        
        recommendations = compatibility.get("recommendations", [])
        for rec in recommendations[:3]:  # Show max 3 recommendations
            rec_label = ctk.CTkLabel(
                self.recommendations_frame,
                text=f"• {rec}",
                font=("DejaVu Sans", 10),  # module-scope: no font_sizes access
                text_color=COLORS["text_muted"],
                wraplength=300,
                justify="left",
            )
            rec_label.pack(anchor="w", pady=1)
        
        # Update model suggestion
        upgrade_message = compatibility.get("upgrade_message")
        if upgrade_message:
            self.suggestion_label.configure(text=f"💡 {upgrade_message[:60]}...")
            self.suggestion_frame.pack(fill="x", pady=(8, 0))
        else:
            self.suggestion_frame.pack_forget()
        
        # Show the banner
        self.pack(fill="x", pady=(8, 0))
    
    def hide(self) -> None:
        """Hide the banner."""
        self._visible = False
        self.pack_forget()
    
    def is_visible(self) -> bool:
        """Check if the banner is currently visible."""
        return self._visible

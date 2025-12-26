"""
Reusable UI components for Wayfinder Voice.

Contains common widgets used throughout the application:
- ToolTip: Hover tooltips for widgets
- ModeSelector: Segmented control for mode selection
- SmoothScrollableFrame: Scrollable container
"""

import customtkinter as ctk
from .theme import COLORS, RADIUS, FONT_SIZES


class ToolTip:
    """
    Modern hover tooltip for CustomTkinter widgets.
    Styled to match the design system.
    """
    
    def __init__(self, widget, text: str, delay: int = 300):
        """
        Create a tooltip for a widget.
        
        Args:
            widget: The widget to attach the tooltip to
            text: Tooltip text (supports multi-line)
            delay: Delay in ms before showing tooltip
        """
        self.widget = widget
        self.text = text
        self.delay = delay
        self.tooltip_window = None
        self.scheduled_id = None
        
        widget.bind("<Enter>", self.on_enter)
        widget.bind("<Leave>", self.on_leave)
        widget.bind("<ButtonPress>", self.on_leave)
    
    def on_enter(self, event=None):
        self.scheduled_id = self.widget.after(self.delay, self.show_tooltip)
    
    def on_leave(self, event=None):
        if self.scheduled_id:
            self.widget.after_cancel(self.scheduled_id)
            self.scheduled_id = None
        self.hide_tooltip()
    
    def show_tooltip(self):
        if self.tooltip_window:
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
            font=("Inter", 12),
            text_color=COLORS["text_primary"],
            wraplength=260,
            justify="left",
        )
        label.pack(padx=12, pady=10)
        
        # Position tooltip above widget
        tw.update_idletasks()
        tw_width = tw.winfo_width()
        tw_height = tw.winfo_height()
        screen_width = tw.winfo_screenwidth()
        screen_height = tw.winfo_screenheight()
        
        # Center above widget
        widget_x = self.widget.winfo_rootx()
        widget_width = self.widget.winfo_width()
        x = widget_x + (widget_width - tw_width) // 2
        
        # Position above with gap
        y = self.widget.winfo_rooty() - tw_height - 6
        
        # Keep on screen horizontally
        if x < 10:
            x = 10
        elif x + tw_width > screen_width - 10:
            x = screen_width - tw_width - 10
        
        # If would go off top, show below
        if y < 10:
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        
        tw.wm_geometry(f"+{x}+{y}")
    
    def hide_tooltip(self):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None
    
    def update_text(self, text: str):
        """Update the tooltip text."""
        self.text = text


class ModeSelector(ctk.CTkFrame):
    """
    Segmented control widget for selecting processing mode.
    Three mutually exclusive options: Local | Hybrid | Remote
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
        
        self.values = values or ["local", "hybrid", "remote"]
        self.current_value = current_value
        self.command = command
        self.buttons: dict[str, ctk.CTkButton] = {}
        
        # Display labels and icons for each mode
        self.display_info = {
            "local": {"label": "Local", "icon": "🔒", "desc": "100% Private"},
            "hybrid": {"label": "Hybrid", "icon": "🔗", "desc": "Local + Cloud AI"},
            "remote": {"label": "Remote", "icon": "☁️", "desc": "Cloud Processing"},
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
            text = f"{info['icon']}  {info['label']}"
            
            is_selected = value == self.current_value
            btn = ctk.CTkButton(
                inner,
                text=text,
                font=("Inter", 13, "bold" if is_selected else "normal"),
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
                font=("Inter", 13, "bold" if is_selected else "normal"),
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
                    font=("Inter", 13, "bold" if is_selected else "normal"),
                )


class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """
    Scrollable frame wrapper that uses CTk's built-in scrolling.
    The custom smooth scrolling implementation was incompatible with 
    the current CustomTkinter version.
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Just use standard CTk scrolling - works reliably


# Setting tooltip descriptions with latency indicators
# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY GUIDE: ⚡ = none, 🟢 = <10ms, 🟡 = 10-100ms, 🔴 = 100ms+, 🚀 = speedup
# ═══════════════════════════════════════════════════════════════════════════════
SETTING_TOOLTIPS: dict[str, str] = {
    # ⚡ No latency impact - UI/configuration only
    "hotkey": "The keyboard shortcut to start/stop voice recording.\n⚡ Latency: None",
    "microphone": "Select which microphone/audio input device to use.\n⚡ Latency: None",
    "hotkey_devices": "Which keyboards, mice, or keypads can trigger the hotkey.\n⚡ Latency: None",
    "benchmark": "Measure transcription speed on your hardware.\nResults customize speed estimates throughout the app.\n⏱️ Run once to get accurate timing predictions.",
    "start_minimized": "Start the app minimized to the system tray.\n⚡ Latency: None",
    "ui_scale": "Adjust the size of the user interface.\n⚡ Latency: None",
    "overlay_type": "Choose the status indicator style:\n• Always On: Stays visible, never steals focus (PyQt6)\n• Disappearing: Shows only during recording (CTk)\n⚠️ Requires restart to take effect.",
    "overlay_scale": "Adjust the size of the status overlay.\nSeparate from the main UI scale.\n⚡ Latency: None",
    "prompt": "Initial text that guides transcription style.\n⚡ Latency: None (processed at model load)",
    "language": "The language for transcription. English is most optimized.\n⚡ Latency: None",
    
    # 🟢 Minimal latency impact (<10ms per invocation)
    "typing_speed": "How fast text is typed out.\n🟢 Instant: 0ms | Fast: ~50ms | Normal: ~200ms | Slow: ~500ms per sentence",
    "ensure_punctuation": "Extra punctuation fixes if model output lacks periods/caps.\n🟢 Latency: +1-3ms (optional, most models handle this well)",
    "audio_preprocessing": "Audio signal processing before transcription.\n🟢 Off: 0ms | Light: +2ms | Medium: +5ms | Heavy: +10ms",
    
    # 🟡 Moderate latency impact (10-100ms)
    "chunked_mode": "Split long recordings into segments, transcribe each,\nand splice results together. Enables unlimited length.\n🟡 Latency: +50-100ms overhead per segment boundary",
    "chunk_duration": "Length of each audio segment (seconds).\nShorter = faster feedback but more splice points.\n🟡 15-30s recommended for best balance",
    
    # 🔴 MAJOR latency impact - These are the biggest factors
    "whisper_model": "Local on-device speech recognition model.\nProcessed entirely on your machine — no cloud API needed.\n🔴 GPU: Tiny ~0.5s | Base ~1s | Small ~1.5s | Medium ~3s | Large ~6s | Turbo ~2s\n🔴 CPU: Tiny ~2s | Base ~4s | Small ~6s | Medium ~12s | Large ~25s | Turbo ~8s",
    "accuracy_mode": "Speed vs accuracy preset - affects beam search depth.\n🔴 Fast: -40% time (beam=1) | Balanced: baseline (beam=5) | High: +60% time (beam=8)",
    "beam_size": "Search width for finding best transcription.\n🔴 1 = fastest (-50%) | 5 = balanced | 10 = slowest (+100%)",
    
    # GPU/Backend - Can dramatically change all timings
    "backend": "Transcription engine selection.\n⚙️ whisper.cpp: CPU-optimized, lower memory\n⚙️ Faster-Whisper: Better GPU utilization (up to 10x faster)",
    "gpu_acceleration": "Use GPU for transcription.\n🚀 Enabled: 3-10x faster than CPU (requires CUDA/ROCm/Vulkan)",
    "gpu_layers": "Model layers to offload to GPU.\n⚙️ Auto: Maximum speed | Fewer: Saves VRAM, slower",
}


def get_dynamic_tooltip(key: str, config: dict) -> str:
    """
    Generate dynamic tooltip text based on benchmark results.
    Falls back to static text with TBD for unbenchmarked values.
    """
    benchmark_results = config.get("benchmark_results", {})
    fastest = config.get("benchmark_fastest_processor", None)
    
    # Model-specific tooltip with actual benchmarked speeds
    if key == "whisper_model":
        base_text = "Local on-device speech recognition model.\nProcessed entirely on your machine — no cloud API needed."
        
        if not benchmark_results:
            return f"{base_text}\n\n⏱️ Run benchmark to measure speeds on your hardware."
        
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
                if fastest == "gpu" and "gpu_10s" in result:
                    time_str = f"{result['gpu_10s']:.1f}s"
                elif "cpu_10s" in result:
                    time_str = f"{result['cpu_10s']:.1f}s"
                else:
                    time_str = "TBD"
                name = model_names.get(model_id, model_id)
                speed_lines.append(f"{name}: ~{time_str}")
        
        if speed_lines:
            speeds = " | ".join(speed_lines)
            return f"{base_text}\n\n🚀 {processor_label} speeds (10s audio):\n{speeds}"
        
        return f"{base_text}\n\n⏱️ Run benchmark to measure speeds on your hardware."
    
    # Accuracy mode tooltip with benchmarked impact
    if key == "accuracy_mode":
        base_text = "Speed vs accuracy preset - affects beam search depth."
        if benchmark_results and fastest:
            return f"{base_text}\n🔴 Fast: ~40% faster | Balanced: baseline | High: ~60% slower\n(Based on your {fastest.upper()} benchmarks)"
        return f"{base_text}\n🔴 Fast: -40% time (beam=1) | Balanced: baseline (beam=5) | High: +60% time (beam=8)\n⏱️ Run benchmark for exact timings"
    
    # GPU acceleration tooltip with measured speedup
    if key == "gpu_acceleration":
        base_text = "Use GPU for transcription."
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
                return f"{base_text}\n🚀 Your GPU is {avg_speedup:.1f}x faster than CPU on average!"
        return f"{base_text}\n🚀 TBD — run benchmark to measure your GPU speedup"
    
    # Default to static tooltip
    return SETTING_TOOLTIPS.get(key, "")


def create_section_header(parent, title: str, font_family: str = "Inter") -> ctk.CTkLabel:
    """Create a styled section header label."""
    return ctk.CTkLabel(
        parent,
        text=title,
        font=(font_family, FONT_SIZES["heading"], "bold"),
        text_color=COLORS["text_primary"],
        anchor="w",
    )


def create_body_label(parent, text: str, font_family: str = "Inter") -> ctk.CTkLabel:
    """Create a styled body text label."""
    return ctk.CTkLabel(
        parent,
        text=text,
        font=(font_family, FONT_SIZES["body"]),
        text_color=COLORS["text_secondary"],
        anchor="w",
    )



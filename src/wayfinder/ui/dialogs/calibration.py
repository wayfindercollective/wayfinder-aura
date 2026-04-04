# NOTE: Not wired in. Needs CTkFrame panel conversion (CLAUDE.md Rule #2) before use.
"""
Audio Calibration Dialog for Wayfinder Aura.

Provides a setup wizard that:
1. Records a short audio sample
2. Analyzes for clipping, noise floor, signal level
3. Recommends optimal settings
4. Optionally applies settings automatically
"""

import customtkinter as ctk
import threading
import time
from typing import Callable, Optional

from ..theme import COLORS, RADIUS, SPACING, FONT_SIZES
from ...core.recorder import AudioCalibrator, AudioCalibrationResult


class AudioLevelMeter(ctk.CTkFrame):
    """
    Visual audio level meter with peak indicator.
    """
    
    def __init__(self, parent, **kwargs):
        super().__init__(parent, fg_color=COLORS["bg_input"], corner_radius=RADIUS["sm"], **kwargs)
        
        self.configure(height=24)
        
        # Level bar
        self.level_bar = ctk.CTkFrame(
            self,
            fg_color=COLORS["accent"],
            corner_radius=RADIUS["xs"],
            height=16,
        )
        self.level_bar.place(x=4, y=4, relwidth=0, height=16)
        
        # Peak indicator (red section for clipping)
        self.peak_zone = ctk.CTkFrame(
            self,
            fg_color=COLORS["error"],
            corner_radius=0,
            height=16,
        )
        # Position at 90%+ of the bar width
        self.peak_zone.place(relx=0.9, y=4, relwidth=0.1, height=16)
        
        self._level = 0.0
    
    def set_level(self, level: float) -> None:
        """Set the current level (0.0 to 1.0)."""
        self._level = min(1.0, max(0.0, level))
        
        # Color based on level
        if self._level > 0.95:
            color = COLORS["error"]  # Clipping
        elif self._level > 0.8:
            color = COLORS["accent_yellow"]  # Hot
        else:
            color = COLORS["accent"]  # Good
        
        self.level_bar.configure(fg_color=color)
        
        # Update width (as relative width minus padding)
        rel_width = self._level * 0.98  # Leave some padding
        self.level_bar.place(x=4, y=4, relwidth=rel_width, height=16)


class AudioCalibrationDialog(ctk.CTkToplevel):
    """
    Audio calibration wizard dialog.
    
    Guides the user through testing their microphone and
    automatically recommends optimal settings.
    """
    
    RECORDING_DURATION = 5.0  # seconds
    
    def __init__(
        self,
        parent,
        device_id: int | None = None,
        on_apply: Optional[Callable[[dict], None]] = None,
        **kwargs
    ):
        """
        Create the audio calibration dialog.
        
        Args:
            parent: Parent window
            device_id: Audio device to test (None for default)
            on_apply: Callback when user applies settings, receives dict of settings
        """
        super().__init__(parent, **kwargs)
        
        self.device_id = device_id
        self.on_apply = on_apply
        self.calibrator: Optional[AudioCalibrator] = None
        self.result: Optional[AudioCalibrationResult] = None
        self._recording = False
        self._update_job = None
        
        # Window setup
        self.title("Audio Calibration")
        self.geometry("420x520")
        self.resizable(False, False)
        self.configure(fg_color=COLORS["bg_main"])
        
        # Center on parent
        self.transient(parent)
        self.grab_set()
        
        self._build_ui()
        
        # Center the window
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 420) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 520) // 2
        self.geometry(f"+{x}+{y}")
    
    def _build_ui(self) -> None:
        """Build the dialog UI."""
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=24, pady=(24, 16))
        
        ctk.CTkLabel(
            header,
            text="🎙️  Audio Calibration",
            font=("Inter", 20, "bold"),
            text_color=COLORS["text_bright"],
        ).pack(anchor="w")
        
        ctk.CTkLabel(
            header,
            text="Test your microphone to optimize settings",
            font=("Inter", 13),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w", pady=(4, 0))
        
        # Main content card
        card = ctk.CTkFrame(
            self,
            fg_color=COLORS["bg_card"],
            corner_radius=RADIUS["lg"],
            border_width=1,
            border_color=COLORS["border"],
        )
        card.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        
        # Instructions
        self.instruction_label = ctk.CTkLabel(
            card,
            text="Click 'Start Test' and speak normally for 5 seconds.\nTry to speak at your typical volume.",
            font=("Inter", 12),
            text_color=COLORS["text_primary"],
            justify="center",
        )
        self.instruction_label.pack(pady=(24, 16))
        
        # Level meter
        meter_frame = ctk.CTkFrame(card, fg_color="transparent")
        meter_frame.pack(fill="x", padx=24, pady=(0, 8))
        
        ctk.CTkLabel(
            meter_frame,
            text="Audio Level",
            font=("Inter", 11),
            text_color=COLORS["text_secondary"],
        ).pack(anchor="w")
        
        self.level_meter = AudioLevelMeter(meter_frame)
        self.level_meter.pack(fill="x", pady=(4, 0))
        
        # Progress/timer
        self.progress_frame = ctk.CTkFrame(card, fg_color="transparent")
        self.progress_frame.pack(fill="x", padx=24, pady=(8, 16))
        
        self.timer_label = ctk.CTkLabel(
            self.progress_frame,
            text="Ready to test",
            font=("Inter", 13, "bold"),
            text_color=COLORS["text_primary"],
        )
        self.timer_label.pack()
        
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_frame,
            fg_color=COLORS["bg_input"],
            progress_color=COLORS["accent"],
            height=8,
            corner_radius=4,
        )
        self.progress_bar.pack(fill="x", pady=(8, 0))
        self.progress_bar.set(0)
        
        # Results section (initially hidden)
        self.results_frame = ctk.CTkFrame(card, fg_color="transparent")
        # Don't pack yet - shown after test
        
        # Results content
        self.quality_label = ctk.CTkLabel(
            self.results_frame,
            text="",
            font=("Inter", 16, "bold"),
            text_color=COLORS["text_bright"],
        )
        self.quality_label.pack(pady=(0, 12))
        
        # Metrics grid
        metrics_frame = ctk.CTkFrame(self.results_frame, fg_color=COLORS["bg_elevated"], corner_radius=RADIUS["sm"])
        metrics_frame.pack(fill="x", padx=16, pady=(0, 12))
        
        self.metrics_labels = {}
        metrics = [
            ("Peak Level", "peak"),
            ("Noise Floor", "noise"),
            ("Signal/Noise", "snr"),
            ("Clipping", "clip"),
        ]
        
        for i, (label, key) in enumerate(metrics):
            row = ctk.CTkFrame(metrics_frame, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(8 if i == 0 else 2, 8 if i == len(metrics)-1 else 2))
            
            ctk.CTkLabel(
                row,
                text=label,
                font=("Inter", 11),
                text_color=COLORS["text_secondary"],
                width=100,
                anchor="w",
            ).pack(side="left")
            
            value_label = ctk.CTkLabel(
                row,
                text="—",
                font=("JetBrains Mono", 11),
                text_color=COLORS["text_primary"],
            )
            value_label.pack(side="right")
            self.metrics_labels[key] = value_label
        
        # Issues/recommendations
        self.issues_label = ctk.CTkLabel(
            self.results_frame,
            text="",
            font=("Inter", 11),
            text_color=COLORS["text_secondary"],
            wraplength=340,
            justify="left",
        )
        self.issues_label.pack(fill="x", padx=16, pady=(0, 8))
        
        # Recommended setting
        self.recommend_frame = ctk.CTkFrame(
            self.results_frame,
            fg_color=COLORS["bg_input"],
            corner_radius=RADIUS["sm"],
        )
        self.recommend_frame.pack(fill="x", padx=16, pady=(0, 8))
        
        ctk.CTkLabel(
            self.recommend_frame,
            text="Recommended Setting",
            font=("Inter", 10),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w", padx=12, pady=(8, 2))
        
        self.recommend_label = ctk.CTkLabel(
            self.recommend_frame,
            text="",
            font=("Inter", 13, "bold"),
            text_color=COLORS["accent"],
        )
        self.recommend_label.pack(anchor="w", padx=12, pady=(0, 8))
        
        # Buttons
        button_frame = ctk.CTkFrame(self, fg_color="transparent")
        button_frame.pack(fill="x", padx=24, pady=(0, 24))
        
        self.cancel_btn = ctk.CTkButton(
            button_frame,
            text="Cancel",
            font=("Inter", 13),
            fg_color=COLORS["bg_input"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_primary"],
            corner_radius=RADIUS["md"],
            height=40,
            width=100,
            command=self.destroy,
        )
        self.cancel_btn.pack(side="left")
        
        self.apply_btn = ctk.CTkButton(
            button_frame,
            text="Apply Settings",
            font=("Inter", 13, "bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            text_color=COLORS["text_bright"],
            corner_radius=RADIUS["md"],
            height=40,
            width=120,
            command=self._apply_settings,
            state="disabled",
        )
        self.apply_btn.pack(side="right")
        
        self.test_btn = ctk.CTkButton(
            button_frame,
            text="Start Test",
            font=("Inter", 13, "bold"),
            fg_color=COLORS["bg_elevated"],
            hover_color=COLORS["bg_hover"],
            text_color=COLORS["text_bright"],
            corner_radius=RADIUS["md"],
            height=40,
            width=100,
            command=self._toggle_test,
        )
        self.test_btn.pack(side="right", padx=(0, 8))
    
    def _toggle_test(self) -> None:
        """Start or stop the calibration test."""
        if self._recording:
            self._stop_test()
        else:
            self._start_test()
    
    def _start_test(self) -> None:
        """Start recording for calibration."""
        self._recording = True
        self.test_btn.configure(text="Stop", fg_color=COLORS["error"])
        self.apply_btn.configure(state="disabled")
        self.results_frame.pack_forget()
        
        self.instruction_label.configure(
            text="🎤 Recording... Speak normally now!",
            text_color=COLORS["accent"],
        )
        
        # Start calibrator
        self.calibrator = AudioCalibrator(device=self.device_id)
        self.calibrator.start()
        
        self._start_time = time.time()
        self._update_during_recording()
    
    def _update_during_recording(self) -> None:
        """Update UI during recording."""
        if not self._recording or self.calibrator is None:
            return
        
        elapsed = time.time() - self._start_time
        remaining = max(0, self.RECORDING_DURATION - elapsed)
        
        # Update level meter
        level = self.calibrator.get_current_level()
        self.level_meter.set_level(level)
        
        # Update progress
        progress = min(1.0, elapsed / self.RECORDING_DURATION)
        self.progress_bar.set(progress)
        self.timer_label.configure(text=f"{remaining:.1f}s remaining")
        
        if elapsed >= self.RECORDING_DURATION:
            self._stop_test()
        else:
            self._update_job = self.after(50, self._update_during_recording)
    
    def _stop_test(self) -> None:
        """Stop recording and analyze results."""
        self._recording = False
        
        if self._update_job:
            self.after_cancel(self._update_job)
            self._update_job = None
        
        self.test_btn.configure(text="Test Again", fg_color=COLORS["bg_elevated"])
        
        if self.calibrator:
            self.result = self.calibrator.stop_and_analyze()
            self.calibrator = None
            self._show_results()
    
    def _show_results(self) -> None:
        """Display calibration results."""
        if not self.result:
            return
        
        r = self.result
        
        # Quality assessment
        quality_text = {
            "excellent": "✅ Excellent Audio Quality",
            "good": "✅ Good Audio Quality", 
            "fair": "⚠️ Fair Audio Quality",
            "poor": "❌ Poor Audio Quality",
        }
        quality_color = {
            "excellent": COLORS["success"],
            "good": COLORS["success"],
            "fair": COLORS["accent_yellow"],
            "poor": COLORS["error"],
        }
        
        self.quality_label.configure(
            text=quality_text.get(r.overall_quality, "Unknown"),
            text_color=quality_color.get(r.overall_quality, COLORS["text_primary"]),
        )
        
        # Metrics
        self.metrics_labels["peak"].configure(
            text=f"{r.peak_level * 100:.0f}%",
            text_color=COLORS["error"] if r.peak_level > 0.95 else COLORS["text_primary"],
        )
        self.metrics_labels["noise"].configure(
            text=f"{r.noise_floor * 100:.1f}%",
            text_color=COLORS["accent_yellow"] if r.noise_floor > 0.03 else COLORS["text_primary"],
        )
        self.metrics_labels["snr"].configure(
            text=f"{r.signal_to_noise:.0f} dB",
            text_color=COLORS["success"] if r.signal_to_noise > 30 else COLORS["text_primary"],
        )
        self.metrics_labels["clip"].configure(
            text=f"{r.clipping_percentage:.1f}%" if r.clipping_detected else "None ✓",
            text_color=COLORS["error"] if r.clipping_percentage > 1 else COLORS["success"],
        )
        
        # Issues and recommendations
        issues_text = "\n".join(f"• {issue}" for issue in r.issues[:3])
        if r.recommendations:
            issues_text += "\n\n" + "\n".join(f"💡 {rec}" for rec in r.recommendations[:2])
        self.issues_label.configure(text=issues_text)
        
        # Recommended preprocessing
        preprocess_names = {
            "off": "Off (no processing)",
            "light": "Light (normalization only)",
            "medium": "Medium (normalize + filter)",
            "heavy": "Heavy (full noise reduction)",
        }
        self.recommend_label.configure(
            text=f"Audio Processing: {preprocess_names.get(r.recommended_preprocessing, r.recommended_preprocessing)}"
        )
        
        # Update instruction
        self.instruction_label.configure(
            text="Test complete! Review results below.",
            text_color=COLORS["text_primary"],
        )
        
        self.timer_label.configure(text="Analysis complete")
        self.progress_bar.set(1.0)
        
        # Show results and enable apply
        self.results_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.apply_btn.configure(state="normal")
    
    def _apply_settings(self) -> None:
        """Apply the recommended settings."""
        if not self.result:
            return
        
        settings = {
            "audio_preprocessing": self.result.recommended_preprocessing,
        }
        
        if self.on_apply:
            self.on_apply(settings)
        
        self.destroy()

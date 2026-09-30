"""
The recommended Ultra setup, applied once when an install switches to Ultra.

Free runs on CPU with chunking off (both are Ultra features), so a fresh Ultra
activation used to feel identical to Free until the user found each switch.
On the first switch to Ultra this turns on what the evaluation says helps
(docs/EVAL-2026-09-24.md):

- GPU acceleration, on hardware where it is the better default (Apple Silicon,
  NVIDIA or AMD). whisper.cpp falls back to its CPU server if the GPU fails.
- Chunk Processing "Auto": dictations under 30 seconds stay one request, longer
  ones are transcribed in the background while you speak. On Turbo Q5 a long
  dictation in one pass lost up to half its words; Auto kept them all
  (docs/EVAL-2026-09-30-chunking.md).
- Text cleanup on. With the Normal style it only removes um/uh (no model, about
  a millisecond); Turbo Q5 writes fillers down far more often than Base.
- Large v3 Turbo Q5 as the speech model, but only when it is already on disk
  and the GPU is on (on CPU it is about 9x slower than Base). Otherwise the
  Ultra tip offers the download.

It runs once per install. Every later change is the user's, so nothing here is
reapplied on launch. An install that was already Ultra before this existed
keeps its settings as they are.

Pure functions, no Tk and no I/O, so they stay headlessly testable.
"""

from __future__ import annotations

from pathlib import Path

# None: not decided yet (a config from before this key existed).
# False: seen on Free, so the next switch to Ultra applies the defaults.
# True: done (or already Ultra before this existed). Never reapplied.
ULTRA_DEFAULTS_KEY = "ultra_defaults_applied"

RECOMMENDED_SPEECH_MODEL = "ggml-large-v3-turbo-q5_0.bin"

# Vendors whose GPU beats the CPU for whisper.cpp by default. Integrated Intel
# graphics can be slower than the CPU, so it stays an explicit choice.
_GPU_DEFAULT_VENDORS = frozenset({"apple", "nvidia", "amd"})


def gpu_is_default_capable(vendor: str, platform_name: str) -> bool:
    """Whether GPU acceleration should be on by default for this GPU.

    macOS needs Apple Silicon (Metal). Elsewhere a discrete-class NVIDIA or
    AMD GPU (Vulkan); callers also check that a GPU build of whisper.cpp
    ships (Windows keeps it beside the CPU build).
    """
    vendor = (vendor or "").lower()
    if platform_name == "darwin":
        return vendor == "apple"
    return vendor in _GPU_DEFAULT_VENDORS - {"apple"}


def _has(gate, feature: str) -> bool:
    try:
        return bool(gate is not None and gate.has_feature(feature))
    except Exception:
        return False


def apply_ultra_defaults(
    config: dict,
    gate,
    *,
    gpu_capable: bool,
    recommended_model_path: str | None = None,
    activated_now: bool = False,
) -> list[str]:
    """Apply the recommended Ultra setup once. Returns the keys it changed.

    ``activated_now`` is True when the user just activated a key in the app.
    At launch it is False: an install that is Ultra with no record yet was
    Ultra before this feature existed and keeps its settings.
    ``recommended_model_path`` is the Large v3 Turbo Q5 file if it is on disk.
    Never raises on a broken gate: nothing is changed.
    """
    changed: list[str] = []

    def _set(key: str, value: object) -> None:
        if config.get(key) != value:
            config[key] = value
            changed.append(key)

    state = config.get(ULTRA_DEFAULTS_KEY)
    try:
        premium = bool(getattr(gate, "is_premium", False))
    except Exception:
        return changed
    if state is True:
        return changed
    if not premium:
        if state is None:
            _set(ULTRA_DEFAULTS_KEY, False)
        return changed
    if state is None and not activated_now:
        # Already Ultra before this existed: those settings are the user's.
        _set(ULTRA_DEFAULTS_KEY, True)
        return changed

    gpu_on = bool(config.get("use_gpu", False))
    if _has(gate, "gpu_acceleration") and gpu_capable and not gpu_on:
        _set("use_gpu", True)
        gpu_on = True

    # Free forced "off"; an explicit "on" (or "auto") is kept.
    if _has(gate, "chunked_recording") and config.get("chunked_mode", "off") in (None, "", "off"):
        _set("chunked_mode", "auto")

    # Opt-in on Free. Normal needs no model; the styles Ultra unlocks need it on.
    if not config.get("post_processing_enabled", False):
        _set("post_processing_enabled", True)

    if recommended_model_path and gpu_on and _has(gate, "large_models"):
        try:
            from wayfinder.license import is_free_transcription_model

            current = str(config.get("model_path", "") or "")
            # Only replace the Free model, never another model the user picked.
            if not current or is_free_transcription_model(current):
                if Path(recommended_model_path).name == RECOMMENDED_SPEECH_MODEL:
                    _set("model_path", str(recommended_model_path))
        except Exception:
            pass

    _set(ULTRA_DEFAULTS_KEY, True)
    return changed


def describe_changes(changed: list[str]) -> str:
    """One log line naming what the Ultra setup switched on; "" if nothing."""
    parts = []
    if "use_gpu" in changed:
        parts.append("GPU acceleration on")
    if "chunked_mode" in changed:
        parts.append("Chunk Processing on Auto (long dictations)")
    if "post_processing_enabled" in changed:
        parts.append("text cleanup on (Normal removes um/uh, no model needed)")
    if "model_path" in changed:
        parts.append("Large v3 Turbo Q5 speech model")
    if not parts:
        return ""
    return "😇 Ultra setup: " + ", ".join(parts) + ". Change any of these in Settings."

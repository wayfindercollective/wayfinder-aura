"""
Ultra under-utilization detection for the launch nudge.

An activated Ultra install starts exactly like Free (GPU off, Base speech
model, free cleanup model, chunking off) because upgrades are opt-in. These
helpers spot an Ultra user who hasn't switched anything on yet so the app can
show one light, dismissible cue instead of leaving the upgrade feeling
identical to Free. Pure functions — no Tk, no I/O — so they stay headlessly
testable.
"""

import os

# Signal keys are stable identifiers (tests/logging); order = display priority.
SIGNAL_PHRASES = {
    "model": "download a faster, more accurate speech model",
    "gpu": "switch on GPU acceleration",
    "cleanup": "grab a premium cleanup model",
    "chunking": "enable chunk processing for long dictations",
}


def underutilization_signals(config, gate, has_gpu: bool) -> "list[str]":
    """Which Ultra capabilities this Ultra user hasn't switched on yet.

    Returns signal keys from SIGNAL_PHRASES, display-priority order.
    Empty list = not premium, or everything relevant is already in use.
    Never raises: an unreadable signal is simply skipped.
    """
    if not getattr(gate, "is_premium", False):
        return []
    signals = []

    try:
        model_name = os.path.basename(str(config.get("model_path", "") or "")).lower()
        if gate.has_feature("large_models") and "base" in model_name:
            signals.append("model")
    except Exception:
        pass

    try:
        if (
            gate.has_feature("gpu_acceleration")
            and has_gpu
            and not config.get("use_gpu", False)
        ):
            signals.append("gpu")
    except Exception:
        pass

    try:
        if gate.has_feature("large_cleanup_models"):
            from wayfinder.core.postprocessor import cleanup_model_allowed

            class _FreeGate:
                @staticmethod
                def has_feature(_feature):
                    return False

            # Under-utilizing only when the CURRENT cleanup model would run on
            # the Free tier. The shared tier logic also recognizes custom
            # 3B+/4B models as premium — the catalog marker alone misses
            # those, since Browse stores it as None for custom paths (Codex
            # review).
            if cleanup_model_allowed(
                str(config.get("llama_cpp_model_path", "") or ""),
                _FreeGate(),
                config.get("llama_cpp_model_requires_feature"),
            ):
                signals.append("cleanup")
    except Exception:
        pass

    try:
        if (
            gate.has_feature("chunked_recording")
            and str(config.get("chunked_mode", "off") or "off") == "off"
        ):
            signals.append("chunking")
    except Exception:
        pass

    return signals


def nudge_text(signals: "list[str]") -> str:
    """One friendly sentence for the launch banner; "" when nothing to say."""
    phrases = [SIGNAL_PHRASES[s] for s in signals if s in SIGNAL_PHRASES]
    if not phrases:
        return ""
    if len(phrases) == 1:
        body = phrases[0]
    elif len(phrases) == 2:
        body = " and ".join(phrases)
    else:
        body = ", ".join(phrases[:-1]) + ", and " + phrases[-1]
    return f"Ultra tip 😇 — you can {body}."

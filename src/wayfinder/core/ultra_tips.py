"""
Ultra under-utilization detection for the launch nudge.

The first switch to Ultra turns GPU acceleration and chunking on
(core/ultra_defaults.py), but it cannot download the bigger models, and users
can switch things off again. These helpers spot an Ultra user who isn't using
an upgrade so the app can show one light, dismissible cue instead of leaving
the upgrade feeling identical to Free. Pure functions — no Tk, no I/O — so
they stay headlessly testable.
"""


# Signal keys are stable identifiers (tests/logging); order = display priority.
SIGNAL_PHRASES = {
    # Measured (scripts/eval_matrix.py, M3 Ultra): Large v3 Turbo Q5 on GPU makes
    # 43% fewer word errors than Base on CPU at the same ~0.27 s per dictation.
    "model": "switch to the Large v3 Turbo Q5 speech model (about 40% fewer mistakes, same speed on the GPU)",
    # No GPU: Turbo Q5 takes ~9x as long as Base on CPU (2.27 s vs 0.26 s);
    # Small makes 25-30% fewer errors than Base (Small EN 8.6% vs 11.5% WER) in 0.76 s.
    "model_cpu": "switch to the Small speech model (about 25% fewer mistakes; about 3x as long on CPU)",
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
        from wayfinder.license import is_free_transcription_model

        # The Free Base/Base.en files only: a custom file with "base" in its
        # name is the user's own pick.
        on_free_model = is_free_transcription_model(str(config.get("model_path", "") or ""))
        if gate.has_feature("large_models") and on_free_model:
            # Turbo Q5 is only as fast as Base on a GPU.
            gpu_usable = has_gpu and gate.has_feature("gpu_acceleration")
            signals.append("model" if gpu_usable else "model_cpu")
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

"""Run a callable with a wall-clock timeout.

Used to bound the in-process inference backends (llama-cpp-python, Faster-Whisper) that block
in a C call which a thread-level timeout cannot interrupt. On timeout we raise so the caller can
recover (emit a terminal event / fall back to raw text) instead of leaving the app stuck in
PROCESSING forever. The underlying call keeps running in its daemon thread until it finishes — so
this bounds *recovery*, not the leaked work. The default CLI backends (whisper.cpp, llama.cpp)
use real ``subprocess`` timeouts and get a hard kill instead.
"""

import threading


class CallTimeout(Exception):
    """Raised when :func:`run_with_timeout` exceeds its deadline."""


def run_with_timeout(fn, timeout, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` in a daemon thread, raising :class:`CallTimeout` past ``timeout``s.

    A ``timeout`` of ``None`` or ``<= 0`` runs ``fn`` directly with no watchdog. The worker
    thread is a daemon, so a leaked (still-running) call will not block interpreter shutdown.
    The original exception from ``fn`` (if any) is re-raised in the caller.
    """
    if not timeout or timeout <= 0:
        return fn(*args, **kwargs)

    result = {}

    def _runner():
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - propagate the original error to the caller
            result["error"] = exc

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise CallTimeout(f"operation exceeded {timeout:.0f}s")
    if "error" in result:
        raise result["error"]
    return result.get("value")

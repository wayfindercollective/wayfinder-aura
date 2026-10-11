"""ggml Metal environment for whisper.cpp children on macOS.

The bundled whisper binaries are built with Metal. Two ggml switches matter:

* CPU mode (``-ng``/``--no-gpu``, the Free path) never computes on the GPU,
  yet ggml still registers the Metal device: a ~7-8s shader compile after
  every install/update (the cache is keyed per executable path) and a
  residency heartbeat that wakes the resident server ~170x/s forever.
  ``GGML_METAL_DEVICES=0`` registers no Metal device; output and speed are
  identical.
* GPU mode keeps Metal's default residency sets. Disabling them saved a small
  amount of idle CPU but let the resident speech model be swapped out under
  memory pressure. On a 24 GB M4, the same 3.94 s clip took 6.28 s after idle
  without residency versus 1.33 s with it. The ~0.37% idle CPU cost keeps the
  model ready for dictation.

Returns ``{}`` off macOS, so Linux/Windows environments are untouched.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

_CPU_FLAGS = {"-ng", "--no-gpu"}


def whisper_metal_env(cmd: Iterable[object] | None = None, *, gpu: bool | None = None) -> dict[str, str]:
    """Environment overrides for a whisper.cpp command (or explicit ``gpu``)."""
    if sys.platform != "darwin":
        return {}
    if gpu is None:
        args = {str(arg) for arg in (cmd or ())}
        gpu = not (args & _CPU_FLAGS)
    if gpu:
        return {}
    return {"GGML_METAL_DEVICES": "0"}

"""ggml Metal environment for whisper.cpp children on macOS.

The bundled whisper binaries are built with Metal. Two ggml switches matter:

* CPU mode (``-ng``/``--no-gpu``, the Free path) never computes on the GPU,
  yet ggml still registers the Metal device: a ~7-8s shader compile after
  every install/update (the cache is keyed per executable path) and a
  residency heartbeat that wakes the resident server ~170x/s forever.
  ``GGML_METAL_DEVICES=0`` registers no Metal device; output and speed are
  identical.
* GPU mode keeps Metal but sets ``GGML_METAL_NO_RESIDENCY=1``: the residency
  heartbeat is what keeps an idle server awake (~185 wakeups/s -> ~0.5/s)
  for a negligible post-idle latency cost.

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
        return {"GGML_METAL_NO_RESIDENCY": "1"}
    return {"GGML_METAL_DEVICES": "0"}

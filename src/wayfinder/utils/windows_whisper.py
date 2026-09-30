"""The two whisper.cpp builds the Windows app carries: CPU and Vulkan (GPU).

packaging/windows/build.py bundles the official CPU build under ``whisper/``
and a Vulkan build of the same pinned whisper.cpp under ``whisper-vulkan/``
(dev builds stage them in ``build/windows-whisper[-vulkan]``). A binary's twin
is the same file name in the sibling folder: the Windows counterpart of the
Flatpak's ``whisper-cli`` / ``whisper-cli-cpu`` pair. The CPU build stays the
default; the Vulkan one serves only Ultra's GPU acceleration (the license gate
in transcriber.get_backend decides, never this module).
"""

from __future__ import annotations

from pathlib import Path

GPU_SUFFIX = "-vulkan"


def gpu_twin(binary: str) -> str | None:
    """The Vulkan build of *binary* (a CPU-build exe), if bundled."""
    if not binary:
        return None
    path = Path(binary)
    if path.parent.name.lower().endswith(GPU_SUFFIX):
        return None
    twin = path.parent.parent / (path.parent.name + GPU_SUFFIX) / path.name
    return str(twin) if twin.is_file() else None


def cpu_twin(binary: str) -> str | None:
    """The CPU build of *binary* (a Vulkan-build exe), if bundled."""
    if not binary:
        return None
    path = Path(binary)
    folder = path.parent.name
    if not folder.lower().endswith(GPU_SUFFIX):
        return None
    twin = path.parent.parent / folder[: -len(GPU_SUFFIX)] / path.name
    return str(twin) if twin.is_file() else None


def crashed(returncode: int | None) -> bool:
    """True if a process died rather than exited with an error.

    POSIX reports a signal as a negative code. Windows reports a crash as its
    NTSTATUS exception code (0xC0000005 access violation, 0xC0000409 stack
    buffer overrun, ...), which subprocess returns as a large positive number.
    """
    if returncode is None:
        return False
    return returncode < 0 or returncode >= 0xC0000000

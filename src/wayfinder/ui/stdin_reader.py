"""Blocking stdin reader for the overlay's command channel.

Why a thread instead of a select()-gated readline() in the timer tick:

readline() on a buffered text stream pulls an entire chunk of the pipe into
Python's internal buffer, so every line after the first vanishes from the raw
fd's readability. A select() gate then reports "nothing to read" while
complete, undelivered commands sit in the buffer. The overlay hit this on
every recording stop: the audio-level stream halts, then "show processing" is
written as the last line before seconds of pipe silence — when it coalesced
into the same buffered read as the final level updates, it stayed invisible
until the next write ("show ready", after transcription) woke the fd. The
user saw "Listening..." through the whole transcription, then a brief
Processing flash.

A dedicated thread doing plain blocking readline() has no such gap: every
line reaches the queue the moment it is read, no matter how the kernel
coalesced the writes. Qt-free so it stays unit-testable (see
tests/test_stdin_reader.py), mirroring overlay_geometry.py.
"""

import queue
import threading


class StdinCommandReader:
    """Pump lines from a text stream into a thread-safe queue."""

    def __init__(self, stream):
        self._stream = stream
        self._queue = queue.Queue()
        self._eof = threading.Event()
        self._thread = threading.Thread(
            target=self._pump, name="overlay-stdin-reader", daemon=True
        )

    def start(self):
        """Start the pump thread; returns self for one-line setup."""
        self._thread.start()
        return self

    def _pump(self):
        try:
            for line in iter(self._stream.readline, ""):
                stripped = line.strip()
                if stripped:
                    self._queue.put(stripped)
        except Exception:
            pass  # broken/closed pipe mid-read — same as EOF for our purposes
        self._eof.set()

    def drain(self):
        """Return every line received since the last call. Never blocks."""
        lines = []
        while True:
            try:
                lines.append(self._queue.get_nowait())
            except queue.Empty:
                return lines

    @property
    def at_eof(self):
        """True once the stream ended AND all buffered lines were drained."""
        return self._eof.is_set() and self._queue.empty()

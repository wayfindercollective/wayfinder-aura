"""Wake the Tk event loop the moment a worker thread queues an event (macOS).

Workers (hotkey listener, transcription, injection) talk to the Tk thread
through ``event_queue``, which Tk polled every 250 ms when idle — so a hotkey
press waited up to a quarter second before recording even began. A self-pipe
registered with ``createfilehandler`` wakes Tk as soon as ``put()`` runs
(measured: 0.1 ms median on Aqua Tk). The 250 ms poll stays as a safety net.

There is no timer involved: the pipe only becomes readable when an event
arrives, so idle CPU is unchanged (CLAUDE.md rule 1).
"""

from __future__ import annotations

import os
import queue


class WakingQueue(queue.Queue):
    """A ``queue.Queue`` whose ``put`` also makes a pipe readable."""

    def __init__(self, maxsize: int = 0):
        super().__init__(maxsize)
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        os.set_blocking(self._write_fd, False)
        self._attached = False

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        try:
            os.write(self._write_fd, b"\0")
        except (BlockingIOError, OSError):
            pass  # pipe full: a wake-up is already pending

    def _consume(self) -> None:
        try:
            while os.read(self._read_fd, 4096):
                pass
        except (BlockingIOError, OSError):
            pass

    def attach(self, widget, drain) -> bool:
        """Call ``drain()`` on the Tk thread whenever something is queued."""
        if self._attached:
            return True
        import tkinter

        def _ready(_fd, _mask):
            self._consume()
            try:
                drain()
            except Exception:
                pass

        try:
            widget.tk.createfilehandler(self._read_fd, tkinter.READABLE, _ready)
        except Exception:
            return False
        self._attached = True
        return True

    def detach(self, widget) -> None:
        if not self._attached:
            return
        try:
            widget.tk.deletefilehandler(self._read_fd)
        except Exception:
            pass
        self._attached = False

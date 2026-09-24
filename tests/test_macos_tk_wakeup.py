"""The waking event queue: put() makes Tk handle events without the idle poll."""
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from wayfinder.ui.macos_tk_wakeup import WakingQueue  # noqa: E402


def test_put_makes_the_pipe_readable_and_consume_clears_it():
    q = WakingQueue()
    q.put(("event", 1))
    assert os.read(q._read_fd, 16)
    q.put(("event", 2))
    q._consume()
    with pytest.raises(BlockingIOError):
        os.read(q._read_fd, 16)
    assert q.get_nowait() == ("event", 1)


def test_a_full_pipe_never_blocks_put():
    q = WakingQueue()
    for i in range(100_000):  # far beyond the pipe buffer
        q.put(i)
    assert q.qsize() == 100_000


@pytest.mark.skipif(sys.platform == "win32", reason="Tk file handlers are POSIX-only")
def test_tk_is_woken_immediately():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    q = WakingQueue()
    handled = []

    def drain():
        while not q.empty():
            handled.append((q.get_nowait(), time.perf_counter()))
        if handled:
            root.quit()

    assert q.attach(root, drain)
    sent = []

    def producer():
        time.sleep(0.1)
        sent.append(time.perf_counter())
        q.put("hotkey")

    threading.Thread(target=producer, daemon=True).start()
    root.after(3000, root.quit)
    root.mainloop()
    q.detach(root)
    root.destroy()
    assert handled and handled[0][0] == "hotkey"
    assert handled[0][1] - sent[0] < 0.05  # vs up to 250 ms with the idle poll

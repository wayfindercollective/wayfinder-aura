"""Regression tests for StdinCommandReader (the "stuck on Listening..." bug).

The old drain gated readline() on select() against the raw fd. When the
kernel handed several pipe writes over in one buffered read, the lines after
the first were invisible to select() and sat undelivered until the NEXT
write — for the overlay that meant a state change written right after the
audio-level stream stopped (exactly what happens on recording stop) arrived
seconds late, bundled with the following command.

These tests drive the reader through a real OS pipe wrapped the same way the
overlay wraps sys.stdin.
"""

import io
import os
import time

from wayfinder.ui.stdin_reader import StdinCommandReader


def _make_pipe_reader():
    rfd, wfd = os.pipe()
    stream = io.TextIOWrapper(os.fdopen(rfd, "rb"), line_buffering=True)
    return StdinCommandReader(stream).start(), wfd


def _drain_until(reader, expected_count, timeout=2.0):
    """Poll drain() like the overlay's 50ms timer until enough lines arrive."""
    lines = []
    deadline = time.monotonic() + timeout
    while len(lines) < expected_count and time.monotonic() < deadline:
        lines.extend(reader.drain())
        time.sleep(0.01)
    return lines


def test_burst_then_silence_delivers_every_line():
    """A state change coalesced with level updates must not need a wake-up write."""
    reader, wfd = _make_pipe_reader()
    burst = b'{"cmd": "level", "value": 0.5}\n' * 5
    burst += b'{"cmd": "show", "state": "processing"}\n'
    os.write(wfd, burst)  # one write -> one coalesced read, then silence

    lines = _drain_until(reader, 6)

    assert len(lines) == 6
    assert lines[-1] == '{"cmd": "show", "state": "processing"}'
    os.close(wfd)


def test_separate_writes_arrive_in_order():
    reader, wfd = _make_pipe_reader()
    os.write(wfd, b'{"cmd": "show", "state": "listening"}\n')
    time.sleep(0.05)
    os.write(wfd, b'{"cmd": "show", "state": "ready"}\n')

    lines = _drain_until(reader, 2)

    assert lines == [
        '{"cmd": "show", "state": "listening"}',
        '{"cmd": "show", "state": "ready"}',
    ]
    os.close(wfd)


def test_eof_only_after_trailing_lines_are_drained():
    reader, wfd = _make_pipe_reader()
    os.write(wfd, b'{"cmd": "hide"}\n')
    os.close(wfd)

    deadline = time.monotonic() + 2.0
    while not reader._eof.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)

    # EOF must not be reported while a command still waits in the queue
    assert not reader.at_eof
    assert reader.drain() == ['{"cmd": "hide"}']
    assert reader.at_eof

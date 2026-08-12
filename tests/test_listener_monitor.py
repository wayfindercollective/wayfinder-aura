"""The pynput wrapper thread must track its inner listener faithfully.

pynput runs keyboard.Listener in its own thread while the wrapper polls
stop_event. If the wrapper outlives a dead listener, callers watching the
wrapper believe a listener is alive and Detect arms against nothing. If the
wrapper exits while the listener is still coming up, the supervisor starts a
second listener and hotkeys can double-fire. Both directions matter.
"""

from threading import Event

from wayfinder.hotkeys.pynput_listener import monitor_listener


class _FakeListener:
    """Thread-like: is_alive() is authoritative, `running` deliberately lies."""

    def __init__(self, alive_for):
        self._alive_for = alive_for
        self.checks = 0
        self.running = True  # pynput can leave this True after the thread returns

    def is_alive(self):
        self.checks += 1
        return self.checks <= self._alive_for


def test_returns_when_the_stop_event_is_set():
    stop = Event()
    stop.set()
    listener = _FakeListener(alive_for=99)
    logs = []

    assert monitor_listener(listener, stop, logs.append, sleep=lambda _s: None) is True
    assert logs == []


def test_returns_when_the_inner_listener_dies():
    listener = _FakeListener(alive_for=2)
    logs = []

    result = monitor_listener(listener, Event(), logs.append, sleep=lambda _s: None)

    assert result is False
    assert any("stopped unexpectedly" in message for message in logs)


def test_liveness_uses_is_alive_not_the_running_flag():
    """`running` stays True here; only is_alive() reports the truth."""
    listener = _FakeListener(alive_for=1)
    assert listener.running is True

    assert monitor_listener(listener, Event(), lambda _m: None, sleep=lambda _s: None) is False


def test_a_listener_that_never_dies_keeps_polling_until_stopped():
    stop = Event()
    listener = _FakeListener(alive_for=99)

    def _sleep(_seconds):
        if listener.checks >= 3:
            stop.set()

    assert monitor_listener(listener, stop, lambda _m: None, sleep=_sleep) is True

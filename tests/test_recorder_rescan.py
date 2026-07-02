"""WarmMic PortAudio-rescan heal: when every input in the fallback chain fails,
_open() must rescan PortAudio's device table, re-resolve the saved mic by name,
and retry the chain exactly once (2026-07-02 boot-while-mic-off failure mode:
a USB-hub mic powered on after the app started is invisible to PortAudio's
init-time device snapshot, so every rung — cached index, system default,
None — fails with PaErrorCode -9999 until the tables are rebuilt)."""

import pytest

from wayfinder.core import recorder as rec


def test_open_rescans_and_reresolves_on_total_failure(monkeypatch):
    wm = rec.WarmMic(device=42, resolve_device=lambda: 7)
    calls = {"chain": 0, "rescan": 0}

    def fake_chain(self):
        calls["chain"] += 1
        if calls["chain"] == 1:
            raise RuntimeError("all inputs failed")
        # second walk (post-rescan) succeeds

    monkeypatch.setattr(rec.WarmMic, "_open_chain", fake_chain)
    monkeypatch.setattr(rec, "_pa_rescan",
                        lambda: (calls.__setitem__("rescan", calls["rescan"] + 1), True)[1])

    wm._open()

    assert calls["chain"] == 2, "chain must be retried exactly once after the rescan"
    assert calls["rescan"] == 1
    assert wm.device == 7, "retry must target the freshly re-resolved device"


def test_open_raises_first_error_when_rescan_unavailable(monkeypatch):
    wm = rec.WarmMic(device=42, resolve_device=lambda: 7)
    first = RuntimeError("all inputs failed")

    def fake_chain(self):
        raise first

    monkeypatch.setattr(rec.WarmMic, "_open_chain", fake_chain)
    monkeypatch.setattr(rec, "_pa_rescan", lambda: False)

    with pytest.raises(RuntimeError) as exc:
        wm._open()
    assert exc.value is first
    assert wm.device == 42, "device must not be touched when the rescan failed"


def test_open_survives_resolver_exception(monkeypatch):
    def bad_resolver():
        raise OSError("device enumeration exploded")

    wm = rec.WarmMic(device=42, resolve_device=bad_resolver)
    calls = {"chain": 0}

    def fake_chain(self):
        calls["chain"] += 1
        if calls["chain"] == 1:
            raise RuntimeError("all inputs failed")

    monkeypatch.setattr(rec.WarmMic, "_open_chain", fake_chain)
    monkeypatch.setattr(rec, "_pa_rescan", lambda: True)

    wm._open()  # resolver blew up, but the post-rescan retry still runs
    assert calls["chain"] == 2
    assert wm.device == 42, "failed resolve keeps the prior device for the retry"


def test_open_without_resolver_still_retries_after_rescan(monkeypatch):
    wm = rec.WarmMic(device=42)  # no resolve_device wired
    calls = {"chain": 0}

    def fake_chain(self):
        calls["chain"] += 1
        if calls["chain"] == 1:
            raise RuntimeError("all inputs failed")

    monkeypatch.setattr(rec.WarmMic, "_open_chain", fake_chain)
    monkeypatch.setattr(rec, "_pa_rescan", lambda: True)

    wm._open()
    assert calls["chain"] == 2

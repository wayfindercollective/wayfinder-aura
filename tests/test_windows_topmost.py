"""Windows pill topmost keeper (20 Oct 2026: the pill, still flagged topmost,
was ranked below ordinary windows and hidden behind them)."""
import ctypes
import sys
from pathlib import Path

import pytest

# The keeper's logic is pure Python driven through a faked user32 (FakeUser32
# below), so it's tested that way on macOS/Linux too — only the tests that
# need a real HWND/windll are Windows-only.
windows_only = pytest.mark.skipif(not hasattr(ctypes, "windll"), reason="Windows only")

ROOT = Path(__file__).resolve().parents[1]
PILL = 100


class FakeUser32:
    """A z-order (top first) of (hwnd, visible, topmost[, cloaked])."""

    def __init__(self, stack, sticky=False):
        self.stack = [tuple(w) + (False,) * (4 - len(w)) for w in stack]
        self.restored = []
        self.flags = None   # SWP flags passed to the last SetWindowPos call
        self.sticky = sticky  # True: restoring doesn't move the pill (it gets pushed back)

    def _index(self, h):
        return [w[0] for w in self.stack].index(h)

    def GetTopWindow(self, _):
        return self.stack[0][0] if self.stack else None

    def GetWindow(self, h, _cmd):
        i = self._index(h) + 1
        return self.stack[i][0] if i < len(self.stack) else None

    def IsWindowVisible(self, h):
        return self.stack[self._index(h)][1]

    def GetWindowLongW(self, h, _idx):
        return 0x8 if self.stack[self._index(h)][2] else 0

    def SetWindowPos(self, h, after, *_rest):
        self.restored.append((h, after))
        self.flags = _rest[-1] if _rest else None
        if not self.sticky:
            entry = self.stack.pop(self._index(h))
            # Real Windows sets WS_EX_TOPMOST as a side effect of landing in
            # the topmost band (HWND_TOPMOST, or just behind another topmost
            # window) -- a lost topmost flag is restored by the very
            # insertion the keeper already does, not a separate style
            # fix-up (the reviewer: accepted behaviour). Modelled here so a
            # test can assert on it without a real HWND.
            entry = entry[:2] + (True,) + entry[3:]
            # after=HWND_TOPMOST (-1, never a real hwnd here): straight to the
            # top, as before. A real hwnd: insert directly behind it, matching
            # Win32's hWndInsertAfter semantics.
            ids = [w[0] for w in self.stack]
            at = ids.index(after) + 1 if after in ids else 0
            self.stack.insert(at, entry)
        return 1


@pytest.fixture
def wt(monkeypatch):
    from wayfinder.ui import windows_topmost
    return windows_topmost


def use(monkeypatch, wt, stack, sticky=False):
    fake = FakeUser32(stack, sticky)
    monkeypatch.setattr(wt, "_user32", fake)
    monkeypatch.setattr(wt, "_cloaked", lambda h: fake.stack[fake._index(h)][3])
    return fake


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def keeper_for(wt, reports, clock=None, front="Code.exe"):
    return wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock or Clock(),
                            front=lambda: front)


def test_healthy_pill_has_nothing_above_it(monkeypatch, wt):
    use(monkeypatch, wt, [(1, True, True), (PILL, True, True), (2, True, False)])
    assert wt.normal_windows_above(PILL) == 0


def test_demoted_pill_counts_visible_ordinary_windows_only(monkeypatch, wt):
    use(monkeypatch, wt, [(1, True, True),      # Magnifier-style topmost: fine
                          (2, True, False),     # VS Code
                          (3, False, False),    # hidden: doesn't count
                          (4, True, False),     # Explorer
                          (PILL, True, True)])
    assert wt.normal_windows_above(PILL) == 2


def test_missing_pill_reads_as_not_demoted(monkeypatch, wt):
    use(monkeypatch, wt, [(1, True, False), (2, True, False)])
    assert wt.normal_windows_above(PILL) == 0


def test_cloaked_windows_do_not_count(monkeypatch, wt):
    use(monkeypatch, wt, [(2, True, False, True),   # other virtual desktop
                          (PILL, True, True)])
    assert wt.normal_windows_above(PILL) == 0


def test_keeper_restores_without_activating_and_reports_at_once(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    reports = []
    keeper = keeper_for(wt, reports)
    assert keeper.check() == 1
    assert fake.restored == [(PILL, wt._HWND_TOPMOST)]
    assert reports == [{"restores": 1, "above": 1, "fronts": ["Code.exe"], "held": True}]
    assert keeper.check() == 0          # back on top: nothing more to do
    assert len(reports) == 1


def test_restore_never_places_the_pill_above_a_legitimate_topmost_window(monkeypatch, wt):
    """20 Oct: with [legit topmost A, ordinary B, pill], HWND_TOPMOST always
    goes to the very top, giving [pill, A, B] and un-topping A. The repair
    must land the pill just below A instead, leaving A on top — keeping its
    own WS_EX_TOPMOST (round 3: it never lost it here) and never stealing
    focus to do it."""
    fake = use(monkeypatch, wt, [(1, True, True), (2, True, False), (PILL, True, True)])
    reports = []
    keeper = keeper_for(wt, reports)
    assert keeper.check() == 1               # only ordinary B (2) counts as "above"
    assert fake.restored == [(PILL, 1)]       # inserted just below A (hwnd 1), not HWND_TOPMOST
    order = [w[0] for w in fake.stack]
    assert order.index(1) < order.index(PILL) < order.index(2)   # A, pill, B
    assert wt._user32.GetWindowLongW(PILL, wt._GWL_EXSTYLE) & wt._WS_EX_TOPMOST   # style: still set
    assert fake.flags & wt._SWP_NOACTIVATE    # foreground: the call can't have stolen it
    assert reports[-1]["held"] is True


def test_a_pill_that_lost_ws_ex_topmost_is_restored_into_the_anchors_band(monkeypatch, wt):
    """Round 3: the restoration tests above all start with the pill still
    flagged WS_EX_TOPMOST, just mis-positioned. Here it's lost the flag
    entirely too (e.g. some other app's z-order fight stripped it, not just
    moved it) — restore_topmost() must still land it just below the
    legitimate anchor (A), and Windows re-sets WS_EX_TOPMOST as a side
    effect of that insertion (the reviewer: accepted behaviour, not a bug
    to work around), still without stealing focus."""
    fake = use(monkeypatch, wt, [(1, True, True), (2, True, False), (PILL, True, False)])
    reports = []
    keeper = keeper_for(wt, reports)
    assert keeper.check() == 1               # only ordinary B (2) counts as "above"
    assert fake.restored == [(PILL, 1)]       # inserted just below A (hwnd 1), not HWND_TOPMOST
    order = [w[0] for w in fake.stack]
    assert order.index(1) < order.index(PILL) < order.index(2)   # A, pill, B
    assert wt._user32.GetWindowLongW(PILL, wt._GWL_EXSTYLE) & wt._WS_EX_TOPMOST   # style: restored
    assert fake.flags & wt._SWP_NOACTIVATE    # foreground: the call can't have stolen it
    assert reports[-1]["held"] is True


def test_later_restores_are_batched_and_flushed_in_a_quiet_spell(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    fronts = iter(["Code.exe", "Slack.exe", "explorer.exe"])
    keeper = wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock,
                              front=lambda: next(fronts))
    keeper.check()                      # reported at once
    for step in (12, 12):               # two more demotions inside 30 s
        clock.t += step
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
    assert len(reports) == 1
    clock.t += 10                       # no demotion, but 30 s since the report
    keeper.check()
    assert reports[1] == {"restores": 2, "above": 1, "fronts": ["Slack.exe", "explorer.exe"],
                          "held": True}


def test_restore_that_does_not_hold_backs_off_for_a_minute(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)], sticky=True)
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)
    keeper.check()
    assert reports[-1]["held"] is False
    for _ in range(59):
        clock.t += 1
        keeper.check()
    assert len(fake.restored) == 1      # no fight every second
    clock.t += 2
    keeper.check()
    assert len(fake.restored) == 2      # tries again after the pause


def test_a_restore_that_does_not_hold_while_active_tries_again_every_few_seconds(monkeypatch, wt):
    # Sol pill r5: during a dictation the pause after a restore that did not
    # hold is ACTIVE_MAX_PAUSE_S, not the minute-long back-off.
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)], sticky=True)
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)
    keeper.set_active(True)                     # one immediate attempt; sticky never holds
    assert len(fake.restored) == 1
    assert reports[-1]["held"] is False and reports[-1]["pause_s"] == wt.ACTIVE_MAX_PAUSE_S
    attempts = [clock.t]
    for _ in range(30):
        clock.t += 1
        before = len(fake.restored)
        keeper.check()
        if len(fake.restored) > before:
            attempts.append(clock.t)
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert len(gaps) >= 5 and all(gap == wt.ACTIVE_MAX_PAUSE_S + 1 or gap == wt.ACTIVE_MAX_PAUSE_S
                                  for gap in gaps)
    keeper.set_active(False)
    clock.t += 10
    keeper.check()
    assert reports[-1]["pause_s"] == wt.BACKOFF_S   # idle again: the full back-off


def test_rapid_real_switches_are_throttled_by_the_repair_cooldown_not_backoff(monkeypatch, wt):
    """Demoted on every window switch (20 Oct): every restore must go ahead
    before too long, or the pill would stay hidden behind the window just
    switched to. Sol pill r4 adds a flat REPAIR_COOLDOWN_S floor underneath
    that (see the alternating-windows test below, finding 2) -- a rapid run
    of real switches is throttled to the cooldown's pace, not fought every
    single tick, but each attempt that does run still holds right away, so
    this never turns into the long, escalating pause a persistent contender
    gets (see test_delayed_redemotion_against_an_unchanged_foreground_backs_
    off below)."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)
    for _ in range(20):                     # 20 switches in 4 s
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
        clock.t += 0.2
    assert len(fake.restored) == 2           # throttled to ~1 per REPAIR_COOLDOWN_S over 4 s
    assert keeper._paused_until == float("-inf")   # never escalated into a real back-off
    assert all(r["held"] for r in reports)   # whichever attempts ran all held right away


def test_delayed_redemotion_against_an_unchanged_foreground_backs_off(monkeypatch, wt):
    """20 Oct: the sticky=True test above only models a restore that fails to
    hold AT ONCE. Here every restore holds the instant check() verifies it,
    but the same foreground window re-demotes the pill again before the next
    tick — a mock with an unchanged foreground produced 120 restores in 120 s
    with no back-off (window-order fighting, flicker)."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)   # front="Code.exe" the whole time
    for _ in range(120):                      # re-demoted once a second for 2 minutes
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
        clock.t += 1.0
    assert len(fake.restored) < 20            # backed off well short of fighting every tick
    assert all(r["held"] for r in reports)    # each individual restore still held when checked


def test_contention_resets_when_the_foreground_changes(monkeypatch, wt):
    """Switching to a different app must get a clean slate, not inherit the
    previous app's back-off — the contention is specific to one offender."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    fronts = iter(["Code.exe"] * 11 + ["Slack.exe"])
    keeper = wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock,
                              front=lambda: next(fronts))
    for _ in range(11):                       # just long enough to trip the back-off
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
        clock.t += 1.0
    assert clock.t < keeper._paused_until     # Code.exe tripped the back-off
    fake.stack.insert(0, fake.stack.pop(fake._index(2)))
    assert keeper.check() == 1                # Slack.exe gets a fresh try, not the pause
    assert fake.restored[-1] == (PILL, wt._HWND_TOPMOST)


def test_second_window_of_the_same_app_does_not_inherit_the_first_ones_back_off(monkeypatch, wt):
    """Round 2: contention was keyed by executable basename, so switching
    from one VS Code window to ANOTHER Code window (same exe, different
    HWND) inherited the first window's pause — even after the first window
    closed, since a bare exe name can't tell them apart. Keying by HWND
    instead must give the second window (or whatever's foreground once the
    first is gone — closing a window means its HWND can never be reported
    as the foreground window again) an immediate, fresh try — past the flat
    REPAIR_COOLDOWN_S floor (Sol pill r4), which no reset bypasses, same as
    any other attempt."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)], sticky=True)
    clock, reports = Clock(), []
    hwnds = iter([201, 202])  # 201: the Code window that trips the back-off; 202: a second one
    keeper = wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock,
                              front=lambda: "Code.exe", front_hwnd=lambda: next(hwnds))
    assert keeper.check() == 1                 # window 201 demotes it; sticky means it never holds
    assert clock.t < keeper._paused_until      # ...so it's paused for BACKOFF_S against hwnd 201
    assert len(fake.restored) == 1
    clock.t += wt.REPAIR_COOLDOWN_S
    assert keeper.check() == 1                 # window 202 (same exe!) still inside the pause window
    assert len(fake.restored) == 2             # must get its own attempt, not inherit 201's pause


def test_prolonged_contention_saturates_the_backoff_level_without_raising(monkeypatch, wt):
    """Round 2: BACKOFF_S * (2 ** level) computed the cap AFTER
    exponentiating, with level growing unboundedly. At level 1024 (about
    5.7 days of continuous contention) float(2 ** level) raises
    OverflowError; check()'s blanket except swallowed it, so
    self._paused_until was never set and repairs then ran every second
    forever. The level must be clamped before it's used as an exponent."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)   # front="Code.exe" the whole time
    keeper._contend_key = "Code.exe"
    keeper._contend_since = clock.t - 100.0   # well past CONTENTION_AFTER_S
    keeper._contend_level = 1024              # forced very high: simulates days of contention
    assert keeper.check() == 1                # must not raise (and not get swallowed by except)
    assert keeper._paused_until == clock.t + wt.MAX_BACKOFF_S
    assert reports and reports[-1]["held"] is True
    assert keeper._contend_level <= wt._MAX_CONTEND_LEVEL   # stays saturated, doesn't keep climbing


def test_active_resumes_repairs_within_the_active_cap_despite_persistent_demotion(monkeypatch, wt):
    """Finding 1 (Sol pill r4), MAJOR: an unseen demoter repeatedly lowers
    the pill beneath a persistent, innocent editor window and then exits
    while recording continues — the editor stays visible and foreground the
    whole time, so the removed blocker-attribution heuristic kept its pause
    alive against the (innocent, still-visible) editor forever: 479 ticks,
    zero repairs, pill buried, despite a dictation being in progress.
    set_active(True) must cap every pause at ACTIVE_MAX_PAUSE_S regardless
    of attribution, so repair attempts keep resuming on their own and the
    pill ends up on top as soon as the actual demotion stops."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)   # front="Code.exe" (the editor) throughout
    keeper.set_active(True)

    # The demoter keeps lowering the pill behind the editor every tick, well
    # past CONTENTION_AFTER_S, with the editor foreground throughout — the
    # same shape test_delayed_redemotion... exercises for the non-active
    # case in the test below, just longer.
    attempt_times = []
    for _ in range(60):
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        before = len(fake.restored)
        keeper.check()
        if len(fake.restored) > before:
            attempt_times.append(clock.t)
        clock.t += 1.0
    assert len(attempt_times) > 1   # repairs kept resuming, not stuck after the first
    gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
    assert max(gaps) <= wt.ACTIVE_MAX_PAUSE_S   # never waited longer than the active cap

    # The demoter stops — the editor is no longer re-demoting the pill, with
    # Code.exe still foreground the whole time (no foreground change to key
    # a fresh try off of).
    for _ in range(int(wt.ACTIVE_MAX_PAUSE_S) + 1):
        clock.t += 1.0
        keeper.check()
    assert wt.normal_windows_above(PILL) == 0   # the pill ends up on top


def test_the_same_persistent_demotion_follows_ordinary_backoff_when_not_active(monkeypatch, wt):
    """Companion to the test above: outside a dictation (set_active() never
    called, or already False), the same persistent re-demotion must follow
    the plain exponential back-off — no repair storm — rather than being
    capped at ACTIVE_MAX_PAUSE_S, which is only for while a dictation is
    actually in progress."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)   # front="Code.exe" the whole time, not active

    for _ in range(500):
        fake.stack.insert(0, fake.stack.pop(fake._index(2)))
        keeper.check()
        clock.t += 1.0
    assert len(fake.restored) < 20              # well short of fighting every tick
    assert keeper._paused_until - clock.t > wt.ACTIVE_MAX_PAUSE_S   # a real (uncapped) back-off


def test_alternating_windows_never_repair_faster_than_the_cooldown(monkeypatch, wt):
    """Finding 2 (Sol pill r4), MAJOR: two ordinary windows alternating which
    one is in front (and above the pill) every tick each look, to the
    simple "different window in front now, fresh try" reset, like a brand
    new contender — bypassing BACKOFF_S/CONTENTION_AFTER_S entirely (20 Oct
    shape: 120 repairs in 120 ticks). REPAIR_COOLDOWN_S is a flat floor that
    no reset — including this one — can bypass, active or not."""
    for active in (False, True):
        fake = use(monkeypatch, wt, [(2, True, False), (3, True, False), (PILL, True, True)])
        clock, reports = Clock(), []
        toggle = [3]

        def front_hwnd():
            toggle[0] = 3 if toggle[0] == 2 else 2
            return toggle[0]

        keeper = wt.TopmostKeeper(lambda: PILL, reports.append, clock=clock,
                                  front=lambda: "X.exe", front_hwnd=front_hwnd)
        if active:
            keeper.set_active(True)

        which = [2]
        attempt_times = []
        for _ in range(120):
            which[0] = 3 if which[0] == 2 else 2
            fake.stack.insert(0, fake.stack.pop(fake._index(which[0])))  # that one jumps above the pill
            before = len(fake.restored)
            keeper.check()
            if len(fake.restored) > before:
                attempt_times.append(clock.t)
            clock.t += 1.0

        gaps = [b - a for a, b in zip(attempt_times, attempt_times[1:])]
        assert not gaps or min(gaps) >= wt.REPAIR_COOLDOWN_S   # never more than one repair per cooldown
        assert len(attempt_times) <= 120 / wt.REPAIR_COOLDOWN_S + 1


def test_entering_active_makes_one_immediate_attempt(monkeypatch, wt):
    """set_active(True) replaces note_recording_started(): a stale pause
    from whatever was contending before must not make the pill wait — it's
    about to matter again right now, the moment a dictation starts."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)], sticky=True)
    clock, reports = Clock(), []
    keeper = keeper_for(wt, reports, clock)
    keeper.check()                        # trips a pause (sticky never holds)
    assert clock.t < keeper._paused_until
    restored_before = len(fake.restored)

    keeper.set_active(True)
    assert len(fake.restored) > restored_before    # tried again immediately, pause or not
    assert keeper._contend_key is not None          # the fresh attempt re-established tracking
    assert keeper._active is True

    # Already active: a second set_active(True) must not make another
    # immediate attempt (idempotent, no repeated repair storm of its own).
    restored_before = len(fake.restored)
    keeper.set_active(True)
    assert len(fake.restored) == restored_before


def test_entering_active_is_a_no_op_while_the_pill_cannot_be_seen(monkeypatch, wt):
    """check()'s own guards (hwnd() returning None covers hidden, cloaked
    and minimised alike — see ui/overlay.py's _pill_hwnd_for()) still apply
    to the immediate attempt set_active(True) makes, same as they did for
    note_recording_started() before it."""
    reports = []
    keeper = wt.TopmostKeeper(lambda: None, reports.append)
    keeper.set_active(True)
    assert keeper._active is True
    assert reports == []


def test_keeper_leaves_a_hidden_pill_alone(monkeypatch, wt):
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    assert keeper.check() == 0
    assert fake.restored == []


def test_keeper_never_raises(monkeypatch, wt):
    def boom():
        raise RuntimeError("winId gone")
    keeper = wt.TopmostKeeper(boom, lambda info: None)
    assert keeper.check() == 0


def test_check_does_nothing_while_suspended_even_if_demoted(monkeypatch, wt):
    """overlay.py's "hide" handler suspends the keeper alongside stopping
    its timer, but a foreground-change callback already queued (e.g. Qt's
    singleShot, scheduled before suspend() ran) can still fire afterwards —
    it must do nothing, not just skip being scheduled in the first place."""
    fake = use(monkeypatch, wt, [(2, True, False), (PILL, True, True)])
    reports = []
    keeper = keeper_for(wt, reports)
    keeper.suspend()
    assert keeper.check() == 0
    assert fake.restored == []
    assert reports == []


def test_suspend_then_resume_hide_then_show(monkeypatch, wt):
    """hide -> show: suspend() must uninstall the hook, resume() must
    reinstall it — each exactly once."""
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    calls = []
    monkeypatch.setattr(keeper, "uninstall", lambda: calls.append("uninstall"))
    monkeypatch.setattr(keeper, "install_foreground_hook",
                         lambda: calls.append("install") or True)
    keeper.suspend()   # hide
    assert calls == ["uninstall"]
    assert keeper._suspended is True
    keeper.resume()    # show
    assert calls == ["uninstall", "install"]
    assert keeper._suspended is False


def test_hide_show_hide_cycles_suspend_and_resume_exactly_once_each(monkeypatch, wt):
    """hide -> show -> hide must not leave two hooks (or skip suspending
    again): each transition calls exactly one of uninstall()/install."""
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    calls = []
    monkeypatch.setattr(keeper, "uninstall", lambda: calls.append("uninstall"))
    monkeypatch.setattr(keeper, "install_foreground_hook",
                         lambda: calls.append("install") or True)
    keeper.suspend()   # hide
    keeper.resume()    # show
    keeper.suspend()   # hide again
    assert calls == ["uninstall", "install", "uninstall"]
    assert keeper._suspended is True


def test_show_twice_does_not_install_the_hook_twice(monkeypatch, wt):
    """Two "show"s in a row (or a "show" with no prior "hide" — e.g. right
    after the keeper is constructed, when the real hook is already
    installed up front) must not install a second hook."""
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    calls = []
    monkeypatch.setattr(keeper, "install_foreground_hook",
                         lambda: calls.append("install") or True)
    keeper.resume()
    keeper.resume()
    assert calls == []   # never suspended: resume() is a no-op both times


def test_suspend_twice_only_uninstalls_once(monkeypatch, wt):
    """Two "hide"s in a row must not try to uninstall an already-uninstalled
    hook a second time (an overlay restart re-sending "hide" must not either)."""
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    calls = []
    monkeypatch.setattr(keeper, "uninstall", lambda: calls.append("uninstall"))
    keeper.suspend()
    keeper.suspend()
    assert calls == ["uninstall"]


def test_restore_flags_never_activate_move_or_resize(wt):
    assert wt._RESTORE_FLAGS == 0x213   # NOSIZE | NOMOVE | NOACTIVATE | NOOWNERZORDER


@windows_only
def test_real_foreground_hook_installs_and_uninstalls(wt):
    keeper = wt.TopmostKeeper(lambda: None, lambda info: None)
    assert keeper.install_foreground_hook()
    keeper.uninstall()
    assert keeper._hook is None


@windows_only
def test_real_z_order_walk_finds_a_real_window(wt):
    import tkinter as tk

    root = tk.Tk()
    try:
        root.attributes("-topmost", True)
        root.update()
        hwnd = int(root.wm_frame(), 16)
        assert wt.normal_windows_above(hwnd) == 0
    finally:
        root.destroy()


def _z_order_index(wt, hwnd):
    """Position of *hwnd* in the real top-level z-order (0 = top)."""
    h = wt._user32.GetTopWindow(None)
    for i in range(wt._MAX_WALK):
        if not h:
            break
        if h == hwnd:
            return i
        h = wt._user32.GetWindow(h, wt._GW_HWNDNEXT)
    raise AssertionError(f"window {hwnd} not found in the z-order")


@windows_only
def test_a_demoted_pill_is_restored_into_a_real_anchors_band(wt):
    """Finding 3 (Sol pill r4), MAJOR: the previous version of this fixture
    put the pill behind a real ordinary window via SetWindowPos and then
    asserted its OWN WS_EX_TOPMOST flag had survived that — it hadn't:
    Windows clears a window's topmost status as a side effect of landing it
    in the normal band (SetWindowPos remarks), so the old fixture was
    invalid before restore_topmost() was even exercised. Demote with the
    real HWND_NOTOPMOST instead — the documented way to remove topmost
    status, confirmed cleared here rather than assumed — with a legitimate
    topmost anchor still above the pill, and only create the ordinary
    window afterwards so it's genuinely new activity landing above an
    already-demoted pill, not a flag this fixture pretends survived."""
    import tkinter as tk

    _HWND_NOTOPMOST = -2
    anchor = tk.Tk()
    pill = tk.Toplevel(anchor)
    try:
        anchor.attributes("-topmost", True)
        anchor.update()
        pill.attributes("-topmost", True)
        pill.update()

        anchor_hwnd = int(anchor.wm_frame(), 16)
        pill_hwnd = int(pill.wm_frame(), 16)

        assert wt._user32.SetWindowPos(pill_hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, wt._RESTORE_FLAGS)
        assert not wt._user32.GetWindowLongW(pill_hwnd, wt._GWL_EXSTYLE) & wt._WS_EX_TOPMOST

        ordinary = tk.Toplevel(anchor)
        ordinary.update()
        try:
            assert wt.normal_windows_above(pill_hwnd) >= 1

            before_fg = wt._user32.GetForegroundWindow()
            assert wt.restore_topmost(pill_hwnd)

            assert wt.normal_windows_above(pill_hwnd) == 0   # ordering: repaired
            assert wt._user32.GetWindowLongW(pill_hwnd, wt._GWL_EXSTYLE) & wt._WS_EX_TOPMOST  # style: restored
            # anchor: still topmost and still above the pill, not un-topped. Not
            # topmost_above(pill) == anchor: a real desktop has other topmost
            # windows (the taskbar, tray tools), and the pill rightly lands at
            # the bottom of that band, below all of them (node 4, 2026-10-22).
            assert wt._user32.GetWindowLongW(anchor_hwnd, wt._GWL_EXSTYLE) & wt._WS_EX_TOPMOST
            assert _z_order_index(wt, anchor_hwnd) < _z_order_index(wt, pill_hwnd)
            assert wt.topmost_above(pill_hwnd) is not None  # inside the topmost band
            assert wt._user32.GetForegroundWindow() == before_fg  # focus: untouched
        finally:
            ordinary.destroy()
    finally:
        pill.destroy()
        anchor.destroy()


def test_overlay_wires_the_keeper_on_windows_only():
    src = (ROOT / "src" / "wayfinder" / "ui" / "overlay.py").read_text(encoding="utf-8")
    block = src.split('if sys.platform == "win32" and not tray_only:', 1)[1].split(
        "# Send ready signal.", 1)[0]
    assert "TopmostKeeper(" in block
    assert "install_foreground_hook()" in block
    assert "_topmost_timer.start(1000)" in block


def test_overlay_hide_show_suspend_and_resume_the_keeper_via_set_state():
    """Round 2 had the "show"/"hide" command handlers call
    resume()/suspend() directly — keyed on which COMMAND ran, not on where
    overlay._state actually landed, so a transient overlay's "show ready"
    (remapped to HIDDEN by set_state() itself) kept resuming the keeper
    regardless, and an automatic hide (idle watchdog, error auto-return)
    that never goes through these two handlers never suspended it at all
    (round 3). Both handlers must now just call set_state() and let ITS one
    hook (overlay._on_hidden_changed, exercised behaviourally in
    tests/test_overlay.py's test_set_state_notifies_... tests) drive the
    keeper — this test only pins the wiring between the two modules.
    _topmost_keeper/_topmost_timer must still be initialised unconditionally
    (None), so an overlay restart on a path that never builds the real
    keeper (non-Windows, or tray_only) can't NameError."""
    src = (ROOT / "src" / "wayfinder" / "ui" / "overlay.py").read_text(encoding="utf-8")

    init_block = src.split("def run_overlay():", 1)[1].split(
        'if sys.platform == "win32" and not tray_only:', 1)[0]
    assert "_topmost_timer = None" in init_block
    assert "_topmost_keeper = None" in init_block

    show_block = src.split('if command == "show":', 1)[1].split(
        'elif command == "ping":', 1)[0]
    assert "overlay.set_state(state)" in show_block
    assert "_topmost_keeper.resume()" not in show_block
    assert "_topmost_timer.start(1000)" not in show_block

    hide_block = src.split('elif command == "hide":', 1)[1].split(
        'elif command == "level":', 1)[0]
    assert "overlay.set_state(OverlayState.HIDDEN)" in hide_block
    assert "_topmost_keeper.suspend()" not in hide_block
    assert "_topmost_timer.stop()" not in hide_block

    win_block = src.split('if sys.platform == "win32" and not tray_only:', 1)[1].split(
        "# Send ready signal.", 1)[0]
    assert "overlay._on_hidden_changed = _sync_topmost_with_hidden_state" in win_block
    sync_block = win_block.split("def _sync_topmost_with_hidden_state", 1)[1].split(
        "overlay._on_hidden_changed =", 1)[0]
    assert "_topmost_keeper.suspend()" in sync_block
    assert "_topmost_timer.stop()" in sync_block
    assert "_topmost_keeper.resume()" in sync_block
    assert "_topmost_timer.start(1000)" in sync_block
    # The keeper must start life matching whatever overlay._state already
    # is (a transient overlay boots HIDDEN, no set_state() call yet).
    assert 'if overlay._state == OverlayState.HIDDEN:' in win_block


def test_overlay_wires_active_state_through_set_state_not_the_show_handler():
    """Sol pill r4: the "show" command handler used to call
    note_recording_started() itself for state="listening" — keyed on which
    COMMAND ran, the same class of bug test_overlay_hide_show_suspend_and_
    resume_the_keeper_via_set_state above pins for hide/show. set_active()
    must instead hang off overlay._on_active_changed, driven from set_state()
    (exercised behaviourally in tests/test_overlay.py's
    test_set_state_notifies_active_... tests), so every path that changes
    overlay._state — not just a plain "show" — keeps the keeper's active
    flag in sync."""
    src = (ROOT / "src" / "wayfinder" / "ui" / "overlay.py").read_text(encoding="utf-8")

    show_block = src.split('if command == "show":', 1)[1].split(
        'elif command == "ping":', 1)[0]
    assert "overlay.set_state(state)" in show_block
    assert "note_recording_started" not in show_block
    assert "_topmost_keeper.set_active(" not in show_block

    win_block = src.split('if sys.platform == "win32" and not tray_only:', 1)[1].split(
        "# Send ready signal.", 1)[0]
    assert "overlay._on_active_changed = _sync_topmost_active_with_state" in win_block
    sync_block = win_block.split("def _sync_topmost_active_with_state", 1)[1].split(
        "overlay._on_active_changed =", 1)[0]
    assert "_topmost_keeper.set_active(active)" in sync_block


def test_main_logs_the_restore():
    src = (ROOT / "wayfinder_main.py").read_text(encoding="utf-8")
    assert 'data.get("event") == "topmost"' in src
    assert "🫧 Pill was pushed behind" in src
    assert 'data.get("pause_s")' in src and "pausing for a minute" not in src   # Sol pill r5

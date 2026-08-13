"""Browser opening must never claim a success it did not observe.

The shipped regression: 1.1.7 spawned `xdg-open` onto DEVNULL and logged
"Opening in your browser…" the instant `fork` returned. Seventeen clicks on the
Ultra panel produced seventeen success lines and zero tabs, with the opener's
stderr discarded, so nothing on disk said why.

The subtler regression these guard against is the *second* version of that bug:
the portal is a request-reply RPC whose `OpenURI` returns only an
acknowledgement, so treating "the call succeeded" or "the call is still
running" as "the browser opened" reintroduces it one layer down — and since the
portal is tried first, it would do so on essentially every desktop.
"""

import subprocess
import sys
import types
from unittest.mock import patch

import pytest

from wayfinder.utils import open_url as ou

URL = "https://wayfindercollective.io/aura"


class FakeProc:
    """Stands in for Popen: `wait` either returns a code or times out."""

    def __init__(self, code=0, hangs=False, stderr=b""):
        self._code = code
        self._hangs = hangs
        self.returncode = code
        self.stderr = _FakeStream(stderr)

    def wait(self, timeout=None):
        if self._hangs:
            raise subprocess.TimeoutExpired(cmd="opener", timeout=timeout)
        return self._code


class _FakeStream:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, n=-1):
        end = self._pos + n if n >= 0 else len(self._data)
        chunk = self._data[self._pos:end]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    """Pin the candidate list.

    `$BROWSER` decides whether a third exec candidate exists, so leaving it to
    the developer's shell makes every call-count assertion environment-
    dependent. The in-flight guard is module state and leaks across tests.
    """
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.setattr(ou, "host_env", lambda: {"PATH": "/usr/bin"})
    ou._inflight.clear()
    yield
    ou._inflight.clear()


@pytest.fixture
def openers_exist():
    with patch.object(ou.shutil, "which", side_effect=lambda name, path=None: f"/usr/bin/{name}"):
        yield


@pytest.fixture
def no_portal(monkeypatch):
    """Portal unreachable, so the exec chain is what's under test."""
    monkeypatch.setattr(ou, "_portal_open", lambda url: (False, "D-Bus unavailable (ImportError)"))


def _spawns(monkeypatch):
    """Record each spawned argv and drive the fake Popen from a script.

    Exhausting the script is an error rather than an implicit success: a script
    that runs short must fail loudly, not quietly exercise the happy path.
    """
    calls = []
    script = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if not script:
            raise AssertionError(f"unscripted spawn: {cmd}")
        return script.pop(0)

    monkeypatch.setattr(ou.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ou.threading, "Thread", _ImmediateThread)
    return calls, script


class _ImmediateThread:
    """Runs the target inline so stderr drains deterministically in tests."""

    def __init__(self, target=None, args=(), name=None, daemon=None, **kwargs):
        self._target, self._args = target, args

    def start(self):
        if self._target:
            self._target(*self._args)

    def join(self, timeout=None):
        pass


# --- the portal contract ---------------------------------------------------

def test_portal_acknowledgement_is_not_an_open(monkeypatch, openers_exist):
    """The regression this rework exists for.

    `OpenURI` returning a Request handle means the portal accepted the request,
    not that a browser opened. Only a Response of 0 is evidence.
    """
    monkeypatch.setattr(ou, "_portal_open", lambda url: (False, "no Response within 20s"))
    calls, script = _spawns(monkeypatch)
    script.append(FakeProc(0))

    result = ou.open_url(URL)

    assert result.opener != "portal"
    assert calls, "an unanswered portal must fall through, not report success"


def test_portal_response_zero_is_success(monkeypatch):
    monkeypatch.setattr(ou, "_portal_open", lambda url: (True, "portal confirmed the open"))

    result = ou.open_url(URL)

    assert result.ok is True
    assert result.opener == "portal"


def test_portal_is_tried_before_any_exec_opener(monkeypatch, openers_exist):
    order = []
    monkeypatch.setattr(ou, "_portal_open", lambda url: (order.append("portal"), (False, "x"))[1])
    calls, script = _spawns(monkeypatch)
    script.append(FakeProc(0))

    ou.open_url(URL)

    assert order == ["portal"]
    assert calls[0][0][0] == "gio"


def test_user_cancelling_the_chooser_stops_the_chain(monkeypatch, openers_exist):
    """Opening it anyway, behind their back, is worse than doing nothing."""
    monkeypatch.setattr(ou, "_portal_open", lambda url: (False, "cancelled in the app chooser"))
    calls, _script = _spawns(monkeypatch)

    result = ou.open_url(URL)

    assert result.ok is False
    assert not calls, "a declined open must not be retried behind the user's back"


# --- _portal_open against a fake bus ---------------------------------------
#
# Every other test here monkeypatches _portal_open away; these run it for real
# so a Gio signature or protocol-ordering error cannot ship unnoticed.

class _Reply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeMainContext:
    def push_thread_default(self):
        pass

    def pop_thread_default(self):
        pass


class _FakeTimer:
    def set_callback(self, cb):
        pass

    def attach(self, context):
        pass

    def destroy(self):
        pass


class _FakeBus:
    """A session bus that speaks exactly the portal's request-reply shape.

    ``answers`` controls whether a Response ever arrives; ``mismatch`` makes
    ``OpenURI`` mint its own handle instead of honouring ``handle_token``, the
    way pre-0.9 portals do.

    ``call_sync`` asserts the full OpenURI wire contract (destination, path,
    interface, tuple format, plain-dict ``a{sv}``, reply type) so structural
    drift fails here even where PyGObject is absent.  Like GDBus, a Response
    is matched against subscriptions at RECEIVE time — one that arrives with
    no matching subscription is dropped, which is what makes the
    handle-mismatch race reproducible.
    """

    def __init__(self, response_code=0, answers=True, mismatch=False):
        self._response_code = response_code
        self._answers = answers
        self._mismatch = mismatch
        self.subscriptions = {}
        self.events = []
        self._next_id = 1
        self._pending = None

    def get_unique_name(self):
        return ":1.42"

    def signal_subscribe(self, sender, iface, signal, path, arg0, flags, cb):
        sid = self._next_id
        self._next_id += 1
        self.subscriptions[sid] = (sender, path, cb)
        self.events.append(("subscribe", sender, path))
        return sid

    def signal_unsubscribe(self, sid):
        self.subscriptions.pop(sid, None)
        self.events.append(("unsubscribe", sid))

    def _matches(self, path):
        return any(sp is None or sp == path for _s, sp, _cb in self.subscriptions.values())

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        assert dest == "org.freedesktop.portal.Desktop"
        assert path == "/org/freedesktop/portal/desktop"
        assert iface == "org.freedesktop.portal.OpenURI"
        assert method == "OpenURI"
        rt = reply_type if isinstance(reply_type, str) else reply_type.dup_string()
        assert rt == "(o)"
        if hasattr(params, "get_type_string"):  # real GLib.Variant
            assert params.get_type_string() == "(ssa{sv})"
            body = params.unpack()
            token = body[2]["handle_token"]
        else:
            fmt, body = params
            assert fmt == "(ssa{sv})"
            assert isinstance(body[2], dict), \
                "a{sv} takes a plain dict of Variants; a pre-wrapped Variant raises in PyGObject"
            token = body[2]["handle_token"][1]
        assert isinstance(token, str) and token
        sender = self.get_unique_name().lstrip(":").replace(".", "_")
        predicted = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        actual = predicted + "_minted" if self._mismatch else predicted
        self.events.append(("call", method))
        # Receive-time matching, like GDBus: a Response nothing is subscribed
        # to at this moment never reaches a callback.
        if self._answers and self._matches(actual):
            self._pending = (actual, self._response_code)
        return _Reply((actual,))

    def dispatch(self):
        """What loop.run() does: deliver the pending Response to subscribers."""
        if self._pending is None:
            return
        path, code = self._pending
        for sender, spath, cb in list(self.subscriptions.values()):
            if spath is None or spath == path:
                cb(None, sender, path, "org.freedesktop.portal.Request", "Response",
                   _Reply((code, {})))

    def close_sync(self, cancellable):
        self.events.append(("close",))


class _FakeLoop:
    def __init__(self, bus):
        self._bus = bus

    def run(self):
        # A matching Response quits via the callback; no match models the
        # timeout timer firing — either way run() returns.
        self._bus.dispatch()

    def quit(self):
        pass


def _install_fake_gi(monkeypatch, bus, real_glib=None):
    """Wire the fakes; with ``real_glib`` the variants are built by the real
    GLib machinery while the connection and main loop stay fake."""
    glib = types.SimpleNamespace(
        MainContext=types.SimpleNamespace(new=_FakeMainContext),
        MainLoop=types.SimpleNamespace(new=lambda ctx, running: _FakeLoop(bus)),
        Variant=real_glib.Variant if real_glib else (lambda fmt, value: (fmt, value)),
        VariantType=real_glib.VariantType if real_glib else (lambda s: s),
        timeout_source_new_seconds=lambda s: _FakeTimer(),
        SOURCE_REMOVE=False,
    )
    gio = types.SimpleNamespace(
        BusType=types.SimpleNamespace(SESSION=2),
        DBusConnectionFlags=types.SimpleNamespace(
            AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2
        ),
        DBusCallFlags=types.SimpleNamespace(NONE=0),
        DBusSignalFlags=types.SimpleNamespace(NONE=0),
        dbus_address_get_for_bus_sync=lambda bus_type, cancellable: "unix:fake",
        DBusConnection=types.SimpleNamespace(new_for_address_sync=lambda *a, **k: bus),
    )
    repo = types.ModuleType("gi.repository")
    repo.Gio, repo.GLib = gio, glib
    gi = types.ModuleType("gi")
    gi.repository = repo
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repo)
    return bus


def test_portal_subscribes_before_calling(monkeypatch):
    """The portal may answer before OpenURI returns; a subscription set up
    afterwards silently misses that response."""
    bus = _install_fake_gi(monkeypatch, _FakeBus(response_code=0))

    ou._portal_open(URL)

    kinds = [e[0] for e in bus.events]
    assert kinds.index("subscribe") < kinds.index("call")


def test_portal_pins_the_response_sender(monkeypatch):
    """An unpinned subscription lets any connection forge the verdict."""
    bus = _install_fake_gi(monkeypatch, _FakeBus(response_code=0))

    ou._portal_open(URL)

    senders = [e[1] for e in bus.events if e[0] == "subscribe"]
    assert senders and all(s == "org.freedesktop.portal.Desktop" for s in senders)


@pytest.mark.parametrize("code,ok,fragment", [
    (0, True, "portal confirmed the open"),
    (1, False, "cancelled in the app chooser"),
    (2, False, "Response code 2"),
])
def test_portal_response_codes(code, ok, fragment, monkeypatch):
    _install_fake_gi(monkeypatch, _FakeBus(response_code=code))

    got_ok, detail = ou._portal_open(URL)

    assert got_ok is ok
    assert fragment in detail


def test_portal_no_response_is_a_failure(monkeypatch):
    """OpenURI acknowledged, Response never came: the original bug's shape."""
    _install_fake_gi(monkeypatch, _FakeBus(answers=False))

    got_ok, detail = ou._portal_open(URL)

    assert got_ok is False
    assert "no Response" in detail


def test_portal_follows_a_mismatched_handle(monkeypatch):
    """Pre-0.9 portals mint their own handle; the spec says follow it.

    The fake matches at receive time, like GDBus, and the minted handle's
    Response arrives while call_sync is still returning — so anything pinned
    to the predicted path drops it, times out, and would send the exec chain
    after a URL the portal already opened. Only an all-paths subscription that
    exists before the call survives this.
    """
    _install_fake_gi(monkeypatch, _FakeBus(response_code=0, mismatch=True))

    got_ok, detail = ou._portal_open(URL)

    assert got_ok is True, detail


def test_portal_builds_variants_the_real_glib_accepts(monkeypatch):
    """Runs _portal_open with the REAL GLib variant machinery (fake bus/loop).

    The field bug this guards: a pre-wrapped Variant in the a{sv} slot made
    PyGObject raise KeyError — a signature error no tuple-based fake can see.
    Skips where PyGObject is not installed (the uv test env); runs on the
    distrobox, the host, and the Flatpak SDK.
    """
    real_glib = pytest.importorskip("gi.repository.GLib")
    _install_fake_gi(monkeypatch, _FakeBus(response_code=0), real_glib=real_glib)

    got_ok, detail = ou._portal_open(URL)

    assert got_ok is True, detail


def test_portal_unique_tokens_across_calls(monkeypatch):
    """The spec asks for unique, unguessable tokens; pid+thread-id repeats
    across sequential worker threads."""
    seen = set()

    class TokenBus(_FakeBus):
        def call_sync(self, *args, **kwargs):
            token = args[4][1][2]["handle_token"][1]
            seen.add(token)
            return super().call_sync(*args, **kwargs)

    bus = TokenBus(response_code=0)
    _install_fake_gi(monkeypatch, bus)

    ou._portal_open(URL)
    ou._portal_open(URL)

    assert len(seen) == 2


# --- exec opener contract --------------------------------------------------

def test_exec_opener_still_running_is_a_handoff(monkeypatch, openers_exist, no_portal):
    """A $BROWSER that execs a foreground browser blocks for its whole life.

    This heuristic is valid *only* for exec openers, which is why the portal
    never reaches this code path.
    """
    _calls, script = _spawns(monkeypatch)
    script.append(FakeProc(hangs=True))

    result = ou.open_url(URL)

    assert result.ok is True
    assert "still running" in result.detail


def test_nonzero_exit_is_not_a_success(monkeypatch, openers_exist, no_portal):
    _calls, script = _spawns(monkeypatch)
    script.extend([FakeProc(3), FakeProc(3)])

    result = ou.open_url(URL)

    assert result.ok is False
    assert result.attempts, "a failure with no evidence is the original bug"


def test_failure_detail_comes_from_stderr(monkeypatch, openers_exist, no_portal):
    _calls, script = _spawns(monkeypatch)
    script.extend([FakeProc(4, stderr=b"gio: no handler\n"), FakeProc(4)])

    result = ou.open_url(URL)

    assert any("no handler" in a for a in result.attempts)


def test_first_failure_falls_through_to_the_next_opener(monkeypatch, openers_exist, no_portal):
    calls, script = _spawns(monkeypatch)
    script.extend([FakeProc(1), FakeProc(0)])

    result = ou.open_url(URL)

    assert result.ok is True
    assert [c[0][0] for c in calls] == ["gio", "xdg-open"]


def test_exec_success_is_not_confirmed(monkeypatch, openers_exist, no_portal):
    """Only the portal's Response is a verdict; an exec opener exiting 0 is a
    hand-off, and the UI must not report it as an observed open."""
    _calls, script = _spawns(monkeypatch)
    script.append(FakeProc(0))

    result = ou.open_url(URL)

    assert result.ok is True
    assert result.confirmed is False


def test_portal_success_is_confirmed(monkeypatch):
    monkeypatch.setattr(ou, "_portal_open", lambda url: (True, "portal confirmed the open"))

    result = ou.open_url(URL)

    assert result.confirmed is True


def test_stderr_is_bounded_by_bytes_not_lines():
    """One never-terminated line must not grow the buffer without limit."""
    sink = bytearray()

    ou._drain(_FakeStream(b"x" * (ou._STDERR_TAIL_BYTES * 3)), sink)

    assert len(sink) <= ou._STDERR_TAIL_BYTES


def test_openers_run_detached_and_keep_stderr(monkeypatch, openers_exist, no_portal):
    calls, script = _spawns(monkeypatch)
    script.append(FakeProc(0))

    ou.open_url(URL)

    _cmd, kwargs = calls[0]
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] is subprocess.PIPE, "DEVNULL here is what hid the bug"


def test_host_env_is_used_for_spawned_openers(monkeypatch, openers_exist, no_portal):
    calls, script = _spawns(monkeypatch)
    script.append(FakeProc(0))
    monkeypatch.setattr(ou, "host_env", lambda: {"PATH": "/usr/bin", "MARK": "1"})

    ou.open_url(URL)

    assert calls[0][1]["env"]["MARK"] == "1"


# --- $BROWSER handling -----------------------------------------------------

def test_browser_is_resolved_against_the_scrubbed_path(monkeypatch):
    """Resolving against the inherited PATH could select a bundled binary,
    which hostexec is explicit must never run with host_env."""
    seen = {}

    def fake_which(name, path=None):
        seen["path"] = path
        return "/usr/bin/brave"

    monkeypatch.setenv("BROWSER", "brave")
    monkeypatch.setattr(ou.shutil, "which", fake_which)

    ou._browser_command(URL, {"PATH": "/scrubbed/bin"})

    assert seen["path"] == "/scrubbed/bin"


@pytest.mark.parametrize("value", ["/usr/bin/firefox:/usr/bin/brave", "mybrowser %s"])
def test_ambiguous_browser_forms_are_declined(value, monkeypatch):
    monkeypatch.setenv("BROWSER", value)
    assert ou._browser_command(URL, {"PATH": "/usr/bin"}) is None


# --- input safety ----------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://example.com",
    "https://",
    "",
    # Truthy netloc, no host — checking netloc instead of hostname passes these.
    "https://@/path",
    "https://:80/path",
    # urlsplit parses lazily; a garbage port only raises when .port is read.
    "https://example.com:notaport/x",
    # Control characters / whitespace in the authority are never legitimate.
    "https://exa mple.com/x",
    "https://example.com\x00evil/x",
    # urlsplit strips \t\r\n BEFORE parsing (WHATWG), so these parse to a
    # clean netloc — but the unstripped original is what reaches the opener.
    "https://exa\nmple.com/x",
    "https://exa\tmple.com/x",
    "https://example.com/\rx",
])
def test_non_http_urls_are_refused(url, monkeypatch):
    called = []
    monkeypatch.setattr(ou.subprocess, "Popen", lambda *a, **k: called.append(a))
    monkeypatch.setattr(ou, "_portal_open", lambda u: (True, "should not be reached"))

    result = ou.open_url(url)

    assert result.ok is False
    assert result.detail, "a refusal with no reason is undiagnosable"
    assert not called


@pytest.mark.parametrize("url", [
    "https://wayfindercollective.io/aura",
    "HTTPS://wayfindercollective.io/aura",
    "http://localhost:8080/x",
    "https://user:secret@example.com/x",
])
def test_valid_http_urls_are_accepted(url, monkeypatch):
    """Schemes are case-insensitive per RFC 3986 — an uppercase one in a saved
    config must not silently kill the Buy Now button."""
    monkeypatch.setattr(ou, "_portal_open", lambda u: (True, "portal confirmed the open"))

    assert ou.open_url(url).ok is True


# --- concurrency + reporting ----------------------------------------------

def test_repeat_clicks_do_not_stack_openers(monkeypatch):
    """Five impatient clicks must not become five tabs."""
    started = []

    def slow_portal(url):
        started.append(url)
        assert URL in ou._inflight
        return True, "portal confirmed the open"

    monkeypatch.setattr(ou, "_portal_open", slow_portal)
    ou._inflight.add(URL)

    result = ou.open_url(URL)

    assert result.ok is False
    assert not started


def test_async_reports_failure_and_never_raises_into_caller(monkeypatch):
    monkeypatch.setattr(ou, "open_url", lambda url: (_ for _ in ()).throw(RuntimeError("boom")))
    seen = []

    ou.open_url_async(URL, seen.append).join(timeout=5)

    assert len(seen) == 1
    assert seen[0].ok is False
    assert "RuntimeError" in seen[0].detail


def test_callback_errors_are_logged_not_swallowed(monkeypatch):
    """Silently eating a reporting bug is the exact failure mode of the module
    whose whole thesis is that evidence must survive."""
    monkeypatch.setattr(ou, "_portal_open", lambda u: (True, "portal confirmed the open"))
    with patch.object(ou.log, "exception") as logged:
        ou.open_url_async(URL, lambda r: 1 / 0).join(timeout=5)

    assert logged.called


def test_describe_failure_surfaces_detail_when_there_are_no_attempts(monkeypatch):
    """A refused URL has an empty attempts list; its reason lives in detail."""
    result = ou.OpenResult(False, detail="refusing non-http(s) URL: 'file:///x'")

    lines = ou.describe_failure("file:///x", result)

    assert any("refusing non-http(s)" in line for line in lines)

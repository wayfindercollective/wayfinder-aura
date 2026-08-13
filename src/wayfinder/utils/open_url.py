"""Open a URL in the user's browser — and only claim what we actually observed.

The bug this exists to kill: the shipped opener spawned ``xdg-open``
fire-and-forget onto ``DEVNULL`` and logged success the instant ``fork``
returned.  A browser that never launched was indistinguishable from one that
did — the 1.1.7 AppImage wrote "Opening in your browser…" seventeen times for
seventeen clicks that opened nothing, and discarded the stderr that would have
said why.

The rule that follows is narrower than "check the exit code", because *what
counts as evidence differs by opener*:

* The **portal** is a request-reply RPC.  ``OpenURI`` returns a ``Request``
  handle immediately and delivers the real outcome later on that request's
  ``Response`` signal.  An acknowledgement is not an open — treating one as
  success is the original bug wearing a different hat, which is why this module
  subscribes to ``Response`` and waits for it.  ``gdbus call`` cannot do that
  (it prints the handle and exits 0), so the portal is spoken in-process over
  D-Bus.  That is also why it is safe from the problem that motivated using it:
  a D-Bus call is socket I/O, so unlike a spawned host binary it cannot be
  killed by the bundle's libraries leaking into a child.
* An **exec opener** (``xdg-open``, ``gio``, ``$BROWSER``) may legitimately
  block for the browser's entire lifetime.  For these, and only these, "still
  running after ``SETTLE_SECONDS``" is a genuine hand-off.  Applying that
  heuristic to a request-reply call would turn a wedged portal into a reported
  success — so the two kinds never share a code path.

If the portal is unreachable we skip it entirely rather than fall back to an
unverified variant: an unconfirmed request that later succeeds would open a
second tab once the next candidate ran.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .hostexec import host_env
from .logging import get_logger

log = get_logger(__name__)

# How long an exec opener may run before we read "still alive" as a hand-off.
# Long enough to catch fast failures (no handler, broken $BROWSER wrapper,
# library mismatch), short enough that a dead chain still reaches the next
# candidate while the user is looking at the panel.
SETTLE_SECONDS = 2.5

# The portal answers promptly or not at all; this only has to outlast a chooser
# dialog that the user is not interacting with.
PORTAL_TIMEOUT_SECONDS = 20

# Keep the tail of an opener's stderr for diagnostics without letting a browser
# that chatters for hours grow anything unbounded. Bounded in bytes, not lines:
# a single never-terminated line would grow a line-based buffer without limit.
_STDERR_TAIL_BYTES = 8192

_PORTAL_DEST = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_PORTAL_IFACE = "org.freedesktop.portal.OpenURI"

# org.freedesktop.portal.Request.Response codes.
_PORTAL_OK = 0
_PORTAL_CANCELLED = 1

_KIND_EXEC = "exec"

# Serialises repeat clicks: without it, five impatient clicks on Buy Now spawn
# five chains and up to five tabs.
_inflight_lock = threading.Lock()
_inflight: set[str] = set()


@dataclass(frozen=True)
class OpenResult:
    """Outcome of an open attempt, carrying the evidence that produced it.

    ``ok`` means the URL was handed to something that accepted it.
    ``confirmed`` means the portal's ``Response`` positively reported the open —
    the only evidence in the chain that is a verdict rather than a hand-off.
    Callers must word the two differently: claiming "opened" on a hand-off is a
    milder cousin of the bug this module exists to kill.
    """

    ok: bool
    opener: str = ""
    detail: str = ""
    attempts: list[str] = field(default_factory=list)
    confirmed: bool = False


def _normalised_url(url: str) -> str | None:
    """The URL if it is a well-formed http(s) URL, else None.

    Openers hand arbitrary schemes to arbitrary handlers, so ``file://`` and
    ``javascript:`` must never reach one.  Schemes are case-insensitive per
    RFC 3986 — a config holding ``HTTPS://…`` is valid and must not be refused.

    ``hostname``, not ``netloc``: ``https://@/path`` has a truthy netloc and no
    host at all.  ``parts.port`` is touched deliberately — urlsplit parses
    lazily and a garbage port only raises when it is read.
    """
    # Control characters are checked on the RAW string: urlsplit strips
    # \t\r\n before parsing (WHATWG), so "https://exa\nmple.com" would
    # otherwise sail through with a clean netloc — and the unstripped original
    # is what gets handed to the opener.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not hostname:
        return None
    # A space in the authority is never legitimate either.
    if " " in parts.netloc:
        return None
    return url


# --- the portal: request-reply, and we wait for the reply -------------------

def _portal_open(url: str) -> tuple[bool, str]:
    """Ask the desktop portal to open ``url`` and wait for its verdict.

    Subscribes to the request's ``Response`` before issuing the call — the
    portal may answer before ``OpenURI`` even returns, and a subscription set up
    afterwards would miss it.  Uses a private connection on a thread-default
    context so signal dispatch happens here rather than on whichever main
    context happens to own the shared session bus.
    """
    try:
        from gi.repository import Gio, GLib
    except Exception as exc:
        return False, f"D-Bus unavailable ({type(exc).__name__})"

    context = GLib.MainContext.new()
    context.push_thread_default()
    bus = None
    subscription = None
    try:
        address = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION, None)
        bus = Gio.DBusConnection.new_for_address_sync(
            address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

        # The request object path is derived from our unique name and the token
        # we choose, so it is knowable before the call is made. The spec asks
        # for unique, unguessable tokens — pid+thread-id is neither, because
        # thread ids are reused across sequential worker threads.
        unique = bus.get_unique_name() or ""
        token = f"wayfinder_{secrets.token_hex(16)}"
        sender = unique.lstrip(":").replace(".", "_")
        request_path = f"{_PORTAL_PATH}/request/{sender}/{token}"

        responses: dict[str, int] = {}
        target = {"path": request_path}
        loop = GLib.MainLoop.new(context, False)

        def _on_response(_conn, _sender, path, _iface, _signal, params):
            try:
                responses[path] = params.unpack()[0]
            except Exception:
                responses[path] = -1
            if path == target["path"]:
                loop.quit()

        # One subscription, every path, filtered in the handler. GDBus matches
        # signals against subscriptions at RECEIVE time, so a path-pinned
        # subscription cannot be moved safely: pre-0.9 portals ignore
        # handle_token and mint their own request path, and their Response can
        # arrive while call_sync is still returning that path to us — matched
        # by nothing, silently dropped, and the 20s stall then sends the exec
        # chain after a URL the portal already opened. The connection is
        # private and the sender is pinned to the portal's well-known name
        # (GDBus resolves it to the owning unique name), so this sees only
        # portal Responses addressed to us and nothing can forge one.
        subscription = bus.signal_subscribe(
            _PORTAL_DEST,
            "org.freedesktop.portal.Request",
            "Response",
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_response,
        )

        # The `a{sv}` member of the tuple format takes a plain dict of Variants;
        # handing it a pre-wrapped Variant makes PyGObject try to iterate it as
        # a mapping and raise KeyError.
        options = {"handle_token": GLib.Variant("s", token)}
        reply = bus.call_sync(
            _PORTAL_DEST,
            _PORTAL_PATH,
            _PORTAL_IFACE,
            "OpenURI",
            GLib.Variant("(ssa{sv})", ("", url, options)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            PORTAL_TIMEOUT_SECONDS * 1000,
            None,
        )

        # Follow the returned handle when it differs from the prediction — the
        # handler is already watching every path, so there is no window in
        # which the minted handle's Response can be missed.
        try:
            actual_path = reply.unpack()[0] or request_path
        except Exception:
            actual_path = request_path
        target["path"] = actual_path

        timer = GLib.timeout_source_new_seconds(PORTAL_TIMEOUT_SECONDS)
        timer.set_callback(lambda *_: (loop.quit(), GLib.SOURCE_REMOVE)[1])
        timer.attach(context)
        try:
            loop.run()
        finally:
            timer.destroy()

        if actual_path not in responses:
            return False, f"no Response within {PORTAL_TIMEOUT_SECONDS}s"
        code = responses[actual_path]
        if code == _PORTAL_OK:
            return True, "portal confirmed the open"
        if code == _PORTAL_CANCELLED:
            return False, "cancelled in the app chooser"
        return False, f"portal declined (Response code {code})"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if bus is not None and subscription is not None:
                bus.signal_unsubscribe(subscription)
            if bus is not None:
                bus.close_sync(None)
        except Exception:
            pass
        context.pop_thread_default()


# --- exec openers: spawn, watch briefly, keep the stderr --------------------

def _drain(stream, sink: bytearray) -> None:
    """Consume an opener's stderr so it can never block on a full pipe.

    Bounded to the last ``_STDERR_TAIL_BYTES`` bytes: a browser that logs GTK
    warnings for hours — or emits one never-terminated line — costs us the
    oldest bytes, not a growing buffer.
    """
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            sink += chunk
            del sink[:-_STDERR_TAIL_BYTES]
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _spawn_and_settle(cmd: list[str], env: dict) -> tuple[bool, str]:
    """Run an exec opener detached and report whether it took the URL.

    ``start_new_session`` puts it in its own session, so the browser outlives
    Aura and never inherits Aura's controlling terminal or process group.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return False, "not installed"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    tail = bytearray()
    reader = threading.Thread(
        target=_drain, args=(proc.stderr, tail), name="wayfinder-opener-stderr", daemon=True
    )
    reader.start()

    try:
        code = proc.wait(timeout=SETTLE_SECONDS)
    except subprocess.TimeoutExpired:
        # A foreground browser is holding the opener open. Reap it later so it
        # cannot linger as a zombie for the life of the app.
        threading.Thread(
            target=_reap, args=(proc,), name="wayfinder-opener-reap", daemon=True
        ).start()
        return True, "handed off (opener still running)"

    reader.join(timeout=1.0)
    if code == 0:
        return True, "exited 0"
    lines = tail.decode("utf-8", "replace").strip().splitlines()
    return False, lines[-1] if lines else f"exit {code}"


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    except Exception:
        pass


def _browser_command(url: str, env: dict) -> list[str] | None:
    """``$BROWSER`` as an argv, when it names one runnable executable.

    Resolved against the *scrubbed* PATH like every other candidate: the
    inherited PATH still has the bundle prepended, so resolving against it could
    select a bundled binary — which ``hostexec`` is explicit must never be run
    with ``host_env``.  The XDG spec also allows a colon-separated list and a
    ``%s`` placeholder; both are rare and ambiguous enough to decline.
    """
    raw = (os.environ.get("BROWSER") or "").strip()
    if not raw or os.pathsep in raw or "%s" in raw:
        return None
    resolved = shutil.which(raw, path=env.get("PATH"))
    if not resolved and os.path.isabs(raw) and os.access(raw, os.X_OK):
        resolved = raw
    return [resolved, url] if resolved else None


def _exec_candidates(url: str, env: dict) -> Iterator[tuple[str, list[str]]]:
    """Exec openers to try, best first, once the portal is out of the picture."""
    yield ("gio", ["gio", "open", url])
    yield ("xdg-open", ["xdg-open", url])
    browser = _browser_command(url, env)
    if browser:
        yield ("$BROWSER", browser)


# --- public API ------------------------------------------------------------

def open_url(url: str) -> OpenResult:
    """Try the portal, then the exec openers; return the first verified success.

    Blocking — call from a worker thread, or use ``open_url_async``.
    """
    safe = _normalised_url(url)
    if safe is None:
        return OpenResult(False, detail=f"refusing non-http(s) URL: {url!r}")

    with _inflight_lock:
        if safe in _inflight:
            return OpenResult(False, detail="already opening this link")
        _inflight.add(safe)
    try:
        return _run_chain(safe)
    finally:
        with _inflight_lock:
            _inflight.discard(safe)


def _run_chain(url: str) -> OpenResult:
    env = host_env()
    attempts: list[str] = []

    ok, detail = _portal_open(url)
    attempts.append(f"portal: {detail}")
    if ok:
        return OpenResult(True, "portal", detail, attempts, confirmed=True)
    if detail == "cancelled in the app chooser":
        # The user declined. Opening it behind their back would be worse than
        # doing nothing, so the chain stops here.
        return OpenResult(False, "portal", detail, attempts)

    for name, cmd in _exec_candidates(url, env):
        if not os.path.isabs(cmd[0]) and not shutil.which(cmd[0], path=env.get("PATH")):
            attempts.append(f"{name}: not found")
            continue
        ok, detail = _spawn_and_settle(cmd, env)
        attempts.append(f"{name}: {detail}")
        if ok:
            return OpenResult(True, name, detail, attempts)

    return OpenResult(False, detail="every opener failed", attempts=attempts)


def open_url_async(url: str, on_result: Callable[[OpenResult], None]) -> threading.Thread:
    """Run ``open_url`` off the calling thread and hand the result to ``on_result``.

    ``on_result`` fires on the worker thread. Callers that touch Tk widgets must
    marshal it themselves; ``WayfinderApp.log`` is already thread-safe via the
    event queue, so logging from the callback needs no hop.
    """
    def _run() -> None:
        try:
            result = open_url(url)
        except Exception as exc:
            log.exception("open_url raised for %s", url)
            result = OpenResult(False, detail=f"{type(exc).__name__}: {exc}")
        try:
            on_result(result)
        except Exception:
            # Never let a reporting bug vanish — that is the failure this
            # module exists to make impossible.
            log.exception("open_url result callback failed for %s", url)

    thread = threading.Thread(target=_run, name="wayfinder-open-url", daemon=True)
    thread.start()
    return thread


def describe_failure(url: str, result: OpenResult) -> list[str]:
    """Log lines explaining a failed open, newest evidence included.

    Lives here rather than in the app so the second caller that needs to open a
    URL does not re-derive the reporting (CLAUDE.md rule 4).
    """
    lines = [f"⚠ Couldn't open {url}"]
    if result.detail:
        lines.append(f"   {result.detail}")
    lines.extend(f"   • {attempt}" for attempt in result.attempts)
    return lines

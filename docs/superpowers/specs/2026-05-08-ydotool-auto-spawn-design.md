# Design — ydotool auto-spawn

**Status:** approved in chat 2026-05-08, pending Peter's written-spec review
**Author:** Claude (with Peter)
**Scope:** `wayfinder-aura` Linux text-injection path. macOS path untouched.

## Motivation

Today the app relies on the user having a `ydotoold` daemon already running via a system or user systemd service. The unit name and scope vary by distro:

- Arch / Manjaro / SteamOS: `ydotool.service` (user-mode)
- Fedora / Bazzite / Silverblue: `ydotoold.service` (system-mode)
- Debian / Ubuntu: varies; some packages ship no unit at all

The app's current error message at `src/wayfinder/core/injector.py:88` hardcodes the Fedora command (`sudo systemctl enable --now ydotoold`), which fails on Arch/SteamOS with `Unit ydotoold.service does not exist`. This was logged as Issue 4 in `STEAMDECK-INSTALL-LOG.md` and prioritized for v2 as "auto-spawn ydotoold instead of relying on user/system service" (line 480, ★★).

The fix: the app spawns its own private `ydotoold` at startup with a known socket path. Per-distro service-naming chaos is sidestepped entirely. This is a Linux-general improvement — not Steam Deck specific. Steam Deck is just where the bug bites hardest.

## Non-goals

- Bundling `ydotool` in the Flatpak manifest (requires Flathub policy work on uinput access; deferred to a separate change).
- Bundling a `ydotoold` binary in source/dev installs. We rely on system-installed `ydotoold` from PATH, plus the existing `$APPDIR/usr/bin/ydotool` lookup for AppImage builds.
- Changing the macOS injection path (clipboard paste via `_inject_text_pyautogui`).
- Adding a setup-time installer that runs `gpasswd`/`pacman` on the user's behalf. The app surfaces a clear error when uinput access is missing; the user fixes their system.

## Architecture

A new module `src/wayfinder/core/ydotool_daemon.py` owns the lifecycle of an app-private `ydotoold` child process. It mirrors the existing precedent in `src/wayfinder/core/transcriber.py:WhisperServerBackend` (long-lived child, atexit cleanup, signal-driven shutdown).

**Public API (module-level — module is the singleton):**

```
ensure_running()  -> str | None    # spawn if not already; return socket path on success, None on failure
socket_path()     -> str | None    # current socket path, or None
last_error()      -> str | None    # most recent failure reason for surfacing in setup UI / overlay
shutdown()        -> None          # SIGTERM, then SIGKILL on timeout; unlink socket
```

**Touch points (everything else stays put):**

- `src/wayfinder/core/setup.py:check_text_injection` calls `ydotool_daemon.ensure_running()` during startup preflight. If it returns `None`, surface `last_error()` as a non-fatal warning in the setup wizard / overlay. App startup never blocks.
- `src/wayfinder/core/injector.py` is simplified: drops `_get_ydotool_env()`'s 3-path socket search and the wrong "sudo systemctl enable ydotoold" hint. `inject_text()` reads `ydotool_daemon.socket_path()` and sets `YDOTOOL_SOCKET` from that. `check_ydotool_ready()` defers its socket question to the daemon module.

## Module internals

### State (module-level)

```
_proc:              subprocess.Popen | None
_socket_path:       str | None
_last_error:        str | None
_lock:              threading.Lock         # serializes ensure_running / shutdown
_atexit_registered: bool
```

`_lock` makes `ensure_running()` thread-safe — `inject_text()` runs on the transcription worker thread; startup spawns from main thread. Without a lock they could race-spawn.

### Socket path policy

Primary: `${XDG_RUNTIME_DIR}/wayfinder-aura/ydotool.sock`

- `os.makedirs(parent, mode=0o700, exist_ok=True)`
- ydotoold creates the socket with mode `0o600` (we pass `-P 0600`)
- Per-user, per-session, auto-cleaned at logout

Fallback if `XDG_RUNTIME_DIR` is unset (rare — broken PAM session, some SSH-without-systemd-pam cases): `${TMPDIR:-/tmp}/wayfinder-aura-${UID}/ydotool.sock`, same `0o700`/`0o600` perms.

### Spawn sequence (`ensure_running()`)

Acquire `_lock`. Then:

1. If `_proc is not None and _proc.poll() is None` → return `_socket_path` (idempotent fast path).
2. Resolve `ydotoold_bin` via `shutil.which("ydotoold")` (and existing `$APPDIR/usr/bin/ydotoold` lookup for AppImage). If not found → set `_last_error` to install hint (see Error handling §1) and return `None`.
3. Compute `_socket_path` per the policy above. `os.makedirs` parent. `os.unlink(_socket_path)` ignoring `FileNotFoundError` (clear stale socket from prior crash).
4. `Popen([ydotoold_bin, "-p", _socket_path, "-P", "0600"], stdout=PIPE, stderr=PIPE, close_fds=True)`.
5. Stepped poll for socket appearance: delays `[1, 2, 5, 10, 20, 50, 50, 100, 100, 200] ms` (~540 ms cumulative cap). Between each delay:
   - If `_proc.poll() is not None`: child exited early. Drain stderr non-blockingly (`_proc.communicate(timeout=0.05)`), classify error (see §2), set `_last_error`, return `None`.
   - If `Path(_socket_path).is_socket()`: success. Break.
6. If poll loop exhausted (~540 ms) with daemon still alive → set `_last_error` per §3, send SIGTERM, return `None`.
7. Register `atexit.register(shutdown)` and `signal.signal(SIGTERM, lambda *_: sys.exit(0))` if `not _atexit_registered`.
8. Return `_socket_path`.

### Shutdown (`shutdown()`)

Acquire `_lock`. Then in a `try/finally`:

1. If `_proc is None` → nothing to do.
2. `_proc.terminate()` (SIGTERM).
3. `_proc.wait(timeout=2.0)`. On `TimeoutExpired`: `_proc.kill()` (SIGKILL); `_proc.wait(timeout=1.0)`.
4. `finally:` unlink `_socket_path` (ignore `FileNotFoundError`); set `_proc = None`, `_socket_path = None`.

## Speed and reliability properties

### Speed

- **Startup overhead:** typical <30 ms (Popen ~10 ms, socket file appears in 5–15 ms on Deck-class CPU).
- **Per-injection overhead:** zero new work. Today's `_get_ydotool_env()` does 3× `Path.exists()` stats on every `inject_text()` call. New code reads `_socket_path` (a Python attribute). Net win on every injection.
- **Stepped backoff** in the readiness poll: happy path returns the moment the socket appears (5–15 ms), not at the timeout.

### Reliability

| Risk | Mitigation |
|---|---|
| Stale socket file from prior crash | `os.unlink` (ignoring ENOENT) before spawn — ydotoold won't bind otherwise |
| Daemon dies immediately on uinput perm error | Poll loop checks `_proc.poll()` between socket-stats; classifies actual error from stderr instead of waiting out the ~540 ms timeout |
| Daemon crashes mid-session | `inject_text()` catches `subprocess.CalledProcessError` from `ydotool type`. If error indicates connection failure AND `_proc.poll() is not None`, calls `ensure_running()` once and retries the injection. Bounded: single retry, no loop |
| App receives SIGTERM (logout, kill) — atexit doesn't fire by default | `signal.signal(SIGTERM, lambda *_: sys.exit(0))` raises `SystemExit` → atexit fires → `shutdown()` runs |
| Race: two callers spawn simultaneously | `_lock` around the spawn block. Second caller observes `_proc is not None and _proc.poll() is None` and returns existing socket |
| Orphan after hard crash (segfault, OOM, atexit didn't fire) | Harmless. Next launch unlinks the stale socket; orphan ydotoold loses its socket and gets reaped at logout when `XDG_RUNTIME_DIR` clears |
| Shutdown deadlock | SIGTERM then SIGKILL with bounded waits; socket unlinked in `finally` |

## Error handling

Five failure categories. Each sets `_last_error`; `ensure_running()` returns `None`. App startup does NOT block.

### §1 — `ydotoold` binary not found

- **Detection:** `shutil.which("ydotoold")` returns `None`.
- **Distro hint** parsed from `/etc/os-release` `ID` (and `ID_LIKE`):
  - `arch` / `manjaro` / `steamos` / `*arch*` → `sudo pacman -S ydotool`
  - `fedora` / `bazzite` / `silverblue` / `*fedora*` → `sudo dnf install ydotool`
  - `debian` / `ubuntu` / `pop` / `*debian*` → `sudo apt install ydotool`
  - default → `install ydotool via your distro's package manager`
- **Message:** `"ydotoold not installed. <hint>"`
- The result of os-release parsing is cached at module load (file is small and stable).

### §2 — uinput permission denied (most common on fresh installs)

- **Detection:** child exits within ~50 ms of spawn; stderr matches `r"uinput|permission"i`.
- **Message:** `"ydotoold cannot access /dev/uinput. Run: sudo gpasswd -a $USER input — then log out and back in. Or install ydotool's udev rule at /etc/udev/rules.d/."`
- If the user IS already in the `input` group (check via `os.getgrnam("input").gr_mem`), append: `(You're already in 'input' — check your udev rules and verify /dev/uinput perms with 'ls -la /dev/uinput'.)`

### §3 — Socket never appears, daemon still alive

- **Detection:** poll loop exhausts (~540 ms) with `_proc.poll() is None`.
- **Message:** `"ydotoold started but didn't create socket within 540ms. Last stderr: <last 500 chars captured non-blockingly>"`
- Send SIGTERM to the live-but-stuck daemon before returning.

### §4 — Spawn raises

- **Detection:** `Popen()` raises `FileNotFoundError` (TOCTOU between `which` and `Popen`) or `PermissionError`.
- **Message:** `"Failed to spawn ydotoold: <repr(exc)>"`

### §5 — Cannot create socket directory

- **Detection:** `os.makedirs()` raises `OSError`.
- **Message:** `"Cannot create socket directory <path>: <oserror.strerror>"`

### Surfacing

- **Startup:** `core/setup.py:check_text_injection` returns `(False, last_error)`. Setup wizard / overlay surfaces a yellow "Text injection unavailable: <reason>" warning. App continues.
- **Per-injection:** `inject_text()` raises `InjectionError(last_error)`. Existing callers of `inject_text()` already catch `InjectionError` (the exception is re-exported from `core/__init__.py:23` and `app.py:145`).

The wrong message at `injector.py:88` (sudo systemctl enable ydotoold) is removed in this change.

## Testing

Tests live at `tests/test_ydotool_daemon.py` plus one new test in `tests/test_injector.py` for the lazy-retry path.

### Unit tests (no real daemon — `subprocess.Popen` and filesystem patched)

1. `test_socket_path_uses_xdg_runtime_dir` — `XDG_RUNTIME_DIR=/tmp/x` → socket path `/tmp/x/wayfinder-aura/ydotool.sock`.
2. `test_socket_path_falls_back_when_xdg_unset` — env unset → `${TMPDIR:-/tmp}/wayfinder-aura-<uid>/ydotool.sock`.
3. `test_spawn_args` — `Popen` called with `[ydotoold, "-p", socket, "-P", "0600"]`.
4. `test_binary_missing_returns_distro_hint` — `shutil.which` patched to None; `last_error` matches install-hint template.
5. `test_distro_hint_detection` — parameterized over `/etc/os-release` ID (arch, fedora, debian, unknown) with expected hint string.
6. `test_unlinks_stale_socket_before_spawn` — pre-create a socket file at the path; verify `os.unlink` called before `Popen`.
7. `test_idempotent_when_alive` — second `ensure_running()` with `_proc.poll() is None` does NOT call `Popen` again.
8. `test_respawns_when_dead` — `_proc.poll()` returns 1; second call spawns.
9. `test_uinput_perm_error_detected_fast` — `Popen` mock exits ~10 ms with stderr `"open /dev/uinput: Permission denied"` → `last_error` matches uinput-perm template, NOT timeout template.
10. `test_socket_timeout_with_live_daemon` — `Popen` mock stays alive but never creates socket → `last_error` contains stderr capture.
11. `test_shutdown_sigterm_then_sigkill` — `_proc.wait(timeout=2)` raises `TimeoutExpired` → SIGKILL sent.
12. `test_shutdown_unlinks_socket_in_finally` — even when `Popen.kill` raises, socket file is unlinked.
13. `test_thread_safety_concurrent_ensure_running` — two threads call `ensure_running()` simultaneously; only one `Popen` invocation.
14. `test_atexit_registered_once` — multiple calls don't register N atexit handlers.

### Integration test (real ydotoold required, gated)

```python
@pytest.mark.integration
def test_real_spawn_and_shutdown_smoke():
    if shutil.which("ydotoold") is None:
        pytest.skip("ydotoold not installed")
    if not os.access("/dev/uinput", os.R_OK | os.W_OK):
        pytest.skip("no /dev/uinput rw access")
    socket = ydotool_daemon.ensure_running()
    assert socket and Path(socket).is_socket()
    subprocess.run(
        ["ydotool", "mousemove", "--", "0", "0"],
        env={**os.environ, "YDOTOOL_SOCKET": socket},
        check=True, timeout=5,
    )
    ydotool_daemon.shutdown()
    assert not Path(socket).exists()
```

`mousemove 0 0` is a verified no-op confirming the client→daemon protocol works without injecting visible input.

### Lazy-retry test in `test_injector.py`

15. `test_inject_text_retries_once_when_daemon_dies_mid_session` — first `subprocess.run` mock returns exit 1 + "connection refused"; second succeeds. Verify `ensure_running()` is called between them and only ONE retry happens (no infinite loop).

### Deliberately NOT tested

- Real keystroke injection into a window (too brittle for CI; manual smoke is `ydotool type "test"` into a focused terminal during dev).
- ydotoold-version-specific behavior (treated as a black box; integration test catches gross breakage).
- Multi-user scenarios (out of scope).

## Files changed (implementation summary)

- **New:** `src/wayfinder/core/ydotool_daemon.py` (~150 LOC including docstrings).
- **New:** `tests/test_ydotool_daemon.py` (15 unit tests + 1 integration).
- **Modified:** `src/wayfinder/core/injector.py` — drop `_get_ydotool_env()`'s 3-path search; `check_ydotool_ready()` and `inject_text()` defer to `ydotool_daemon`. Add lazy-retry on `CalledProcessError` with connection-failure stderr. Net diff: roughly −30 / +20 LOC.
- **Modified:** `src/wayfinder/core/setup.py` — `check_text_injection` calls `ydotool_daemon.ensure_running()` and returns `last_error` on failure. Net diff: ~+10 LOC.
- **Modified:** `tests/test_injector.py` — add `test_inject_text_retries_once_when_daemon_dies_mid_session` (~+30 LOC).

Estimated total: ~+250 LOC, ~−30 LOC, 4 files touched, 1 new module + 1 new test file.

## Open questions for review

None blocking. Design has been validated section-by-section in chat. Implementation can proceed via `writing-plans` skill once Peter approves this written spec.

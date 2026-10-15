"""Game reports: players tell Wayfinder how Aura worked in a game.

Design: docs/plans/game-reports-design-2026-10-15.md (sections 4 to 6 and 11).

* Nothing is sent until the player presses Send. A report holds exactly the
  game's key and name, the Aura version, the operating system and its major
  version (``coarse_os()``), the answer, the note if typed, a random report ID
  used only for game reports (``reporterId``, made at the first Send) and a
  counter (``seq``, never decreasing). Never a window title, an unlisted game,
  a play time, the crash-report install ID, the licence key, the machine ID or
  the plan: nothing in this module touches licensing.
* ``game-reports.json`` beside ``config.json`` (owner-only, written atomically,
  under a lock) keeps the ID, the counter, per-game dictation counts (to ask
  once, after a few uses), what the player sent and what still waits.
* Sending and withdrawal run on background threads with 10-second timeouts and
  never raise. Withdrawal sends the counter as it was when the player pressed
  Withdraw, and every retry resends that same value.
* Released builds only; a source checkout sends only when the URL override for
  that request is set.

The game key names a game in every report and in Wayfinder's server-side
catalog, which ``scripts/export_game_catalog.py`` generates from the Games-tab
lists with this same function. Changing how a key is made orphans every report
already sent for that game.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..utils.fs_security import atomic_write_json
from ..utils.platform import get_config_dir

GAME_KEY_MAX = 64
GAME_KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_APOSTROPHES = ("'", "’")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def game_key(name: str) -> str:
    """The report key for a Games-tab name: ``Garry's Mod`` -> ``garrys-mod``.

    NFKD, then accents dropped and ASCII kept; lower-case; apostrophes dropped;
    every other run of non-alphanumerics becomes ``-``; trimmed; at most 64
    characters (re-trimmed so a cut never ends on ``-``). A name with no ASCII
    letter or digit gives ``""``, which is not a valid key.
    """
    text = unicodedata.normalize("NFKD", name or "")
    for mark in _APOSTROPHES:
        text = text.replace(mark, "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = _NON_ALNUM.sub("-", text).strip("-")
    return text[:GAME_KEY_MAX].rstrip("-")


def is_valid_game_key(key: object) -> bool:
    """A non-empty lower-case slug of at most 64 characters."""
    return (isinstance(key, str) and 0 < len(key) <= GAME_KEY_MAX
            and GAME_KEY_RE.match(key) is not None)


# --- endpoints and limits ---------------------------------------------------------

_REPORT_ENV = "WAYFINDER_AURA_GAME_REPORT_URL"
_WITHDRAW_ENV = "WAYFINDER_AURA_GAME_WITHDRAW_URL"
GAME_REPORT_URL = os.environ.get(
    _REPORT_ENV, "https://fine-shrimp-886.convex.site/api/aura/game-report")
GAME_WITHDRAW_URL = os.environ.get(
    _WITHDRAW_ENV, "https://fine-shrimp-886.convex.site/api/aura/game-report/withdraw")
HTTP_TIMEOUT = 10         # every request; redirects are never followed, so a
                          # report only ever goes to the URL above
STATE_NAME = "game-reports.json"
ASK_AFTER_USES = 3
DAILY_SENDS = 5
KEEP_DAYS = 14             # the send window; then "Not confirmed" (attempted) or dropped
PRUNE_DAYS = 730           # what was sent is kept on this computer for 2 years
NOTE_MAX = 280
ANSWERS = ("works", "some_issues", "doesnt_work")

# Local report states (design section 6.1).
QUEUED, SENDING, NOT_CONFIRMED, SENT, FAILED = (
    "queued", "sending", "not_confirmed", "sent", "failed")
WITHDRAWING = "withdrawing"
_STATES = (QUEUED, SENDING, NOT_CONFIRMED, SENT, FAILED)
# A request for these started without a definite "nothing written": the
# server may hold them, so withdrawing them asks the server.
_MAYBE_STORED = (SENDING, NOT_CONFIRMED, SENT)
_DEFINITE_NO_WRITE = (400, 413, 415)
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

_lock = threading.RLock()            # the state file
_send_lock = threading.Lock()        # one sender pass at a time
_withdraw_lock = threading.Lock()    # one withdrawal pass at a time
_logged_corrupt = False
_change_listener: Callable[[], None] | None = None


def _is_packaged() -> bool:
    """A released build (PyInstaller bundle)."""
    return bool(getattr(sys, "frozen", False))


def _endpoint(url: str, env: str) -> str | None:
    """Released builds send; a source checkout only with the override set."""
    if _is_packaged() or os.environ.get(env):
        return url
    return None


def _app_version() -> str:
    try:
        from .. import __version__

        return str(__version__)[:40]
    except Exception:
        return "unknown"


def set_change_listener(listener: Callable[[], None] | None) -> None:
    """Called (on a worker thread) after sending or withdrawal changes what
    Your reports shows. The app marshals it to the Tk thread."""
    global _change_listener
    _change_listener = listener


def _changed() -> None:
    listener = _change_listener
    if listener is not None:
        try:
            listener()
        except Exception:
            pass


# --- the coarse operating system (design 5.1) ----------------------------------------

_HOST_OS_RELEASE = Path("/run/host/os-release")   # the host's, inside a Flatpak
_OS_VERSION = re.compile(r"^[a-z0-9][a-z0-9 ._-]{0,23}$")


def _parse_os_release(text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        info[key.strip()] = value.strip().strip('"').strip("'")
    return info


def _read_os_release() -> dict[str, str]:
    try:
        if _HOST_OS_RELEASE.is_file():
            return _parse_os_release(_HOST_OS_RELEASE.read_text(encoding="utf-8"))
    except OSError:
        pass
    try:
        return dict(platform.freedesktop_os_release())
    except (OSError, AttributeError):
        return {}


def _windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except Exception:
        match = re.search(r"\d+\.\d+\.(\d+)", platform.version() or "")
        return int(match[1]) if match else 0


def coarse_os(*, system: str | None = None, mac_version: str | None = None,
              windows_build: int | None = None,
              os_release: dict | None = None) -> tuple[str, str]:
    """("macos", "15"), ("windows", "11") or ("linux", "steamos 3").

    Only the major version: never the kernel, hostname, CPU, display server or
    package type. Windows 11 is build 22000 or later."""
    system = system or sys.platform
    if system == "darwin":
        version = mac_version if mac_version is not None else platform.mac_ver()[0]
        major = (version or "").split(".")[0]
        return "macos", major if major.isdigit() else "unknown"
    if system == "win32":
        build = windows_build if windows_build is not None else _windows_build()
        if not build:
            return "windows", "unknown"
        return "windows", "11" if build >= 22000 else "10"
    info = os_release if os_release is not None else _read_os_release()
    ident = re.sub(r"[^a-z0-9._-]", "", str(info.get("ID") or "").lower()).lstrip("._-")
    major = re.match(r"\d{1,6}", str(info.get("VERSION_ID") or ""))
    value = f"{ident} {major[0]}" if ident and major else ident
    value = value[:24].rstrip()
    return "linux", value if _OS_VERSION.match(value) else "linux"


# --- notes (design 4.2 and 8) -----------------------------------------------------------

_LINK = re.compile(r"://|www\.", re.IGNORECASE)
_BIDI = re.compile("[‪-‮⁦-⁩‎‏]")


def clean_note(text: str | None) -> tuple[str | None, str | None]:
    """(note, problem): control characters stripped, whitespace collapsed, at
    most 280 characters, links refused (problem ``"links"``), blank -> None."""
    raw = text or ""
    if _LINK.search(raw):
        return None, "links"
    cleaned = []
    for ch in _BIDI.sub("", raw):
        category = unicodedata.category(ch)
        if ch in "\t\n\r\v\f\x85  ":
            cleaned.append(" ")
        elif category not in ("Cc", "Cs"):
            cleaned.append(ch)
    note = " ".join("".join(cleaned).split())[:NOTE_MAX].strip()
    return (note or None), None


# --- the state file ---------------------------------------------------------------------

def state_path() -> Path:
    return get_config_dir() / STATE_NAME


def _fresh() -> dict:
    return {"v": 1, "seq": 0, "order": 0, "games": {}, "reports": [], "withdrawals": [],
            "sends": {"day": "", "count": 0}}


class _Corrupt(ValueError):
    pass


def _int(value: Any, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise _Corrupt("not a count")
    return value


def _normalize(data: Any) -> dict:
    """The state with every part checked; a bad entry is dropped, a bad core
    (ID, counter, shape) makes the whole file corrupt."""
    if not isinstance(data, dict) or data.get("v") != 1:
        raise _Corrupt("unknown state")
    out = _fresh()
    out["seq"] = _int(data.get("seq", 0))
    out["order"] = _int(data.get("order", 0))
    reporter = data.get("reporterId")
    if reporter is not None:
        if not (isinstance(reporter, str) and _HEX32.match(reporter)):
            raise _Corrupt("bad reporter ID")
        out["reporterId"] = reporter
    games = data.get("games", {})
    reports = data.get("reports", [])
    withdrawals = data.get("withdrawals", [])
    if not (isinstance(games, dict) and isinstance(reports, list) and isinstance(withdrawals, list)):
        raise _Corrupt("bad shape")
    for key, game in games.items():
        try:
            if not (is_valid_game_key(key) and isinstance(game, dict)):
                continue
            attempted = [r for r in game.get("attempted", []) if isinstance(r, str) and _HEX32.match(r)]
            out["games"][key] = {"name": str(game.get("name") or key)[:200],
                                 "uses": _int(game.get("uses", 0)),
                                 "asked": game.get("asked") is True,
                                 "attempted": attempted,
                                 "order": _int(game.get("order", 0))}
        except _Corrupt:
            continue
    for entry in reports:
        try:
            if not (isinstance(entry, dict) and entry.get("state") in _STATES
                    and is_valid_game_key(entry.get("gameKey"))
                    and isinstance(entry.get("gameName"), str)
                    and entry.get("answer") in ANSWERS
                    and _HEX32.match(str(entry.get("reportId", "")))):
                continue
            _int(entry.get("seq"), 1)
            rid = entry.get("reporterId")
            if rid is not None and not _HEX32.match(str(rid)):
                continue
            out["reports"].append(entry)
        except _Corrupt:
            continue
    for entry in withdrawals:
        try:
            if not (isinstance(entry, dict) and _HEX32.match(str(entry.get("reporterId", "")))
                    and (entry.get("gameKey") is None or is_valid_game_key(entry.get("gameKey")))):
                continue
            _int(entry.get("throughSeq"), 1)
            out["withdrawals"].append(entry)
        except _Corrupt:
            continue
    sends = data.get("sends")
    if isinstance(sends, dict) and isinstance(sends.get("day"), str):
        try:
            out["sends"] = {"day": sends["day"], "count": _int(sends.get("count", 0))}
        except _Corrupt:
            pass
    return out


def _set_aside(path: Path, why: str) -> None:
    global _logged_corrupt
    try:
        os.replace(path, path.with_name(path.name + ".bad"))
    except OSError:
        pass
    if not _logged_corrupt:
        _logged_corrupt = True
        print(f"[game-reports] the state file was unreadable ({why}); kept it as "
              f"{path.name}.bad and started fresh", flush=True)


def _load() -> dict:
    """The state (call under ``_lock``). A corrupt file is renamed ``.bad``."""
    path = state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fresh()
    except OSError:
        return _fresh()
    try:
        return _normalize(json.loads(text))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        _set_aside(path, type(exc).__name__)
        return _fresh()


def _save(data: dict) -> bool:
    """Write atomically, owner-only. False if it could not be written."""
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data, mode=0o600)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _game(data: dict, key: str, name: str) -> dict:
    game = data["games"].setdefault(
        key, {"name": name, "uses": 0, "asked": False, "attempted": [], "order": 0})
    game["name"] = name
    return game


def _ensure_reporter(data: dict) -> str:
    if not data.get("reporterId"):
        data["reporterId"] = secrets.token_hex(16)
    return data["reporterId"]


# --- counting and asking (design 4.1) ------------------------------------------------------

def note_game_use(name: str) -> None:
    """One more dictation delivered into this supported game. Thread-safe;
    only a count and an ordering number are kept, never a time."""
    key = game_key(name)
    if not is_valid_game_key(key):
        return
    try:
        with _lock:
            data = _load()
            game = _game(data, key, name)
            game["uses"] = min(game["uses"] + 1, 1_000_000)
            data["order"] += 1
            game["order"] = data["order"]
            _save(data)
    except Exception:
        pass


def mark_asked(key: str) -> None:
    """The card for this game was answered or closed: never ask again."""
    try:
        with _lock:
            data = _load()
            if key in data["games"]:
                data["games"][key]["asked"] = True
                _save(data)
    except Exception:
        pass


@dataclass(frozen=True)
class PromptGame:
    key: str
    name: str


def _candidates(data: dict) -> list[tuple[int, str, str]]:
    return sorted(((g["order"], key, g["name"]) for key, g in data["games"].items()
                   if g["uses"] >= ASK_AFTER_USES and not g["asked"]), reverse=True)


def eligible_prompt(frontmost_name: str | None, busy: bool, now: float, *,
                    enabled: bool = True) -> PromptGame | None:
    """The game to ask about now, most recently played first, or None.

    Eligible: dictated into it 3 or more times, never asked on this computer,
    the switch on, its key reportable and intake permission fresh. Never while
    a game is in front or Aura is busy."""
    if busy or not enabled or frontmost_name:
        return None
    try:
        from . import game_labels

        if not game_labels.intake_open(now):
            return None
        allowed = game_labels.reportable()
        with _lock:
            candidates = _candidates(_load())
    except Exception:
        return None
    for _order, key, name in candidates:
        if key in allowed:
            return PromptGame(key, name)
    return None


def wants_labels_refresh(now: float, *, enabled: bool = True) -> bool:
    """A game would be asked about but permission has lapsed and a labels
    fetch is due."""
    if not enabled:
        return False
    try:
        from . import game_labels

        with _lock:
            if not _candidates(_load()):
                return False
        return not game_labels.intake_open(now) and game_labels.fetch_due(now)
    except Exception:
        return False


# --- sending a report (design 5.1 and 11) -----------------------------------------------

def _today(now: float) -> str:
    return date.fromtimestamp(now).isoformat()


def may_report(key: str, now: float | None = None) -> bool:
    """A new report for this game may be sent now: intake permission is fresh
    and the game is reportable. Checked again at every Send."""
    try:
        from . import game_labels

        now = time.time() if now is None else now
        return game_labels.intake_open(now) and key in game_labels.reportable()
    except Exception:
        return False


def report_window(key: str | None, now: float | None = None) -> float:
    """Seconds left to offer new reports (for ``key``, or any game with
    ``None``), from the permission as it is now; 0 means retire the controls.
    Expiry callbacks ask this instead of trusting the deadline they were
    scheduled for, so a refresh that renewed permission meanwhile keeps the
    player's draft and just moves the next check."""
    try:
        from . import game_labels

        now = time.time() if now is None else now
        if key is not None and not may_report(key, now):
            return 0.0
        return game_labels.permission_left(now)
    except Exception:
        return 0.0


def submit(game_name: str, answer: str, note: str | None = "", *,
           now: float | None = None,
           on_sent: Callable[[], None] | None = None) -> str:
    """Queue a report (the player pressed Send). Returns ``"queued"``,
    ``"closed"`` (permission lapsed, intake closed or the game not
    reportable), ``"limit"`` (5 Sends today), ``"link"`` (the note has a
    link), ``"invalid"`` or ``"error"``. Sending happens on a background
    thread; ``on_sent`` runs there afterwards."""
    now = time.time() if now is None else now
    key = game_key(game_name)
    if answer not in ANSWERS or not is_valid_game_key(key):
        return "invalid"
    if not may_report(key, now):
        return "closed"
    cleaned, problem = clean_note(note)
    if problem:
        return "link"
    try:
        os_name, os_version = coarse_os()
        with _lock:
            data = _load()
            today = _today(now)
            if data["sends"]["day"] != today:
                data["sends"] = {"day": today, "count": 0}
            if data["sends"]["count"] >= DAILY_SENDS:
                return "limit"
            data["sends"]["count"] += 1
            _ensure_reporter(data)
            data["seq"] += 1
            # A newer answer replaces one that never left this computer.
            data["reports"] = [r for r in data["reports"]
                               if not (r["gameKey"] == key and r["state"] == QUEUED)]
            entry = {"reportId": secrets.token_hex(16), "seq": data["seq"],
                     "gameKey": key, "gameName": game_name, "answer": answer,
                     "appVersion": _app_version(), "os": os_name, "osVersion": os_version,
                     "state": QUEUED, "createdAt": now}
            if cleaned:
                entry["note"] = cleaned
            data["reports"].append(entry)
            _game(data, key, game_name)["asked"] = True
            if not _save(data):
                return "error"
    except Exception:
        return "error"
    _send_soon(on_sent)
    return "queued"


def build_payload(entry: dict) -> dict:
    """Exactly the fields of design section 5.1; ``note`` only when typed."""
    payload = {
        "reportId": entry["reportId"],
        "reporterId": entry["reporterId"],
        "seq": entry["seq"],
        "gameKey": entry["gameKey"],
        "gameName": entry["gameName"],
        "answer": entry["answer"],
        "appVersion": entry["appVersion"],
        "os": entry["os"],
        "osVersion": entry["osVersion"],
    }
    if entry.get("note"):
        payload["note"] = entry["note"]
    return payload


def _expire(data: dict, now: float) -> bool:
    """Drop never-attempted reports past the send window and prune old ones."""
    before = len(data["reports"])
    keep = []
    for entry in data["reports"]:
        age = now - float(entry.get("createdAt") or 0)
        if entry["state"] == QUEUED and age > KEEP_DAYS * 86400:
            continue
        if entry["state"] != QUEUED and age > PRUNE_DAYS * 86400:
            continue
        keep.append(entry)
    data["reports"] = keep
    return len(keep) != before


def _due(entry: dict, now: float) -> bool:
    age = now - float(entry.get("createdAt") or 0)
    return entry["state"] in (QUEUED, SENDING, NOT_CONFIRMED) and age <= KEEP_DAYS * 86400


def _body(response) -> dict:
    try:
        body = json.loads((response.content or b"")[:4096].decode("utf-8"))
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _mark_attempted(data: dict, entry: dict) -> None:
    game = _game(data, entry["gameKey"], entry["gameName"])
    if entry["reporterId"] not in game["attempted"]:
        game["attempted"].append(entry["reporterId"])


def send_pending(now: float | None = None) -> int:
    """POST waiting reports, oldest first. Returns how many were confirmed.
    Each is marked Sending on disk before its request starts. Never raises."""
    url = _endpoint(GAME_REPORT_URL, _REPORT_ENV)
    if url is None:
        return 0
    try:
        import requests
    except Exception:
        return 0
    if not _send_lock.acquire(timeout=HTTP_TIMEOUT * 3):
        return 0
    sent = 0
    tried: set[str] = set()
    try:
        while True:
            now_ = time.time() if now is None else now
            with _lock:
                data = _load()
                if _expire(data, now_):
                    _save(data)
                entry = next((r for r in sorted(data["reports"], key=lambda r: r["seq"])
                              if r["reportId"] not in tried and _due(r, now_)), None)
                if entry is None:
                    break
                tried.add(entry["reportId"])
                if not entry.get("reporterId"):
                    entry["reporterId"] = _ensure_reporter(data)
                entry["state"] = SENDING
                if not _save(data):
                    break
                payload = build_payload(entry)
            try:
                response = requests.post(url, json=payload, timeout=HTTP_TIMEOUT,
                                         allow_redirects=False)
                status, body = response.status_code, _body(response)
            except Exception:
                status, body = None, {}
            with _lock:
                data = _load()
                current = next((r for r in data["reports"]
                                if r["reportId"] == payload["reportId"]), None)
                if current is not None:  # None: withdrawn meanwhile (the barrier covers it)
                    if status == 200 and body.get("withdrawn") is True:
                        data["reports"].remove(current)
                    elif status == 200:
                        current["state"] = SENT
                        _mark_attempted(data, current)
                        sent += 1
                    elif status in _DEFINITE_NO_WRITE:
                        current["state"] = FAILED
                    else:
                        current["state"] = NOT_CONFIRMED
                        _mark_attempted(data, current)
                    _save(data)
            _changed()
            if status is None or status not in (200, *_DEFINITE_NO_WRITE):
                break  # offline, limited, closed or not live: try again later
    except Exception:
        pass  # never into the caller; what is on disk is retried later
    finally:
        _send_lock.release()
    return sent


def _send_soon(on_done: Callable[[], None] | None = None) -> None:
    def run() -> None:
        try:
            send_pending()
        finally:
            if on_done is not None:
                try:
                    on_done()
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True, name="game-report-send").start()


def outcome(key: str) -> str:
    """How the newest report for a game ended up: ``"sent"``, ``"saved"``
    (waiting or not confirmed), ``"failed"`` or ``"gone"``."""
    try:
        with _lock:
            reports = [r for r in _load()["reports"] if r["gameKey"] == key]
    except Exception:
        return "saved"
    if not reports:
        return "gone"
    state = max(reports, key=lambda r: r["seq"])["state"]
    return {SENT: "sent", FAILED: "failed"}.get(state, "saved")


# --- withdrawal (design 5.2 and 6.1) ---------------------------------------------------------

def _maybe_stored_ids(data: dict, key: str | None) -> set[str]:
    keys = [key] if key is not None else list(data["games"])
    ids = {rid for k in keys for rid in data["games"].get(k, {}).get("attempted", [])}
    ids |= {r["reporterId"] for r in data["reports"]
            if (key is None or r["gameKey"] == key)
            and r["state"] in _MAYBE_STORED and r.get("reporterId")}
    return ids


def withdraw(key: str | None = None, now: float | None = None) -> str:
    """Withdraw one game's reports, or all with ``key=None``.

    Under the lock the game's reports leave the queue. If any request for it
    may have reached the server, a withdrawal is stored with ``throughSeq``
    fixed at the counter now and sent in the background (``"withdrawing"``);
    otherwise everything was local and is simply deleted (``"deleted"``)."""
    now = time.time() if now is None else now
    try:
        with _lock:
            data = _load()
            through = data["seq"]
            ids = _maybe_stored_ids(data, key)
            name = None
            for r in data["reports"]:
                if key is not None and r["gameKey"] == key:
                    name = r["gameName"]
            if key is not None and name is None:
                name = data["games"].get(key, {}).get("name", key)
            data["reports"] = [r for r in data["reports"] if key is not None and r["gameKey"] != key]
            if not ids or through < 1:
                if key is None:
                    data.pop("reporterId", None)  # nothing ever reached the server
                _save(data)
                result = "deleted"
            else:
                for rid in sorted(ids):
                    # A boundary never changes once made: a later press adds its
                    # own, so acknowledging an earlier one can't drop it.
                    if any(w["reporterId"] == rid and w.get("gameKey") == key
                           and w["throughSeq"] == through for w in data["withdrawals"]):
                        continue  # this exact boundary is already waiting
                    data["withdrawals"].append({
                        "id": secrets.token_hex(8), "reporterId": rid, "gameKey": key,
                        "gameName": name, "throughSeq": through, "createdAt": now})
                if not _save(data):
                    return "error"
                result = "withdrawing"
    except Exception:
        return "error"
    if result == "withdrawing":
        _withdraw_soon()
    return result


def withdraw_all(now: float | None = None) -> str:
    """Withdraw everything this computer sent; the report ID is replaced only
    once the server confirms."""
    return withdraw(None, now=now)


def _settle(data: dict, rid: str, key: str | None, through: int) -> None:
    """After the server confirmed a withdrawal through ``through``: forget
    ``rid`` for games where nothing newer from it may be stored."""
    for game_key_, game in data["games"].items():
        if key is not None and game_key_ != key:
            continue
        newer = any(r["gameKey"] == game_key_ and r.get("reporterId") == rid
                    and r["seq"] > through and r["state"] in _MAYBE_STORED
                    for r in data["reports"])
        if not newer and rid in game["attempted"]:
            game["attempted"].remove(rid)
    if key is None and data.get("reporterId") == rid:
        data.pop("reporterId")  # Withdraw all: a new ID is made at the next Send


def send_withdrawals(now: float | None = None) -> int:
    """POST waiting withdrawals with their stored ``throughSeq``. An entry
    stays Withdrawing until the server answers 200. Never raises."""
    url = _endpoint(GAME_WITHDRAW_URL, _WITHDRAW_ENV)
    if url is None:
        return 0
    try:
        import requests
    except Exception:
        return 0
    if not _withdraw_lock.acquire(timeout=HTTP_TIMEOUT * 3):
        return 0
    done = 0
    tried: set[str] = set()
    try:
        while True:
            with _lock:
                data = _load()
                pending = next((w for w in sorted(data["withdrawals"],
                                                  key=lambda w: w.get("createdAt") or 0)
                                if w.get("id") not in tried), None)
                if pending is None:
                    break
                if not pending.get("id"):
                    pending["id"] = secrets.token_hex(8)
                    _save(data)
                tried.add(pending["id"])
                payload = {"reporterId": pending["reporterId"],
                           "throughSeq": pending["throughSeq"]}
                if pending.get("gameKey"):
                    payload["gameKey"] = pending["gameKey"]
            try:
                status = requests.post(url, json=payload, timeout=HTTP_TIMEOUT,
                                       allow_redirects=False).status_code
            except Exception:
                break  # offline: retried at the next launch or labels fetch
            if status != 200:
                if status == 429:
                    break
                continue
            with _lock:
                # Clear exactly the boundary that was sent; any other stays queued.
                data = _load()
                data["withdrawals"] = [w for w in data["withdrawals"] if w.get("id") != pending["id"]]
                _settle(data, pending["reporterId"], pending.get("gameKey"), pending["throughSeq"])
                _save(data)
            done += 1
            _changed()
    except Exception:
        pass
    finally:
        _withdraw_lock.release()
    return done


def _withdraw_soon() -> None:
    def run() -> None:
        send_withdrawals()
        _changed()

    threading.Thread(target=run, daemon=True, name="game-report-withdraw").start()


def retry_pending() -> None:
    """At launch and after a labels fetch: waiting withdrawals first, then the
    queue. Runs on the caller's (background) thread; never raises."""
    try:
        send_withdrawals()
        send_pending()
    except Exception:
        pass


# --- Your reports (always available, design 4.3) -------------------------------------------

@dataclass(frozen=True)
class ReportView:
    game_key: str | None
    game_name: str
    answer: str | None
    note: str | None
    app_version: str | None
    created_at: float | None
    status: str           # sent, queued, not_confirmed, failed or withdrawing


def your_reports() -> list[ReportView]:
    """What this computer sent or will send, newest first, then withdrawals
    still waiting for the server. Works offline and with intake closed."""
    try:
        with _lock:
            data = _load()
    except Exception:
        return []
    rows, seen = [], set()
    for w in data["withdrawals"]:  # one Withdrawing row per game, however many presses
        if w.get("gameKey") not in seen:
            seen.add(w.get("gameKey"))
            rows.append(ReportView(w.get("gameKey"), w.get("gameName") or "All your reports",
                                   None, None, None, w.get("createdAt"), WITHDRAWING))
    for r in sorted(data["reports"], key=lambda r: r["seq"], reverse=True):
        status = NOT_CONFIRMED if r["state"] == SENDING else r["state"]
        rows.append(ReportView(r["gameKey"], r["gameName"], r["answer"], r.get("note"),
                               r.get("appVersion"), r.get("createdAt"), status))
    return rows


def latest_answers() -> dict[str, str]:
    """The newest answer this computer gave for each game ("You said: ...")."""
    try:
        with _lock:
            reports = _load()["reports"]
    except Exception:
        return {}
    answers: dict[str, tuple[int, str]] = {}
    for r in reports:
        if r["gameKey"] not in answers or r["seq"] > answers[r["gameKey"]][0]:
            answers[r["gameKey"]] = (r["seq"], r["answer"])
    return {key: answer for key, (_seq, answer) in answers.items()}

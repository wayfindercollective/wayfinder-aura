"""Community game labels: the daily labels document from Wayfinder.

Design: docs/plans/game-reports-design-2026-10-15.md (sections 3.4, 4.3, 4.4
and 5.3). One anonymous GET, at most once a day, in the background: no body,
no cookies, no ID. The document says, per game and operating system, the
label after each of the newest trusted Aura version lines, whether Wayfinder
is accepting new reports (``accepting``) and which games may be reported
(``reportable``).

* Display: the last good document is used for up to 30 days, then the labels
  from code.
* Intake permission is short-lived: ``accepting: true`` lets Aura offer new
  reports only for 24 hours after a successful fetch whose ``generatedAt`` was
  at most an hour older than the response's ``Date``. A failed refresh never
  extends it, and a fetch time in the future (clock moved back) counts as
  expired.

Released builds only; a source checkout fetches only when
``WAYFINDER_AURA_GAME_LABELS_URL`` is set. Nothing here touches licensing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..utils.fs_security import atomic_write_json
from ..utils.platform import get_cache_dir
from .game_reports import game_key, is_valid_game_key

_URL_ENV = "WAYFINDER_AURA_GAME_LABELS_URL"
GAME_LABELS_URL = os.environ.get(_URL_ENV, "https://wayfindercollective.io/aura/game-labels")
HTTP_TIMEOUT = 10
CACHE_NAME = "game-labels.json"
FETCH_EVERY_S = 86_400
PERMISSION_S = 86_400
DISPLAY_S = 30 * 86_400
MAX_SKEW_S = 3_600
MAX_BYTES = 1_000_000

LEVELS = ("untested", "playable", "works")
SYSTEMS = ("macos", "windows", "linux")
_LINE = re.compile(r"^\d{1,3}\.\d{1,3}$")
_MAX_GAMES = 5_000
_MAX_LINES = 50
_MAX_COUNT = 10_000_000

_lock = threading.Lock()
_refreshing = threading.Event()


def _is_packaged() -> bool:
    return bool(getattr(sys, "frozen", False))


def _url() -> str | None:
    """Where to fetch from, or None when this build must not fetch."""
    if _is_packaged() or os.environ.get(_URL_ENV):
        return GAME_LABELS_URL
    return None


# --- validation ------------------------------------------------------------------

def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNT


def _valid_line(state: Any) -> bool:
    return (isinstance(state, dict)
            and isinstance(state.get("line"), str) and _LINE.match(state["line"]) is not None
            and state.get("level") in LEVELS
            and _is_bool(state.get("verified"))
            and isinstance(state.get("sourceLine"), str)
            and _LINE.match(state["sourceLine"]) is not None
            and _is_bool(state.get("carried"))
            and all(_is_count(state.get(k)) for k in ("works", "someIssues", "doesntWork")))


def _valid_game(entry: Any) -> bool:
    return (isinstance(entry, dict)
            and is_valid_game_key(entry.get("key"))
            and entry.get("os") in SYSTEMS
            and _is_bool(entry.get("complete"))
            and isinstance(entry.get("lines"), list)
            and len(entry["lines"]) <= _MAX_LINES
            and all(_valid_line(s) for s in entry["lines"]))


def validate_labels(doc: Any) -> dict | None:
    """The document if Aura understands every part of it, else None.

    Unknown ``v``, levels, systems or shapes are rejected outright (the last
    good cache stays in use); unknown extra fields are ignored."""
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    generated = doc.get("generatedAt")
    if not (_is_bool(doc.get("accepting")) and _is_bool(doc.get("capped"))
            and isinstance(generated, (int, float)) and not isinstance(generated, bool)
            and generated >= 0):
        return None
    reportable = doc.get("reportable")
    games = doc.get("games")
    if not (isinstance(reportable, list) and len(reportable) <= _MAX_GAMES
            and all(is_valid_game_key(k) for k in reportable)):
        return None
    if not (isinstance(games, list) and len(games) <= _MAX_GAMES
            and all(_valid_game(g) for g in games)):
        return None
    return doc


# --- the cache ---------------------------------------------------------------------

def cache_path() -> Path:
    return get_cache_dir() / CACHE_NAME


def _load_cache() -> dict:
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("doc") is not None and validate_labels(data.get("doc")) is None:
        data.pop("doc", None)
    return data


def _save_cache(data: dict) -> None:
    try:
        atomic_write_json(cache_path(), data)
    except OSError:
        pass


def _age(now: float, then: Any) -> float | None:
    """Seconds since ``then``; None if unknown or in the future (clock moved back)."""
    if not isinstance(then, (int, float)) or isinstance(then, bool) or then > now:
        return None
    return now - then


def fetch_due(now: float) -> bool:
    """At most one fetch attempt a day (a clock moved back makes one due)."""
    with _lock:
        age = _age(now, _load_cache().get("lastAttempt"))
    return age is None or age >= FETCH_EVERY_S


def intake_open(now: float) -> bool:
    """True only within 24 hours of a successful, fresh fetch that said
    ``accepting: true``."""
    with _lock:
        cache = _load_cache()
    doc = cache.get("doc")
    age = _age(now, cache.get("fetchedAt"))
    return (doc is not None and cache.get("fresh") is True and doc.get("accepting") is True
            and age is not None and age < PERMISSION_S)


def reportable() -> frozenset[str]:
    """The game keys Wayfinder accepts reports for (from the cached document)."""
    with _lock:
        doc = _load_cache().get("doc")
    return frozenset(doc.get("reportable", ())) if doc else frozenset()


def labels_doc(now: float) -> dict | None:
    """The last good document while it is under 30 days old, else None."""
    with _lock:
        cache = _load_cache()
    age = _age(now, cache.get("fetchedAt"))
    if cache.get("doc") is None or age is None or age > DISPLAY_S:
        return None
    return cache["doc"]


# --- fetching ------------------------------------------------------------------------

def _response_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _read_capped(response) -> bytes:
    body = bytearray()
    for chunk in response.iter_content(65_536):
        body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise ValueError("labels document too large")
    return bytes(body)


def refresh(now: float | None = None, force: bool = False) -> bool:
    """Fetch the labels document if a fetch is due. True only when a valid,
    fresh document was stored (and so permission renewed). Never raises."""
    now = time.time() if now is None else now
    url = _url()
    if url is None:
        return False
    with _lock:
        cache = _load_cache()
        age = _age(now, cache.get("lastAttempt"))
        if not force and age is not None and age < FETCH_EVERY_S:
            return False
        cache["lastAttempt"] = now
        _save_cache(cache)
    try:
        import requests

        # Never follow a redirect: the document comes from this one URL.
        response = requests.get(url, timeout=HTTP_TIMEOUT, stream=True, allow_redirects=False,
                                headers={"Accept": "application/json"})
        try:
            if response.status_code != 200:
                return False
            doc = validate_labels(json.loads(_read_capped(response).decode("utf-8")))
            date = _response_date(response.headers.get("Date"))
        finally:
            response.close()
    except Exception:
        return False
    if doc is None:
        return False
    judge = date if date is not None else now
    if abs(judge - doc["generatedAt"] / 1000.0) > MAX_SKEW_S:
        return False  # a stale copy: no permission, and the old cache stays for display
    with _lock:
        cache = _load_cache()
        cache.update({"doc": doc, "fetchedAt": now, "date": date, "fresh": True})
        _save_cache(cache)
    return True


def refresh_in_background(on_done: Callable[[bool], None] | None = None,
                          now: float | None = None) -> bool:
    """Start a refresh on a worker thread when one is due. ``on_done(ok)`` runs
    on that thread. False when nothing was started."""
    now = time.time() if now is None else now
    if _url() is None or _refreshing.is_set() or not fetch_due(now):
        return False
    _refreshing.set()

    def run() -> None:
        ok = False
        try:
            ok = refresh(now=time.time())
        finally:
            _refreshing.clear()
            if on_done is not None:
                try:
                    on_done(ok)
                except Exception:
                    pass

    threading.Thread(target=run, daemon=True, name="game-labels-refresh").start()
    return True


# --- which label a Games-tab row shows (design 3.4 and 4.3) ---------------------------

def _line_key(line: str) -> tuple[int, int]:
    major, minor = line.split(".")
    return int(major), int(minor)


def app_line(version: str) -> str | None:
    """``1.2.1-beta.4`` -> ``1.2``; None when the version can't be read."""
    match = re.match(r"^v?(\d{1,3})\.(\d{1,3})\.\d{1,3}(?:-[0-9A-Za-z.]+)?$", (version or "").strip())
    return f"{int(match[1])}.{int(match[2])}" if match else None


def _reporters(count: int) -> str:
    return f"{count} anonymous reporter{'' if count == 1 else 's'} say{'s' if count == 1 else ''}"


def _caption(status: str, state: dict, lines: list[dict]) -> str:
    from .macos_game_chat import COMMUNITY, VERIFIED

    source = state["sourceLine"]
    if status == VERIFIED:
        return f"Tested by Wayfinder on Aura {source}."
    if status == COMMUNITY:
        if state["carried"]:
            return f"Works with (community), confirmed on Aura {source}."
        return f"{_reporters(state['works'])} it works on Aura {state['line']}."
    origin = next((s for s in lines if s["line"] == source), None)
    if origin is not None and origin["someIssues"] + origin["doesntWork"] > 0:
        return f"Problems reported on Aura {source}."
    return ""


_UNSET: Any = object()


def display_status(entry, os_family: str, app_line_: str | None, *,
                   doc: Any = _UNSET, now: float | None = None) -> tuple[str, str]:
    """(status, caption) for a Games-tab row.

    Not recommended from code always wins. Otherwise the newest published
    line not newer than the app's own line decides; with no such line, or no
    usable document, the label from code stands with no caption."""
    from .macos_game_chat import COMMUNITY, NOT_RECOMMENDED, PLAYABLE, UNTESTED, VERIFIED

    if entry.status == NOT_RECOMMENDED:
        return NOT_RECOMMENDED, ""
    if doc is _UNSET:
        doc = labels_doc(time.time() if now is None else now)
    if not doc or not app_line_ or not _LINE.match(app_line_):
        return entry.status, ""
    key = game_key(entry.name)
    found = next((g for g in doc["games"] if g["key"] == key and g["os"] == os_family), None)
    if found is None:
        return entry.status, ""
    mine = _line_key(app_line_)
    usable = [s for s in found["lines"] if _line_key(s["line"]) <= mine]
    if not usable:
        return entry.status, ""
    state = max(usable, key=lambda s: _line_key(s["line"]))
    level = state["level"]
    status = (VERIFIED if level == "works" and state["verified"] else
              COMMUNITY if level == "works" else
              PLAYABLE if level == "playable" else UNTESTED)
    caption = _caption(status, state, found["lines"])
    if doc["capped"] or not found["complete"]:
        caption = f"{caption} Based on recent versions only.".strip()
    return status, caption

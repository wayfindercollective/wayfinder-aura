"""Shared fakes for the game-report tests.

``Transport`` replaces requests' HTTP adapter, so the real request code in
``game_reports`` / ``game_labels`` runs (json encoding, timeouts, status
handling) while nothing reaches the network.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Any

import requests
from requests.structures import CaseInsensitiveDict


@dataclass
class Sent:
    method: str
    url: str
    body: Any
    timeout: Any
    headers: dict = field(default_factory=dict)


class Transport:
    """Answers each request from a script: ``(status, body[, headers])`` or an
    exception to raise (offline, timeout). With the script empty it answers
    200 ``{"ok": true}``."""

    def __init__(self, monkeypatch):
        self.sent: list[Sent] = []
        self.answers: list[Any] = []
        self.on_send = None  # called with the Sent before answering
        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send",
                            lambda adapter, request, **kw: self._send(adapter, request, **kw))

    def answer(self, *answers):
        self.answers.extend(answers)
        return self

    def posts(self, url_part: str = "") -> list[Sent]:
        return [s for s in self.sent if s.method == "POST" and url_part in s.url]

    def gets(self) -> list[Sent]:
        return [s for s in self.sent if s.method == "GET"]

    def _send(self, _adapter, request, **kwargs):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        sent = Sent(request.method, request.url, json.loads(body) if body else None,
                    kwargs.get("timeout"), dict(request.headers))
        self.sent.append(sent)
        if self.on_send is not None:
            self.on_send(sent)
        answer = self.answers.pop(0) if self.answers else (200, {"ok": True})
        if isinstance(answer, BaseException):
            raise answer
        status, payload, *rest = answer
        headers = dict(rest[0]) if rest else {}
        content = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict({"Content-Type": "application/json", **headers})
        response.raw = io.BytesIO(content)
        response.url = request.url
        response.request = request
        response.encoding = "utf-8"
        return response


def http_date(epoch_s: float) -> str:
    return formatdate(epoch_s, usegmt=True)


def labels_doc(*, accepting=True, generated_ms=0, reportable=("world-of-warcraft",),
               games=(), capped=False) -> dict:
    return {"v": 1, "accepting": accepting, "generatedAt": generated_ms, "capped": capped,
            "reportable": list(reportable), "games": list(games)}


def line(line_, level, *, verified=False, source=None, carried=False, works=0, some=0, doesnt=0):
    return {"line": line_, "level": level, "verified": verified,
            "sourceLine": source or line_, "carried": carried,
            "works": works, "someIssues": some, "doesntWork": doesnt}


def game(key, os_, lines, *, complete=True):
    return {"key": key, "os": os_, "complete": complete, "lines": list(lines)}

"""Headless UI inspection: widget tree dump (no display needed)."""
from __future__ import annotations

import json
import os
import stat

from wayfinder.ui import ui_inspect as U


class _W:
    def __init__(self, cls, text=None, state=None, w=100, req=80, kids=(), content=None):
        self.__class__ = type(cls, (_W,), {})
        self._text, self._state, self._w, self._req = text, state, w, req
        self._kids, self._content, self.master = list(kids), content, None
        for k in self._kids:
            k.master = self
        self._label = type("Inner", (), {"winfo_reqwidth": lambda s, r=req: r})() if text else None

    def cget(self, key):
        return {"text": self._text, "state": self._state, "text_color": "#fff"}.get(key)

    def winfo_rootx(self): return 0
    def winfo_rooty(self): return 0
    def winfo_width(self): return self._w
    def winfo_height(self): return 20
    def winfo_ismapped(self): return True
    def winfo_children(self): return self._kids
    def get(self, *a): return self._content


def test_texts_disabled_and_clipped_are_reported(tmp_path):
    ok = _W("CTkLabel", text="Fits", req=80)
    clipped = _W("CTkLabel", text="Much too long for this card", req=300)
    off = _W("CTkSwitch", text="Strong", state="disabled", req=40)
    box = _W("CTkTextbox", content="secret key 123")
    card = _W("CTkFrame", w=200, kids=[ok, clipped, off, box])
    summary = U.write_inspection(card, card, tmp_path / "ui.json")
    assert "Fits" in summary["texts"] and "Strong" in summary["texts"]
    assert summary["disabled"] == ["Strong"]
    assert summary["clipped"] == [{"text": "Much too long for this card", "needs": 300, "has": 200}]
    raw = (tmp_path / "ui.json").read_text()
    assert "secret key" not in raw  # contents never recorded, only length
    assert json.loads(raw)["tree"]["children"][3]["content_chars"] == 14
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(tmp_path / "ui.json").st_mode) == 0o600

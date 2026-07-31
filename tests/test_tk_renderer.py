"""Regression tests for the packaged Tk/Xft renderer gate."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from wayfinder.utils import tk_renderer


class _FakeTk:
    def __init__(self, *, font_system="xft", family="DejaVu Sans", family_count=20):
        self.font_system = font_system
        self.family = family
        self.families = tuple(f"Family {i}" for i in range(family_count))

    def call(self, *args):
        if args == ("info", "patchlevel"):
            return "8.6.14"
        if args == ("tk", "windowingsystem"):
            return "x11"
        if args == ("::tk::pkgconfig", "get", "fontsystem"):
            return self.font_system
        if args == ("font", "families"):
            return self.families
        raise AssertionError(args)

    @staticmethod
    def splitlist(value):
        return value


class _FakeRoot:
    def __init__(self, fake_tk):
        self.tk = fake_tk
        self.withdrawn = False
        self.destroyed = False

    def withdraw(self):
        self.withdrawn = True

    def destroy(self):
        self.destroyed = True


def _factories(*, font_system="xft", family="DejaVu Sans", family_count=20):
    fake_tk = _FakeTk(
        font_system=font_system, family=family, family_count=family_count
    )
    root = _FakeRoot(fake_tk)
    font = SimpleNamespace(actual=lambda option: family)
    return root, lambda: root, lambda **kwargs: font


def test_xft_renderer_with_product_font_passes(monkeypatch):
    monkeypatch.setattr(tk_renderer.sys, "platform", "linux")
    root, root_factory, font_factory = _factories()

    result = tk_renderer.require_compatible_tk_renderer(
        root_factory=root_factory, font_factory=font_factory
    )

    assert result.font_system == "xft"
    assert result.actual_family == "DejaVu Sans"
    assert root.withdrawn and root.destroyed


@pytest.mark.parametrize(
    ("font_system", "family", "family_count", "message"),
    [
        ("x11", "fixed", 4, "font system is 'x11'"),
        ("xft", "fixed", 20, "resolves to 'fixed'"),
        ("xft", "DejaVu Sans", 4, "only 4 font families"),
    ],
)
def test_legacy_or_incomplete_renderer_fails(
    monkeypatch, font_system, family, family_count, message
):
    monkeypatch.setattr(tk_renderer.sys, "platform", "linux")
    _, root_factory, font_factory = _factories(
        font_system=font_system, family=family, family_count=family_count
    )

    with pytest.raises(RuntimeError, match=message):
        tk_renderer.require_compatible_tk_renderer(
            root_factory=root_factory, font_factory=font_factory
        )
